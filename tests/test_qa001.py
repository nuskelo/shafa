"""QA-001: аудит пілоту — правила v1.6 (перефразовані приклади, не її пости)."""

import pytest

from shafa import ai, fill, rules
from shafa.db import Status
from test_ai import NAMES, answer, by_name
from test_core008 import item, refill_card, set_problem
from test_review import status

WOMEN_BOOTS = "Жіночий одяг / Взуття / Черевики"
WOMEN_SNEAKERS = "Жіночий одяг / Взуття / Кросівки"


def check(text, **changes):
    changes.setdefault("material_upper", [])
    changes.setdefault("season", [])
    return by_name(ai.check_answer(answer(**changes), NAMES, text))


def doubt(field, reason):
    return {"field": field, "reason": reason}


# --- Кілька товарів в одному пості ---


@pytest.mark.parametrize(
    "text",
    [
        (
            "Нові черевики Jomos. Є декілька розмірів.\n* 41 розмір. Устілка 26см.\n"
            "* 44 розмір. Устілка 28см.\n1500грн"
        ),
        (
            "Нові кросівки Rax. В наявності кілька розмірів:\n"
            "* Вказаний 39 розмір. Ідуть на 38.\n* Вказаний 40 розмір. Ідуть на 39.\n1500грн"
        ),
        "Нові кросівки Rax. Вказаний 39 розмір, ідуть на 38. Вказаний 41, ідуть на 40. 1500грн",
    ],
)
def test_several_sizes_are_several_items(text):
    assert rules.many_sizes(text)


@pytest.mark.parametrize(
    "text",
    [
        "Черевики Rax. Вказаний 39 розмір. Ідуть на 38. Устілка 25см. 900грн",
        "Черевики Rax. Розпаровка: лівий 38, правий 39. 500грн",
        "Черевики Rax 38 розміру. Підійдуть на 38-39. 900грн",
    ],
)
def test_one_item_stays_one(text):
    assert not rules.many_sizes(text)


def test_refill_sends_several_items_to_problems_without_ai(conn, root):
    """Картку завантажили до правила — «Перезаповнити» не дає їй пройти повз «Проблеми»."""
    text = "Нові черевики Jomos. Є декілька розмірів.\n* 41 розмір.\n* 44 розмір.\n1500грн"
    many = refill_card(conn, root, "1", 1, text=text)
    ready = refill_card(conn, root, "2", 2)

    assert fill.mark_refill(conn, [many, ready]) == 2
    assert (status(conn, many), item(conn, many)["problem_code"]) == (Status.PROBLEM, "many_items")
    assert item(conn, many)["refill_at"] is None
    assert status(conn, ready) == Status.NEW


def test_refill_keeps_gloves_in_problems(conn, root):
    gloves = refill_card(conn, root, "1", 1, text="Шкіряні рукавички Avan, розмір M. 300грн")
    set_problem(conn, gloves, "no_section")

    fill.mark_refill(conn, [gloves])

    assert (status(conn, gloves), item(conn, gloves)["problem_code"]) == (
        Status.PROBLEM,
        "no_section",
    )


# --- Ярлик не європейський: «вказаний розмір 5, ідуть на 37» ---


@pytest.mark.parametrize(
    ("text", "sizes"),
    [
        ("Черевики Clarks. Вказаний розмір 6. Ідуть на 39. 900грн", ["39"]),
        ("Нові черевики Clarks. Вказаний розмір 6. Ідуть на 39. 900грн", ["39"]),
        ("Кеди Ted. Вказаний 4,5 розмір. Ідуть на 37,5. 900грн", ["37,5"]),
        ("Кеди Ted. Вказаний розмір 12. Ідуть на 30. 500грн", ["30"]),
        ("Кеди, вказаний 9,5 US, ідуть на 10 US. 900грн", None),  # обидва US — таблиця US
        ("Кеди Ted. На бирці 24,5см. Ідуть на 38. 900грн", None),  # бирка з устілкою — не ярлик
    ],
)
def test_foreign_label_gives_goes_size(text, sizes):
    assert rules.label_sizes(text, rules.condition(text)) == sizes


@pytest.mark.parametrize("system", ["EU", "UK"])
def test_foreign_label_card_is_adult_and_not_yellow(system):
    text = "Замшеві черевики Rax в ідеальному стані. Вказаний розмір 5. Ідуть на 37. 1100грн"
    fields = check(text, sizes=["5", "37"], size_system=system)

    assert (fields["section"].value, fields["section"].yellow) == (WOMEN_BOOTS, False)
    assert (fields["sizes"].value, fields["sizes"].yellow) == (["37"], False)


def test_foreign_label_group_comes_from_goes_size():
    """ШІ поставило дитяче за ярликом «5» — код бере групу за «ідуть на 37»: доросле жіноче."""
    text = "Кросівки Rax в ідеальному стані. Вказаний розмір 5. Ідуть на 37. 1100грн"
    girls = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
    fields = check(text, section=girls, sizes=["5", "37"], doubts=[doubt("section", "стать?")])
    assert (fields["section"].value, fields["section"].yellow) == (WOMEN_SNEAKERS, False)


def test_non_european_size_never_says_babies():
    fields = check("Черевики Rax, розмір 7. 900грн", sizes=["7"])
    assert "малюк" not in (fields["section"].reason or "")
    assert fields["sizes"].yellow  # 7 немає у списку Шафи — людина вирішить


# --- Тип: уги й сапожки — лише з першого речення ---


def test_uggs_in_first_sentence_are_uggs():
    text = "Уги Emu оригінал у відмінному стані. 37 розмір. Висота сапожка 15см. 1500грн"
    fields = check(text, section="Жіночий одяг / Взуття / Чоботи", sizes=["37"])
    assert (fields["section"].value, fields["section"].yellow) == (
        "Жіночий одяг / Взуття / Уггі",
        False,
    )


@pytest.mark.parametrize(
    ("text", "section"),
    [
        ("Уги Emu у відмінному стані. 37 розмір. Висота сапожка 15см. 1500грн", "Уггі"),
        ("Черевики Ugg у відмінному стані. 37 розмір. Висота сапожка 15см. 1500грн", "Черевики"),
    ],
)
def test_measurement_word_is_not_the_type(text, section):
    path = f"Жіночий одяг / Взуття / {section}"
    assert check(text, section=path, sizes=["37"])["section"].value == path


# --- Поле, якого в типу немає, — порожнє, не жовте ---


def test_field_missing_in_section_is_dropped_quietly():
    text = "Термо кросівки Rax в ідеальному стані. 38 розмір. 1200грн"
    fields = check(text, section=WOMEN_SNEAKERS, sizes=["38"], style="Спортивний")
    assert (fields["style"].value, fields["style"].yellow) == (None, False)


# --- Підкладка «шерсть» — «Вовна» ---


@pytest.mark.parametrize("lining", [["Шерсть"], ["Вовна"]])
def test_wool_lining(lining):
    text = "Шкіряні черевики Rax. 37 розмір. Верх - натуральна шкіра, всередині шерсть. 1200грн"
    fields = check(text, sizes=["37"], material_lining=lining)
    assert (fields["material_lining"].value, fields["material_lining"].yellow) == (["Вовна"], False)


# --- Сезон: «всередині фліс» — як «на флісі» ---


@pytest.mark.parametrize(
    ("text", "season"),
    [
        ("Черевики Rax. Верх - замша, всередині фліс. 900грн", ["Зима"]),
        ("Черевики Rax без флісу. 900грн", None),
        ("Черевики Rax, всередині мікрофліс. Мембрана gore-tex. 900грн", ["Демісезон"]),
    ],
)
def test_fleece_inside_is_winter(text, season):
    assert rules.season(text)[0] == season


def test_prompt_has_qa_001_rules():
    prompt = ai.system_prompt()
    for phrase in (
        "«уги» — «Уггі»",
        "явні жіночі ознаки",
        "на шнурках — не Челсі",
        "«шерсть» → «Вовна»",
        "«вказаний розмір 5, ідуть на 37»",
        "«всередині фліс»",
    ):
        assert phrase in prompt, phrase
