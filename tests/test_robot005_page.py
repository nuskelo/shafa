"""ROBOT-005 у справжньому Chromium: блок розміру українською, російською, змішаний, невідомий.

Сторінка й скрипти списків — ті самі, що в ROBOT-003 (react-select); без Chromium — пропуск.
"""

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver
from test_robot003_page import PAGE as BASE
from test_robot003_page import chromium  # noqa: F401  (фікстура)
from test_robot_form import bare_session

SCRIPT = BASE[BASE.index("<script>") :]


def sizes(label, *names):
    buttons = "".join(f'<button type="button">{n}</button>' for n in names)
    return f'<div class="field"><div class="label">{label}</div>{buttons}</div>'


def material(options, chips):
    return (
        '<div class="field"><div class="label">Матеріал</div>'
        f'<div class="select" data-options="{options}" data-chips="{chips}"></div></div>'
    )


def page_html(*fields):
    return (
        '<!doctype html><meta charset="utf-8"><body><form>'
        + "".join(fields)
        + '<input name="price" value="150"></form>'
        + """<script>
window.clicked = [];
document.addEventListener('click', e => {
  if (e.target.matches('button')) window.clicked.push(e.target.textContent);
});
</script>"""
        + SCRIPT
        + "</body>"
    )


RU = sizes("Размер", "Европейский", "Украинский", "Американский", "34", "37", "46", "Другой")
UK = sizes("Розмір", "Європейський", "Український", "Американський", "34", "37", "46", "Інший")
MIXED = sizes("Розмір", "Европейский", "Український", "Американський", "37")
UNKNOWN = sizes("Розмір", "Euro", "UA", "37")


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


def clicked(driver):
    return driver.page.evaluate("window.clicked")


def test_russian_block_european_and_size(make):
    driver = make(RU)
    assert driver.pick("Розмір", ["Європейський"]) == []
    assert driver.pick("Розмір", ["37"]) == []
    assert clicked(driver) == ["Европейский", "37"]


def test_ukrainian_block_as_before(make):
    driver = make(UK)
    driver.pick("Розмір", ["Європейський"])
    driver.pick("Розмір", ["37"])
    assert clicked(driver) == ["Європейський", "37"]


def test_mixed_block(make):
    driver = make(MIXED)
    driver.pick("Розмір", ["Європейський"])
    driver.pick("Розмір", ["Український"])
    assert clicked(driver) == ["Европейский", "Український"]


def test_other_is_found_as_russian(make):
    driver = make(RU)
    driver.pick("Розмір", ["Інший"])
    assert clicked(driver) == ["Другой"]


def test_unknown_name_is_field_missing_with_buttons(make):
    driver = make(UNKNOWN)
    with pytest.raises(FieldMissing, match="Розмір: Європейський") as error:
        driver.pick("Розмір", ["Європейський"])
    assert "Euro" in error.value.details and clicked(driver) == []


def test_russian_label_without_ukrainian_one(make):
    driver = make(sizes("Размер", "Европейский"))
    assert driver._find("Розмір") is not None


def test_russian_chip_kept_and_russian_option_chosen(make):
    driver = make(material("Замша|Кожа|Текстиль", "Кожа"))
    assert driver.pick("Матеріал", ["Шкіра"]) == []
    driver.verify()
    driver = make(material("Замша|Кожа|Текстиль", ""))
    driver.pick("Матеріал", ["Шкіра"])
    mark = driver._find("Матеріал")
    assert driver.page.evaluate(form.CHIPS, mark) == ["Кожа"]
    driver.verify()


def test_size_tabs_listed_for_russian_form(make):
    driver = make(sizes("Размер", "Euro"))
    assert any("Размер" in t for t in driver.page.evaluate(form.LIST_FIELDS, None)["sizes"])


def test_verify_survives_language_switch_after_picking(make):
    """Шафа перемалювала блок іншою мовою між вибором і звіркою — це не зміна значення."""
    driver = make(material("Замша|Кожа|Текстиль", "Кожа"))
    driver.pick("Матеріал", ["Шкіра"])
    driver.page.evaluate(
        "document.querySelector('.select__multi-value__label').textContent = 'Шкіра'"
    )
    driver.verify()
    driver.page.evaluate(
        "document.querySelector('.select__multi-value__label').textContent = 'Замша'"
    )
    with pytest.raises(FieldMissing):
        driver.verify()
