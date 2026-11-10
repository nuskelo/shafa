"""KLB-001: поля Клубку з Картки (без браузера й мережі)."""

import pytest

from shafa import ai, review
from shafa.db import Status
from shafa.klubok import fields
from shafa.klubok.fields import KlubokAd, Skip
from test_review import add_card

BOOTS = "Жіночий одяг / Взуття / Черевики"
TEXT = (
    "Шкіряні черевики Ecco 38 розміру. На хутрі. Мембрана gore-tex. Устілка 24,5 см. "
    "В ідеальному стані. 1200грн"
)


def blank(section=BOOTS, sizes=("38",), season=("Зима",), colors=("Чорний",), brand="Ecco", **kw):
    items = {
        "title": "Шкіряні черевики Ecco 38 розміру в ідеальному стані",
        "section": section,
        "sizes": list(sizes),
        "colors": list(colors),
        "brand": brand,
        "season": list(season),
        "gender": None,
        "photo_order": ["IMG_2.JPG", "IMG_1.JPG"],
    } | kw
    return [ai.Field(name, value) for name, value in items.items()]


def make(conn, root, text=TEXT, status=Status.APPROVED, **kw):
    item_id = add_card(conn, root, blank=blank(**kw), text=text, status=status)
    return review.load_card(conn, item_id)


def test_winter_boots_full_ad(conn, root):
    result = fields.ad(make(conn, root), root)

    assert isinstance(result, KlubokAd)
    assert result.leaf.id == 1000637  # зима → зимові черевики
    assert result.name == "Шкіряні черевики Ecco 38 розміру в ідеальному стані"
    assert result.price == 1200  # ціна з поста, не +15% Шафи
    assert result.sizes == (38,)
    assert result.sex is None  # у дорослих поля статі немає
    assert result.condition == fields.CONDITION_USED
    assert result.color == 7
    assert result.brand == "Ecco"
    assert [p.name for p in result.photos] == ["IMG_2.JPG", "IMG_1.JPG"]  # порядок Картки
    assert result.photos[0].parent == root / "1342"


def test_not_winter_goes_to_demi(conn, root):
    result = fields.ad(make(conn, root, season=("Демісезон",)), root)
    assert result.leaf.id == 1000633


def test_empty_season_is_read_from_text(conn, root):
    winter = fields.ad(make(conn, root, text=TEXT, season=()), root)
    assert winter.leaf.id == 1000637  # «на хутрі» → зима за текстом (rules.season)


def test_price_is_last_price_in_text_not_shafa_price(conn, root):
    text = "Черевики Ecco 38 розміру. Була 1500грн, тепер 1200грн"
    assert fields.ad(make(conn, root, text=text), root).price == 1200


def test_half_size_goes_to_both(conn, root):
    result = fields.ad(make(conn, root, sizes=("38,5",)), root)
    assert result.sizes == (38, 39)


def test_sizes_for():
    assert fields.sizes_for(["38", "38,5"]) == [38, 39]
    assert fields.sizes_for(["41"]) == [41]
    assert fields.sizes_for(["XL"]) is None
    assert fields.sizes_for(["37,5", "39"]) == [37, 38, 39]


def test_description_is_first_sentence_then_card_description(conn, root):
    result = fields.ad(make(conn, root), root)
    first, _, rest = result.text.partition("\n\n")
    assert first == "Шкіряні черевики Ecco 38 розміру."
    assert rest.startswith("На хутрі.")
    assert "1200" not in result.text  # ціни в описі немає


def test_description_price_in_first_sentence_is_removed(conn, root):
    text = "Черевики Ecco 38 розміру 1200грн. На хутрі, стан ідеальний"
    result = fields.ad(make(conn, root, text=text), root)
    assert result.text.startswith("Черевики Ecco 38 розміру.\n\n")
    assert "1200" not in result.text


def test_description_uses_card_edit(conn, root):
    c = make(conn, root)
    from shafa import review as rv

    assert (
        fields.description(c.post_text, "Мій опис.")
        == "Шкіряні черевики Ecco 38 розміру.\n\nМій опис."
    )
    assert rv  # правка опису в Картці = `description` Картки: ту саму функцію бере `ad`


def test_sex_only_for_kids(conn, root):
    kids = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
    girl = make(
        conn,
        root,
        section=kids,
        season=(),
        sizes=("30",),
        text="Кросівки для дівчинки 30 розміру. 300грн",
    )
    result = fields.ad(girl, root)
    assert result.leaf.id == 1000397
    assert result.sex == fields.SEX_GIRL


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Кросівки для дівчинки 30 розміру. 300грн", 0),
        ("Кросівки для хлопчика 30 розміру. 300грн", 1),
        ("Кросівки 30 розміру. 300грн", 2),
    ],
)
def test_sex_values(text, want):
    assert fields.sex_for(text) == want


def test_adult_never_gets_sex_even_if_text_says_girl(conn, root):
    text = "Черевики для дівчинки 38 розміру. 300грн"
    assert fields.ad(make(conn, root, text=text), root).sex is None


def test_bag_has_no_size_and_no_sex(conn, root):
    bag = make(
        conn, root, section="Аксесуари / Сумки та рюкзаки / Сумки", sizes=(), season=(),
        text="Сумка Zara. Майже нова. 400грн",
    )  # fmt: skip
    result = fields.ad(bag, root)
    assert result.leaf.id == 1000288
    assert result.sizes == ()
    assert result.sex is None


def test_men_bag_by_gender(conn, root):
    bag = make(
        conn, root, section="Аксесуари / Сумки та рюкзаки / Сумки", sizes=(), season=(),
        gender="Для чоловіків", text="Сумка Zara. Майже нова. 400грн",
    )  # fmt: skip
    assert fields.ad(bag, root).leaf.id == 1000990


def test_new_condition(conn, root):
    c = make(conn, root, text="Нові черевики Ecco 38 розміру. З етикеткою. 1200грн")
    assert fields.ad(c, root).condition == fields.CONDITION_NEW


@pytest.mark.parametrize(
    ("colors", "want"),
    [
        (["Чорний"], 7),
        (["Білий", "Чорний"], 8),  # два — перший
        (["Білий", "Чорний", "Червоний"], 99),  # три й більше — «Разноцветное»
        (["Різнокольоровий"], 99),
        (["Персиковий"], None),  # немає відповідника — не ставимо
        ([], None),
        (["Бордовий"], 17),
        (["М'ятний"], 3),
        (["Графітовий"], 6),
    ],
)
def test_color_key(colors, want):
    assert fields.color_key(colors) == want


def test_no_brand_is_none(conn, root):
    assert fields.ad(make(conn, root, brand=None), root).brand is None


# --- Skip ---


def test_skip_no_category(conn, root):
    belt = make(conn, root, section="Аксесуари / Ремені та пояси / Ремені", sizes=())
    result = fields.ad(belt, root)
    assert isinstance(result, Skip)
    assert result.code == "klubok_no_category"


def test_skip_no_price(conn, root):
    result = fields.ad(make(conn, root, text="Черевики Ecco 38 розміру. На хутрі"), root)
    assert isinstance(result, Skip)
    assert result.code == "klubok_no_price"


def test_skip_title_long(conn, root):
    result = fields.ad(make(conn, root, title="Дуже довга назва " * 8), root)
    assert isinstance(result, Skip)
    assert result.code == "klubok_title_long"


def test_title_of_exactly_100_is_ok(conn, root):
    assert isinstance(fields.ad(make(conn, root, title="а" * 100), root), KlubokAd)


def test_skip_size_missing(conn, root):
    result = fields.ad(make(conn, root, sizes=()), root)
    assert isinstance(result, Skip)
    assert result.code == "klubok_size"


def test_skip_size_not_in_leaf_list(conn, root):
    result = fields.ad(make(conn, root, sizes=("50",)), root)  # у знімку-фікстурі розміри 20…46
    assert isinstance(result, Skip)
    assert result.code == "klubok_size"
    assert "50" in result.text


def test_skip_no_photos(conn, root):
    c = make(conn, root)
    for file in (root / "1342").iterdir():
        file.unlink()
    result = fields.ad(c, root)
    assert isinstance(result, Skip)
    assert result.code == "klubok_no_photos"


def test_photos_capped_at_15(conn, root):
    c = make(conn, root)
    from dataclasses import replace

    many = [replace(c.photos[0], position=i, file_name=f"P{i}.JPG") for i in range(1, 21)]
    for p in many:
        (root / "1342" / p.file_name).write_bytes(b"x")
    result = fields.ad(replace(c, photos=many), root)
    assert len(result.photos) == 15
    assert result.photos[0].name == "P1.JPG"
