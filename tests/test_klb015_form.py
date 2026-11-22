"""KLB-015: категорії Клубку приходять із запиту — перша Картка сесії не падає, поки список порожній.

Підставний Клубок (`klubok_fake`): `late_categories` — опції з'являються через 3 с, `late_once` —
перше відкриття форми їх так і не дає, повторне — одразу. Без фіксу (`_select` бачив порожній список
і одразу казав «немає «Детское»») обидва тести червоніють.
"""

import pytest

from shafa.klubok import catalog
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_form as form
from test_klb001_page import chromium, make_ad, opened  # noqa: F401

KIDS_WINTER = 1000041  # Детское / Обувь / Зимняя обувь


@pytest.fixture
def server():
    srv = fake.Server(delay=(0.02, 0.2), seed=1)
    yield srv
    srv.close()


def session_for(chromium, server, scenario):  # noqa: F811
    return fake.FakeSession(None, server.base, scenario, context=chromium.new_context())


def test_first_kids_card_waits_for_late_categories(chromium, server, tmp_path):  # noqa: F811
    session = session_for(chromium, server, "late_categories")
    try:
        driver = opened(session)
        ad = make_ad(tmp_path, KIDS_WINTER, sex=0, sizes=(30,), color=99)
        driver.fill(ad)
        driver.verify(ad)
        assert driver.state()["categoryId"] == str(KIDS_WINTER)
        assert server.opens == 1  # дочекались без перечитування
    finally:
        session.close()


def test_rereads_form_once_when_categories_never_come(chromium, server, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(form, "SELECT_WAIT", 1.0)
    session = session_for(chromium, server, "late_once")
    try:
        driver = opened(session)
        driver.choose_category(catalog.leaf(KIDS_WINTER))
        assert driver.state()["categoryId"] == str(KIDS_WINTER)
        assert server.opens == 2  # одне перечитування форми
    finally:
        session.close()


def test_error_lists_options_after_one_reread(chromium, server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(form, "SELECT_WAIT", 1.0)
    monkeypatch.setattr(fake, "LATE_MS", 600_000)
    session = session_for(chromium, server, "late_categories")
    try:
        driver = opened(session)
        with pytest.raises(form.KlubokError) as caught:
            driver.choose_category(catalog.leaf(KIDS_WINTER))
        assert caught.value.code == "klubok_field_missing"
        assert "немає «Детское»" in caught.value.text
        assert "Є: 'Выберите категорию'" in caught.value.text
        assert server.opens == 2  # перечитали рівно один раз
    finally:
        session.close()


def test_select_lists_available_options(chromium, server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(form, "SELECT_WAIT", 1.0)
    session = session_for(chromium, server, "ok")
    try:
        driver = opened(session)
        with pytest.raises(form._ListMissing) as caught:
            driver._select("#category", "Немає такої")
        assert "Є: 'Выберите категорию', 'Детское'" in str(caught.value)
    finally:
        session.close()
