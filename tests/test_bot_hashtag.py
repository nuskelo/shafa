import pytest

from shafa.bot.hashtag import ASK_KIND, ASK_SIZE, BAG_TAG, decide


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("Кросівки Lowa 37 розміру в ідеальному стані. 1200грн", "#VS_37_розмір"),
        ("Черевики Ecco в гарному стані. 38 розмір. Устілка 25см. 1500грн", "#VS_38_розмір"),
        ("Кеди. Вказаний 38 розмір. Ідуть на 37. Устілка 24см. 800грн", "#VS_37_розмір"),
        ("Кеди. Вказаний 38 розмір. Йдуть на 37,5. 800грн", "#VS_37_розмір"),
        ("Кеди. Вказаний 38 розмір. Ідуть на 37-38. 800грн", "#VS_38_розмір"),  # рішення: B
        ("Мокасини. 39,5 розмір. 900грн", "#VS_39_розмір"),
        ("Мокасини. 39.5 розмір. 900грн", "#VS_39_розмір"),
        ("Шльопанці в стані нових. 36-37 розмір. 300грн", "#VS_36_розмір"),
        ("Кросівки New Balance у відмінному стані. Розмір 41. 1100грн", "#VS_41_розмір"),
        ("Кросівки в ідеальному стані.42 розмір. 1100грн", "#VS_42_розмір"),
        ("Дитячі черевики 22 розміру. 400грн", "#VS_22_розмір"),
        ("Босоніжки з ремінцем. 37 розмір. 500грн", "#VS_37_розмір"),
    ],
)
def test_size_hashtag_like_hers(text, tag):
    assert decide(text).tag == tag
    assert decide(text).ask is None


@pytest.mark.parametrize(
    "text",
    [
        # вада живого прогону: перше слово «Тест», а габарити «Розмір 30см*20см» — не розмір
        "Тест 5: сумка шкіряна Furla. Розмір 30см*20см. 1800грн",
        "Шкіряна сумка Furla в ідеальному стані. Розмір 30см*20см. 1500грн",
        "Сумка шкіряна. Розмір 30х20 см. 900грн",
        "Сумочка через плече. Розмір 25*15см. 700грн",
        "Гаманець шкіряний. 400грн",
        "Новий рюкзак Kanken. 1200грн",
        "Ремінь шкіряний. 300грн",
        "Оригінал Michael Kors, шкіряна сумка-шопер. 2500грн",
        "Furla. Шкіряна сумка в ідеальному стані. 1500грн",  # слово-річ не в першому реченні
    ],
)
def test_bags(text):
    decision = decide(text)
    assert (decision.tag, decision.ask) == (BAG_TAG, None)
    assert decision.note == "хештег: #VS_сумки"


@pytest.mark.parametrize(
    ("text", "ask"),
    [
        ("Кросівки Nike в ідеальному стані. 1200грн", ASK_SIZE),  # взуття без розміру
        ("Кросівки Nike. 88 розмір. 1200грн", ASK_SIZE),  # поза 15–50
        ("Босоніжки з ремінцем. 500грн", ASK_SIZE),
        ("Тест 6: щось гарне. 500грн", ASK_KIND),  # річ не впізнано
        ("Furla, ідеальний стан. 1500грн", ASK_KIND),
    ],
)
def test_never_silently_without_hashtag(text, ask):
    decision = decide(text)
    assert (decision.tag, decision.ask) == (None, ask)


@pytest.mark.parametrize("text", ["Рукавиці шкіряні. 300грн", "Шарф кашеміровий. 500грн"])
def test_other_things_explicitly_without_hashtag(text):
    decision = decide(text)
    assert (decision.tag, decision.ask) == (None, None)
    assert decision.note.startswith("без хештега")


@pytest.mark.parametrize(
    ("text", "note"),
    [
        ("Кросівки Lowa 37 розміру. 1200грн\n#VS_37_розмір", "хештег твій: #VS_37_розмір"),
        ("Сумка. 900грн #VS_сумки", "хештег твій: #VS_сумки"),
    ],
)
def test_her_own_hashtag_not_duplicated(text, note):
    assert decide(text) == decide(text).__class__(None, note, None)
