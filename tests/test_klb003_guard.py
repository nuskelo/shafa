"""KLB-003: guard публікації (квиток, reCAPTCHA, кнопка) і вхід у Клубок — чисті функції."""

import pytest

from shafa.robot import browser
from shafa.robot import klubok_form as form
from shafa.robot import klubok_guard as guard
from test_robot_browser import WAIT_FOR_LOGIN, LoginContext

SUBMIT = "https://klubok.com/api/add-edit-used"


def verdict(method, url, *, publishing=True, ticket=None, content_type=""):
    return guard.request_verdict(
        method, url, content_type, True, publishing=publishing, ticket=ticket
    )


def test_submit_without_ticket_is_blocked():
    assert verdict("POST", SUBMIT) == (False, "публікація без квитка")


def test_ticket_lets_exactly_one_submit():
    ticket = guard.Ticket(1)
    assert verdict("POST", SUBMIT, ticket=ticket) == (True, guard.SUBMITTED)
    assert not ticket.alive
    allowed, why = verdict("POST", SUBMIT, ticket=ticket)
    assert not allowed and "уже використано" in why


def test_dry_fill_never_submits_even_with_a_ticket():
    ticket = guard.Ticket(1)
    assert not verdict("POST", SUBMIT, publishing=False, ticket=ticket)[0]
    assert ticket.alive  # квиток навіть не торкнуто


@pytest.mark.parametrize(
    "url",
    [
        "http://klubok.com/api/add-edit-used",  # не https
        "https://m.klubok.com/api/add-edit-used",  # інший хост
        "https://klubok.com/api/add-edit-used/x",
        "https://klubok.com/api/add-edit-user",
    ],
)
def test_ticket_is_for_exact_host_and_path(url):
    ticket = guard.Ticket(1)
    assert not verdict("POST", url, ticket=ticket)[0]
    assert ticket.alive


def test_recaptcha_post_only_while_publishing():
    url = "https://www.google.com/recaptcha/api2/reload?k=x"
    assert verdict("POST", url) == (True, "reCAPTCHA")
    assert not verdict("POST", url, publishing=False)[0]
    for other in (
        "https://www.google.com/search",
        "https://evil.example/recaptcha/api2/reload",
        "https://www.gstatic.com/recaptcha/x",
    ):
        assert not verdict("POST", other)[0], other
    assert verdict("GET", "https://www.gstatic.com/recaptcha/releases/x/recaptcha__uk.js")[0]


@pytest.mark.parametrize(
    "url", ["https://klubok.com/payment/37953234?action=used-submit-ok-logged",
            "https://klubok.com/error429?action=Add_new_item&score=0.1"],
)  # fmt: skip
def test_pages_after_submit_stay_blocked_even_with_a_ticket(url):
    assert not verdict("GET", url, ticket=guard.Ticket(1))[0]


def test_submit_click_only_btn_submit_with_live_ticket_and_not_human():
    ticket = guard.Ticket(1)
    assert guard.submit_click_allowed("btn-submit", ticket, human=False)
    assert not guard.submit_click_allowed("btn-submit", None, human=False)
    assert not guard.submit_click_allowed("btn-submit", ticket, human=True)
    assert not guard.submit_click_allowed("other", ticket, human=False)
    ticket.use()
    assert not guard.submit_click_allowed("btn-submit", ticket, human=False)


def test_ordinary_click_still_never_presses_publish():
    assert not guard.click_allowed("Опубликовать", "submit")
    assert not guard.click_allowed("Опубликовать", "button")


def test_klubok_login_opens_its_own_profile_and_form(tmp_path, monkeypatch):
    context = LoginContext(close_at=1.0)
    opened = {}

    def open_profile(chromium, profile):
        opened["profile"] = profile
        return context

    def goto(url):
        opened["url"] = url

    context.goto = goto
    monkeypatch.setattr(browser, "open_profile", open_profile)
    monkeypatch.setattr(browser, "launch", lambda p: pytest.fail("профіль Шафи"))

    class Playwright:
        chromium = None

    why = WAIT_FOR_LOGIN(
        Playwright(), limit=30.0, flag=tmp_path / "flag", clock=lambda: context.t,
        profile=tmp_path / "klubok-profile", url=form.FORM_URL,
    )  # fmt: skip

    assert why == "closed"
    assert opened == {"profile": tmp_path / "klubok-profile", "url": "https://klubok.com/item/add"}
    assert (tmp_path / "klubok-profile").is_dir()


def test_shafa_login_unchanged(tmp_path, monkeypatch):
    context = LoginContext(close_at=1.0)
    urls = []
    context.goto = urls.append
    monkeypatch.setattr(browser, "launch", lambda p: context)
    WAIT_FOR_LOGIN(None, limit=30.0, flag=tmp_path / "flag", clock=lambda: context.t)
    assert urls == [browser.NEW_URL]
