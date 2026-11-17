"""KLB-004: «Обновить все» на Клубку робить робот (guard, правила, підставний Клубок, щоденний запуск)."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from shafa import events
from shafa.bot import alerts
from shafa.klubok import queue, renew
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_form as form
from shafa.robot import klubok_guard as guard
from shafa.robot import klubok_publish as kp
from shafa.robot import klubok_renew as kr
from shafa.robot.klubok_guard import Forbidden
from test_klb001_page import chromium  # noqa: F401  (фікстура браузера на модуль)
from test_klb002_queue import OTHER_PAIR, ad_row, add

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)  # = `fake.Server.clock` за замовчуванням
RENEW = "https://klubok.com/renew/all"


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")
    monkeypatch.setattr(form, "FIELD_WAIT", 1.0)
    monkeypatch.setattr(form, "BRAND_WAIT", 1.5)


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: out.append((kind, text, kw)))
    return out


@pytest.fixture
def server():
    srv = fake.Server(delay=(0.01, 0.05), seed=1, listings=20)
    yield srv
    srv.close()


def open_session(chromium, server, scenario="ok", **kw):  # noqa: F811
    kw.setdefault("renewing", True)
    return fake.FakeSession(None, server.base, scenario, context=chromium.new_context(), **kw)


@pytest.fixture
def session(chromium, server):  # noqa: F811
    s = open_session(chromium, server)
    yield s
    s.close()


def renew_hits(server):
    return [path for method, path in server.hits if method == "GET" and "renew" in path]


def do_renew(conn, session, tmp_path, now=NOW, **kw):
    return kr.renew_once(conn, session, now=lambda: now, log=lambda line: None,
                         page_pause=(0, 0), settle=(0, 0), data_dir=tmp_path, **kw)  # fmt: skip


# --- guard ---


def test_guard_passes_exactly_one_renew_all_with_ticket():
    ticket = guard.RenewTicket()
    assert guard.request_verdict("GET", RENEW, renew=ticket) == (True, guard.RENEWED)
    allowed, why = guard.request_verdict("GET", RENEW, renew=ticket)
    assert not allowed and "уже використано" in why


def test_guard_blocks_renew_all_without_ticket():
    assert not guard.request_verdict("GET", RENEW)[0]
    assert not guard.request_verdict("GET", RENEW, publishing=True, ticket=guard.Ticket(1))[0]


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "https://klubok.com/renew/123"),
        ("GET", "https://klubok.com/users/198135?pg=renew-used&id=1"),
        ("GET", "https://klubok.com/users/198135?pg=marksold&id=1"),
        ("GET", "https://klubok.com/renew/all?id=1"),
        ("GET", "https://klubok.com/renew/all/"),
        ("GET", "http://klubok.com/renew/all"),
        ("GET", "https://m.klubok.com/renew/all"),
        ("GET", "https://klubok.com:8443/renew/all"),
        ("POST", RENEW),
        ("GET", "https://klubok.com/payment/37953236"),
    ],
)
def test_guard_blocks_everything_else_even_with_live_renew_ticket(method, url):
    ticket = guard.RenewTicket()
    assert not guard.request_verdict(method, url, renew=ticket)[0]
    assert ticket.alive  # чужий запит квиток не гасить


def test_renew_ticket_only_for_renew_or_start_of_publishing():
    plain = form.Session()
    plain._setup(True)
    with pytest.raises(Forbidden):
        plain.issue_renew_ticket()  # знімок, dry-fill
    once = form.Session()
    once._setup(True, renewing=True)
    once.issue_renew_ticket()
    with pytest.raises(Forbidden):
        once.issue_renew_ticket()  # один на сесію
    late = form.Session()
    late._setup(True, publishing=True)
    late.issue_ticket(1)
    with pytest.raises(Forbidden):
        late.issue_renew_ticket()  # після квитка Картки — ні


# --- правила без браузера ---

MANAGE = (
    '<td> <a href="/renew/all" class="b-tabs__item__link" onclick="ga(\'send\')" > '
    "Обновить все (1785) </a> </td>"
)


def test_link_reads_count_from_manage_page():
    assert renew.link(MANAGE) == renew.Link(True, 1785)
    assert renew.link('<a href="/users/1?pg=renew-used&id=2">Обновить</a>') == renew.Link(False)
    assert renew.link("") == renew.Link(False)


def at(days=0.0):
    return NOW + timedelta(days=days)


def test_judge():
    after = {1: at(7), 2: at(7), 3: at(1)}
    assert renew.judge([1, 2], 3, after, 3, NOW).ok
    bad = renew.judge([1, 2, 3], 3, after, 3, NOW)
    assert not bad.ok and bad.renewed == [1, 2]  # 2 з 3 < 95 %
    assert not renew.judge([1, 2], 10, after, 3, NOW).ok  # активних < 90 %
    assert renew.judge([], 3, after, 3, NOW, renew.Link(False)).ok
    assert not renew.judge([], 3, after, 3, NOW, renew.Link(True, 2)).ok


def listings(conn, *dates, read=NOW):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO klubok_listings_read VALUES (1, ?, ?)",
            (read.isoformat(), len(dates)),
        )
        for n, moment in enumerate(dates, start=1):
            conn.execute(
                "INSERT INTO klubok_listings (id, name, sizes, renew_date, added_at) "
                "VALUES (?, ?, '[]', ?, 'x')",
                (n, f"Пара {n}", moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )


def test_status_lines(conn, tmp_path):
    listings(conn, at(1.5), at(3))
    assert renew.status(conn, NOW, tmp_path).line.startswith("Обновить все: наступне 26.11 о ")
    late = renew.status(conn, at(1.5) + timedelta(hours=1), tmp_path)
    assert late.line.startswith("Обновить все: прострочено з 26.11") and not late.warn
    assert renew.status(conn, at(1.5) + timedelta(hours=13), tmp_path).warn
    renew.save(tmp_path, day=at(1.5).date().isoformat(), at=at(1.5).isoformat(), ok=False,
               text="оновлено лише 0 з 1")  # fmt: skip
    failed = renew.status(conn, at(1.5) + timedelta(minutes=5), tmp_path)
    assert failed.warn and "оновлено лише 0 з 1" in failed.line


def test_at_setting(monkeypatch):
    monkeypatch.delenv(renew.AT_SETTING, raising=False)
    assert f"{renew.at_setting():%H:%M}" == "20:00"
    monkeypatch.setenv(renew.AT_SETTING, "")
    assert renew.at_setting() is None
    monkeypatch.setenv(renew.AT_SETTING, "вечір")
    with pytest.raises(renew.SetupError):
        renew.at_setting()


# --- підставний Клубок ---


def published_ad(conn, root, klubok_id):
    item_id = add(conn, root, 7301)
    ad_row(conn, item_id, queue.PUBLISHED, klubok_id=klubok_id)
    return item_id


def test_overdue_one_get_snapshot_event_renewed_at(conn, root, server, session, said, tmp_path):
    item_id = published_ad(conn, root, 800_000_003)
    out = do_renew(conn, session, tmp_path)
    assert out.code == kr.RENEWED and out.link == 20 and out.overdue == 20 and out.renewed == 20
    assert renew_hits(server) == ["/renew/all"] and server.renews == 1
    assert [k for k, *_ in said] == ["klubok.renew"]
    stamp = conn.execute("SELECT renewed_at FROM klubok_ads WHERE item_id = ?", (item_id,))
    assert stamp.fetchone()[0] == NOW.isoformat(timespec="seconds")
    assert renew.due(conn) == NOW + timedelta(days=7)  # новий знімок
    assert renew.load(tmp_path)["ok"] is True


def test_second_renew_in_same_session_is_blocked(conn, server, session, tmp_path, said):
    assert do_renew(conn, session, tmp_path).code == kr.RENEWED
    with pytest.raises(Forbidden):
        session.renew_all()
    with pytest.raises(Exception, match="ERR_FAILED"):  # навіть прямий перехід — guard обриває
        session.page.goto(session.site_url("/renew/all"))
    assert server.renews == 1
    assert any("уже використано" in why for url, why in session.blocked)


def test_not_due_no_renew_requests(conn, server, session, said, tmp_path):
    out = do_renew(conn, session, tmp_path, now=datetime(2026, 11, 16, 12, tzinfo=UTC))
    assert out.code == kr.NOT_DUE and out.text == "Наступне оновлення: 17.11 10:00"
    assert renew_hits(server) == [] and server.manage_reads == 0 and said == []


def test_no_link_on_manage_page_means_no_get(conn, server, session, said, tmp_path):
    server.clock = datetime(2026, 11, 16, tzinfo=UTC)  # Клубок: ще нічого не прострочено
    out = do_renew(conn, session, tmp_path)
    assert out.code == kr.NO_LINK and out.overdue == 20
    assert renew_hits(server) == [] and server.manage_reads == 1
    assert [k for k, *_ in said] == ["klubok.renew_skip"]
    assert "day" not in renew.load(tmp_path)


def test_already_renewed_today_no_get(conn, server, session, said, tmp_path):
    renew.save(tmp_path, day=NOW.date().isoformat(), ok=True)
    assert do_renew(conn, session, tmp_path).code == kr.DONE_TODAY
    assert renew_hits(server) == [] and server.manage_reads == 0


def test_stale_renew_is_loud_failure(conn, server, session, said, tmp_path):
    server.renew_mode = "stale"
    out = do_renew(conn, session, tmp_path)
    assert out.code == kr.FAILED and server.renews == 1
    assert [(k, kw.get("loud")) for k, _, kw in said] == [("klubok.renew_failed", True)]
    assert "не підтвердилось" in said[0][1]
    state = renew.load(tmp_path)
    assert state["ok"] is False and state["day"] == NOW.date().isoformat()


def test_429_on_renew_stops_and_is_loud(conn, server, session, said, tmp_path):
    server.too_many.add("/renew/all")
    with pytest.raises(kr.RenewStop) as stop:
        do_renew(conn, session, tmp_path)
    assert stop.value.code == kr.TOO_MANY
    assert [k for k, *_ in said] == ["klubok.renew_failed"]


def test_no_login_stops_before_any_renew(conn, chromium, server, said, tmp_path):  # noqa: F811
    s = open_session(chromium, server, "login")
    try:
        with pytest.raises(kr.RenewStop) as stop:
            do_renew(conn, s, tmp_path)
    finally:
        s.close()
    assert stop.value.code == kr.LOGIN and renew_hits(server) == []


def human_session(chromium, server, link='a[href="/renew/all"]'):  # noqa: F811
    """Сесія `--human-click`, де «людина» натискає `link` на сторінці керування."""
    s = open_session(chromium, server, human=True)
    original = s.open_manage

    def human_opens_and_clicks():
        original()
        s.page.evaluate("sel => setTimeout(() => document.querySelector(sel).click(), 300)", link)

    s.open_manage = human_opens_and_clicks
    return s


def test_human_click_goes_through_guard(conn, chromium, server, said, tmp_path):  # noqa: F811
    seen = []  # чи стояла позначка доби, коли запит дійшов до Клубку
    renew_overdue = server.renew_overdue
    server.renew_overdue = lambda: (seen.append(renew.load(tmp_path).get("day")), renew_overdue())
    s = human_session(chromium, server)
    try:
        out = do_renew(conn, s, tmp_path, human=True, human_wait=10)
    finally:
        s.close()
    assert out.code == kr.RENEWED and server.renews == 1
    assert seen == [NOW.date().isoformat()]  # позначка — щойно guard пропустив (RENEWED)


def test_human_wrong_link_no_mark_retry_same_day(conn, chromium, server, said, tmp_path):  # noqa: F811
    wrong = human_session(chromium, server, 'a[href*="pg=renew-used"]')  # «Обновить» одного
    try:
        out = do_renew(conn, wrong, tmp_path, human=True, human_wait=2)
    finally:
        wrong.close()
    assert out.code == kr.NOT_CLICKED and server.renews == 0
    assert any("небезпечна" in why for url, why in wrong.blocked if url.endswith("/users/198135"))
    assert "day" not in renew.load(tmp_path)
    again = human_session(chromium, server)  # того ж дня — ще раз, тепер правильне посилання
    try:
        out = do_renew(conn, again, tmp_path, human=True, human_wait=10)
    finally:
        again.close()
    assert out.code == kr.RENEWED and server.renews == 1


def test_human_does_not_click(conn, chromium, server, said, tmp_path):  # noqa: F811
    s = open_session(chromium, server, human=True)
    try:
        out = do_renew(conn, s, tmp_path, human=True, human_wait=1)
    finally:
        s.close()
    assert out.code == kr.NOT_CLICKED and server.renews == 0
    assert "day" not in renew.load(tmp_path)  # можна ще раз сьогодні


def test_publish_session_renews_first(conn, root, chromium, server, said):  # noqa: F811
    # справжній годинник: знімок пишеться з `datetime.now`, а черга не бере старший за добу
    server.clock = datetime.now(UTC)  # штучні оголошення прострочені з 17.11 10:00 UTC
    add(conn, root, 7301)
    add(conn, root, 7302, **OTHER_PAIR)
    s = fake.FakeSession(None, server.base, context=chromium.new_context(), publishing=True)
    opts = kp.Options(limit=1, submit_wait=3.0, pause=(0, 0), page_pause=(0, 0), settle=(0, 0))
    try:
        result = kp.run(conn, root, s, opts, sleep=lambda x: None, log=lambda x: None)
    finally:
        s.close()
    assert result.code == kp.DONE and server.renews == 1
    paths = [path for _method, path in server.hits]
    first_submit = next(i for i, path in enumerate(paths) if "add-edit-used" in path)
    assert paths.index("/renew/all") < first_submit
    assert [k for k, *_ in said][:3] == ["klubok.start", "klubok.renew", "klubok.post"]


# --- щоденний запуск ---


class Busy:
    def __init__(self):
        self.value = None

    def __call__(self):
        return self.value


@pytest.fixture
def daily(conn, tmp_path, monkeypatch):
    monkeypatch.setenv(renew.AT_SETTING, "20:00")
    spawned, lines, busy = [], [], Busy()
    clock = {"now": datetime(2026, 11, 24, 20, 5).astimezone()}

    class NoClose:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    d = kr.Daily(
        command=lambda: ["--klubok-renew"], spawn=spawned.append, busy=busy, log=lines.append,
        connect=NoClose, clock=lambda: clock["now"], folder=tmp_path,
    )  # fmt: skip
    return d, spawned, busy, clock


def test_daily_starts_when_due_and_free_once_a_day(conn, daily):
    d, spawned, busy, clock = daily
    listings(conn, clock["now"] - timedelta(hours=1), read=clock["now"])
    busy.value = {"mode": "klubok-publish"}
    assert d.tick() == "busy" and spawned == []
    busy.value = None
    clock["now"] += timedelta(minutes=40)  # Mac спав / робот звільнився — той самий день
    assert d.tick() == "started" and spawned == [["--klubok-renew"]]
    assert d.tick() is None and len(spawned) == 1


def test_daily_off_early_or_far(conn, daily, monkeypatch):
    d, spawned, _busy, clock = daily
    listings(conn, clock["now"] + timedelta(days=3), read=clock["now"])
    assert d.tick() is None  # термін далеко, знімок свіжий
    with conn:
        conn.execute(
            "UPDATE klubok_listings SET renew_date = ?",
            ((clock["now"] + timedelta(minutes=30)).isoformat(),),
        )
    clock["now"] = clock["now"].replace(hour=19)
    assert d.tick() is None  # ще не 20:00
    clock["now"] = clock["now"].replace(hour=21)
    monkeypatch.setenv(renew.AT_SETTING, "")
    assert d.tick() is None  # вимкнено
    monkeypatch.setenv(renew.AT_SETTING, "20:00")
    monkeypatch.setenv(queue.FLAG, "")
    assert d.tick() is None  # Клубок вимкнено
    assert spawned == []


def test_daily_due_within_hour_does_not_close_the_day(conn, daily):
    d, spawned, _busy, clock = daily
    listings(conn, clock["now"] + timedelta(minutes=30), read=clock["now"])
    assert d.tick() == "started"  # команда скаже «ще не настав»
    clock["now"] += timedelta(minutes=10)
    assert d.tick() is None  # не частіше ніж раз на годину
    clock["now"] += timedelta(minutes=55)
    assert d.tick() == "started"  # термін минув — тепер доба закривається
    clock["now"] += timedelta(hours=1, minutes=5)
    assert d.tick() is None and len(spawned) == 2


def test_daily_runs_when_snapshot_is_old(conn, daily):
    d, _spawned, _busy, clock = daily
    listings(conn, clock["now"] + timedelta(days=3), read=clock["now"] - timedelta(hours=25))
    assert d.tick() == "started"


def test_daily_thread_stops(conn, daily):
    d, *_ = daily
    stop = threading.Event()
    stop.set()
    d.run(stop, interval=0)


# --- тривога Власнику ---


def test_alert_when_overdue_longer_than_12_hours(conn, tmp_path):
    listings(conn, NOW - timedelta(hours=13))
    state = alerts.snapshot(conn, tmp_path, NOW)
    found = alerts._renew_events(conn, tmp_path, state, NOW)
    assert len(found) == 1 and "прострочено з" in found[0].text
    found[0].commit(state)
    assert alerts._renew_events(conn, tmp_path, state, NOW + timedelta(hours=2)) == []
    listings_ok = alerts._renew_events(conn, tmp_path, {"renew_due": ""}, NOW - timedelta(hours=2))
    assert listings_ok == []  # прострочено лише 11 год
