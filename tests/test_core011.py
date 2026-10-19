"""CORE-011: живий знімок 19.10 — аксесуари без розміру, широкий «Матеріал», мова підписів."""

import json
from pathlib import Path

import pytest

from shafa import ai, catalog
from shafa.robot import lang
from test_core010 import GLOVES, GLOVES_TEXT, HATS, SCARVES, answer
from test_robot007 import snapshot_values

REAL = Path(catalog.__file__).parent / "data" / "shafa-catalog.json"
WOOL_SCARF = "Шарф Burberry 100% вовна. В ідеальному стані. 1500грн"


@pytest.fixture
def real(monkeypatch):
    """Справжній знімок (як його віддала Шафа), а не фікстура."""
    monkeypatch.setattr(catalog, "SNAPSHOT", REAL)
    catalog.snapshot.cache_clear()


# --- Знімок і охоронець ROBOT-007 на справжніх даних ---


def test_guard_sees_every_value_and_title_of_the_real_snapshot(real):
    """Фікстура мала лише 24 розділи і мову, яку ми собі уявляли: охоронець дивиться на справжній."""
    missing = [f"{t}: {v}" for t, v in snapshot_values() if not lang.covered(v)]
    titles = {
        c["title"] for e in catalog.snapshot()["sections"].values() for c in e["characteristics"]
    }
    assert missing == [] and [t for t in titles if not lang.covered(t)] == []
    assert len(catalog.snapshot()["sections"]) == 136


def test_mixed_language_titles_of_the_snapshot_become_ukrainian(real):
    """Шафа віддала «Материал», «Пол», «Полиэстер» поруч з «Матеріал», «Стать»; без цього поле не знайти."""
    raw = json.loads(REAL.read_text(encoding="utf-8"))["sections"]
    assert any(
        c["title"] in ("Материал", "Материал верха", "Пол")
        for e in raw.values()
        for c in e["characteristics"]
    )  # знімок справді змішаний — інакше тест нічого не доводить
    snuds = "Аксесуари / Шарфи і хустки / Снуди"
    assert catalog.characteristic(snuds, "gender")["title"] == "Стать"
    assert "Для жінок" in catalog.characteristic(snuds, "gender")["choices"]
    assert catalog.characteristic(snuds, "material_upper")["title"] == "Матеріал"
    assert "Поліестер" in catalog.characteristic(snuds, "material_upper")["choices"]
    sneakers = "Жіночий одяг / Взуття / Кросівки"
    assert catalog.characteristic(sneakers, "material_upper")["title"] == "Матеріал"


def test_every_accessory_of_the_real_snapshot_has_no_size(real):
    paths = [p for p in catalog.sections() if catalog.section_kind(p) is catalog.Kind.ACCESSORY]
    assert len(paths) > 40
    assert [p for p in paths if catalog.sizes(p)] == []


def test_ukrainian_is_the_first_spelling_of_the_table():
    assert lang.ukrainian("Полиэстер") == "Поліестер"
    assert lang.ukrainian("Поліестер") == "Поліестер"
    assert lang.ukrainian("Козырек") == "Козырек"  # пари немає — як є


# --- Аксесуари без розміру ---


def test_sizes_of_gloves_and_scarves_are_dropped_without_yellow(real):
    for section, text in ((GLOVES, GLOVES_TEXT), (SCARVES, WOOL_SCARF)):
        field = answer(text, section=section, sizes=["S", "M"])["sizes"]
        assert (field.value, field.yellow) == ([], False)


def test_shoes_without_a_size_list_entry_are_still_yellow():
    """Тиха відмова — лише для аксесуарів: у взуття зайвий розмір «у цього розділу немає» не буває."""
    field, _ = ai.check_sizes(["38"], "Дитячий одяг / Для малюків / Взуття / Кросівки", "кросівки")
    assert field.yellow  # 38 немає у списку малюків


# --- Широкий «Матеріал» рукавичок, шарфів і шапок ---


@pytest.mark.parametrize(
    "section, text, given, value, yellow",
    [
        (SCARVES, WOOL_SCARF, ["Вовна"], ["Вовна"], False),  # 4419 «100% вовна»
        (SCARVES, "Шарф, вовна з акрилом. 500грн", ["Вовна", "Акрил"], ["Вовна", "Акрил"], False),
        (SCARVES, "Шарф, шерсть. 500грн", ["Вовна"], ["Вовна"], False),
        (SCARVES, "Шарф кашеміровий. 500грн", ["Кашемір"], ["Кашемір"], False),
        (HATS, "Шапка бавовна, флісова підкладка. 300грн", ["Бавовна"], ["Бавовна"], False),
        (GLOVES, "Рукавички замшеві. 500грн", ["Замша"], ["Замша"], False),
        (GLOVES, "Рукавички шкіра. 500грн", ["Шкіра"], ["Шкіра"], False),
        (SCARVES, "Шарф Burberry. 1500грн", ["Вовна"], [], False),  # не з тексту — відкинуто
        (SCARVES, "Шарф, шовк. 500грн", ["Шовк", "Вовна"], ["Шовк"], False),
        (SCARVES, "Шарф, вовна. 500грн", ["Дерево"], [], False),  # немає у списку Шафи
    ],
)
def test_material_of_scarf_gloves_hats_is_any_value_named_in_the_text(
    real, section, text, given, value, yellow
):
    field = answer(text, section=section, material_upper=given, sizes=[])["material_upper"]
    assert (field.value, field.yellow) == (value, yellow)


def test_fake_leather_in_gloves_is_still_yellow(real):
    field = answer("Рукавички, шкіра і штучна шкіра. 500грн", material_upper=["Шкіра"])[
        "material_upper"
    ]
    assert field.yellow


def test_other_accessories_and_shoes_keep_leather_suede_nubuck_only(real):
    """Обмеження CORE-005 лишається там, де його не скасовано: сумки й взуття."""
    bag = "Аксесуари / Сумки та рюкзаки / Сумки"
    assert not catalog.wide_material(bag)
    assert catalog.wide_material(SCARVES) and catalog.wide_material(HATS.lower())
    assert not catalog.wide_material(None)
    field = ai.check_material(["Шкіра", "Поліестер"], bag, "Сумка шкіряна, поліестер. 500грн")
    assert field.value == ["Шкіра"]
    shoes = "Жіночий одяг / Взуття / Кросівки"
    field = ai.check_material(["Шкіра", "Поліестер"], shoes, "Кросівки шкіра, поліестер. 500грн")
    assert field.value == ["Шкіра"]


# --- Версії: стара відповідь за v1.8 і «Перезаповнити» ---


def test_old_answer_with_missing_material_needs_ai_only_for_these_groups(real):
    old = {"material_upper": [], "section": SCARVES}
    assert ai.needs_ai(4, old, WOOL_SCARF)
    assert not ai.needs_ai(5, old, WOOL_SCARF)  # вже за v1.9
    assert not ai.needs_ai(4, old, "Шарф Burberry. 1500грн")  # матеріалу в тексті немає
    done = {"material_upper": ["Вовна"], "section": SCARVES}
    assert not ai.needs_ai(4, done, WOOL_SCARF)
    shoes = {"material_upper": [], "section": "Жіночий одяг / Взуття / Кросівки"}
    assert not ai.needs_ai(4, shoes, "Кросівки вовна. 500грн")


def test_prompt_tells_the_ai_about_wide_material_and_no_size():
    text = (Path(ai.__file__).parent / "prompts" / "blank.md").read_text(encoding="utf-8")
    assert "100% вовна" in text and "Для всіх аксесуарів" in text
    assert ai.PROMPT_VERSION == 5
