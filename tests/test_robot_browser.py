"""FIX-002: форма «Додати товар» на повільному ноутбуці і вікно ручного входу."""

import itertools

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from shafa.robot import browser

LOGIN_URL = "https://shafa.ua/uk/login?next=/uk/new"
WAIT_FOR_LOGIN = browser.wait_for_manual_login  # справжня; conftest підміняє її на заборону


class Count:
    def __init__(self, n, error=None):
        self.n, self.error = n, error

    def count(self):
        if self.error:
            raise self.error
        return self.n


class SlowPage:
    """Час іде лише в `wait_for_timeout` — як події в синхронному Playwright."""

    def __init__(self, form_at=None, login_at=None, busy_until=0.0, goto_error=None):
        self.t = 0.0
        self.form_at, self.login_at, self.busy_until = form_at, login_at, busy_until
        self.goto_error = goto_error
        self.gotos = []

    @property
    def url(self):
        if self.login_at is not None and self.t >= self.login_at:
            return LOGIN_URL
        return browser.NEW_URL

    def locator(self, selector):
        assert selector == browser.FORM_FIELD
        if self.t < self.busy_until:  # сторінка ще переходить
            return Count(0, RuntimeError("Execution context was destroyed"))
        return Count(int(self.form_at is not None and self.t >= self.form_at))

    def wait_for_timeout(self, ms):
        self.t += ms / 1000

    def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error


def wait(page):
    return browser.wait_form(page, clock=lambda: page.t)


def test_slow_form_on_10th_second_is_form_not_login():
    page = SlowPage(form_at=10.0)
    opened = wait(page)
    assert (opened.state, opened.ok) == (browser.FORM, True)
    assert 10.0 <= opened.waited < 11.0


def test_login_page_is_no_login():
    opened = wait(SlowPage(login_at=2.0))
    assert (opened.state, opened.ok) == (browser.NO_LOGIN, False)
    assert "?" not in opened.detail() and "/uk/login" in opened.detail()  # без параметрів


def test_neither_form_nor_login_is_no_form_after_30_seconds():
    opened = wait(SlowPage())
    assert opened.state == browser.NO_FORM
    assert opened.waited >= browser.FORM_WAIT
    assert opened.detail() == f"адреса {browser.NEW_URL}, чекали 30 с"


def test_page_still_navigating_is_not_an_error():
    assert wait(SlowPage(form_at=0.0, busy_until=3.0)).state == browser.FORM


def bare_session(page):
    session = browser.Session.__new__(browser.Session)
    session.page = page
    return session


def test_open_form_waits_through_slow_load(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    page = SlowPage(form_at=12.0, goto_error=PlaywrightTimeout("load"))
    opened = bare_session(page).open_form()
    assert opened.ok
    assert page.gotos == [(browser.NEW_URL, {"wait_until": "domcontentloaded", "timeout": 30000})]


# --- Вікно входу ---


class LoginContext:
    """Вікно закривають на `close_at`-й секунді; Playwright бачить це лише під час своїх викликів."""

    def __init__(self, close_at=None, disconnect_at=None, raise_once=False):
        self.t = 0.0
        self.close_at, self.disconnect_at, self.raise_once = close_at, disconnect_at, raise_once
        self.pages = [self]
        self.handlers = []
        self.closed_by_us = 0

    # сторінка — той самий об'єкт
    def goto(self, url):
        pass

    def wait_for_timeout(self, ms):
        self.t += ms / 1000
        if self.close_at is not None and self.t >= self.close_at:
            self.pages = []
        if self.disconnect_at is not None and self.t >= self.disconnect_at:
            for handler in self.handlers:
                handler(self)
            self.disconnect_at = None
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("Target page, context or browser has been closed")

    def on(self, event, handler):
        assert event == "close"
        self.handlers.append(handler)

    def new_page(self):
        return self

    def close(self):
        self.closed_by_us += 1


def ticking():
    counter = itertools.count()
    return lambda: next(counter) * 0.5


def test_closed_window_ends_login_mode(tmp_path):
    context = LoginContext(close_at=3.0)
    why = browser.wait_window(context, [], tmp_path / "flag", 60.0, clock=ticking())
    assert why == "closed"


def test_browser_disconnect_ends_login_mode(tmp_path):
    context, closed = LoginContext(disconnect_at=2.0), []
    context.on("close", lambda _c: closed.append(True))
    assert browser.wait_window(context, closed, tmp_path / "flag", 60.0, ticking()) == "closed"


def test_error_while_waiting_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(browser.time, "sleep", lambda s: None)
    context = LoginContext(close_at=1.0, raise_once=True)
    assert browser.wait_window(context, [], tmp_path / "flag", 60.0, ticking()) == "closed"


def test_done_button_and_time_limit(tmp_path):
    flag = tmp_path / "flag"
    flag.touch()
    assert browser.wait_window(LoginContext(), [], flag, 60.0, ticking()) == "done"
    flag.unlink()
    assert browser.wait_window(LoginContext(), [], flag, 60.0, ticking()) == "timeout"


@pytest.mark.parametrize(
    ("context", "flag_after", "why", "closed_by_us"),
    [
        (LoginContext(close_at=2.0), None, "closed", 1),  # Mac: вікно закрите, Chrome — ні
        (LoginContext(disconnect_at=2.0), None, "closed", 0),  # Windows: хрестик закрив Chrome
        (LoginContext(), 1.0, "done", 1),
        (LoginContext(), None, "timeout", 1),
    ],
)
def test_manual_login_closes_browser_properly(
    tmp_path, monkeypatch, context, flag_after, why, closed_by_us
):
    flag = tmp_path / "login-done"
    flag.touch()  # залишок минулого разу — не рахується
    monkeypatch.setattr(browser, "launch", lambda p: context)
    if flag_after is not None:
        original = context.wait_for_timeout

        def press(ms):
            original(ms)
            if context.t >= flag_after:
                flag.touch()

        context.wait_for_timeout = press
    result = WAIT_FOR_LOGIN(None, limit=30.0, flag=flag, clock=lambda: context.t)
    assert result == why
    assert context.closed_by_us == closed_by_us  # штатне закриття — cookies входу записані
    assert not flag.exists()


def test_launch_keeps_minimized_window_fast(tmp_path):
    """CORE-007: згорнуте вікно Chrome не гальмує — аргументи передаються в запуск."""
    calls = {}

    class Chromium:
        def launch_persistent_context(self, profile, **options):
            calls.update(options, profile=profile)
            return "context"

    assert browser.open_profile(Chromium(), tmp_path / "profile") == "context"
    for arg in (
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ):
        assert arg in calls["args"]
    assert calls["headless"] is False and calls["channel"] == "chrome"
