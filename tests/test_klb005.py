"""KLB-005: «Схоже, продано» — виявлення, екран, guard, робот на підставному Клубку, сесія публікації."""

import json
from datetime import UTC, datetime

import pytest

from shafa import events
from shafa.klubok import queue, refresh, screen, snapshot, sold
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_form as form
from shafa.robot import klubok_guard as guard
from shafa.robot import klubok_marksold as km
from shafa.robot import klubok_publish as kp
from shafa.robot.klubok_guard import Forbidden
from test_klb001_page import chromium  # noqa: F401  (фікстура браузера на модуль)
from test_klb002_queue import OTHER_PAIR, ad_row, add, row
from test_klb009 import app, lock_file, spawner  # noqa: F401
from test_web import get, html, location, post

FIRST = fake.listing(0)["id"]  # 800000000 — оголошення підставного Клубку
SECOND = fake.listing(1)["id"]


def url(klubok_id):
    return f"https://klubok.com/users/198135?pg=marksold&id={klubok_id}"


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


def gone(conn, post_id, at="2026-11-20T03:10:00+00:00"):
    with conn:
        conn.execute(
            "INSERT INTO post_checks (post_id, gone_at) VALUES (?, ?) "
            "ON CONFLICT (post_id) DO UPDATE SET gone_at = excluded.gone_at",
            (post_id, at),
        )


def published(conn, root, post_id, klubok_id, **kw):
    item_id = add(conn, root, post_id, **kw)
    ad_row(conn, item_id, queue.PUBLISHED, klubok_id=klubok_id,
           url=f"https://klubok.com/item-x-{klubok_id}", published_at="2026-11-10T10:00:00+00:00")  # fmt: skip
    return item_id


def her_listing(conn, klubok_id, post_id, *, sold_=0, asked_at=None, name="Чоботи Ara 39"):
    with conn:
        conn.execute(
            "INSERT INTO klubok_listings (id, name, sizes, sold, post_id, asked_at, added_at, "
            "photo, url) VALUES (?, ?, '[]', ?, ?, ?, 'x', ?, ?)",
            (klubok_id, name, sold_, post_id, asked_at,
             f"https://img.klubok.com/img/used/x/m/{klubok_id}_1.jpg",
             f"https://klubok.com/item-her-{klubok_id}"),
        )  # fmt: skip


# --- виявлення (без мережі) ---


def test_gone_post_is_asked_once_and_others_untouched(conn, root, said):
    asked = published(conn, root, 7301, FIRST)
    alive = published(conn, root, 7302, SECOND, **OTHER_PAIR)
    gone(conn, 7301)

    assert sold.detect(conn) == [asked]
    ad = row(conn, asked)
    assert ad["state"] == sold.SOLD_ASKED and ad["gone_at"] is not None
    assert row(conn, alive)["state"] == queue.PUBLISHED
    assert [k for k, *_ in said] == ["klubok.sold_asked"]
    assert sold.detect(conn) == []  # повтор нічого не змінює


def test_refresh_runs_detection_after_sync(conn, root):
    asked = published(conn, root, 7301, FIRST)
    gone(conn, 7301)
    refresh.refresh(conn, root)
    assert row(conn, asked)["state"] == sold.SOLD_ASKED


def test_no_means_never_ask_again(conn, root):
    item_id = published(conn, root, 7301, FIRST)
    gone(conn, 7301)
    sold.detect(conn)
    assert sold.answer(conn, sold.AD, item_id, yes=False)
    assert row(conn, item_id)["state"] == queue.PUBLISHED
    assert sold.detect(conn) == [] and sold.asks(conn) == []
    assert row(conn, item_id)["state"] == queue.PUBLISHED


def test_yes_waits_for_robot(conn, root):
    item_id = published(conn, root, 7301, FIRST)
    gone(conn, 7301)
    sold.detect(conn)
    assert sold.answer(conn, sold.AD, item_id, yes=True)
    assert not sold.answer(conn, sold.AD, item_id, yes=True)  # удруге — уже чекає
    assert row(conn, item_id)["sold_ok_at"] is not None
    assert [(t.kind, t.klubok_id) for t in sold.targets(conn)] == [(sold.AD, FIRST)]
    assert sold.asks(conn)[0].waiting


def test_her_listing_with_known_post_is_asked(conn, root):
    her_listing(conn, 555, 7400)
    her_listing(conn, 556, 7401)  # пост на місці
    gone(conn, 7400)
    asks = sold.asks(conn)
    assert [(a.kind, a.key) for a in asks] == [(sold.LISTING, 555)]
    assert not asks[0].waiting

    assert sold.answer(conn, sold.LISTING, 555, yes=True)
    assert sold.asks(conn)[0].waiting
    assert [(t.kind, t.klubok_id) for t in sold.targets(conn)] == [(sold.LISTING, 555)]
    assert sold.answer(conn, sold.LISTING, 555, yes=False)  # передумала, поки чекало
    assert sold.asks(conn) == [] and sold.targets(conn) == []
    assert not sold.answer(conn, sold.LISTING, 556, yes=True)  # пост не зник — не питаємо


def test_listing_tied_to_programme_ad_is_not_asked_twice(conn, root):
    item_id = published(conn, root, 7301, FIRST)
    her_listing(conn, FIRST, 7301)
    gone(conn, 7301)
    sold.detect(conn)
    assert [(a.kind, a.key) for a in sold.asks(conn)] == [(sold.AD, item_id)]


def test_sold_by_hand_is_quietly_sold_in_snapshot(conn, root, said):
    asked = published(conn, root, 7301, FIRST)
    plain = published(conn, root, 7302, SECOND, **OTHER_PAIR)
    gone(conn, 7301)
    sold.detect(conn)
    first = snapshot.Read([fake.listing(0), fake.listing(1)], 2)
    snapshot.save(conn, first)
    without = snapshot.Read([fake.listing(1), *[fake.listing(n) for n in range(2, 20)]], 19)
    snapshot.save(conn, without)  # FIRST зникло з активних і «Обновить»
    assert row(conn, asked)["state"] == sold.SOLD
    assert row(conn, plain)["state"] == queue.PUBLISHED
    assert sold.asks(conn) == []


def test_detect_catches_up_already_sold_listing(conn, root):
    item_id = published(conn, root, 7301, FIRST)
    her_listing(conn, FIRST, None, sold_=1)
    sold.detect(conn)
    assert row(conn, item_id)["state"] == sold.SOLD


# --- guard ---


def test_guard_passes_exactly_one_marksold_of_the_ticket():
    ticket = guard.MarkSoldTicket(FIRST)
    assert guard.request_verdict("GET", url(FIRST), marksold=ticket) == (True, guard.MARKSOLD)
    allowed, why = guard.request_verdict("GET", url(FIRST), marksold=ticket)
    assert not allowed and "уже використано" in why


@pytest.mark.parametrize(
    "bad",
    [
        url(SECOND),  # чужий id
        f"https://klubok.com/users/198135?id={FIRST}&pg=marksold",  # інший порядок
        f"https://klubok.com/users/198135?pg=marksold&id={FIRST}&x=1",  # зайвий параметр
        f"https://klubok.com/users/1?pg=marksold&id={FIRST}",  # інший користувач
        f"http://klubok.com/users/198135?pg=marksold&id={FIRST}",
        f"https://evil.klubok.com/users/198135?pg=marksold&id={FIRST}",
        f"https://klubok.com/users/198135?pg=delete&id={FIRST}",
        f"https://klubok.com/users/198135?pg=renew-used&id={FIRST}",
    ],
)
def test_guard_blocks_anything_but_the_exact_marksold(bad):
    ticket = guard.MarkSoldTicket(FIRST)
    assert not guard.request_verdict("GET", bad, marksold=ticket)[0]
    assert ticket.alive


def test_guard_blocks_marksold_without_ticket_and_mass_action():
    assert not guard.request_verdict("GET", url(FIRST))[0]
    ticket = guard.MarkSoldTicket(FIRST)
    assert not guard.request_verdict("POST", url(FIRST), marksold=ticket)[0]  # markSoldAll — POST
    assert not guard.request_verdict(
        "POST", "https://klubok.com/users/198135/manage", "application/x-www-form-urlencoded",
        marksold=ticket,
    )[0]  # fmt: skip
    assert ticket.alive


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_ticket_needs_a_real_id(value):
    with pytest.raises(ValueError):
        guard.MarkSoldTicket(value)


# --- робот на підставному Клубку ---


@pytest.fixture
def server():
    srv = fake.Server(delay=(0.01, 0.05), seed=1, listings=6)
    yield srv
    srv.close()


def open_session(chromium, server, **kw):  # noqa: F811
    kw.setdefault("marking", True)
    return fake.FakeSession(None, server.base, "ok", context=chromium.new_context(), **kw)


@pytest.fixture
def session(chromium, server):  # noqa: F811
    s = open_session(chromium, server)
    yield s
    s.close()


def confirmed(conn, root, post_id=7301, klubok_id=FIRST, **kw):
    item_id = published(conn, root, post_id, klubok_id, **kw)
    gone(conn, post_id)
    sold.detect(conn)
    sold.answer(conn, sold.AD, item_id, yes=True)
    return item_id


def mark(conn, session, **kw):
    return km.mark_sold(conn, session, log=lambda line: None, pause=(0, 0), settle=(0, 0),
                        page_pause=(0, 0), **kw)  # fmt: skip


def marksold_hits(server):
    return [p for m, p in server.hits if m == "GET" and "marksold" in p]


def test_yes_one_get_then_removed_by_sold_tab(conn, root, server, session, said):
    item_id = confirmed(conn, root)
    said.clear()
    outcome = mark(conn, session)

    assert outcome.code == km.DONE and [t.key for t in outcome.removed] == [item_id]
    assert server.marksolds == [FIRST]
    assert marksold_hits(server) == [f"/users/198135?pg=marksold&id={FIRST}"]
    assert server.item_tabs == [2]  # перевірка — один запит «Проданные», знімок не читали
    ad = row(conn, item_id)
    assert ad["state"] == sold.REMOVED and ad["removed_at"] is not None
    assert [k for k, *_ in said] == ["klubok.sold"]
    assert server.traps == []
    assert mark(conn, session).code == km.NOTHING and server.marksolds == [FIRST]  # повтору немає


def test_not_changed_is_loud_and_asks_again(conn, root, server, session, said):
    item_id = confirmed(conn, root)
    server.marksold_mode = "stale"
    said.clear()
    outcome = mark(conn, session)

    assert outcome.code == km.FAILED and server.marksolds == [FIRST]
    ad = row(conn, item_id)
    assert ad["state"] == sold.SOLD_ASKED and ad["sold_ok_at"] is None
    assert ad["problem_text"] == km.ACTIVE_TEXT
    assert [(k, kw.get("loud")) for k, _t, kw in said] == [("klubok.sold_failed", True)]
    assert sold.asks(conn)[0].note == km.ACTIVE_TEXT


def test_hidden_but_not_sold_is_check_on_klubok(conn, root, server, session):
    item_id = confirmed(conn, root)
    server.marksold_mode = "hidden"
    mark(conn, session)
    assert row(conn, item_id)["problem_text"] == km.HIDDEN_TEXT
    assert row(conn, item_id)["state"] == sold.SOLD_ASKED


def test_429_stops_and_unchecked_goes_back_to_question(conn, root, server, session):
    item_id = confirmed(conn, root)
    server.too_many.add("/users/198135")
    with pytest.raises(km.MarkStop) as stop:
        mark(conn, session)
    assert stop.value.code == km.TOO_MANY
    assert row(conn, item_id)["sold_ok_at"] is None


def test_her_listing_marked_sold(conn, root, server, session):
    her_listing(conn, SECOND, 7400)
    gone(conn, 7400)
    sold.answer(conn, sold.LISTING, SECOND, yes=True)
    outcome = mark(conn, session)
    assert [t.klubok_id for t in outcome.removed] == [SECOND]
    assert conn.execute("SELECT sold FROM klubok_listings WHERE id = ?", (SECOND,)).fetchone()[0]
    assert sold.asks(conn) == []


def test_limit_and_nothing_without_yes(conn, root, server, session):
    published(conn, root, 7301, FIRST)
    gone(conn, 7301)
    sold.detect(conn)  # спитали, але «Так» ніхто не натиснув
    assert mark(conn, session).code == km.NOTHING and server.marksolds == []
    confirmed(conn, root, 7302, SECOND, **OTHER_PAIR)
    item = confirmed(
        conn, root, 7303, fake.listing(2)["id"], brand="Rieker", title="Туфлі Rieker 39"
    )
    outcome = mark(conn, session, limit=1)
    assert len(server.marksolds) == 1 and len(outcome.removed) == 1
    assert row(conn, item)["state"] == sold.SOLD_ASKED  # друге — наступного разу


def test_without_login_stops(conn, root, chromium, server):  # noqa: F811
    confirmed(conn, root)
    s = fake.FakeSession(None, server.base, "login", context=chromium.new_context(), marking=True)
    try:
        with pytest.raises(km.MarkStop) as stop:
            mark(conn, s)
    finally:
        s.close()
    assert stop.value.code == km.LOGIN and server.marksolds == []


def test_session_without_marking_cannot_get_a_ticket(chromium, server):  # noqa: F811
    s = fake.FakeSession(None, server.base, "ok", context=chromium.new_context())
    try:
        with pytest.raises(Forbidden):
            s.issue_marksold_ticket(FIRST)
    finally:
        s.close()


def test_page_cannot_mark_another_id(session, server):
    session.open_form()
    ticket = session.issue_marksold_ticket(FIRST)
    session.page.evaluate("u => fetch(u).catch(() => null)", url(SECOND).replace(
        "https://klubok.com", server.base))  # fmt: skip
    session.page.wait_for_timeout(300)
    assert server.marksolds == [] and ticket.alive


def test_human_click_robot_waits_for_the_link(conn, root, server, session):
    confirmed(conn, root)
    outcome = mark(conn, session, human=True, human_wait=0.5)  # ніхто не натиснув
    assert server.marksolds == [] and len(outcome.skipped) == 1
    assert sold.targets(conn)  # лишилось у черзі на зняття


# --- сесія публікації ---


def test_publish_session_marks_sold_before_first_pair(conn, root, chromium, server, said):  # noqa: F811
    item_id = confirmed(conn, root)
    add(conn, root, 7302, **OTHER_PAIR)  # пара в черзі
    s = fake.FakeSession(None, server.base, "ok", context=chromium.new_context(), publishing=True)
    try:
        opts = kp.Options(limit=1, submit_wait=3.0, pause=(0, 0), page_pause=(0, 0), renew=False,
                          marksold_pause=(0, 0), marksold_settle=(0, 0))  # fmt: skip
        result = kp.run(conn, root, s, opts, sleep=lambda s_: None, log=lambda line: None)
    finally:
        s.close()
    assert result.code == kp.DONE, result.reason
    assert row(conn, item_id)["state"] == sold.REMOVED
    order = [p for m, p in server.hits if "marksold" in p or "add-edit-used" in p]
    assert order[0].startswith("/users/198135?pg=marksold") and "add-edit-used" in order[-1]
    assert len(order) == 2
    kinds = [k for k, *_ in said]
    assert kinds.index("klubok.sold") < kinds.index("klubok.post")


# --- екран ---


def test_decide_shows_block_counts_and_answers(conn, root, app, lock_file):  # noqa: F811
    item_id = published(conn, root, 7301, FIRST)
    gone(conn, 7301, "2026-11-20T03:10:00+00:00")
    her_listing(conn, 555, 7400)
    gone(conn, 7400)
    sold.detect(conn)
    assert screen.counts(conn)["decide"] == 2
    page = html(get(app, "/klubok/decide"))
    assert "Схоже, продано (2)" in page and "Так, продано — зняти з Клубку" in page
    assert "пост 7301 зник з каналу 20.11.2026" in page

    response = post(app, "/klubok/sold", kind="ad", key=str(item_id), answer="yes")
    assert "msg=klubok_sold_yes" in location(response)
    assert "Чекає робота" in html(get(app, "/klubok/decide"))
    response = post(app, "/klubok/sold", kind="listing", key="555", answer="no")
    assert "msg=klubok_sold_no" in location(response)
    assert screen.counts(conn)["decide"] == 1
    assert "msg=stale" in location(
        post(app, "/klubok/sold", kind="listing", key="555", answer="no")
    )
    assert get(app, "/klubok/sold").status == 404


@pytest.mark.parametrize(
    "form_", [{"kind": "x", "key": "1", "answer": "yes"}, {"kind": "ad", "key": "a", "answer": "yes"},
              {"kind": "ad", "key": "1", "answer": "maybe"}],
)  # fmt: skip
def test_bad_answer_is_400(app, form_):  # noqa: F811
    assert post(app, "/klubok/sold", **form_).status == 400


def test_answer_needs_token(conn, root, app):  # noqa: F811
    item_id = published(conn, root, 7301, FIRST)
    gone(conn, 7301)
    sold.detect(conn)
    response = post(app, "/klubok/sold", token="wrong", kind="ad", key=str(item_id), answer="yes")
    assert response.status >= 400
    assert row(conn, item_id)["sold_ok_at"] is None


def test_answer_marks_are_parsed():
    stamp = datetime(2026, 11, 22, tzinfo=UTC).isoformat()
    assert sold.answer_of(None) is None
    assert sold.answer_of(f"{stamp}|yes") == sold.YES
    assert sold.answer_of(f"{stamp}|no") == sold.NO
    assert sold.answer_of(stamp) is None
    assert json.dumps(sold.YES)  # константи — прості рядки
