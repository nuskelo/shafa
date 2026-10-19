"""CORE-012 у справжньому Chromium: блок «Виберіть размер *» рукавичок (розмітка за dry-fill 3650)."""

import pytest

from shafa.robot import browser, form, sync
from shafa.robot.form import PageDriver
from test_robot003_page import chromium  # noqa: F401  (фікстура)
from test_robot005_page import clicked, page_html, sizes
from test_robot_form import bare_session

BUTTONS = ("XS", "S", "M", "L", "XL", "2", "3", "6.5", "One Size", "Інший")
GLOVES = sizes("Виберіть размер *", *BUTTONS)  # підпис мішаний: «Виберіть» + «размер»


@pytest.fixture
def make(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(form, "LIST_WAIT", 1)
    pages = []

    def build(*fields):
        page = chromium.new_page()
        page.set_content(page_html(*fields))
        pages.append(page)
        session = bare_session()
        session.page = page
        return PageDriver(session)

    yield build
    for page in pages:
        page.close()


def test_two_letters_are_pressed_in_the_mixed_label_block(make):
    driver = make(GLOVES)
    assert driver.pick("Виберіть розмір", ["S", "M"]) == []
    assert clicked(driver) == ["S", "M"]


def test_other_is_pressed_when_the_text_has_no_size(make):
    driver = make(GLOVES)
    driver.pick("Виберіть розмір", ["Інший"])
    assert clicked(driver) == ["Інший"]


def test_sync_reads_the_block_as_the_snapshot_stores_it(make):
    driver = make(GLOVES)
    block = sync.read_size_block(driver.page)
    assert block == {
        "title": "Виберіть розмір",
        "required": True,
        "choices": list(BUTTONS),
    }
