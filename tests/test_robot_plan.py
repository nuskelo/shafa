import pytest

from shafa import ai, review
from shafa.db import Status
from shafa.robot.plan import PlanError, Step, insole_choices, plan
from test_review import add_card

BOOTS = "Жіночий одяг / Взуття / Черевики"
TEXT = (
    "Шкіряні черевики Ecco 38 розміру. На хутрі. Мембрана gore-tex. Устілка 24,5 см. "
    "В ідеальному стані. 1200грн"
)
BLANK = [
    ai.Field("title", "Шкіряні черевики ecco 38 розміру"),
    ai.Field("section", BOOTS),
    ai.Field("sizes", ["38", "38,5"]),
    ai.Field("colors", ["Чорний"]),
    ai.Field("brand", "ecco"),
    ai.Field("material_upper", ["Шкіра"]),
    ai.Field("material_lining", ["Хутро"]),
    ai.Field("season", ["Зима"]),
    ai.Field("type", "Челсі"),
    ai.Field("style", "Повсякденний"),
    ai.Field("keywords", ["черевики", "черевики жіночі", "черевики ecco", "черевики 38"]),
    ai.Field("photo_order", ["IMG_2.JPG", "IMG_1.JPG"]),
]


def approved(conn, root, blank=BLANK, text=TEXT, **kw):
    item_id = add_card(conn, root, blank=blank, text=text, status=Status.APPROVED, **kw)
    return review.load_card(conn, item_id)


def by_label(steps):
    return {(s.kind, s.label): s.value for s in steps}


def test_full_plan_for_boots(conn, root):
    steps, notes = plan(approved(conn, root), root)

    assert steps[0] == Step(
        "photos", "Фото", [str(root / "1342" / "IMG_2.JPG"), str(root / "1342" / "IMG_1.JPG")]
    )  # порядок як на екрані перевірки
    assert steps[1].kind == "own_photos"
    got = by_label(steps)
    assert got[("text", "Назва")] == "Шкіряні черевики ecco 38 розміру"
    assert got[("category", "Розділ")] == ["Жіночий одяг", "Взуття", "Черевики"]
    assert next(s for s in steps if s.kind == "category").target == "zhenskaya-obuv/botinki"
    assert got[("condition", "Стан")] == "Ідеальний"
    assert got[("brand", "Бренд")] == "Ecco"  # написання Шафи
    assert got[("size_system", "Розмір")] == "Європейський"
    assert got[("sizes", "Розмір")] == ["38", "38.5"]  # як кнопки на Шафі
    assert got[("colors", "Колір")] == ["Чорний"]
    assert got[("choice", "Матеріал верху")] == ["Шкіра"]
    assert got[("choice", "Матеріал підкладки")] == ["Хутро"]
    assert got[("choice", "Сезон")] == ["Зима"]
    assert got[("choice", "Тип")] == ["Челсі"]
    assert got[("choice", "Стиль")] == ["Повсякденний"]
    assert got[("choice", "Мембрана")] == ["Gore-Tex"]
    assert got[("choice", "Довжина устілки")] == ["24", "24,5"]
    assert got[("text", "Ціна")] == "1380"
    assert steps[-1].target == "price"
    assert notes == []


def test_values_outside_shafa_are_noted_not_filled(conn, root):
    blank = [f for f in BLANK if f.name not in ("brand", "type")] + [
        ai.Field("brand", "Невідомий бренд"),
        ai.Field("type", "Мартінси"),
    ]
    steps, notes = plan(approved(conn, root, blank=blank), root)

    labels = {s.label for s in steps}
    assert "Бренд" not in labels and "Тип" not in labels
    assert "Бренду «Невідомий бренд» немає у списку Шафи — не заповнено (OQ-004)" in notes
    assert "Тип: немає у списку Шафи — Мартінси" in notes


def test_backpack_has_no_sizes_and_gender_is_filled(conn, root):
    blank = [
        ai.Field("title", "Рюкзак шкіряний"),
        ai.Field("section", "Аксесуари / Сумки та рюкзаки / Рюкзаки"),
        ai.Field("colors", ["Коричневий"]),
        ai.Field("gender", "Для жінок"),
    ]
    steps, _ = plan(approved(conn, root, blank=blank, text="Рюкзак шкіряний. 900грн"), root)
    got = by_label(steps)
    assert ("sizes", "Розмір") not in got
    assert got[("choice", "Стать")] == ["Для жінок"]


def test_kids_category_and_babies_ukrainian_sizes(conn, root):
    girls = [
        ai.Field("title", "Кросівки 30 розміру"),
        ai.Field("section", "Дитячий одяг / Для дівчаток / Взуття / Кросівки"),
        ai.Field("sizes", ["30"]),
        ai.Field("colors", ["Рожевий"]),
        ai.Field("teen", True),
    ]
    got = by_label(plan(approved(conn, root, blank=girls), root)[0])
    assert got[("choice", "Категорія")] == ["Для підлітків"]

    babies = [
        ai.Field("title", "Кросівки 18 розміру"),
        ai.Field("section", "Дитячий одяг / Для малюків / Взуття / Кросівки"),
        ai.Field("sizes", ["18,5"]),
        ai.Field("colors", ["Синій"]),
        ai.Field("gender", "Унісекс"),
    ]
    card = approved(conn, root, blank=babies, folder="1349", post_id=1349)
    got = by_label(plan(card, root)[0])
    assert got[("size_system", "Розмір")] == "Український"
    assert got[("sizes", "Розмір")] == ["18.5"]
    assert got[("choice", "Стать")] == ["Унісекс"]


@pytest.mark.parametrize(
    ("status", "message"), [(Status.REVIEW, "не схвалена"), (Status.PROBLEM, "не схвалена")]
)
def test_only_approved_cards(conn, root, status, message):
    item_id = add_card(conn, root, blank=BLANK, text=TEXT, status=status)
    with pytest.raises(PlanError, match=message):
        plan(review.load_card(conn, item_id), root)


def test_missing_photo_stops_plan(conn, root):
    card = approved(conn, root)
    (root / "1342" / "IMG_1.JPG").unlink()
    with pytest.raises(PlanError, match="Немає фото: IMG_1.JPG"):
        plan(card, root)


def test_card_not_ready_stops_plan(conn, root):
    blank = [f for f in BLANK if f.name != "colors"]
    item_id = add_card(conn, root, blank=blank, text=TEXT, status=Status.APPROVED)
    with pytest.raises(PlanError, match="no_colors"):
        plan(review.load_card(conn, item_id), root)


@pytest.mark.parametrize(
    ("lengths", "choices", "expected"),
    [
        ([24.0, 24.5], ["24", "24,5", "25"], ["24", "24,5"]),
        ([25.5], ["25,5 см", "26 см"], ["25,5 см"]),
        ([40.0], ["25,5 см"], []),
        ([22.5, 23.0, 23.5], ["22,5", "23", "23,5"], ["22,5", "23"]),
    ],
)
def test_insole_choices(lengths, choices, expected):
    assert insole_choices(lengths, choices, limit=2) == expected
