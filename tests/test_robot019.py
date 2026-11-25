"""ROBOT-019: вхід на Шафу — рання перевірка, вікно входу на `/uk/login`, «Старт» не вгадує."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from shafa import __main__ as cli
from shafa import events, web
from shafa.robot import browser, control, login_check
from test_robot_publish import Env, add
from test_web import PORT, TOKEN, get, html, location, post
from test_web_robot import Spawner

NOW = datetime(2026, 11, 25, 12, 0).astimezone()
REAL_LAUNCH, REAL_LOGIN = browser.launch, browser.wait_for_manual_login  # до заглушок conftest


@pytest.fixture(autouse=True)
def new_cards_allowed(monkeypatch):
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


@pytest.fixture
def stub_chrome(monkeypatch):
    """Справжні `launch`/`wait_for_manual_login`, але з підставним Playwright (Chrome не відкрито)."""
    monkeypatch.setattr(browser, "launch", REAL_LAUNCH)
    monkeypatch.setattr(browser, "wait_for_manual_login", REAL_LOGIN)


# --- підставний Chrome ---


class Page:
    def __init__(self, on_goto=None):
        self.urls, self.on_goto = [], on_goto

    def goto(self, url, **kw):
        self.urls.append(url)
        if self.on_goto:
            self.on_goto()

    def on(self, *a):
        pass

    def wait_for_timeout(self, ms):
        pass


class Context:
    def __init__(self, page):
        self.pages, self.handlers, self.closed = [page], {}, False

    def on(self, name, handler):
        self.handlers[name] = handler

    def route(self, *a):
        pass

    def close(self):
        self.closed = True


class Playwright:
    def __init__(self, page=None):
        self.launched = {}
        self.page = page or Page()
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

    def launch(self, profile, **kw):
        self.launched = {"profile": profile, **kw}
        return Context(self.page)


class Stub:
    """Підставна сесія для `check`: що показала Шафа."""

    def __init__(self, state, closed=None):
        self.state, self.closed = state, closed

    def open_form(self):
        return browser.Opened(self.state, "https://shafa.ua/uk/new")

    def close(self):
        if self.closed is not None:
            self.closed.append(True)


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: out.append((kind, text, kw)))
    return out


def once(state, now=NOW, folder=None, said=None):
    emit = (lambda kind, text, **kw: said.append((kind, text, kw))) if said is not None else None
    return login_check.check_once(Stub(state), now, folder, **({"emit": emit} if emit else {}))


# --- 1. вікно входу відкриває сторінку входу ---


def test_login_window_opens_login_page_not_the_form(stub_chrome, tmp_path):
    flag = tmp_path / "готово"
    pw = Playwright(Page(on_goto=flag.touch))  # «людина» одразу натиснула «Я увійшов»
    assert browser.wait_for_manual_login(pw, flag=flag) == "done"
    assert browser.LOGIN_URL == "https://shafa.ua/uk/login"
    assert pw.page.urls == [browser.LOGIN_URL]
    assert pw.page.urls != [browser.NEW_URL]
    assert browser.LOGIN_PATH in browser.LOGIN_URL  # `wait_form` розпізнає її як сторінку входу


def test_klubok_login_window_keeps_its_own_url(stub_chrome, tmp_path):
    flag = tmp_path / "готово"
    pw = Playwright(Page(on_goto=flag.touch))
    browser.wait_for_manual_login(pw, flag=flag, profile=tmp_path / "p", url="https://k/signin")
    assert pw.page.urls == ["https://k/signin"]


# --- 2. перевірка входу без вікна ---


def test_check_runs_headless_but_window_for_work(stub_chrome):
    pw = Playwright()
    browser.Session(pw, headless=True)
    assert pw.launched["headless"] is True
    pw = Playwright()
    browser.Session(pw)
    assert pw.launched["headless"] is False  # публікація й dry-fill — видиме вікно, як раніше


def test_login_present_is_recorded_without_alert(tmp_path, said):
    assert once(browser.FORM, folder=tmp_path, said=said).ok is True
    assert login_check.store.read(tmp_path)[1] is True and said == []


def test_login_lost_is_recorded_and_owner_told_once_a_day(tmp_path, said):
    assert once(browser.NO_LOGIN, folder=tmp_path, said=said).ok is False
    assert login_check.store.read(tmp_path)[1] is False
    assert len(said) == 1 and said[0][0] == "shafa.login" and said[0][2]["loud"] is True
    assert events.KINDS["shafa.login"].loud
    once(browser.NO_LOGIN, NOW + timedelta(hours=3), tmp_path, said)
    assert len(said) == 1  # той самий день — тихо
    once(browser.NO_LOGIN, NOW + timedelta(days=1), tmp_path, said)
    assert len(said) == 2


def test_unclear_answer_records_nothing(tmp_path, said):
    outcome = once(browser.NO_FORM, folder=tmp_path, said=said)
    assert outcome.ok is None and login_check.store.read(tmp_path) is None and said == []


def test_cli_check_exit_codes_and_closes_browser(tmp_path, said):
    logs, closed = [], []
    for state, code in ((browser.FORM, 0), (browser.NO_LOGIN, 1), (browser.NO_FORM, 2)):
        got = cli._login_check(None, logs.append, lambda s=state: Stub(s, closed))
        assert got == code
    assert len(closed) == 3 and logs[0] == "Шафа: вхід є" and "злетів" in logs[1]


def test_cli_check_chrome_failure_is_not_a_lost_login(tmp_path, said):
    def boom():
        raise RuntimeError("Chrome не знайдено")

    logs = []
    assert cli._login_check(None, logs.append, boom) == 2
    assert login_check.store.read() is None and said == []


def test_login_window_rechecks_right_after_closing(monkeypatch, said):
    """Після закриття вікна входу перевірка йде одразу (панель зеленіє сама)."""
    from playwright import sync_api

    class PW:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    ran = []
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: PW())
    monkeypatch.setattr(browser, "wait_for_manual_login", lambda p, **kw: "closed")
    monkeypatch.setattr(cli, "_login_check", lambda p, log: ran.append("check") or 0)
    args = cli.build_parser().parse_args(["robot", "--login"])
    assert cli._with_browser(args, None, None, 0, 0) == 0
    assert ran == ["check"] and control._login_at() is not None


# --- 3. Watch ---


def watch(busy=False, folder=None):
    spawned, logs, clock = [], [], [NOW]
    w = login_check.Watch(
        command=lambda: ["--login-check"], spawn=spawned.append, busy=lambda: busy,
        log=logs.append, clock=lambda: clock[0], folder=folder,
    )  # fmt: skip
    return w, spawned, logs, clock


def test_watch_checks_at_start_then_once_a_day(tmp_path):
    w, spawned, _logs, clock = watch(folder=tmp_path)
    assert w.tick() == "started" and spawned == [["--login-check"]]
    login_check.store.write(True, NOW, folder=tmp_path)  # перевірка записала результат
    clock[0] = NOW + timedelta(hours=12)
    assert w.tick() is None
    clock[0] = NOW + timedelta(hours=25)
    assert w.tick() == "started" and len(spawned) == 2


def test_watch_waits_while_robot_is_busy(tmp_path):
    w, spawned, logs, _clock = watch(busy=True, folder=tmp_path)
    assert w.tick() == "busy" and spawned == []
    w.tick()
    assert len(logs) == 1  # у журнал — один раз на годину


def test_watch_retry_not_more_often_than_ten_minutes(tmp_path):
    w, spawned, _logs, clock = watch(folder=tmp_path)
    w.tick()
    clock[0] = NOW + timedelta(minutes=5)
    assert w.tick() is None
    clock[0] = NOW + timedelta(days=2)
    assert w.tick() == "started" and len(spawned) == 2


# --- 4. панель ---


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


def state(conn, lock_file):
    return control.state(conn, lock_file, day_limit=30, now=datetime.now().astimezone())


def approved(conn, root):
    add(Env(conn, root, root.parent), "1342", 1342)


def test_lost_login_makes_panel_ask_to_log_in(conn, root, lock_file):
    approved(conn, root)
    assert state(conn, lock_file).can_start
    login_check.store.write(False, datetime.now().astimezone(), "x")
    s = state(conn, lock_file)
    assert s.code == "login_needed" and not s.can_start and "login" in s.buttons
    assert "спершу увійдіть на шафу" in s.why_not.lower() and s.why_code == "login"


def test_window_closed_after_failed_check_lifts_it_until_next_check(conn, root, lock_file):
    approved(conn, root)
    at = datetime.now().astimezone()
    login_check.store.write(False, at, "x")
    control.mark_login(at + timedelta(minutes=1))
    assert state(conn, lock_file).code == "stopped"


def test_fresh_ok_check_overrides_old_login_stop(conn, root, lock_file):
    from test_ux002 import ended

    approved(conn, root)
    ended(conn, "2026-10-13T11:00:00+00:00", "2026-10-13T11:30:00+00:00", stop_code="login",
          stop_reason="x")  # fmt: skip
    assert state(conn, lock_file).code == "login_needed"
    login_check.store.write(True, datetime.now().astimezone())  # новіша перевірка: вхід є
    assert state(conn, lock_file).code == "stopped"


def test_green_note_after_login_window_and_ok_check(conn, root, lock_file):
    approved(conn, root)
    assert state(conn, lock_file).login_ok == ""
    at = datetime.now().astimezone()
    control.mark_login(at)
    login_check.store.write(True, at + timedelta(seconds=20))
    s = state(conn, lock_file)
    assert s.can_start and s.login_ok == "Вхід є — можна «Старт»."
    assert control.login_ok_note(at + timedelta(hours=3)) == ""  # згодом — не набридає


# --- 5. «Старт» ---


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    spawner = Spawner()
    calls = []

    def probe():
        calls.append(1)
        probe.effect()

    probe.effect = lambda: None
    application = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=lock_file, login_probe=probe,
    )  # fmt: skip
    application.spawner_calls, application.probe_calls, application.probe = (
        spawner.calls,
        calls,
        probe,
    )
    approved(conn, root)
    return application


def now():
    return datetime.now().astimezone()


def test_start_with_login_ok_goes(app):
    app.probe.effect = lambda: login_check.store.write(True, now())
    page = post(app, "/robot", action="start", back="/review")
    assert "robot_started" in location(page) and len(app.spawner_calls) == 1
    assert app.probe_calls == [1]  # свіжої перевірки не було — попросили


def test_start_with_fresh_ok_check_does_not_probe_again(app):
    login_check.store.write(True, now())
    post(app, "/robot", action="start", back="/review")
    assert app.probe_calls == [] and len(app.spawner_calls) == 1


def test_start_with_lost_login_does_not_begin_session(app):
    app.probe.effect = lambda: login_check.store.write(False, now(), "x")
    page = post(app, "/robot", action="start", back="/review")
    assert "robot_cannot_start_login" in location(page)
    assert app.spawner_calls == []  # робота не запущено
    shown = html(get(app, "/review?msg=robot_cannot_start_login"))
    assert "спершу увійдіть на Шафу" in shown
    panel = html(get(app, "/robot/panel"))
    assert "Шафа вийшла з вашого акаунта" in panel and "Увійти на Шафу" in panel


def test_start_when_shafa_did_not_answer_still_goes(app):
    page = post(app, "/robot", action="start", back="/review")  # перевірка нічого не записала
    assert "robot_started" in location(page) and len(app.spawner_calls) == 1


def test_start_probe_crash_does_not_block(app):
    def boom():
        raise OSError("немає Chrome")

    app.login_probe = boom
    page = post(app, "/robot", action="start", back="/review")
    assert "robot_started" in location(page)


def test_panel_green_after_login(app):
    at = now()
    control.mark_login(at)
    login_check.store.write(True, at + timedelta(seconds=5))
    assert "Вхід є — можна «Старт»" in html(get(app, "/robot/panel"))
