"""ROBOT-005: назви на формі порівнюються без мови (таблиця UK↔RU)."""

from collections import Counter

import pytest

from shafa import catalog
from shafa.robot import lang
from shafa.robot.form import FieldMissing, among, exact, pick_values, same


def test_table_has_no_ambiguous_spelling():
    """Одне написання не може бути в двох різних назвах — інакше «вгадування»."""
    seen = Counter()
    for group in lang.GROUPS:
        for name in {lang._norm(n) for n in group}:
            seen[name] += 1
    assert [n for n, c in seen.items() if c > 1] == []


@pytest.mark.parametrize(
    "ours, form",
    [
        ("Європейський", "Европейский"),
        ("Український", "Украинский"),
        ("Американський", "Американский"),
        ("Британський", "Британский"),
        ("Інший", "Другой"),
        ("Розмір", "Размер"),
        ("Новий", "Новое"),
        ("Чорний", "Чёрный"),
        ("Шкіра", "Кожа"),
        ("Шкіра", "Шкира"),
    ],
)
def test_pairs_match_both_ways(ours, form):
    assert same(ours, form) and same(form, ours)
    assert form in lang.variants(ours) and ours in lang.variants(form)


def test_snapshot_values_are_in_the_table():
    """Те, що робот шукає зі знімка: системи розміру, стани, кольори — усе має російську пару."""
    data = catalog.snapshot()
    names = set(catalog.SIZE_SYSTEMS) | {"Інший"} | set(data["conditions"]) | set(data["colors"])
    missing = [n for n in names if lang.variants(n) == [n]]
    assert missing == []


def test_other_spelling_from_snapshot_is_covered():
    """У знімку «Інший» записано як «Другой» — це те саме."""
    assert same("Інший", "Другой")


def test_unknown_name_is_not_translated():
    assert not same("Європейський", "Euro")
    assert not same("Шкіра", "Замша")
    assert lang.variants("Euro") == ["Euro"]


def test_whitespace_and_case_ignored():
    assert same("  Европейский ", "європейський")


class Chips:
    """Поле-список із чипами й варіантами (як react-select)."""

    listed = True

    def __init__(self, chips, options):
        self._chips, self._options, self.removed = list(chips), list(options), []

    def chips(self):
        return list(self._chips)

    def remove(self, chip):
        self.removed.append(chip)
        self._chips.remove(chip)

    def press(self, value):
        return False

    def search(self, text):
        return [o for o in self._options if text.casefold() in o.casefold()]

    def choose(self, option):
        self._chips.append(option)

    def close(self):
        pass

    def describe(self):
        return ""


def test_russian_chip_counts_as_ours():
    widget = Chips(["Кожа"], ["Замша", "Текстиль"])
    assert pick_values(widget, "Матеріал", ["Шкіра"]) == [] and widget.removed == []


def test_russian_option_found_in_full_list():
    widget = Chips([], ["Замша", "Кожа", "Текстиль"])
    pick_values(widget, "Матеріал", ["Шкіра"])
    assert widget.chips() == ["Кожа"]


def test_foreign_russian_chip_removed():
    widget = Chips(["Замша"], ["Кожа"])
    notes = pick_values(widget, "Матеріал", ["Шкіра"])
    assert widget.removed == ["Замша"] and notes == [
        "Матеріал: Шафа поставила сама «Замша» — прибрано"
    ]


def test_unknown_option_still_field_missing():
    widget = Chips([], ["Замша", "Кожа"])
    with pytest.raises(FieldMissing) as error:
        pick_values(widget, "Матеріал", ["Мембрана"])
    assert "Замша, Кожа" in error.value.details


def test_exact_and_among_use_table():
    assert exact(["Другой", "One size"], "Інший") == "Другой"
    assert among("Новый", ["Новий"])
