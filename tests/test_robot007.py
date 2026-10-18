"""ROBOT-007: блок «Додаткові характеристики» без мови — таблиця й тест-охоронець."""

import pytest

from shafa import catalog
from shafa.robot import lang
from shafa.robot.form import FieldMissing, among, pick_values, same
from test_robot005 import Chips


def snapshot_values():
    """(поле, значення) усіх полів, які робот ставить (у будь-якому розділі), і всього взуття."""
    titles = {t for names in catalog.FIELD_TITLES.values() for t in names}
    found = set()
    for path, entry in catalog.snapshot()["sections"].items():
        for item in entry["characteristics"]:
            if item["title"] in titles or "Взуття" in path:
                found.update((item["title"], value) for value in item["choices"])
    return sorted(found)


def test_every_value_of_every_field_is_known_to_the_table():
    """Нове поле чи значення у знімку без російської пари (чи запису в SAME) — червоний тест.

    Так наступне «Повседневный» не дійде до живої Шафи (ROBOT-007).
    """
    missing = [f"{title}: {value}" for title, value in snapshot_values() if not lang.covered(value)]
    assert missing == []


def test_guard_sees_the_fields_the_plan_sets():
    """Охоронець справді дивиться на поля плану (інакше він нічого не охороняє)."""
    titles = {title for title, _ in snapshot_values()}
    for names in catalog.FIELD_TITLES.values():
        assert titles & set(names)
    values = snapshot_values()
    assert ("Стиль", "Повсякденний") in values and ("Мембрана", "Gore-Tex") in values
    assert len(values) > 250


def test_covered_is_not_loose():
    assert lang.covered("Повсякденний") and lang.covered("Gore-Tex") and lang.covered("25,5 см")
    assert not lang.covered("Повсякденно") and not lang.covered("Чарівний")


def test_same_values_do_not_hide_a_translation():
    """У SAME — лише те, що справді однакове; якщо в таблиці є російська пара, це помилка."""
    both = [n for n in lang.SAME if lang.variants(n) != [n]]
    assert both == []


@pytest.mark.parametrize(
    "ours, form",
    [
        ("Високі", "Высокие"),  # живий dry-fill 3588, 18.10
        ("Низькі", "Низкие"),
        ("Середні", "Средние"),
        ("Високий", "Высокий"),
        ("Дуже високий", "Очень высокий"),
        ("Низький", "Низкий"),
        ("Середній", "Средний"),
        ("Без застібки", "Без застежки"),
        ("Шнурівка", "Шнуровка"),
        ("Демісезон", "Демисезон"),
        ("Літо", "Лето"),
        ("Осінь", "Осень"),
        ("Класичний", "Классический"),
        ("Повсякденний", "Повседневный"),
        ("Спортивний", "Спортивный"),
        ("Є", "Есть"),
        ("Немає", "Нет"),
        ("Пласка", "Плоская"),
        ("Утепленні", "Утеплені"),  # на живій формі 3644 — так
        ("Повсякденний", "Повсякдений"),  # так у знімку
        ("Зав'язки", "Зав’язки"),  # апостроф
    ],
)
def test_live_pairs(ours, form):
    assert same(ours, form) and same(form, ours)


@pytest.mark.parametrize(
    "value", ["Gore-Tex", "Замок", "Каблук", "Платформа", "Танкетка", "Шпилька", "Нубук", "Зима"]
)
def test_values_same_in_both_languages_are_found_as_is(value):
    assert lang.covered(value) and lang.variants(value)[0] == value


def test_ages_both_languages():
    assert same("3 роки", "3 года") and same("5 років", "5 лет") and same("1 місяць", "1 месяц")
    assert same("11 років", "11 лет") and same("0 місяців", "0 месяцев")
    assert not same("3 роки", "3 лет")


def test_russian_spelling_of_another_value_is_not_ours():
    assert not same("Повсякденний", "Спортивный")
    assert not same("Високий", "Высокие")
    assert not same("Низький", "Низкие")


def test_russian_chips_count_as_ours_for_style():
    widget = Chips(["Повседневный"], ["Классический", "Спортивный"])
    assert pick_values(widget, "Стиль", ["Повсякденний"]) == [] and widget.removed == []
    assert among("Повседневный", ["Повсякденний"])


def test_russian_option_chosen_for_style():
    widget = Chips([], ["Классический", "Повседневный", "Спортивный"])
    pick_values(widget, "Стиль", ["Повсякденний"])
    assert widget.chips() == ["Повседневный"]


def test_unknown_style_is_field_missing_with_options():
    widget = Chips([], ["Классический", "Спортивный"])
    with pytest.raises(FieldMissing, match="Стиль: Повсякденний") as error:
        pick_values(widget, "Стиль", ["Повсякденний"])
    assert "Классический" in error.value.details
