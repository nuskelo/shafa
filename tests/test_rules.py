"""Правила `docs/rules.md` v1.1 на вигаданих текстах у формах справжніх постів."""

import pytest

from shafa.rules import (
    IDEAL,
    NEW,
    clean_description,
    condition,
    first_sentence_end,
    insole_lengths,
    many_items,
    membrane,
    price_from_text,
    shafa_price,
)

SIGNATURE = "\n#VS_38_розмір\n\nПишіть сюди 👉 @prodavchynia_test"


# --- Ціна з тексту ---


@pytest.mark.parametrize(
    ("text", "price"),
    [
        ("Кросівки. 38 розмір. 1800грн", 1800),
        ("Кросівки. 38 розмір. 600грн", 600),
        ("Кросівки. 38 розмір. 600 грн", 600),
        ("Кросівки. 38 розмір. 1100грн.", 1100),
        ("Кросівки. 38 розмір. 650грн.\n#VS_38_розмір", 650),
        ("Шарф. Розмір 48см*178см. 950гр", 950),
        ("Пінетки. 18,5см. 50грн", 50),
        ("Кеди. Ширина устілки 9см.1300грн", 1300),
        ("Кросівки. 1800ГРН", 1800),
        ("Кросівки. 38 розмір. 1800грн" + SIGNATURE, 1800),
    ],
)
def test_price_forms(text, price):
    assert price_from_text(text) == price


def test_last_price_wins_strikethrough_old_before_new():
    # У тексті Експорту закреслена стара ціна стоїть перед новою.
    assert price_from_text("Шльопанці. 19,5см. 350грн 150грн" + SIGNATURE) == 150


def test_last_price_wins_over_bought_for():
    assert price_from_text("Босоніжки. Куплені за 2500грн. Взуті раз. 1800грн") == 1800


@pytest.mark.parametrize(
    "text",
    [
        "Знижка 10 % на всі пари!",
        "Розіграш 5 грудня",
        "Устілка 26,5 гр",
        "Група для покупців",
        "",
    ],
)
def test_no_price(text):
    assert price_from_text(text) is None


# --- Ціна на Шафі ---


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        # Приклади з docs/rules.md
        (600, 690),
        (655, 760),
        (420, 501),
        (850, 1001),
        (1200, 1380),
        # Межі 501
        (382, 440),
        (383, 501),
        (434, 501),
        (435, 510),
        # Межі 1001
        (817, 940),
        (818, 1001),
        (826, 1001),
        (827, 1001),
        (869, 1001),
        (870, 1010),
        (871, 1010),
        (1000, 1150),
        # Ціле ×1,15 — без зайвого десятка через float
        (200, 230),
        (1400, 1610),
    ],
)
def test_shafa_price(price, expected):
    assert shafa_price(price) == expected


# --- Стан ---


@pytest.mark.parametrize(
    "text",
    [
        "Нові кросівки Skechers. 38 розмір.",
        "Новий шкіряний гаманець. Розмір 20см*11см.",
        "Нова сумка. Розмір 30см.",
        "Черевики Ecco в стані нових. 38 розмір.",
        "Спідниця в стані нової. Довжина 60см.",
        "Туфлі в новому стані. 37 розмір.",
        "Туфлі у новому стані. 37 розмір.",
        "Черевики Dr. Martens. 38 розмір. Стан нових.",
    ],
)
def test_condition_new(text):
    assert condition(text) == NEW


@pytest.mark.parametrize(
    "text",
    [
        "Кросівки Adidas в ідеальному стані. 42 розмір.",
        "Лофери у відмінному стані. 37 розмір.",
        "Новорічні капці. 36 розмір.",
        "Відправка Новою поштою. Сумка в ідеальному стані.",
        "Нова пошта щодня. Кеди у відмінному стані.",
        "Нова колекція кедів. 38 розмір.",
        "Верх комбінований. Інноваційна підошва. Резинові вставки.",
    ],
)
def test_condition_ideal(text):
    assert condition(text) == IDEAL


# --- Перше речення й опис ---


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("Кросівки Puma в ідеальному стані. 38 розмір.", "Кросівки Puma в ідеальному стані."),
        ("Черевики Dr. Martens в стані нових. 38 розмір.", "Черевики Dr. Martens в стані нових."),
        ("Сумка 30 см. на 20 см. Нова. 500грн", "Сумка 30 см. на 20 см. Нова."),
        ("Туфлі 38 р. Нові. 500грн", "Туфлі 38 р. Нові."),
        ("Сабо, шльопанці і т.д. Нові. 500грн", "Сабо, шльопанці і т.д. Нові."),
        ("Нові шльопанці!! 40 розмір.", "Нові шльопанці!!"),
        ("Чоботи Clarks\nв ідеальному стані.", "Чоботи Clarks\n"),
        ("Лише назва", "Лише назва"),
    ],
)
def test_first_sentence(text, first):
    assert text[: first_sentence_end(text)] == first


def test_clean_description_full_post():
    text = (
        "Кросівки Skechers в ідеальному стані. Вказаний 41 розмір. Ідуть на 42. "
        "Устілка 27,5см. Ширина устілки 9см. 1100грн" + SIGNATURE
    )
    assert clean_description(text, condition(text)) == (
        "Вказаний 41 розмір. Ідуть на 42. Устілка 27,5см. Ширина устілки 9см. "
        "Ідеальний стан, без дефектів."
    )


def test_clean_description_dr_martens_new():
    text = "Нові черевики Dr. Martens. 38 розмір. Устілка 25см. 2200грн" + SIGNATURE
    assert clean_description(text, NEW) == "38 розмір. Устілка 25см. Стан нових, без дефектів."


def test_clean_description_strikethrough_prices_removed():
    text = "Нові шльопанці. Устілка 19,5см. 350грн 150грн" + SIGNATURE
    assert clean_description(text, NEW) == "Устілка 19,5см. Стан нових, без дефектів."


def test_clean_description_keeps_bought_for():
    text = "Босоніжки. Куплені за 2500грн. Взуті раз. 1800грн\n#VS_39_розмір"
    assert clean_description(text, IDEAL) == (
        "Куплені за 2500грн. Взуті раз. Ідеальний стан, без дефектів."
    )


def test_clean_description_contacts_links_tags():
    text = (
        "Сумка. Розмір 30см*20см.  Дзвоніть +38 067 123 45 67 або 0671234567, "
        "пишіть @shop_test, https://example.invalid/x t.me/test_shop www.example.invalid\n"
        "#VS_сумки #VS_Сумки 900 грн."
    )
    assert clean_description(text, IDEAL) == (
        "Розмір 30см*20см. Дзвоніть або, пишіть,. Ідеальний стан, без дефектів."
    )


def test_clean_description_keeps_existing_state_sentence():
    text = "Шорти. Довжина 40см. Стан нових, без дефектів.\n1500грн" + SIGNATURE
    assert clean_description(text, NEW) == "Довжина 40см. Стан нових, без дефектів."


def test_clean_description_keeps_lines_drops_blank_edges():
    text = "Сумка шкіряна.\nРозмір 30см.\n\n\n\nРемінь   довгий\n900грн\n\n" + SIGNATURE
    assert clean_description(text, IDEAL) == (
        "Розмір 30см.\n\nРемінь довгий. Ідеальний стан, без дефектів."
    )


def test_clean_description_title_only():
    assert clean_description("Нові капці. 300грн", NEW) == "Стан нових, без дефектів."


# --- Устілка ---


@pytest.mark.parametrize(
    ("text", "lengths"),
    [
        ("Устілка 26см. Ширина устілки 9см.", [26.0]),
        ("Устілка 26,5см. Ширина устілки 9,5см.", [26.0, 26.5]),
        ("Устілка 26.5см.", [26.0, 26.5]),
        ("Устілка 26,5 см.", [26.0, 26.5]),
        ("УстілкаN26см.".replace("N", ""), [26.0]),
        ("Устілка12,8см. легенькі.", [12.5, 13.0]),
        ("Устілка 25,8см.", [25.5, 26.0]),
        ("Устілка 24,3см.", [24.0, 24.5]),
        ("Устілка 9,5см.", [9.0, 9.5]),
        ("Устілка до згину на п'ятку 26см, від краю до краю 27,5см.", [26.0, 26.5, 27.0, 27.5]),
        ("Устілка до згину на п’ятку 26см, від краю до краю 27см.", [26.0, 26.5, 27.0]),
        ("Устілка до згину на пʼятку 26,5см, від краю до краю 26,5см.", [26.0, 26.5]),
        (
            "Устілка до згину на п'ятку 26см, від краю до краю 27 Ширина устілки 9см.",
            [26.0, 26.5, 27.0],
        ),
        ("Устілка до згину на пятку 25,8см, від краю до краю 26,4см.", [25.5, 26.0, 26.5]),
        ("Устілка в заглибленні 28,5см, від краю до краю 29см.", [28.5, 29.0]),
        ("Устілка від краю до краю 24см.", [24.0]),
        ("Устілка м'яка з пам'яттю 27см. Ширина устілки 9см.", [27.0]),
        ("Устілка 26см, ширина устілки 9,5см.", [26.0]),
        ("Устілка 22см. Шииина устілки 7см.", [22.0]),
        ("Правий 38 розмір (устілка 24,5см), лівий 39 (устілка 25см).", [24.5, 25.0]),
        ("Устілка 26см, 38 розмір.", [26.0]),
        ("Устілка 23см\nВерх 8см.", [23.0]),
        ("Верх - натуральна шкіра, устілка шкіряна. 40 розмір.", []),
        ("Ширина устілки 9см.", []),
        ("Сумка. Розмір 30см*20см.", []),
    ],
)
def test_insole_lengths(text, lengths):
    assert insole_lengths(text) == lengths


def test_insole_range_filled_with_half_steps():
    assert insole_lengths("Устілка 29см, від краю до краю 30,5см.") == [29.0, 29.5, 30.0, 30.5]


def test_insole_no_range_filter_for_kids():
    assert insole_lengths("Устілка 14см.") == [14.0]


# --- Мембрана ---


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Термо черевики з мембраною gore-tex.", "Gore-Tex"),
        ("Черевики з мембраною GORE-TEX.", "Gore-Tex"),
        ("Черевики Goretex.", "Gore-Tex"),
        ("Черевики Lowa GTX.", "Gore-Tex"),
        ("Черевики Lowa gtx, мембрана.", "Gore-Tex"),
        ("Завдяки мембрані не промокають.", "Є"),
        ("Черевики з мембраною.", "Є"),
        ("Шкіряні черевики.", None),
        ("Модель gtxpro.", None),
    ],
)
def test_membrane(text, value):
    assert membrane(text) == value


# --- Кілька товарів в одному пості ---


def test_many_items_lines_with_own_prices():
    text = (
        "Нові черевики. Є кілька розмірів.\n"
        "* 37 розмір. Устілка 23,5см. 1600грн\n"
        "* 39 розмір. Устілка 25см. 1700грн\n" + SIGNATURE
    )
    assert many_items(text)


@pytest.mark.parametrize(
    "text",
    [
        "Кеди. 38 розмір. 900грн" + SIGNATURE,
        "Шльопанці. Устілка 19,5см. 350грн 150грн" + SIGNATURE,
        "Босоніжки. Куплені за 2500грн. Взуті раз. 1800грн" + SIGNATURE,
        "Нові черевики. Є кілька розмірів.\n* 37 розмір.\n* 39 розмір.\nЦіна 1600грн",
    ],
)
def test_one_item(text):
    assert not many_items(text)


# --- CORE-007 ---


def test_said_gender_and_adult_signs():
    from shafa import rules

    assert rules.said_gender("Жіночі черевики 41 розміру") == rules.WOMEN
    assert rules.said_gender("Кеди для хлопчика") == rules.BOYS
    assert rules.said_gender("Підійдуть і на жіночу, і на чоловічу ногу") is None
    assert rules.said_gender("Кросівки унісекс, чоловіча модель") is None
    assert rules.adult_signs("Ботильйони на каблуку 36 розміру")
    assert not rules.adult_signs("Кросівки 36 розміру")


def test_season_without_fur_is_not_winter():
    from shafa import rules

    assert rules.season("Черевики без хутра, 38 розмір") == (None, None)
    assert rules.season("Кеди 38 розмір") == (None, None)
    assert rules.season("Весняні кросівки") == (["Весна"], None)


@pytest.mark.parametrize(
    ("text", "kid"),
    [
        ("Дитячі кросівки 35", True), ("Кеди для дівчинки 36", True), ("Для хлопчика, 35", True),
        ("Підліткові черевики", True), ("Шкільні туфлі 36", True), ("Кросівки Nike 36", False),
        ("Черевики 37, хороший стан", False), ("Туфлі на дівчину 36", False),
    ],
)  # fmt: skip
def test_kid_words(text, kid):
    from shafa import rules

    assert rules.kid_words(text) is kid


@pytest.mark.parametrize("site_line", ["🛍 Більше взуття на сайті 👈", "🛍 Більше взуття на сайті"])
@pytest.mark.parametrize(
    "comments",
    ["Додаткові фото в коментарях ⬇️", "Більше фото в коментарях ⬇️", "Всі фото в коментарях ⬇️"],
)
def test_clean_description_drops_site_and_comments_lines(site_line, comments):
    """TD-049 (BOT-007): рядки підпису бота не йдуть в опис Шафи."""
    text = f"Кеди 38 розмір. 800грн\nВідправка Новою поштою\n#VS_38_розмір\n\nПишіть сюди 👉 @x\n\n{site_line}\n\n{comments}"

    assert clean_description(text, IDEAL) == "Відправка Новою поштою. Ідеальний стан, без дефектів."
