"""KLB-003 D2/D4/D5/D6: сесія публікації на підставному Клубку (Chromium Playwright, без мережі)."""

import pytest

from shafa import events
from shafa.klubok import queue, snapshot
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_form as form
from shafa.robot import klubok_publish as kp
from shafa.robot.klubok_guard import Forbidden
from test_klb001_page import chromium  # noqa: F401  (фікстура браузера на модуль)
from test_klb002_queue import OTHER_PAIR, add, row


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
    srv = fake.Server(delay=(0.01, 0.05), seed=1, listings=7)
    yield srv
    srv.close()


@pytest.fixture
def cards(conn, root):
    """Дві різні пари в черзі (друга — не дубль першої після публікації)."""
    return [add(conn, root, 7301), add(conn, root, 7302, **OTHER_PAIR)]


def open_session(chromium, server, scenario="ok", human=False):  # noqa: F811
    return fake.FakeSession(
        None, server.base, scenario, context=chromium.new_context(), publishing=True, human=human
    )


def run(conn, root, session, *, limit=2, tmp=None, **kw):
    opts = kp.Options(
        limit=limit, post_id=kw.pop("post_id", None), human=kw.pop("human", False),
        submit_wait=kw.pop("submit_wait", 3.0), human_wait=kw.pop("human_wait", 5.0),
        pause=(0, 0), page_pause=(0, 0),
    )  # fmt: skip
    return kp.run(conn, root, session, opts, sleep=lambda s: None, log=lambda line: None,
                  screens=tmp, **kw)  # fmt: skip


@pytest.fixture
def session(chromium, server):  # noqa: F811
    s = open_session(chromium, server)
    yield s
    s.close()


def last_session(conn):
    return conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()


# --- відповіді (D4.4) ---


def test_success_publishes_both_and_links_come_from_final_snapshot(
    conn, root, cards, server, session, said
):
    result = run(conn, root, session)

    assert result.code == kp.DONE, result.problems
    assert [p[1] for p in result.published] == server.published
    for item_id, klubok_id in zip(cards, server.published, strict=True):
        ad = row(conn, item_id)
        assert ad["state"] == queue.PUBLISHED and ad["klubok_id"] == klubok_id
        assert ad["url"] == f"https://klubok.com/item-new-{klubok_id}"  # з кінцевого знімка
    assert server.submits == 2 and server.captcha_posts == 2
    assert server.traps == []  # /payment/… не завантажено
    assert any("очікувано" in why for url, why in session.blocked if "/payment/" in url)
    s = last_session(conn)
    assert (s["site"], s["stop_code"], s["published"], s["problems"]) == ("klubok", "done", 2, 0)
    assert s["ended_at"] is not None
    assert [k for k, *_ in said] == ["klubok.start", "klubok.post", "klubok.post", "klubok.end"]
    assert len(result.lines()) <= 15


def test_error_is_captcha_problem_and_stop(conn, root, cards, server, session, said):
    server.submit = "error"

    result = run(conn, root, session)

    assert result.code == kp.CAPTCHA
    first = row(conn, cards[0])
    assert (first["state"], first["problem_code"]) == ("problem", "klubok_captcha")
    assert "оцінка 0.1" in first["problem_text"] and "оголошення немає" in first["problem_text"]
    assert row(conn, cards[1])["state"] == queue.QUEUED  # другу не брали
    assert server.submits == 1 and server.traps == []
    assert said[-1][0] == "klubok.stop" and said[-1][2]["loud"] is True


@pytest.mark.parametrize("mode", ["timeout", "http500", "notjson", "other"])
def test_sent_without_certain_answer_is_uncertain_and_never_retried(
    conn, root, cards, server, session, mode
):
    server.submit, server.submit_delay = mode, 4.0

    result = run(conn, root, session, submit_wait=1.5)

    assert result.code == kp.UNCERTAIN
    first = row(conn, cards[0])
    assert (first["state"], first["problem_code"]) == ("problem", queue.UNCERTAIN)
    assert server.submits == 1  # повтору немає
    assert row(conn, cards[1])["state"] == queue.QUEUED


def test_not_sent_goes_back_to_queue_counts_in_limit_and_stops(
    conn, root, cards, server, session, monkeypatch, tmp_path
):
    server.submit = "nosend"
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")

    result = run(conn, root, session, submit_wait=1.0, tmp=tmp_path)

    assert result.code == kp.NOT_SENT and server.submits == 0
    first = row(conn, cards[0])
    assert first["state"] == queue.QUEUED and first["publish_started_at"] is not None
    assert queue.take(conn, root, kp.datetime.now().astimezone()).code == queue.LIMIT_REACHED


def test_429_on_submit_is_uncertain_and_stops_429(conn, root, cards, server, session):
    server.submit = "429"
    result = run(conn, root, session)
    assert result.code == kp.TOO_MANY
    assert row(conn, cards[0])["problem_code"] == queue.UNCERTAIN


def test_429_before_submit_releases_card_and_stops(conn, root, cards, server, session):
    server.too_many = {"/api/get-brands"}
    result = run(conn, root, session)
    assert result.code == kp.TOO_MANY and server.submits == 0
    assert row(conn, cards[0])["state"] == queue.QUEUED


# --- квиток і кнопка (D6) ---


def test_submit_without_ticket_is_blocked_in_the_page(session, server):
    assert session.open_form().ok
    session.page.evaluate("fetch('/api/add-edit-used', {method: 'POST', body: '{}'}).catch(e => 0)")
    session.page.wait_for_timeout(300)
    assert server.submits == 0
    assert ("https://klubok.com/api/add-edit-used", "публікація без квитка") in session.blocked
    with pytest.raises(Forbidden):
        session.click(session.page.locator("#btn-submit"), "кнопка")
    with pytest.raises(Forbidden):
        session.click_submit()  # квитка немає


def test_second_post_with_the_same_ticket_is_blocked(conn, root, cards, server, session):
    run(conn, root, session, limit=1)
    session.ticket = ticket = form.klubok_guard.Ticket(cards[0])
    ticket.use()  # як після публікації
    session.page.evaluate("fetch('/api/add-edit-used', {method: 'POST', body: '{}'}).catch(e => 0)")
    session.page.wait_for_timeout(300)
    assert server.submits == 1


def test_dry_fill_session_cannot_get_a_ticket(chromium, server):  # noqa: F811
    s = fake.FakeSession(None, server.base, context=chromium.new_context())
    try:
        with pytest.raises(Forbidden):
            s.issue_ticket(1)
    finally:
        s.close()


def test_human_click_robot_never_presses(conn, root, cards, chromium, server, monkeypatch):  # noqa: F811
    s = open_session(chromium, server, human=True)
    monkeypatch.setattr(s, "click_submit", lambda: pytest.fail("робот не тисне в --human-click"))
    original = s.wait_submit

    def human_presses(limit, stop=lambda: False):
        s.page.evaluate("document.querySelector('#btn-submit').click()")  # «людина»
        return original(limit, stop)

    monkeypatch.setattr(s, "wait_submit", human_presses)
    try:
        result = run(conn, root, s, limit=1, human=True)
    finally:
        s.close()
    assert result.code == kp.DONE and row(conn, cards[0])["state"] == queue.PUBLISHED


def test_human_click_nobody_presses_is_not_sent(conn, root, cards, chromium, server):  # noqa: F811
    s = open_session(chromium, server, human=True)
    try:
        result = run(conn, root, s, limit=1, human=True, human_wait=1.0)
    finally:
        s.close()
    assert result.code == kp.NOT_SENT and row(conn, cards[0])["state"] == queue.QUEUED


def test_human_click_mode_forbids_robot_click(chromium, server):  # noqa: F811
    s = open_session(chromium, server, human=True)
    try:
        assert s.open_form().ok
        s.issue_ticket(1)
        with pytest.raises(Forbidden):
            s.click_submit()
    finally:
        s.close()


def test_foreign_post_blocked_recaptcha_allowed(conn, root, cards, chromium, server):  # noqa: F811
    s = open_session(chromium, server, scenario="evil")
    try:
        result = run(conn, root, s, limit=1)
    finally:
        s.close()
    assert result.code == kp.DONE and server.captcha_posts == 1
    assert ("https://evil.example/collect", "надсилання на чужий сайт") in s.blocked
    assert all("google" not in url for url, _ in s.unexpected_blocks())


# --- вхід, знімок, ліміти, сесія ---


def test_without_login_stops_before_any_card(conn, root, cards, chromium, server, said):  # noqa: F811
    s = open_session(chromium, server, scenario="signup")
    try:
        result = run(conn, root, s)
    finally:
        s.close()
    assert result.code == kp.LOGIN and "--klubok-login" in result.reason
    assert row(conn, cards[0]) is None and server.submits == 0
    assert said[-1][2]["loud"] is True


def test_other_account_is_not_logged_in(conn, root, cards, server, session):
    server.user = "1"
    assert run(conn, root, session).code == kp.LOGIN
    assert server.item_reads == []  # знімок не читали — вхід перевірено до нього
    assert row(conn, cards[0]) is None  # жодна Картка не стартувала


def test_snapshot_failure_stops_before_any_card(conn, root, cards, server, session):
    server.items_fault[0] = 500
    result = run(conn, root, session)
    assert result.code == snapshot.FAILED and server.submits == 0
    assert conn.execute("SELECT count(*) FROM klubok_listings").fetchone()[0] == 0


def test_limit_option_and_daily_limit(conn, root, cards, server, session, monkeypatch):
    assert run(conn, root, session, limit=1).code == kp.DONE and server.submits == 1
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")
    assert run(conn, root, session, limit=5).code == queue.LIMIT_REACHED
    assert server.submits == 1


def test_post_option_takes_only_that_post(conn, root, cards, server, session):
    result = run(conn, root, session, limit=5, post_id=7302)
    assert [p[0] for p in result.published] == [cards[1]]
    assert result.code == queue.EMPTY
    assert row(conn, cards[0])["state"] == queue.QUEUED


def test_form_problem_then_next_card(conn, root, cards, server, session):
    calls = []

    class Driver(form.KlubokDriver):
        def fill(self, ad):
            calls.append(ad.item_id)
            if len(calls) == 1:
                raise form.KlubokError("klubok_field_missing", "На формі немає поля: стан")
            super().fill(ad)

    result = run(conn, root, session, make_driver=Driver)

    assert row(conn, cards[0])["problem_code"] == "klubok_field_missing"
    assert row(conn, cards[1])["state"] == queue.PUBLISHED
    assert result.code == kp.DONE and len(result.problems) == 1


def test_ctrl_c_finishes_current_card_then_stops(conn, root, cards, server, session):
    published = []
    result = run(
        conn, root, session, stopping=lambda: bool(published.append(1) or len(published) > 2)
    )
    assert result.code == kp.STOPPED and len(result.published) == 1


def test_published_but_not_in_her_list_is_loud(conn, root, cards, server, session, said):
    server.hide_published = True
    result = run(conn, root, session, limit=1)
    assert result.code == kp.DONE
    assert row(conn, cards[0])["url"] is None  # посилання ще немає
    missing = [s for s in said if s[0] == "klubok.missing"]
    assert missing and missing[0][2]["loud"] is True


def test_crashed_klubok_session_closed_shafa_untouched(conn, root, cards, server, session):
    with conn:
        conn.execute("INSERT INTO sessions (started_at, site) VALUES ('x', 'klubok')")
        conn.execute("INSERT INTO sessions (started_at, site) VALUES ('x', 'shafa')")
    run(conn, root, session, limit=1)
    rows = conn.execute("SELECT site, stop_code, ended_at FROM sessions ORDER BY id").fetchall()
    assert tuple(rows[0])[:2] == ("klubok", "crash")
    assert rows[1]["ended_at"] is None  # сесію Шафи не чіпаємо


def test_uncertain_after_crash_is_recovered_at_start(conn, root, cards, server, session):
    queue.sync(conn, root, kp.datetime.now().astimezone())
    with conn:
        sid = conn.execute(
            "INSERT INTO sessions (started_at, site) VALUES ('x', 'klubok')"
        ).lastrowid
    queue.start(conn, cards[0], sid)
    result = run(conn, root, session, limit=1)
    assert result.recovered == [cards[0]]
    assert row(conn, cards[0])["problem_code"] == queue.UNCERTAIN
    assert row(conn, cards[1])["state"] == queue.PUBLISHED  # той самий пост не повторюється


def test_limit_range():
    with pytest.raises(ValueError):
        kp.Run(None, None, None, kp.Options(limit=0))
    with pytest.raises(ValueError):
        kp.Run(None, None, None, kp.Options(limit=31))
