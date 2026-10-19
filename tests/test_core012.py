"""CORE-012: обов'язковий блок «Виберіть размер *» у рукавичок (dry-fill 3650, 19.10)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from shafa import ai, catalog, rules
from shafa.robot import form, lang, sync
from shafa.robot.plan import OTHER_SIZE, PlanError, Step, plan, required_gaps
from test_core010 import GLOVES, SCARVES, answer, approved_gloves
from test_robot_form import FakeDriver
from test_robot_sync import COLORS, CONDITIONS, TREE_RESPONSE
from test_robot_sync import catalog as catalog_response

REAL = Path(catalog.__file__).parent / "data" / "shafa-catalog.json"
BUTTONS = ["XS", "S", "M", "L", "XL", "2", "3", "4", "5", "6", "6.5", "7", "7.5", "8", "8.5", "9"]


def sized(text):
    return f"Шкіряні рукавиці Tommy Hilfiger {text}. В ідеальному стані. 900грн"


def size_step(steps):
    return next((s for s in steps if s.kind == "size_block"), None)


# --- Знімок: блок розміру ---


def test_gloves_of_the_snapshot_have_the_required_size_block():
    block = catalog.size_block(GLOVES)
    assert block["required"] is True and block["title"] == "Виберіть розмір"
    assert block["choices"] == [*BUTTONS, "One Size", "Інший"]
    assert catalog.sizes(GLOVES) == []  # систем EU/UA/US тут немає — це окремий блок
    assert catalog.size_block(SCARVES) is None  # для шарфів і шапок — потрібен живий dry-fill


def test_real_snapshot_has_the_same_block():
    real = json.loads(REAL.read_text(encoding="utf-8"))["sections"][GLOVES]["size_block"]
    assert real == catalog.size_block(GLOVES)


def test_size_block_entry_reads_label_and_buttons():
    got = sync.size_block_entry(
        "Виберіть размер *", ["XS", " S ", "S", "Інший", "для нових — опція…"]
    )
    assert got == {"title": "Виберіть розмір", "required": True, "choices": ["XS", "S", "Інший"]}
    assert sync.size_block_entry("Виберіть розмір", ["М"])["required"] is False
    assert sync.size_block_entry("Виберіть размер *", []) is None


def test_build_snapshot_stores_the_size_block():
    block = {"title": "Виберіть розмір", "required": True, "choices": ["S"]}
    path = ("Аксесуари", "Рукавички та рукавиці", "Рукавички")
    entry = catalog_response(2483, {}, [], ["Nike"])
    snapshot = sync.build_snapshot(
        TREE_RESPONSE,
        COLORS,
        CONDITIONS,
        {path: (entry, "rukavychky-rukavytsi/rukavychky")},
        "2026-10-19",
        {path: block},
    )
    assert snapshot["sections"][" / ".join(path)]["size_block"] == block


# --- Правило: літерний розмір з тексту ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("розмір S/M", ["S", "M"]),
        ("розмір: XL", ["XL"]),
        ("р. M", ["M"]),
        ("Размер L", ["L"]),
        ("розмір S і M", ["S", "M"]),
        ("розмір 2XL", ["2XL"]),
        ("рукавички M і L, чорні", []),  # без слова «розмір» літера — не розмір
        ("Tommy L. Hilfiger 900грн", []),
        ("розмір 7,5", []),  # цифровий — не літерний
    ],
)
def test_letter_sizes_from_text(text, expected):
    assert rules.letter_sizes(text) == expected


def test_used_two_sizes_new_one_and_none_empty():
    sizes = lambda text: ai.accessory_sizes(text, False, GLOVES)
    assert sizes(sized("розмір S/M")) == ai.Field("sizes", ["S", "M"])
    assert sizes(sized("розмір XL")) == ai.Field("sizes", ["XL"])
    assert sizes(sized("")) == ai.Field("sizes", [])  # розміру немає — порожньо, без жовтого
    over = sizes(sized("розмір S/M/L"))
    assert (over.value, over.yellow) == ([], True)
    new = ai.accessory_sizes(sized("розмір S/M"), True, GLOVES)
    assert (new.value, new.yellow, new.reason) == (["S"], True, ai.NEW_ONE_SIZE)


def test_gloves_card_takes_sizes_from_text_through_the_answer_check():
    field = answer(sized("розмір S/M"), sizes=["L"])["sizes"]  # ШІ помилився — береться текст
    assert (field.value, field.yellow) == (["S", "M"], False)


# --- Робот: план ---


def test_plan_pushes_both_letters_of_used_gloves(conn, root):
    steps, notes = plan(approved_gloves(conn, root, text=sized("розмір S/M"), sizes=[]), root)
    assert size_step(steps) == Step("size_block", "Виберіть розмір", ["S", "M"])
    assert not {s.kind for s in steps} & {"size_system", "sizes"}
    assert notes == []


def test_plan_without_size_in_text_picks_other(conn, root):
    steps, notes = plan(approved_gloves(conn, root, text=sized(""), sizes=[]), root)
    assert size_step(steps).value == [OTHER_SIZE]
    assert any("«Інший»" in n for n in notes)


def test_plan_of_new_gloves_picks_one_size_and_says_why(conn, root):
    text = "Нові шкіряні рукавиці Tommy Hilfiger розмір S/M. Нові з етикеткою. 900грн"
    steps, notes = plan(approved_gloves(conn, root, text=text, sizes=[]), root)
    assert size_step(steps).value == ["S"]
    assert any(ai.NEW_ONE_SIZE in n for n in notes)


def test_sizes_corrected_by_the_seller_win_over_the_text(conn, root):
    card = approved_gloves(conn, root, text=sized("розмір S/M"), sizes=["L"])
    card.fields["sizes"] = replace(card.fields["sizes"], edited=True)  # її правка в Картці
    assert size_step(plan(card, root)[0]).value == ["L"]


# --- Робот: обов'язкове поле без значення — Проблема до кліку ---


def test_required_field_without_value_is_a_plan_error(conn, root):
    for c in catalog.section(GLOVES)["characteristics"]:
        c["required"] |= (
            c["title"] == "Принт"
        )  # Шафа може зробити поле обов'язковим; плану його нічим заповнити
    card = approved_gloves(conn, root)
    with pytest.raises(PlanError, match="Обов'язкові поля Шафи без значення: Принт"):
        plan(card, root)


def test_required_gaps_names_the_size_block_when_it_is_missing():
    steps = [Step("colors", "Колір", ["Чорний"]), Step("choice", "Стать", ["Для жінок"])]
    gaps = required_gaps(GLOVES, steps)
    assert "Виберіть розмір" in gaps and "Стать" not in gaps and "Колір" not in gaps


def test_complete_gloves_plan_has_no_gaps(conn, root):
    steps, _ = plan(approved_gloves(conn, root), root)
    assert required_gaps(GLOVES, steps) == []


# --- Форма: кроки доходять до драйвера, мова підпису ---


def test_execute_picks_the_size_block_buttons():
    driver = FakeDriver(None)
    form.execute([Step("size_block", "Виберіть розмір", ["S", "M"])], driver)
    assert driver.calls == [("pick", "Виберіть розмір", "S", "M")]


def test_size_block_label_is_the_same_in_all_spellings():
    assert lang.equivalent("Виберіть розмір", "Виберіть размер")
    assert {"Виберіть размер", "Выберите размер"} <= set(lang.variants("Виберіть розмір"))
    assert lang.covered("One Size")
