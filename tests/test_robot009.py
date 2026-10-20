"""ROBOT-009: тимчасові збої Шафи не валять робота; необов'язкове поле не зупиняє сесію."""

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from shafa.db import Status
from shafa.robot import form, publish
from shafa.robot.form import FieldMissing, PageDriver, execute
from shafa.robot.plan import Step, plan
from test_robot_form import FakeDriver
from test_robot_plan import approved
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    Env,
    PubDriver,
    add,
    new_cards_allowed,
    quiet,
    row,
    state,
)


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


class Flaky(PubDriver):
    """Вибір розділу падає `fails` разів поспіль (як `wait_for_load_state` на Шафі)."""

    def __init__(self, *args, fails, error, **kw):
        super().__init__(*args, **kw)
        self.fails, self.error = fails, error
        self.categories = 0

    def choose_category(self, parts, slug):
        self.categories += 1
        if self.categories <= self.fails:
            raise self.error
        super().choose_category(parts, slug)


def flaky(env, fails, error=None):
    error = error or PlaywrightTimeout("Timeout 30000ms exceeded.")

    def make(session):
        env.driver = Flaky(session, env.conn, env.results, fails=fails, error=error)
        return env.driver

    env.make_driver = make


# --- Повтор до кліку ---


def test_timeout_on_category_is_retried_and_published(env):
    item = add(env)
    flaky(env, fails=1)

    report = env.run()

    assert state(env.conn, item)[0] == Status.PUBLISHED
    assert env.session.opened == 3  # форма Старту, потім 2 спроби Картки: відкрита заново
    assert env.driver.submits == [Status.PUBLISHING]  # клік один
    assert sum(env.sleeps) == pytest.approx(sum(publish.RETRY_PAUSE) / 2)  # пауза 30–60 с
    assert any("тимчасовий збій, спроба 1 з 3" in line for line in env.lines)
    assert report.reason == "черга закінчилась"


def test_three_timeouts_stop_with_human_text_and_card_stays_approved(env):
    item = add(env)
    flaky(env, fails=99, error=PlaywrightTimeout("Timeout 30000ms exceeded."))

    report = env.run()

    assert env.session.opened == 4 and env.driver.categories == 3  # 3 спроби
    assert report.code == "slow"
    assert report.reason == (
        "Шафа довго не відповідала — робот спробував 3 рази і зупинився. Натисніть «Старт» пізніше."
    )
    assert "Timeout" not in report.reason
    assert state(env.conn, item) == (Status.APPROVED, None)  # Картка схвалена, без проблеми
    assert row(env.conn, item)["publish_started_at"] is None
    assert publish.started_today(env.conn, env.today) == 0  # ліміт доби не зачеплено
    assert env.driver.submits == []
    assert sum(env.sleeps) == pytest.approx(sum(publish.RETRY_PAUSE))  # дві паузи, не три
    assert sum("Timeout 30000ms" in line for line in env.lines) >= 3  # сирий текст — у журналі
    stop = env.conn.execute("SELECT stop_code, stop_reason FROM sessions").fetchone()
    assert (stop[0], stop[1]) == ("slow", report.reason)


def test_network_error_is_retried_too(env):
    item = add(env)
    flaky(env, fails=2, error=PlaywrightError("Page.goto: net::ERR_INTERNET_DISCONNECTED"))
    env.run()
    assert state(env.conn, item)[0] == Status.PUBLISHED
    assert env.session.opened == 4  # 3 спроби Картки


@pytest.mark.parametrize(
    "error", [RuntimeError("щось у коді"), PlaywrightError("Target page has been closed")]
)
def test_not_transient_error_is_not_retried(env, error):
    item = add(env)
    flaky(env, fails=99, error=error)

    with pytest.raises(type(error)):
        env.run()

    assert env.driver.categories == 1
    assert state(env.conn, item) == (Status.APPROVED, None)


def test_failure_after_click_is_never_retried(env):
    item = add(env)
    env.raises = PlaywrightTimeout("Timeout 60000ms exceeded.")

    with pytest.raises(PlaywrightTimeout):
        env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "publish_uncertain")
    assert len(env.driver.submits) == 1  # клік один, повтору немає
    assert env.session.opened == 2  # старт і одна спроба Картки


def test_stop_during_retry_pause_stops(env):
    item = add(env)
    flaky(env, fails=99)
    original = env.make_driver

    def make(session):
        driver = original(session)
        raw = driver.choose_category

        def fail_and_stop(parts, slug):
            with env.conn:  # Власник натиснув «Стоп» посеред збою
                env.conn.execute("UPDATE sessions SET command = 'stop'")
            raw(parts, slug)

        driver.choose_category = fail_and_stop
        return driver

    env.make_driver = make

    report = env.run()

    assert report.code == "stop"
    assert env.driver.categories == 1  # повтору після Стопу немає
    assert state(env.conn, item) == (Status.APPROVED, None)


# --- Мережа не стихає ---


def test_network_idle_timeout_is_not_an_error():
    class Page:
        seen = None

        def wait_for_load_state(self, state, timeout=None):
            Page.seen = (state, timeout)
            raise PlaywrightTimeout("Timeout 10000ms exceeded.")

    class Session:
        page = Page()
        waited = False

        def choose_category(self, parts, slug):
            return {}

        def wait_autofill(self):
            Session.waited = True

    PageDriver(Session()).choose_category(["Жіночий одяг"], "zhenskaya-obuv")

    assert Page.seen == ("networkidle", form.IDLE_WAIT * 1000)  # коротке очікування, не 30 с
    assert Session.waited  # далі, як завжди


def test_no_unbounded_load_state_waits_left():
    import inspect

    source = inspect.getsource(form) + inspect.getsource(publish)
    assert source.count("wait_for_load_state") == 1  # лише у `_network_idle`, з тайм-аутом


# --- Необов'язкове поле ---


def test_plan_marks_characteristics_from_catalog_as_optional(conn, root):
    steps, _ = plan(approved(conn, root), root)
    choices = [s for s in steps if s.kind == "choice"]
    assert choices and not any(s.required for s in choices)  # у «Черевиках» усе без *
    assert all(s.required for s in steps if s.kind in ("sizes", "colors", "condition"))


class Driver009(FakeDriver):
    def pick(self, label, values):
        self._call("pick", label, *values)

    def abandon(self, label):
        self.calls.append(("abandon", label))


def test_optional_field_missing_is_a_note_not_a_stop():
    driver = Driver009(None, missing="Челсі")
    steps = [
        Step("choice", "Тип", ["Челсі"], required=False),
        Step("choice", "Сезон", ["Зима"], required=False),
    ]

    notes = execute(steps, driver, strict=True)

    assert notes == ["Необов'язкове поле «Тип» не заповнено: на формі не знайдено Челсі"]
    assert ("abandon", "Тип") in driver.calls  # відкритий список закрито
    assert ("pick", "Сезон", "Зима") in driver.calls  # решта кроків іде далі


def test_optional_note_keeps_form_details():
    class Raising(Driver009):
        def pick(self, label, values):
            raise FieldMissing(f"{label}: {values[0]}", "у списку (2): Інше, Шнурки")

    notes = execute([Step("choice", "Тип", ["Челсі"], required=False)], Raising(None))
    assert "у списку (2): Інше, Шнурки" in notes[0]


@pytest.mark.parametrize("kind", ["choice", "sizes", "colors"])
def test_required_field_missing_still_stops(kind):
    driver = Driver009(None, missing="Челсі")
    with pytest.raises(FieldMissing):
        execute([Step(kind, "Поле", ["Челсі"])], driver)  # required за замовчуванням


def test_published_with_optional_field_note(env):
    item = add(env)

    class Missing(PubDriver):
        def pick(self, label, values):
            if label == "Тип":
                raise FieldMissing(f"{label}: {values[0]}")
            super().pick(label, values)

    env.make_driver = lambda session: (
        setattr(env, "driver", Missing(session, env.conn, env.results)) or env.driver
    )

    report = env.run()

    assert state(env.conn, item)[0] == Status.PUBLISHED
    assert report.problems == []
    assert any("Необов'язкове поле «Тип» не заповнено" in note for note in report.notes)
