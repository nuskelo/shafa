import pytest

from shafa import catalog
from shafa.catalog import Kind

WOMEN_SNEAKERS = "Жіночий одяг / Взуття / Кросівки"
WOMEN_BOOTS = "Жіночий одяг / Взуття / Черевики"
MEN_SNEAKERS = "Чоловічий одяг / Взуття / Кросівки"
GIRLS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
BOYS_BOOTS = "Дитячий одяг / Для хлопчиків / Взуття / Бутси"
BABIES = "Дитячий одяг / Для малюків / Взуття / Кросівки"
BACKPACKS = "Аксесуари / Сумки та рюкзаки / Рюкзаки"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (WOMEN_SNEAKERS, WOMEN_SNEAKERS),
        ("жіночий одяг/взуття/ кросівки", WOMEN_SNEAKERS),
        (BOYS_BOOTS, BOYS_BOOTS),
        ("Дитячий одяг / Для дівчаток / Взуття / Бутси", None),  # бутси — лише в хлопчиків
        ("Жіночий одяг / Взуття / Ласти", None),
        ("Взуття", None),
    ],
)
def test_find_section_only_from_snapshot(path, expected):
    assert catalog.find_section(path) == expected


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (WOMEN_SNEAKERS, Kind.WOMEN),
        (MEN_SNEAKERS, Kind.MEN),
        (GIRLS, Kind.KIDS),
        (BOYS_BOOTS, Kind.KIDS),
        (BABIES, Kind.BABIES),
        (BACKPACKS, Kind.ACCESSORY),
        ("Для дому / Посуд / Чашки", None),
    ],
)
def test_section_kind(path, kind):
    assert catalog.section_kind(path) is kind


def test_sizes_are_explicit_lists():
    men = catalog.sizes(MEN_SNEAKERS)
    assert men[:4] == ["36", "37", "38", "38,5"]  # 36,5 і 37,5 у чоловічих немає
    assert "48,5" not in men and men[-1] == "50"
    kids = catalog.sizes(GIRLS)
    assert "40,5" not in kids and kids[-2:] == ["40", "41"]
    assert catalog.sizes(WOMEN_SNEAKERS)[0] == "34" and "46" in catalog.sizes(WOMEN_SNEAKERS)
    assert "Другой" not in men and "One size" not in men
    assert catalog.sizes(BACKPACKS) == []


def test_babies_use_ukrainian_sizes():
    assert catalog.size_system(BABIES) == "Український"
    assert catalog.sizes(BABIES) == [
        "15", "16", "17", "17,5", "18", "18,5", "19", "19,5", "20", "20,5", "21", "21,5", "22",
    ]  # fmt: skip
    assert catalog.size_system(WOMEN_SNEAKERS) == "Європейський"
    assert catalog.size_system(BACKPACKS) is None
    assert catalog.shafa_size("38,5") == "38.5"


def test_characteristics_depend_on_type():
    assert catalog.characteristic(WOMEN_SNEAKERS, "material_upper")["title"] == "Матеріал"
    assert catalog.characteristic(WOMEN_BOOTS, "material_upper")["title"] == "Матеріал верху"
    assert catalog.characteristic(WOMEN_SNEAKERS, "material_lining") is None
    assert "Хутро" in catalog.characteristic(WOMEN_BOOTS, "material_lining")["choices"]
    assert catalog.characteristic(WOMEN_BOOTS, "type")["choices"] == ["Берці", "Челсі"]
    assert catalog.characteristic(GIRLS, "style")["title"] == "Стиль взуття"
    assert catalog.characteristic(WOMEN_SNEAKERS, "season")["max"] == 4
    gender = catalog.characteristic(BABIES, "gender")
    assert gender["required"] and gender["choices"] == ["Для дівчаток", "Для хлопчиків", "Унісекс"]
    assert catalog.characteristic(GIRLS, "teen")["choices"] == ["Для дітей", "Для підлітків"]
    assert catalog.characteristic("Свій / Шлях", "season") is None


def test_brands_shared_list():
    assert "Skechers" in catalog.brands() and "Lowa" in catalog.brands()


def test_canonical():
    assert catalog.canonical("чорний", catalog.COLORS) == "Чорний"
    assert catalog.canonical(" м’ятний ", catalog.COLORS) == "М'ятний"
    assert catalog.canonical("Ультрамарин", catalog.COLORS) is None


def test_render_for_prompt():
    text = catalog.render()
    assert "Жіночий одяг / Взуття / … : Кросівки, Черевики" in text
    assert "- Дитячий одяг / Для малюків / Взуття: система «Український»: 15, 16, 17" in text
    assert "- Аксесуари / Сумки та рюкзаки: розміру немає" in text
    assert "type («Тип», до 1): Берці, Челсі" in text
    assert "gender («Стать» *, до 1): Для дівчаток, Для хлопчиків, Унісекс" in text
    assert all(color in text for color in catalog.COLORS)
