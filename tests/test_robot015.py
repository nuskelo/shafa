"""ROBOT-015: фото «0 з N» один раз — дочекатись або повторити вибір файлів, а не Проблема."""

from datetime import datetime

import pytest

from shafa.db import Status
from shafa.robot import fake, form, publish, rehearse
from shafa.robot.form import PageDriver, PhotosShort, Step
from test_qa003 import RSession, go
from test_qa003 import cards as rehearse_cards
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    Env,
    add,
    new_cards_allowed,
    other_item,
    quiet,
    row,
    state,
)

# --- справжній `PageDriver.set_photos` проти підставної Шафи ---


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import Error, sync_playwright

    playwright = sync_playwright().start()
    try:
        browser_ = playwright.chromium.launch()
    except Error:
        playwright.stop()
        pytest.skip("немає браузера Playwright")
    yield browser_
    browser_.close()
    playwright.stop()


@pytest.fixture(scope="module")
def shafa():
    server = fake.Server()
    yield server
    server.close()


@pytest.fixture
def photos(chromium, shafa, tmp_path, monkeypatch):
    """`photos("photos_retry", 3)` → (драйвер, сесія, шляхи): форма підставної Шафи зі сценарієм."""
    monkeypatch.setattr(form, "PHOTO_WAIT", 1)
    monkeypatch.setattr(form, "PHOTO_EXTRA", 3)
    monkeypatch.setattr(fake, "LATE_MS", 2000)  # пізніше за `PHOTO_WAIT`, раніше за сумарне
    opened = []

    def make(scenario, count=3):
        page = chromium.new_page()
        opened.append(page)
        session = fake.FakeSession.__new__(fake.FakeSession)
        session._setup(allow_upload=True, publish=True)
        session.base, session.page = shafa.base, page
        page.route("**/*", session._route)
        page.goto(f"{shafa.base}/new?s={scenario}")
        paths = []
        for i in range(count):
            path = tmp_path / f"{i}.jpg"
            path.write_bytes(b"jpg")
            paths.append(str(path))
        return PageDriver(session), session, paths

    yield make
    for page in opened:
        page.close()


def test_late_counter_is_waited_for_without_retry(photos):
    driver, session, paths = photos("photos_late")
    assert driver.set_photos(paths) == 3
    assert driver.photo_retries == 0 and session.uploads == 3  # файли вибрано один раз


def test_first_pick_zero_gets_one_retry_then_all_shown(photos):
    driver, session, paths = photos("photos_retry", 1)
    assert driver.set_photos(paths) == 1
    assert driver.photo_retries == 1 and session.uploads == 2


def test_partly_uploaded_is_not_retried_and_stays_short(photos):
    driver, session, paths = photos("photos_part")
    with pytest.raises(PhotosShort, match="2 з 3"):
        form.execute([Step("photos", "Фото", paths)], driver, strict=True)
    assert driver.photo_retries == 0 and session.uploads == 3  # повтор задвоїв би фото


def test_partly_uploaded_is_only_a_note_outside_strict_mode(photos):
    driver, _, paths = photos("photos_part")
    assert form.execute([Step("photos", "Фото", paths)], driver) == [
        "Фото у формі: 2 з 3 — частина не завантажилась"
    ]


def test_retry_that_also_shows_zero_is_photos_short_with_one_retry_only(photos):
    driver, session, paths = photos("photos_zero", 1)
    with pytest.raises(PhotosShort, match="0 з 1"):
        form.execute([Step("photos", "Фото", paths)], driver, strict=True)
    assert driver.photo_retries == 1 and session.uploads == 2  # повтор лише один


def test_all_photos_shown_at_once_no_retry(photos):
    driver, session, paths = photos("ok")
    assert driver.set_photos(paths) == 3
    assert driver.photo_retries == 0 and session.uploads == 3


def test_reset_clears_retries_of_previous_card(photos):
    driver, _, paths = photos("photos_retry", 1)
    driver.set_photos(paths)
    driver.reset()
    assert driver.photo_retries == 0


# --- публікація: журнал повтору й скриншот ---


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def short_form(monkeypatch, module, retries):
    """Форма заповнюється з `retries` повторами фото й закінчується «1 з 2»."""

    def execute(steps, driver, strict):
        driver.photo_retries = retries
        raise PhotosShort("Фото у формі: 1 з 2 — частина не завантажилась")

    monkeypatch.setattr(module, "execute", execute)


def test_publish_photos_short_takes_screenshot_and_logs_retry(env, monkeypatch):
    item = add(env, "1342", 1342)
    other_item(env, item)
    short_form(monkeypatch, publish, 1)

    env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "robot_photos")
    assert any(s.endswith("-1342-фото.png") for s in env.session.page.shots)
    assert env.lines.count("Пост 1342: фото — повторна спроба") == 1
    assert any(
        line.startswith("Пост 1342: фото — Фото у формі: 1 з 2") and "-1342-фото.png" in line
        for line in env.lines
    )


def test_publish_without_retry_logs_no_retry_line(env, monkeypatch):
    item = add(env, "1342", 1342)
    other_item(env, item)
    short_form(monkeypatch, publish, 0)

    env.run()

    assert not any("повторна спроба" in line for line in env.lines)
    assert row(env.conn, item)["problem_code"] == "robot_photos"


# --- пробний прогін: те саме ---


@pytest.fixture
def renv(conn, root, tmp_path):
    e = Env(conn, root, tmp_path)
    e.today = datetime.now().astimezone()
    return e


def test_rehearsal_photos_short_takes_screenshot_and_logs_retry(renv, monkeypatch):
    items = rehearse_cards(renv, 1)
    short_form(monkeypatch, rehearse, 1)
    session = RSession()

    _, lines, _ = go(renv, items, session=session)

    assert state(renv.conn, items[0]) == (Status.PROBLEM, "robot_photos")
    assert any(s.endswith("-1300-фото.png") for s in session.page.shots)
    assert lines.count("Пост 1300: фото — повторна спроба") == 1
