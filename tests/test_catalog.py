import pytest

from shafa import catalog
from shafa.catalog import Kind


def test_reference_lists_match_shafa_form():
    assert len(catalog.COLORS) == 32
    assert len(catalog.SHOE_MATERIALS) == 30
    assert len(catalog.BAG_MATERIALS) == 6
    assert len(catalog.WOMEN_SHOES) == 28
    assert len(set(catalog.COLORS)) == 32


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Жіночий одяг / Взуття / Кросівки", ("Жіночий одяг / Взуття / Кросівки", True)),
        ("жіночий одяг/взуття/ кросівки", ("Жіночий одяг / Взуття / Кросівки", True)),
        ("Жіночий одяг / Взуття / В’єтнамки", ("Жіночий одяг / Взуття / В'єтнамки", True)),
        (
            "Дитячий одяг / Для дівчаток / Взуття / Черевики",
            ("Дитячий одяг / Для дівчаток / Взуття / Черевики", True),
        ),
        (
            "Аксесуари / Сумки та рюкзаки / Рюкзаки",
            ("Аксесуари / Сумки та рюкзаки / Рюкзаки", True),
        ),
        ("Чоловічий одяг / Взуття / Черевики", ("Чоловічий одяг / Взуття / Черевики", False)),
        (
            "Дитячий одяг / Для хлопчиків / Взуття / Кросівки",
            ("Дитячий одяг / Для хлопчиків / Взуття / Кросівки", False),
        ),
        ("Аксесуари / Сумки та рюкзаки / Сумки", ("Аксесуари / Сумки та рюкзаки / Сумки", False)),
        ("Аксесуари / Ремені та пояси / Ремені", ("Аксесуари / Ремені та пояси / Ремені", False)),
        ("Жіночий одяг / Взуття / Ласти", (None, False)),
        ("Чоловічий одяг / Взуття / Ботильйони", (None, False)),
        ("Взуття", (None, False)),
    ],
)
def test_find_section(path, expected):
    assert catalog.find_section(path) == expected


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("Жіночий одяг / Взуття / Кросівки", Kind.WOMEN),
        ("Чоловічий одяг / Взуття / Кеди", Kind.MEN),
        ("Дитячий одяг / Для дівчаток / Взуття / Кеди", Kind.KIDS),
        ("Дитячий одяг / Для хлопчиків / Взуття / Кеди", Kind.KIDS),
        ("Дитячий одяг / Для малюків / Взуття / Кеди", Kind.BABIES),
        ("Аксесуари / Гаманці та портмоне / Гаманці", Kind.ACCESSORY),
        ("Жіночий одяг / Сукні", None),
        ("Аксесуарии / Щось", None),
    ],
)
def test_section_kind(path, kind):
    assert catalog.section_kind(path) is kind


def test_canonical_ignores_case_and_apostrophe():
    assert catalog.canonical("чорний", catalog.COLORS) == "Чорний"
    assert catalog.canonical(" м’ятний ", catalog.COLORS) == "М'ятний"
    assert catalog.canonical("Ультрамарин", catalog.COLORS) is None


@pytest.mark.parametrize(
    ("size", "kind", "ok"),
    [
        (38.0, Kind.WOMEN, True),
        (38.5, Kind.WOMEN, True),
        (33.5, Kind.WOMEN, False),
        (46.5, Kind.WOMEN, False),
        (38.3, Kind.WOMEN, False),
        (22.0, Kind.KIDS, True),
        (41.0, Kind.BABIES, True),
        (21.5, Kind.KIDS, False),
        (47.0, Kind.MEN, True),
        (44.2, Kind.MEN, False),
    ],
)
def test_size_in_range(size, kind, ok):
    assert catalog.size_in_range(size, kind) is ok


def test_render_lists_everything():
    text = catalog.render()
    for value in (
        catalog.COLORS + catalog.SHOE_MATERIALS + catalog.SEASONS + catalog.GENDERS + ("Рюкзаки",)
    ):
        assert value in text
