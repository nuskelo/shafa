"""ROBOT-010: помилка однієї Картки не зупиняє сесію; зупинка — лише коли причина загальна."""

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from shafa.db import Status
from shafa.robot import control, publish
from shafa.robot.form import FieldMissing, PhotosShort
from shafa.robot.publish import Submitted
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    Env,
    add,
    new_cards_allowed,
    other_item,
    quiet,
    row,
    state,
)


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def cards(env, n):
    """n схвалених Карток, найстаріша перша; «Це інший товар» — щоб схожі назви не були дублем."""
    items = []
    for i in range(n):
        item = add(env, str(1300 + i), 1300 + i, f"2025-01-{1 + i:02d}T10:00:00")
        other_item(env, item)
        items.append(item)
    return items


def script(monkeypatch, *outcomes):
    """Що буде з кожною Карткою під час заповнення форми: "ok", виняток чи "slow" (3 тайм-аути)."""
    at = {"card": 0, "attempt": 0}

    def execute(steps, driver, strict):
        out = outcomes[at["card"]] if at["card"] < len(outcomes) else "ok"
        if out == "slow":
            at["attempt"] += 1
            if at["attempt"] == publish.ATTEMPTS:
                at["card"], at["attempt"] = at["card"] + 1, 0
            raise PlaywrightTimeout("Timeout 30000ms exceeded.")
        at["card"] += 1
        if isinstance(out, Exception):
            raise out
        return []

    monkeypatch.setattr(publish, "execute", execute)


def clicked(env):
    return env.conn.execute(
        "SELECT COUNT(*) FROM items WHERE publish_started_at IS NOT NULL"
    ).fetchone()[0]


def states(env, items):
    return [state(env.conn, i) for i in items]


MISSING = FieldMissing("Сезон")
PHOTOS = PhotosShort("1 з 2")


def test_field_missing_on_second_card_session_goes_on(env, monkeypatch):
    items = cards(env, 4)
    script(monkeypatch, "ok", MISSING, "ok", "ok")

    report = env.run()

    assert states(env, items) == [
        (Status.PUBLISHED, None), (Status.PROBLEM, "robot_field_missing"),
        (Status.PUBLISHED, None), (Status.PUBLISHED, None),
    ]  # fmt: skip
    assert (len(report.published), len(report.problems), report.code) == (3, 1, "done")
    assert len(env.driver.submits) == 3
    assert "Пост 1301: robot_field_missing — " in report.text()  # рядок на кожну Картку з помилкою


@pytest.mark.parametrize(
    ("bad", "code", "result"),
    [
        (PHOTOS, "robot_photos", None),
        ("slow", "robot_slow", None),
        ("ok", "robot_not_sent", Submitted(False)),
        ("ok", "publish_error", Submitted(True, errors=["size: Невірний розмір"])),
    ],
)
def test_each_card_error_is_a_problem_and_next_card_goes_on(env, monkeypatch, bad, code, result):
    items = cards(env, 2)
    script(monkeypatch, bad, "ok")
    if result is not None:
        env.results.append(result)

    report = env.run()

    assert states(env, items) == [(Status.PROBLEM, code), (Status.PUBLISHED, None)]
    assert report.code == "done"
    assert [p[1] for p in report.problems] == [code]


def test_slow_text_is_human_and_names_the_button(env, monkeypatch):
    (item,) = cards(env, 1)
    script(monkeypatch, "slow")
    env.run()
    text = row(env.conn, item)["problem_text"]
    assert "Шафа довго не відповідала на цій Картці" in text and "Перевірити ще раз" in text
    assert "Timeout" not in text


def test_three_failures_in_a_row_stop_with_streak(env, monkeypatch):
    items = cards(env, 4)
    script(monkeypatch, MISSING, PHOTOS, "slow", "ok")

    report = env.run()

    assert states(env, items)[:3] == [
        (Status.PROBLEM, "robot_field_missing"), (Status.PROBLEM, "robot_photos"),
        (Status.PROBLEM, "robot_slow"),
    ]  # fmt: skip
    assert state(env.conn, items[3]) == (Status.APPROVED, None)  # четверту не чіпали
    assert env.driver.submits == []
    assert report.code == "streak"
    assert report.reason == (
        "3 Картки поспіль не вдались (robot_field_missing, robot_photos, robot_slow) — "
        "схоже, загальна причина. Скажіть Власнику"
    )
    assert env.conn.execute("SELECT stop_code FROM sessions").fetchone()[0] == "streak"


def test_success_resets_the_streak(env, monkeypatch):
    items = cards(env, 4)
    script(monkeypatch, MISSING, "ok", MISSING, MISSING)

    report = env.run()

    assert report.code == "done"
    assert [s[0] for s in states(env, items)] == [
        Status.PROBLEM, Status.PUBLISHED, Status.PROBLEM, Status.PROBLEM,
    ]  # fmt: skip


def test_five_failures_per_session_stop_even_with_successes_between(env, monkeypatch):
    items = cards(env, 10)
    script(monkeypatch, MISSING, "ok", MISSING, "ok", MISSING, "ok", MISSING, "ok", MISSING, "ok")

    report = env.run()

    assert report.code == "streak"
    assert report.reason.startswith("5 Карток за сесію не вдались (")
    assert "загальна причина" in report.reason
    assert len(report.problems) == 5 and len(report.published) == 4
    assert state(env.conn, items[9]) == (Status.APPROVED, None)


def test_pause_is_taken_after_a_failed_card(env, monkeypatch):
    cards(env, 2)
    script(monkeypatch, MISSING, "ok")

    env.run()

    assert sum(env.sleeps) == pytest.approx(sum(publish.PAUSE) / 2)  # одна пауза між Картками


def test_no_pause_after_the_stop_by_streak(env, monkeypatch):
    cards(env, 3)
    script(monkeypatch, MISSING, MISSING, MISSING)
    env.run()
    assert sum(env.sleeps) == pytest.approx(sum(publish.PAUSE))  # дві паузи, не три


def test_card_after_a_failed_one_gets_a_fresh_form(env, monkeypatch):
    cards(env, 2)
    script(monkeypatch, MISSING, "ok")
    env.run()
    assert env.session.opened == 3  # форма Старту й по одній на кожну Картку


def test_failed_card_that_clicked_counts_in_the_day_limit(env, monkeypatch):
    items = cards(env, 3)
    script(monkeypatch, "ok", "ok", "ok")
    env.results.append(Submitted(True, errors=["size: Невірний розмір"]))

    report = env.run(day_limit=2)

    assert state(env.conn, items[0]) == (Status.PROBLEM, "publish_error")
    assert state(env.conn, items[1])[0] == Status.PUBLISHED
    assert state(env.conn, items[2]) == (Status.APPROVED, None)
    assert report.code == "limit"
    assert clicked(env) == 2


def test_card_without_click_is_not_in_the_day_limit(env, monkeypatch):
    items = cards(env, 4)
    script(monkeypatch, MISSING, "ok", "ok", "ok")

    report = env.run(day_limit=2)

    assert [s[0] for s in states(env, items)] == [
        Status.PROBLEM, Status.PUBLISHED, Status.PUBLISHED, Status.APPROVED,
    ]  # fmt: skip
    assert clicked(env) == 2
    assert report.code == "limit"


def test_skipped_cards_do_not_touch_the_streak(env, monkeypatch):
    items = cards(env, 5)
    with env.conn:  # дві Картки — за старими правилами: пропущені без спроби
        env.conn.execute("UPDATE items SET rules_version = 1 WHERE id IN (?, ?)", items[1:3])
    script(monkeypatch, MISSING, MISSING, "ok", "ok")

    report = env.run()

    assert report.code == "done"  # дві помилки + пропуски між ними — не три поспіль
    assert state(env.conn, items[1])[1] == publish.STALE_RULES


# --- Зупинка одразу, як і було ---


def test_login_stops_at_once(env):
    items = cards(env, 2)
    env.session.logins = [True, False]
    report = env.run()
    assert report.code == "login"
    assert state(env.conn, items[1]) == (Status.APPROVED, None)


def test_no_form_stops_at_once(env):
    from shafa.robot import browser

    items = cards(env, 2)
    env.session.logins = [True, browser.NO_FORM]
    report = env.run()
    assert report.code == "no_form"
    assert [s[0] for s in states(env, items)] == [Status.APPROVED, Status.APPROVED]


def test_missing_submit_button_stops_at_once(env):
    items = cards(env, 2)
    env.raises = FieldMissing("Додати річ")
    report = env.run()
    assert report.code == "site"
    assert state(env.conn, items[0]) == (Status.PROBLEM, "robot_field_missing")
    assert state(env.conn, items[1]) == (Status.APPROVED, None)


@pytest.mark.parametrize(
    ("result", "code", "stop"),
    [
        (Submitted(False, blocked=True), "robot_publish_blocked", "error"),
        (Submitted(True), "publish_uncertain", "error"),  # відповідь без id
    ],
)
def test_blocked_and_uncertain_stop_at_once(env, result, code, stop):
    items = cards(env, 2)
    env.results.append(result)
    report = env.run()
    assert report.code == stop
    assert state(env.conn, items[0]) == (Status.PROBLEM, code)
    assert state(env.conn, items[1]) == (Status.APPROVED, None)


def test_stop_button_stops_between_cards(env, monkeypatch):
    items = cards(env, 2)
    script(monkeypatch, MISSING, "ok")
    original = publish.Run._one

    def one(self, report, item_id, driver, listings):
        result = original(self, report, item_id, driver, listings)
        publish.request(self.conn, "stop")  # «Стоп» натиснули під час першої Картки
        return result

    monkeypatch.setattr(publish.Run, "_one", one)
    report = env.run()
    assert report.code == "stop"
    assert state(env.conn, items[1]) == (Status.APPROVED, None)


def test_screen_has_text_for_streak_and_keeps_old_ones():
    assert "загальна причина" in control.LAST["streak"]
    assert control.LAST["site"] and control.LAST["photos"] and control.LAST["slow"]
