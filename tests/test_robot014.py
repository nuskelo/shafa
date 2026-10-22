"""ROBOT-014: вікно «У вас від'ємний баланс» поверх форми — закрити хрестиком, гроші не чіпати."""

import json
from datetime import date

import pytest

from shafa import config, events
from shafa.db import Status
from shafa.robot import balance, guard, rehearse
from shafa.robot.balance import BalanceStop, Seen
from shafa.robot.form import FieldMissing
from test_qa003 import RSession, cards, go, script, states  # noqa: F401 — autouse-фікстури
from test_qa003 import env as rehearse_env  # noqa: F401
from test_robot003_page import chromium  # noqa: F401 — фікстура браузера
from test_robot_form import FakeDriver, FakePage, bare_session
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    Env,
    PubDriver,
    add,
    new_cards_allowed,
    quiet,
    row,
    state,
)

AMOUNT = " (−193.15 грн)"
CARD_NUMBER = "545708******2827"

# Вікно за скриншотом 22.10: заголовок, текст, «Підтвердити», хрестик без тексту; унизу червона
# смуга з «Поповнити» (формі не заважає).
PAGE = """<!doctype html><meta charset="utf-8"><body>
<form><label>Виберіть розділ</label><input name="price"></form>
<div id="strip" style="position:fixed;bottom:0;left:0;right:0;height:40px">
  У вас від'ємний баланс −193.15 грн :( <button id="topup" type="button">Поповнити</button></div>
<div id="modal" class="modal" role="dialog" style="position:fixed;top:100px;left:300px;width:400px;
  height:300px;background:#fff">
  <button id="x" class="close" type="button" style="position:absolute;top:8px;right:8px">
    <svg width="12" height="12"></svg></button>
  <h2>У вас від'ємний баланс</h2>
  <p>Підтвердіть списання 194.15 грн з картки, щоб уникнути деактивації.</p>
  <p>Картка, з якої буде списана сума: 545708******2827</p>
  <button id="ok" type="button" style="position:absolute;bottom:8px;right:8px">Підтвердити</button>
</div>
<script>
window.clicks = [];
for (const id of ['ok', 'topup', 'x'])
  document.getElementById(id).addEventListener('click', () => window.clicks.push(id));
if (!window.location.hash.includes('stuck'))
  document.getElementById('x').addEventListener('click',
    () => { document.getElementById('modal').style.display = 'none'; });
if (window.location.hash.includes('esc'))
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.getElementById('modal').style.display = 'none'; });
</script></body>"""

STRIP_ONLY = """<!doctype html><meta charset="utf-8"><body><form><input name="price"></form>
<div style="position:fixed;bottom:0">У вас від'ємний баланс −193.15 грн :(
<button onclick="window.topup = 1">Поповнити</button></div></body>"""


def page_session(chromium, html):  # noqa: F811
    page = chromium.new_page()
    page.set_content(html)
    session = bare_session()
    session.page = page
    return session, page


# --- Що клікати можна ---


@pytest.mark.parametrize("label", ["", "×", "Закрити", " ", "✕", "close"])
def test_close_labels_are_allowed(label):
    assert balance.safe_close(label)


@pytest.mark.parametrize(
    "label",
    ["Підтвердити", "Поповнити", "Підтвердити списання", "Оплатити", "Погасити борг", None, "Далі"],
)
def test_money_buttons_are_never_a_close(label):
    assert not balance.safe_close(label)


def test_guard_stays_as_is_so_balance_module_keeps_its_own_barrier():
    """`guard.py` слів «Підтвердити»/«Поповнити» не знає (його не міняємо) — тому бар'єр тут."""
    assert guard.click_allowed("Підтвердити")
    assert not balance.safe_close("Підтвердити")


# --- Справжній Chromium ---


def test_modal_is_closed_by_cross_and_money_buttons_untouched(chromium):  # noqa: F811
    session, page = page_session(chromium, PAGE)

    seen = balance.dismiss(session)

    assert seen == Seen(AMOUNT, closed=True)  # сума — з червоної смуги; номер картки не читано
    assert page.evaluate("window.clicks") == ["x"]  # «Підтвердити» і «Поповнити» — ні
    assert CARD_NUMBER not in repr(seen)
    page.close()


def test_escape_is_second_try_when_cross_does_nothing(chromium):  # noqa: F811
    session, page = page_session(chromium, PAGE.replace("window.location.hash", "'stuck esc'"))

    seen = balance.dismiss(session)

    assert seen == Seen(AMOUNT, closed=True)
    assert page.evaluate("window.clicks") == ["x"]
    page.close()


def test_modal_that_will_not_close_is_reported_not_forced(chromium):  # noqa: F811
    session, page = page_session(chromium, PAGE.replace("window.location.hash", "'stuck'"))

    seen = balance.dismiss(session)

    assert seen == Seen(AMOUNT, closed=False)
    assert page.evaluate("window.clicks") == ["x"]  # лише хрестик (і Escape), ніколи «Підтвердити»
    page.close()


def test_red_strip_alone_is_not_the_modal(chromium):  # noqa: F811
    session, page = page_session(chromium, STRIP_ONLY)

    assert balance.dismiss(session) is None
    assert page.evaluate("window.topup") is None
    page.close()


def test_no_close_button_means_no_click_at_all(chromium):  # noqa: F811
    html = PAGE.replace('<button id="x"', '<button id="y" style="display:none"').replace(
        "'ok', 'topup', 'x'", "'ok', 'topup', 'y'"
    )
    html = html.replace("getElementById('x')", "getElementById('y')")
    session, page = page_session(chromium, html)

    seen = balance.dismiss(session)

    assert seen is not None and not seen.closed
    assert page.evaluate("window.clicks") == []
    page.close()


# --- Сторінка підробна: що клікнули ---


class ScriptPage(FakePage):
    def __init__(self, found):
        super().__init__()
        self.found = found

    def evaluate(self, script, *args):
        return self.found.pop(0) if self.found else None

    def locator(self, selector):
        raise AssertionError(f"клік не очікувався: {selector}")

    def wait_for_timeout(self, ms):
        pass


def test_money_button_picked_as_close_is_not_clicked_escape_only():
    session = bare_session()
    session.page = ScriptPage(
        [{"balance": "193.15", "charge": None, "close": "Підтвердити"}] * 2 + [None]
    )
    pressed = []
    session.press = pressed.append

    seen = balance.dismiss(session)

    assert pressed == ["Escape"] and seen.closed
    assert seen.amount == AMOUNT


# --- guarded: вікно до заповнення, посеред, повторно ---


def scripted(monkeypatch, *seen):
    queue = list(seen)
    monkeypatch.setattr(balance, "dismiss", lambda session: queue.pop(0) if queue else None)
    return queue


def runner(*outcomes):
    calls = []

    def run(steps, driver, strict):
        calls.append(strict)
        out = outcomes[len(calls) - 1] if len(calls) <= len(outcomes) else ["ok"]
        if isinstance(out, Exception):
            raise out
        return out

    run.calls = calls
    return run


MISSING = FieldMissing("Розділ")


def test_no_modal_is_transparent(monkeypatch):
    scripted(monkeypatch, None)
    run = runner(["n"])

    assert balance.guarded(bare_session(), FakeDriver(None), [], run) == (["n"], None)


def test_modal_before_fill_is_closed_and_noted(monkeypatch):
    scripted(monkeypatch, Seen(AMOUNT, True))

    notes, amount = balance.guarded(bare_session(), FakeDriver(None), [], runner(["n"]))

    assert notes == [balance.NOTE, "n"] and amount == AMOUNT


def test_modal_before_fill_that_stays_stops(monkeypatch):
    scripted(monkeypatch, Seen(AMOUNT, False))
    run = runner()

    with pytest.raises(BalanceStop) as stop:
        balance.guarded(bare_session(), FakeDriver(None), [], run)

    assert run.calls == [] and "−193.15 грн" in stop.value.text


def test_modal_on_field_missing_is_closed_then_form_filled_again(monkeypatch):
    scripted(monkeypatch, None, Seen(AMOUNT, True))
    run = runner(MISSING, ["n"])

    notes, amount = balance.guarded(bare_session(), FakeDriver(None), [], run)

    assert notes == [balance.NOTE, "n"] and amount == AMOUNT and len(run.calls) == 2


def test_field_missing_without_modal_is_the_old_error(monkeypatch):
    scripted(monkeypatch, None, None)

    with pytest.raises(FieldMissing):
        balance.guarded(bare_session(), FakeDriver(None), [], runner(MISSING))


def test_modal_again_on_same_form_stops(monkeypatch):
    scripted(
        monkeypatch, Seen(AMOUNT, True), Seen(AMOUNT, True)
    )  # до заповнення і після «нема поля»

    with pytest.raises(BalanceStop):
        balance.guarded(bare_session(), FakeDriver(None), [], runner(MISSING))


def test_modal_comes_back_after_refill_stops(monkeypatch):
    scripted(monkeypatch, None, Seen(AMOUNT, True), Seen(AMOUNT, True))

    with pytest.raises(BalanceStop):
        balance.guarded(bare_session(), FakeDriver(None), [], runner(MISSING, MISSING))


# --- Тривога раз на добу, без номера картки ---


def alerts(tmp_path):
    path = tmp_path / events.NAME
    return (
        [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def test_alert_once_a_day_without_card_number(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path))
    day = date(2026, 10, 22)

    assert balance.announce(AMOUNT, day) is True
    assert balance.announce(AMOUNT, day) is False
    assert balance.announce(AMOUNT, date(2026, 10, 23)) is True

    sent = [e for e in alerts(config.data_dir()) if e["kind"] == "shafa.balance"]
    assert len(sent) == 2 and sent[0]["loud"] is True
    assert "−193.15" in sent[0]["text"] and "545708" not in json.dumps(sent, ensure_ascii=False)
    assert events.info("shafa.balance").loud and not events.info("shafa.balance").skip


# --- Публікація ---


class Modal(PubDriver):
    """Розділ не знайдено, поки на сторінці вікно (`modal` — що побачить `dismiss`)."""

    def __init__(self, *args, raise_missing=0, **kw):
        super().__init__(*args, **kw)
        self.missing_left = raise_missing

    def choose_category(self, parts, slug):
        if self.missing_left:
            self.missing_left -= 1
            raise FieldMissing("Розділ")
        super().choose_category(parts, slug)


@pytest.fixture
def env(conn, root, tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    return Env(conn, root, tmp_path)


def driver_with(env, missing):
    def make(session):
        env.driver = Modal(session, env.conn, env.results, raise_missing=missing)
        return env.driver

    env.make_driver = make


def test_publish_closes_modal_notes_it_and_publishes(env, monkeypatch):
    item = add(env)
    driver_with(env, missing=1)
    scripted(monkeypatch, None, Seen(AMOUNT, True))

    report = env.run()

    assert state(env.conn, item)[0] == Status.PUBLISHED
    assert env.driver.submits == [Status.PUBLISHING]
    assert any(balance.NOTE in line for line in env.lines)
    assert report.code == "done"
    assert len([e for e in alerts(config.data_dir()) if e["kind"] == "shafa.balance"]) == 1


def test_publish_stops_session_when_modal_stays_card_goes_back_untouched(env, monkeypatch):
    item = add(env)
    driver_with(env, missing=1)
    scripted(monkeypatch, None, Seen(AMOUNT, False))

    report = env.run()

    assert report.code == "shafa_balance"
    assert "−193.15 грн" in report.reason and "Робот гроші не списує" in report.reason
    assert state(env.conn, item) == (Status.APPROVED, None)  # Картка не винна, не в «Проблемах»
    assert env.driver.submits == []
    session = env.conn.execute("SELECT stop_code, stop_reason FROM sessions").fetchone()
    assert session["stop_code"] == "shafa_balance" and "−193.15" in session["stop_reason"]
    assert not any(e["kind"] == "shafa.balance" for e in alerts(config.data_dir()))  # ⛔ шле база


def test_publish_stops_when_modal_returns_on_same_form(env, monkeypatch):
    item = add(env)
    driver_with(env, missing=2)
    scripted(monkeypatch, Seen(AMOUNT, True), Seen(AMOUNT, True))

    report = env.run()

    assert report.code == "shafa_balance"
    assert state(env.conn, item)[0] == Status.APPROVED
    assert env.driver.submits == []


def test_publish_without_modal_keeps_old_field_missing_problem(env, monkeypatch):
    item = add(env)
    driver_with(env, missing=1)
    scripted(monkeypatch, None, None)

    report = env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "robot_field_missing")
    assert report.code == "done"


# --- Пробний прогін ---


def test_rehearse_notes_closed_modal_and_goes_on(
    rehearse_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    items = cards(rehearse_env, 2)
    script(monkeypatch, "ok", "ok")
    scripted(monkeypatch, Seen(AMOUNT, True))  # лише перша Картка

    report, lines, _ = go(rehearse_env, items)

    assert report.code == "done" and report.good == 2
    assert any(balance.NOTE in line for line in lines)


def test_rehearse_stops_when_modal_stays_cards_untouched(
    rehearse_env,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    items = cards(rehearse_env, 3)
    script(monkeypatch)
    scripted(monkeypatch, Seen(AMOUNT, False))

    report, _, _ = go(rehearse_env, items)

    assert report.code == "shafa_balance" and "−193.15 грн" in report.reason
    assert report.checked == 0
    assert states(rehearse_env, items) == [(Status.APPROVED, None)] * 3
    assert rehearse.last_summary(rehearse_env.tmp / "дані-звіту")["stop"] == "shafa_balance"
