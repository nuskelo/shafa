"""CORE-009: рішення Власника за QA-001 і перерахунок правилами без ШІ (перефразовані приклади)."""

import pytest

from shafa import ai, rules
from test_ai import NAMES, answer, by_name

WOMEN_SNEAKERS = "Жіночий одяг / Взуття / Кросівки"
MEN_SNEAKERS = "Чоловічий одяг / Взуття / Кросівки"
GIRLS_SNEAKERS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"


def check(text, **changes):
    changes.setdefault("material_upper", [])
    changes.setdefault("season", [])
    return by_name(ai.check_answer(answer(**changes), NAMES, text))


def doubt(field, reason):
    return {"field": field, "reason": reason}


# --- A1: 40 без ознак статі — жіноче ---


@pytest.mark.parametrize("section", [WOMEN_SNEAKERS, GIRLS_SNEAKERS])
def test_40_without_signs_is_women_not_yellow(section):
    fields = check(
        "Кросівки Asics 40 розміру, носились мало. 950грн",
        section=section, sizes=["40"], teen=None,
        doubts=[doubt("section", "40 — жіночі чи чоловічі, з фото не видно")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].yellow) == (WOMEN_SNEAKERS, False)


def test_40_men_when_ai_sees_men_line_and_yellow_only_on_conflict():
    text = "Кросівки Asics 40 розміру. 950грн"
    fields = check(text, section=MEN_SNEAKERS, sizes=["40"])
    assert (fields["section"].value, fields["section"].yellow) == (MEN_SNEAKERS, False)
    fields = check(
        text, section=MEN_SNEAKERS, sizes=["40"],
        doubts=[doubt("section", "модель чоловіча, але рожеві вставки")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].yellow) == (MEN_SNEAKERS, True)


def test_40_said_men_in_text_is_men():
    fields = check(
        "Чоловічі кросівки Asics 40 розміру. 950грн", section=WOMEN_SNEAKERS, sizes=["40"]
    )
    assert (fields["section"].value, fields["section"].yellow) == (MEN_SNEAKERS, False)


@pytest.mark.parametrize("size", ["41", "42"])
def test_41_42_still_ai_with_doubt(size):
    fields = check(
        f"Кросівки Asics {size} розміру. 950грн", section=WOMEN_SNEAKERS, sizes=[size],
        doubts=[doubt("section", "жіночі чи чоловічі — не видно")],
    )  # fmt: skip
    assert fields["section"].yellow


# --- A2: «вказаний X, ідуть на Y» — на Шафу Y ---


@pytest.mark.parametrize(
    ("text", "ai_sizes", "sizes"),
    [
        ("Кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн", ["38", "37"], ["37"]),
        ("Нові кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн", ["38"], ["37"]),
        ("Кеди Vans, на ярлику 39, ідуть на 38,5. 700грн", ["39"], ["38,5"]),
    ],
)
def test_label_gives_goes_size_for_used_and_new(text, ai_sizes, sizes):
    fields = check(text, section=WOMEN_SNEAKERS, sizes=ai_sizes)
    assert (fields["sizes"].value, fields["sizes"].yellow) == (sizes, False)


def test_label_rule_keeps_description_from_post():
    assert rules.label_sizes("Кеди. Вказаний 38 розмір, ідуть на 37. 700грн") == ["37"]
    assert "Вказаний 38 розмір" in rules.clean_description(
        "Кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн", rules.IDEAL
    )


# --- A3: 35–36 у можливо дитячих моделях ---


@pytest.mark.parametrize(
    "text",
    [
        "Кросівки Salomon Speedcross Kids 35 розміру. 800грн",
        "Кросівки Adidas Duramo Junior 36 розміру. 600грн",
        "Черевики Keen Targhee Jr 35,5 розміру. 900грн",
        "Кросівки Salomon XA Pro J 36 розміру. 800грн",
    ],
)
def test_35_36_kid_line_women_but_yellow(text):
    size = rules.text_sizes(text)[0]
    fields = check(text, section=WOMEN_SNEAKERS, sizes=[ai._number(size)])
    assert fields["section"].value == WOMEN_SNEAKERS
    assert (fields["section"].yellow, fields["section"].reason) == (True, ai.MAYBE_KID)


def test_35_36_ai_sees_kid_signs_on_photo_women_but_yellow():
    fields = check(
        "Кросівки Geox 35 розміру. 600грн", section=GIRLS_SNEAKERS, sizes=["35"], teen=False,
        doubts=[doubt("section", "можливо дитяча модель: липучки, принт з єдинорогом")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].reason) == (WOMEN_SNEAKERS, ai.MAYBE_KID)
    assert not fields["teen"].yellow


@pytest.mark.parametrize("size", ["35", "36"])
def test_35_36_without_signs_women_not_yellow(size):
    fields = check(
        f"Кросівки Geox {size} розміру. 600грн", section=GIRLS_SNEAKERS, sizes=[size],
        doubts=[doubt("section", "35-36 — дитяче чи доросле")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].yellow) == (WOMEN_SNEAKERS, False)


def test_kid_line_with_heel_is_adult_and_37_not_touched():
    fields = check(
        "Ботильйони Kids на каблуку 36 розміру. 900грн", section=WOMEN_SNEAKERS, sizes=["36"]
    )
    assert not fields["section"].yellow
    fields = check("Кросівки Nike Junior 37 розміру. 600грн", section=WOMEN_SNEAKERS, sizes=["37"])
    assert not fields["section"].yellow


@pytest.mark.parametrize(
    ("text", "kid"),
    [
        ("Кросівки Salomon Speedcross J 35", True),
        ("Кросівки Nike KIDS 36", True),
        ("Кросівки J.Crew 36", False),
        ("Кросівки Nike Air Jordan 36", False),
        ("Кросівки Jrlite 36", False),
    ],
)
def test_kid_model(text, kid):
    assert rules.kid_model(text) is kid
