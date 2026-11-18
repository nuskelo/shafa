"""KLB-013: прострочені оголошення Клубку зникають з «Активные» (`type=0`) і живуть у вкладці
«Обновить» (`type=1`) — знімок і «Обновить все» мають це знати."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shafa import __main__ as cli
from shafa import events
from shafa.bot import alerts
from shafa.klubok import dup, queue, renew, snapshot
from shafa.review import load_card
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_publish as kp
from shafa.robot import klubok_renew as kr
from test_klb001_page import chromium  # noqa: F401  (фікстура браузера на модуль)
from test_klb002_queue import OTHER_PAIR, add
from test_klb003_snapshot import http_fetch, listings, read_row
from test_klb004 import NOW, do_renew, open_session, renew_hits


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: out.append((kind, text, kw)))
    return out


@pytest.fixture
def server():
    srv = fake.Server(delay=(0.01, 0.05), seed=1, listings=1798)
    yield srv
    srv.close()


def refresh(conn, srv):
    return snapshot.refresh(conn, http_fetch(srv), pause=lambda: None, wait=lambda s: None,
                            log=lambda line: None)  # fmt: skip


def sold(conn, ids):
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT sold FROM klubok_listings WHERE id IN ({marks})", ids)
    return [r[0] for r in rows]


# --- підставний як живий ---


def test_fake_tabs_like_live_18_11(server):
    server.expire(10)
    soon = (server.clock + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    server.listings[10]["renew_date"] = soon  # ще не прострочене, але вже «Обновить»
    fetch = http_fetch(server)
    active = json.loads(fetch(snapshot.items_path(0, snapshot.PAGE_ALL))[1])
    tab = json.loads(fetch(snapshot.items_path(0, snapshot.PAGE_ALL, snapshot.RENEWABLE))[1])
    assert len(active["items"]) == 1788 and active["items_count"] == 1787  # як 1788 при 1785
    assert active["tabs_count"] == [1787, 11, 0, 0]
    assert len(tab["items"]) == tab["items_count"] == 11


# --- знімок = «Активные» ∪ «Обновить» ---


def test_ten_expired_stay_unsold_and_are_seen_by_dups(conn, root, server):
    item_id = add(conn, root, 7301)  # Ecco 38
    server.listings[0].update(name="Черевики Ecco оригінал 38,5 розміру", price=1100)
    refresh(conn, server)
    server.expire(10)
    assert refresh(conn, server) == 1798
    ids = [item["id"] for item in server.listings[:10]]
    assert sold(conn, ids) == [0] * 10
    assert listings(conn)[ids[0]]["renew_date"] == server.listings[0]["renew_date"]  # як є
    assert read_row(conn)["count"] == 1798
    assert ids[0] in [d.klubok_id for d in dup.find_all(conn, load_card(conn, item_id))]


def test_1780_expired_is_not_a_drop(conn, server):
    refresh(conn, server)
    server.expire(1780)
    assert refresh(conn, server) == 1798
    assert sum(sold(conn, [i["id"] for i in server.listings])) == 0


def test_renew_tab_error_writes_nothing(conn, server):
    refresh(conn, server)
    before, before_read = listings(conn), tuple(read_row(conn))
    server.expire(10)
    server.tab_fault[snapshot.RENEWABLE] = 500
    with pytest.raises(snapshot.SnapshotError) as error:
        refresh(conn, server)
    assert error.value.code == snapshot.FAILED and "«Обновить»" in error.value.text
    assert listings(conn) == before and tuple(read_row(conn)) == before_read  # не sold = 1


def test_union_below_active_tab_is_incomplete(conn, server):
    server.listings.pop(0)
    real = http_fetch(server)

    def fetch(path):  # лічильник вкладки «Активные» більший, ніж прочитано разом
        status, text = real(path)
        return status, text.replace('"tabs_count": [1797,', '"tabs_count": [1799,')

    with pytest.raises(snapshot.Incomplete):
        snapshot.read(fetch, lambda: None)


def test_expired_group_is_due_wanted_and_alerted(conn, server, tmp_path):
    refresh(conn, server)
    server.expire(10)
    refresh(conn, server)
    late = server.clock + timedelta(hours=13)  # прострочені з clock − 1 доба
    assert renew.due(conn) == server.clock - timedelta(days=1)
    assert renew.wanted(conn, server.clock)
    state = alerts.snapshot(conn, tmp_path, late)
    assert len(alerts._renew_events(conn, tmp_path, state, late)) == 1


# --- «Обновить все»: посилання → GET → знімок ---


def test_1780_expired_renewed_by_robot(conn, chromium, server, said, tmp_path):  # noqa: F811
    refresh(conn, server)  # учора: 1798 активних
    server.expire(1780)  # 18.11: «Обновить все» ніхто не натиснув
    s = open_session(chromium, server)
    try:
        out = do_renew(conn, s, tmp_path)
    finally:
        s.close()
    assert out.code == kr.RENEWED and out.link == 1780 and out.overdue == 1780
    assert renew_hits(server) == ["/renew/all"] and server.renews == 1
    assert read_row(conn)["count"] == 1798 and renew.overdue(conn, NOW) == []
    kind, _text, kw = said[-1]
    assert kind == "klubok.renew" and kw["data"]["tabs"] == [1798, 0, 0, 0]


def test_snapshot_before_fails_but_link_renews(conn, chromium, server, said, tmp_path):  # noqa: F811
    refresh(conn, server)
    server.expire(1780)
    s = open_session(chromium, server)
    real, failed = s.fetch_text, []

    def first_tab_read_fails(path, *args, **kw):
        if "type=1" in path and not failed:
            failed.append(path)
            return 500, ""
        return real(path, *args, **kw)

    s.fetch_text = first_tab_read_fails
    try:
        out = do_renew(conn, s, tmp_path)
    finally:
        s.close()
    assert failed and out.code == kr.RENEWED and out.overdue == 1780  # P = N з посилання
    assert server.renews == 1 and read_row(conn)["count"] == 1798


def test_snapshot_before_fails_without_link_stops(conn, chromium, server, said, tmp_path):  # noqa: F811
    refresh(conn, server)
    server.tab_fault[snapshot.RENEWABLE] = 500
    s = open_session(chromium, server)
    try:
        with pytest.raises(kr.RenewStop) as stop:
            do_renew(conn, s, tmp_path)
    finally:
        s.close()
    assert stop.value.code == snapshot.FAILED and server.renews == 0


def test_publish_session_with_1780_expired_renews_first(conn, root, chromium, server, said):  # noqa: F811
    server.clock = datetime.now(UTC)  # знімок пишеться з `datetime.now`
    refresh(conn, server)
    server.expire(1780)
    add(conn, root, 7301)
    add(conn, root, 7302, **OTHER_PAIR)
    s = fake.FakeSession(None, server.base, context=chromium.new_context(), publishing=True)
    opts = kp.Options(limit=1, submit_wait=3.0, pause=(0, 0), page_pause=(0, 0), settle=(0, 0))
    try:
        result = kp.run(conn, root, s, opts, sleep=lambda x: None, log=lambda x: None)
    finally:
        s.close()
    assert result.code == kp.DONE and server.renews == 1, result.reason
    paths = [path for _method, path in server.hits]
    assert paths.index("/renew/all") < next(i for i, p in enumerate(paths) if "add-edit-used" in p)
    assert [k for k, *_ in said][:3] == ["klubok.start", "klubok.renew", "klubok.post"]


# --- текст зупинки й вивід ---


class Manage:
    def __init__(self, html):
        self.html = html

    def fetch_text(self, path):
        return 200, self.html


def test_drop_text_when_renew_link_is_there():
    drop = snapshot.SnapshotError(snapshot.DROPPED, "було 1798, стало 17 — перевірте на Клубку")
    link = '<a href="/renew/all">Обновить все (1780)</a>'
    assert kr.explain(Manage(link), drop) == (
        "Схоже, оголошення прострочені (1780) — потрібне «Обновить все» (знімок не записано)"
    )
    assert kr.explain(Manage("<p>немає</p>"), drop) == drop.text
    failed = snapshot.SnapshotError(snapshot.FAILED, "HTTP 500")
    assert kr.explain(Manage(link), failed) == "HTTP 500"


def test_renew_cli_prints_the_result_once(monkeypatch):
    def fake_renew(conn, session, *, log, **kw):
        log("Обновить все: Наступне оновлення: 25.11 16:31")
        return kr.Outcome(kr.NOT_DUE, "Наступне оновлення: 25.11 16:31")

    monkeypatch.setattr(kr, "renew_once", fake_renew)
    lines = []
    assert cli._klubok_renew(None, None, False, lines.append) == 0
    assert lines == ["Обновить все: Наступне оновлення: 25.11 16:31"]
