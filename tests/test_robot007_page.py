"""ROBOT-007 у справжньому Chromium: «Додаткові характеристики» російською, українською, змішано.

Значення блоків — з живого dry-fill 3588 (18.10.2026); без Chromium — пропуск.
"""

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver
from test_robot003_page import chromium  # noqa: F401  (фікстура)
from test_robot005_page import page_html
from test_robot_form import bare_session


def block(label, *names):
    buttons = "".join(f'<button type="button">{n}</button>' for n in names)
    return f'<div class="field"><div class="label">{label}</div>{buttons}</div>'


def listed(label, options, chips=""):
    return (
        f'<div class="field"><div class="label">{label}</div>'
        f'<div class="select" data-options="{options}" data-chips="{chips}"></div></div>'
    )


# 3588: підписи блоків українські, значення російські
RU = (
    block("Висота виробу", "Высокие", "Низкие", "Средние"),
    block("Висота каблука", "Высокий", "Очень высокий", "Низкий", "Средний"),
    block("Застібка", "Без застежки", "Замок", "Шнуровка"),
    block("Сезон", "Весна", "Демисезон", "Зима", "Лето", "Осень"),
    block("Стиль", "Классический", "Повседневный", "Спортивный"),
    block("Мембрана", "Gore-Tex", "Есть", "Нет"),
    block("Тип підошви", "Каблук", "Плоская", "Платформа", "Танкетка", "Шпилька"),
)
# 3644: те саме українською
UK = (
    block("Висота виробу", "Високі", "Низькі", "Середні"),
    block("Застібка", "Без застібки", "Замок", "Шнурівка"),
    block("Сезон", "Весна", "Демісезон", "Зима", "Літо", "Осінь"),
    block("Стиль", "Класичний", "Повсякденний", "Спортивний"),
    block("Мембрана", "Gore-Tex", "Є", "Немає"),
)
OURS = {
    "Висота виробу": "Високі",
    "Висота каблука": "Високий",
    "Застібка": "Шнурівка",
    "Сезон": "Демісезон",
    "Стиль": "Повсякденний",
    "Мембрана": "Є",
    "Тип підошви": "Пласка",
}


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


def pick_all(driver, labels):
    for label in labels:
        driver.pick(label, [OURS[label]])


def test_russian_block_all_fields(make):
    driver = make(*RU)
    pick_all(driver, OURS)
    assert clicked(driver) == [
        "Высокие",
        "Высокий",
        "Шнуровка",
        "Демисезон",
        "Повседневный",
        "Есть",
        "Плоская",
    ]


def test_ukrainian_block_as_before(make):
    driver = make(*UK)
    pick_all(driver, ["Висота виробу", "Застібка", "Сезон", "Стиль", "Мембрана"])
    assert clicked(driver) == ["Високі", "Шнурівка", "Демісезон", "Повсякденний", "Є"]


def test_mixed_block(make):
    driver = make(
        block("Стиль", "Классический", "Повседневный", "Спортивный"),
        block("Сезон", "Весна", "Демісезон", "Зима"),
        block("Мембрана", "Gore-Tex", "Є", "Немає"),
    )
    pick_all(driver, ["Стиль", "Сезон", "Мембрана"])
    assert clicked(driver) == ["Повседневный", "Демісезон", "Є"]


def test_value_same_in_both_languages_is_pressed_as_is(make):
    driver = make(*RU)
    driver.pick("Застібка", ["Замок"])
    driver.pick("Мембрана", ["Gore-Tex"])
    assert clicked(driver) == ["Замок", "Gore-Tex"]


def test_unknown_value_is_field_missing_with_buttons(make):
    driver = make(block("Стиль", "Деловой", "Спортивный"))
    with pytest.raises(FieldMissing, match="Стиль: Повсякденний") as error:
        driver.pick("Стиль", ["Повсякденний"])
    assert "Деловой" in error.value.details and clicked(driver) == []


def test_russian_style_not_taken_for_another_value(make):
    driver = make(block("Стиль", "Классический", "Спортивный"))
    with pytest.raises(FieldMissing):
        driver.pick("Стиль", ["Повсякденний"])
    assert clicked(driver) == []


def test_chips_in_russian_block_and_verify(make):
    """Список із чипами: російський чип — наш, звірка наприкінці проходить через ту саму таблицю."""
    driver = make(listed("Стиль", "Классический|Повседневный|Спортивный", "Повседневный"))
    assert driver.pick("Стиль", ["Повсякденний"]) == []
    driver.verify()
    driver = make(listed("Стиль", "Классический|Повседневный|Спортивный", ""))
    driver.pick("Стиль", ["Повсякденний"])
    assert driver.page.evaluate(form.CHIPS, driver._find("Стиль")) == ["Повседневный"]
    driver.verify()


def test_chips_foreign_value_still_fails_verify(make):
    driver = make(listed("Стиль", "Классический|Повседневный|Спортивный", "Повседневный"))
    driver.pick("Стиль", ["Повсякденний"])
    driver.page.evaluate(
        "document.querySelector('.select__multi-value__label').textContent = 'Спортивный'"
    )
    with pytest.raises(FieldMissing):
        driver.verify()
