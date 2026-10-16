"""CORE-005, `docs/rules.md` v1.3: розмір US/UK → EU таблицею коду; матеріал — шкіра/замша/нубук."""

import pytest

from shafa import ai, rules, sizes
from shafa.catalog import Kind
from test_ai import NAMES, answer, by_name

WOMEN = "Жіночий одяг / Взуття / Кросівки"
MEN = "Чоловічий одяг / Взуття / Кросівки"
GIRLS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
BABIES = "Дитячий одяг / Для малюків / Взуття / Кросівки"
BAG = "Аксесуари / Сумки та рюкзаки / Рюкзаки"
TEXT = "Кросівки Nike, розмір 8 US. В ідеальному стані. 1200грн"


def check(text=TEXT, **changes):
    return by_name(ai.check_answer(answer(**changes), NAMES, text))


# --- Таблиця ---


@pytest.mark.parametrize(
    ("raw", "system", "kind", "eu", "doubt"),
    [
        (["8,5"], "US", Kind.WOMEN, ["39"], None),
        (["6.5"], "UK", Kind.WOMEN, ["39"], None),
        (["10"], "US", Kind.MEN, ["43"], None),
        (["9.5"], "UK", Kind.MEN, ["43"], None),
        (["13C"], "US", Kind.KIDS, ["31"], None),
        (["4Y"], "US", Kind.KIDS, ["36"], None),
        (["8"], "US", Kind.WOMEN, ["38"], "у тексті US 8 — за таблицею EU 38–39, глянь ярлик"),
        (["3"], "US", Kind.MEN, [], "у тексті US 3 — такого розміру немає в таблиці, глянь ярлик"),
        (["8"], "US", None, [],
         "у тексті US 8 — переведення в EU лише для жіночого, чоловічого й дитячого"),
        (["5"], "US", Kind.BABIES, [],
         "у тексті US 5 — переведення в EU лише для жіночого, чоловічого й дитячого"),
        (["38"], "EU", Kind.WOMEN, ["38"], None),
    ],
)  # fmt: skip
def test_to_eu(raw, system, kind, eu, doubt):
    assert sizes.to_eu(raw, system, kind) == (eu, doubt)


def test_parse():
    assert [sizes.parse(s) for s in ("8", "8,5", "8.5", "8½", "US 9", "12C", "ж")] == [
        8, 8.5, 8.5, 8.5, 9, 12, None
    ]  # fmt: skip


# --- Бланк ---


def test_us_size_converted_by_code():
    fields = check(section=MEN, sizes=["10"], size_system="US")
    assert (fields["sizes"].value, fields["sizes"].yellow) == (["43"], False)


def test_us_size_between_rows_is_yellow():
    fields = check(section=WOMEN, sizes=["8"], size_system="US")
    assert fields["sizes"].value == ["38"]
    assert fields["sizes"].reason == "у тексті US 8 — за таблицею EU 38–39, глянь ярлик"


def test_us_size_without_gender_is_yellow_and_empty():
    fields = check(section="Щось / Невідоме", sizes=["8"], size_system="US")
    assert fields["sizes"].value == [] and fields["sizes"].yellow
    assert "US 8" in fields["sizes"].reason


def test_us_size_label_rule_still_applies():
    """«Вказаний US 9,5, ідуть на US 10» (вживане) → обидва, у європейських."""
    text = "Кросівки, вказаний 9,5 US, ідуть на 10 US. 1200грн"
    fields = check(text, section=MEN, sizes=["9,5", "10"], size_system="US")
    assert fields["sizes"].value == ["42"]  # 9,5 — діапазон 42–43: перше, жовте
    assert fields["sizes"].yellow
    fields = check(text, section=MEN, sizes=["9", "10"], size_system="US")
    assert (fields["sizes"].value, fields["sizes"].yellow) == (["42", "43"], False)


def test_eu_size_unchanged():
    fields = check(section=WOMEN, sizes=["38"], size_system="EU")
    assert (fields["sizes"].value, fields["sizes"].yellow) == (["38"], False)
    fields = check(section=WOMEN, sizes=["38"])  # старі відповіді без size_system
    assert (fields["sizes"].value, fields["sizes"].yellow) == (["38"], False)


def test_kids_us_size():
    fields = check(section=GIRLS, sizes=["1"], size_system="US")
    assert (fields["sizes"].value, fields["sizes"].yellow) == (["32"], False)


def test_babies_us_size_is_yellow():
    fields = check(section=BABIES, sizes=["5"], size_system="US")
    assert fields["sizes"].value == [] and fields["sizes"].yellow


def test_schema_has_size_system():
    assert ai.SCHEMA["properties"]["size_system"]["enum"] == ["EU", "US", "UK"]


# --- Матеріал ---


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("Шкіряні кросівки. 900грн", ["Шкіра"], ["Шкіра"]),
        ("Кросівки шкіра і нубук. 900грн", ["Шкіра", "Нубук"], ["Шкіра", "Нубук"]),
        ("Замшеві кросівки. 900грн", ["замша"], ["Замша"]),
        ("Кросівки текстиль, сітка. 900грн", ["Текстиль", "Сітка"], []),
        ("Кросівки шкіра і текстиль. 900грн", ["Шкіра", "Текстиль"], ["Шкіра"]),
        ("Кросівки. 900грн", ["Шкіра"], []),  # не в тексті — ні
        ("Кросівки. 900грн", ["Текстиль"], []),
    ],
)
def test_material_only_leather_suede_nubuck_from_text(text, value, expected):
    field = check(text, section=WOMEN, sizes=["38"], material_upper=value)["material_upper"]
    assert (field.value, field.yellow) == (expected, False)


def test_fake_leather_is_yellow():
    text = "Кросівки, штучна шкіра. 900грн"
    field = check(text, section=WOMEN, sizes=["38"], material_upper=["Шкіра"])["material_upper"]
    assert field.yellow and field.reason == "у тексті є штучна шкіра — перевір"


def test_prompt_says_three_materials_and_eu():
    prompt = ai.PROMPT_FILE.read_text(encoding="utf-8")
    assert "лише «Шкіра», «Замша», «Нубук»" in prompt
    assert "size_system" in prompt


# --- Мембрана ---


@pytest.mark.parametrize(
    "text", ["Кросівки Nike 38 розміру. 900грн", "Черевики, водонепроникні. 1200грн"]
)
def test_membrane_only_from_text(text):
    assert rules.membrane(text) is None
    assert "membrane" not in ai.SCHEMA["properties"]  # ШІ мембрану не ставить
