"""KLB-018: вікно входу в Клубок — `/signin`; рання перевірка входу; автостарт не «згорає»."""

import json
from datetime import datetime, timedelta

import pytest

from shafa import __main__ as cli
from shafa import events
from shafa.klubok import auto, login, queue
from shafa.robot import browser, klubok_control, klubok_login_check
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_form as form
from shafa.robot import klubok_publish as kp
from test_klb001_page import chromium  # noqa: F401  (фікстура браузера на модуль)
from test_klb014 import NOW, ready, robot, session  # noqa: F401


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")
    monkeypatch.delenv(auto.AUTO_SETTING, raising=False)
    monkeypatch.delenv(auto.AT_SETTING, raising=False)
    monkeypatch.setattr(form, "FIELD_WAIT", 1.0)


# --- 1. вікно входу ---


def test_login_window_opens_signin_not_the_form(monkeypatch, capsys):
    from playwright import sync_api

    opened = {}

    class Playwright:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: Playwright())
    monkeypatch.setattr(
        browser, "wait_for_manual_login", lambda p, **kw: opened.update(kw) or "closed"
    )
    args = cli.build_parser().parse_args(["robot", "--klubok-login"])
    assert cli._klubok_with_browser(args, None) == 0
    assert opened["url"] == form.LOGIN_URL == "https://klubok.com/signin"
    assert opened["url"] != form.FORM_URL
    assert "/signin" in form.LOGIN_PATHS  # guard не вважає перехід на форму входу «несподіваним»


# --- 2. перевірка входу ---


def open_session(chromium, server, scenario="ok"):  # noqa: F811
    return fake.FakeSession(None, server.base, scenario, context=chromium.new_context())


@pytest.fixture
def server():
    srv = fake.Server(delay=(0.01, 0.05), seed=1)
    yield srv
    srv.close()


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: out.append((kind, text, kw)))
    return out


def run_check(chromium, server, tmp_path, scenario="ok", now=NOW, emit=None):  # noqa: F811
    s = open_session(chromium, server, scenario)
    try:
        return klubok_login_check.check_once(s, now, tmp_path, **({"emit": emit} if emit else {}))
    finally:
        s.close()


def test_login_present(chromium, server, tmp_path, said):  # noqa: F811
    outcome = run_check(chromium, server, tmp_path, emit=lambda *a, **k: said.append(a))
    assert outcome.ok is True and said == []
    assert login.read(tmp_path)[1] is True
    assert server.submits == 0  # лише читання


def test_login_lost_is_recorded_and_owner_told_once_a_day(chromium, server, tmp_path, said):  # noqa: F811
    tell = lambda kind, text, **kw: said.append((kind, text, kw))
    outcome = run_check(chromium, server, tmp_path, "signup", emit=tell)
    assert outcome.ok is False
    assert login.read(tmp_path)[1] is False
    assert len(said) == 1 and said[0][0] == "klubok.login" and said[0][2]["loud"] is True
    # друга перевірка того ж дня — запис оновився, нового повідомлення немає
    run_check(chromium, server, tmp_path, "signup", NOW + timedelta(hours=2), tell)
    assert len(said) == 1
    run_check(chromium, server, tmp_path, "signup", NOW + timedelta(days=1), tell)  # новий день
    assert len(said) == 2


def test_other_account_counts_as_lost(chromium, server, tmp_path, said):  # noqa: F811
    server.user = "1"
    assert run_check(chromium, server, tmp_path, emit=lambda *a, **k: said.append(a)).ok is False


def test_event_kind_is_loud():
    assert events.KINDS["klubok.login"].loud


def test_state_login_needed_after_failed_check_until_login(conn, root, tmp_path):
    now = datetime.now().astimezone()
    lock = tmp_path / "robot.lock"
    assert klubok_control.state(conn, lock, now).code != "login_needed"
    login.write(False, now - timedelta(minutes=5), "x")
    state = klubok_control.state(conn, lock, now)
    assert state.code == "login_needed" and not state.can_start
    klubok_control.mark_login(now)  # вікно входу закрито після перевірки
    assert klubok_control.state(conn, lock, now).code != "login_needed"
    login.write(False, now + timedelta(minutes=1), "x")  # нова перевірка — знову злетів
    assert klubok_control.state(conn, lock, now).code == "login_needed"
    login.write(True, now + timedelta(minutes=2))
    assert klubok_control.state(conn, lock, now).code != "login_needed"


# --- 2. автостарт і перевірка ---


def with_check(a):
    checks = []
    a.check = lambda: checks.append(1)
    return checks


def test_login_ok_check_lets_autostart_go(conn, robot):  # noqa: F811
    a, spawned, _lines, _busy, _clock = robot
    checks = with_check(a)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    login.write(True, NOW - timedelta(minutes=2), folder=a.folder)
    assert a.tick() == "started" and len(spawned) == 1 and checks == []


def test_lost_login_blocks_autostart(conn, robot):  # noqa: F811
    a, spawned, lines, _busy, _clock = robot
    checks = with_check(a)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    login.write(False, NOW - timedelta(minutes=2), folder=a.folder)
    login.write(False, NOW - timedelta(minutes=2))  # `state` Клубку читає теку даних, не a.folder
    assert a.tick() is None and spawned == [] and checks == []
    assert "Спершу увійдіть" in lines[-1] or "вхід злетів" in lines[-1]


def test_no_fresh_check_asks_for_one_then_waits_for_answer(conn, robot):  # noqa: F811
    a, spawned, _lines, _busy, clock = robot
    checks = with_check(a)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "checking" and checks == [1] and spawned == []
    clock["now"] += timedelta(minutes=1)
    assert a.tick() == "checking" and checks == [1] and spawned == []  # вдруге не просимо
    login.write(True, clock["now"], folder=a.folder)  # відповідь прийшла
    assert a.tick() == "started" and len(spawned) == 1


def test_check_answer_lost_login_means_no_start(conn, robot):  # noqa: F811
    a, spawned, lines, _busy, clock = robot
    with_check(a)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "checking"
    clock["now"] += timedelta(minutes=1)
    login.write(False, clock["now"], folder=a.folder)
    assert a._login(clock["now"], "d") == "login" and spawned == []
    assert "вхід злетів" in lines[-1]


def test_silent_check_does_not_hold_autostart_forever(conn, robot):  # noqa: F811
    a, spawned, _lines, _busy, clock = robot
    checks = with_check(a)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "checking"
    clock["now"] += timedelta(minutes=2)
    assert a.tick() == "checking"
    clock["now"] += timedelta(minutes=2)  # ≥ 3 хв мовчання — сесія перевірить вхід сама
    assert a.tick() == "started" and len(spawned) == 1 and checks == [1]


def test_login_after_check_makes_it_stale(conn, robot):  # noqa: F811
    a, _spawned, _lines, _busy, _clock = robot
    checks = with_check(a)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    login.write(False, NOW - timedelta(minutes=3), folder=a.folder)
    klubok_control.mark_login(NOW - timedelta(minutes=1))  # Власниця увійшла після перевірки
    assert a.tick() == "checking" and checks == [1]  # стара «злетів» не рахується — перевіряємо


def test_login_stop_does_not_block_after_new_login(conn, robot):  # noqa: F811
    a, spawned, lines, *_ = robot
    session(conn, "shafa", NOW - timedelta(hours=1))
    session(conn, "klubok", NOW - timedelta(minutes=20), code=kp.LOGIN)
    assert a.tick() is None and spawned == [] and "не буде" in lines[-1]
    klubok_control.mark_login(NOW - timedelta(minutes=5))
    assert a.tick() == "started" and len(spawned) == 1


# --- 3. автостарт не «згорає» ---


def started(conn, robot_fixture):
    a, spawned, lines, busy, clock = robot_fixture
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "started"
    return a, spawned, lines, busy, clock


def test_process_without_session_frees_the_day(conn, robot):  # noqa: F811
    a, spawned, lines, _busy, clock = started(conn, robot)
    clock["now"] += timedelta(minutes=2)
    assert a.tick() is None and len(spawned) == 1  # ще рано вважати процес мертвим
    clock["now"] += timedelta(minutes=2)  # 4 хв: сесії немає, робот вільний
    assert a.tick() is None and "повтор не раніше" in lines[-1]  # позначку знято, але ще < 10 хв
    assert (
        auto.read_mark(a.folder)["dead"] is True
        and auto.marked_today(clock["now"], a.folder) is None
    )
    clock["now"] += timedelta(minutes=7)  # 11 хв від запуску
    assert a.tick() == "started" and len(spawned) == 2
    assert auto.read_mark(a.folder)["tries"] == 2


def test_busy_robot_keeps_the_mark(conn, robot):  # noqa: F811
    a, spawned, _lines, busy, clock = started(conn, robot)
    busy.value = {"mode": "publish"}  # процес ще працює (відкриває форму, знімок…)
    clock["now"] += timedelta(minutes=30)
    assert a.tick() is None and len(spawned) == 1
    assert not auto.read_mark(a.folder).get("dead")


def test_session_started_closes_the_day(conn, robot):  # noqa: F811
    a, spawned, _lines, _busy, clock = started(conn, robot)
    session(conn, "klubok", NOW + timedelta(minutes=2), started=NOW + timedelta(seconds=40))
    clock["now"] += timedelta(hours=2)
    assert a.tick() is None and len(spawned) == 1
    assert auto.read_mark(a.folder)["session"] is not None


def test_retries_are_capped_per_day(conn, robot):  # noqa: F811
    a, spawned, lines, _busy, clock = started(conn, robot)
    for _ in range(auto.MAX_TRIES * 3):
        clock["now"] += timedelta(minutes=11)
        a.tick()
    assert len(spawned) == auto.MAX_TRIES and "кнопкою «Старт»" in lines[-1]


# --- запуск перевірки в програмі ---


def make_watch(tmp_path, now):
    spawned, lines, state = [], [], {"now": now, "busy": None}
    w = klubok_login_check.Watch(
        command=lambda: ["--klubok-login-check"], spawn=spawned.append,
        busy=lambda: state["busy"], log=lines.append,
        clock=lambda: state["now"], folder=tmp_path,
    )  # fmt: skip
    return w, spawned, lines, state


def test_watch_checks_at_app_start_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_KLUBOK_RENEW_AT", "off")
    w, spawned, _lines, state = make_watch(tmp_path, NOW.replace(hour=9))
    assert w.tick() == "started" and spawned == [["--klubok-login-check"]]
    state["now"] += timedelta(hours=2)
    assert w.tick() is None and len(spawned) == 1


def test_watch_waits_while_robot_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_KLUBOK_RENEW_AT", "off")
    w, spawned, _lines, state = make_watch(tmp_path, NOW.replace(hour=9))
    state["busy"] = {"mode": "publish"}
    assert w.tick() == "busy" and spawned == []
    state["busy"] = None
    state["now"] += timedelta(minutes=1)
    assert w.tick() == "started"


def test_watch_daily_before_renew(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_KLUBOK_RENEW_AT", "20:00")
    w, spawned, _lines, state = make_watch(tmp_path, NOW.replace(hour=9))
    w.tick()
    spawned.clear()
    login.write(True, state["now"], folder=tmp_path)  # результат ранкової перевірки
    state["now"] = NOW.replace(hour=18, minute=59)
    assert w.tick() is None
    state["now"] = NOW.replace(hour=19, minute=0)  # за годину до «Обновить все»
    assert w.tick() == "started" and len(spawned) == 1
    login.write(True, state["now"], folder=tmp_path)
    state["now"] = NOW.replace(hour=19, minute=30)
    assert w.tick() is None and len(spawned) == 1


def test_watch_check_is_19_00_whatever_the_renew_setting(tmp_path, monkeypatch):
    """KLB-019: `SHAFA_KLUBOK_RENEW_AT` — не час; перевірка входу щодня о 19:00, `off` — немає."""
    monkeypatch.setenv("SHAFA_KLUBOK_RENEW_AT", "23:30")
    w, spawned, _lines, state = make_watch(tmp_path, NOW.replace(hour=9))
    w.tick()
    spawned.clear()
    login.write(True, state["now"], folder=tmp_path)
    state["now"] = NOW.replace(hour=19, minute=0)
    assert w.tick() == "started"
    monkeypatch.setenv("SHAFA_KLUBOK_RENEW_AT", "off")
    state["now"] = NOW.replace(day=NOW.day + 1, hour=19, minute=5)
    assert w.tick() is None and len(spawned) == 1


def test_watch_silent_when_klubok_off(tmp_path, monkeypatch):
    monkeypatch.delenv(queue.FLAG)
    w, spawned, *_ = make_watch(tmp_path, NOW)
    assert w.tick() is None and spawned == []


def test_state_files_are_json(tmp_path):
    login.write(False, NOW, "причина", tmp_path)
    assert json.loads((tmp_path / login.STATE).read_text(encoding="utf-8"))["ok"] is False
