"""KLB-010: Клубок на екрані = Шафа — дублі, хвилі, Експорт іншого чату, черга завжди актуальна."""

import pytest

from shafa.klubok import dup, queue
from test_klb002_queue import TODAY, add, snapshot, states


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def facts(name, brand=None, price=1200, sizes=()):
    return dup.Facts(name=name, sizes=tuple(sizes), brand=brand, price=price, group=None)


# --- F11 / N18: дублі ---


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        # різні номери моделі — інша пара
        (facts("Кросівки New Balance 237 44,5 розміру", "New Balance"),
         facts("Кросівки New Balance 530 оригінал в ідеальному стані"), False),
        (facts("Кросівки Asics Gel-1130 38 розміру", "Asics"),
         facts("Кросівки Asics 1130 38 розміру"), True),
        # без бренду: інший підтип — ні; той самий підтип і ціна ±25 % — так; ±33 % — ні
        (facts("Новий шкіряний рюкзак"), facts("Шкіряна сумка в ідеальному стані"), False),
        (facts("Шкіряна сумка в новому стані", price=1200),
         facts("Шкіряна сумка Італія", price=1300), True),
        (facts("Шкіряна сумка в новому стані", price=1200),
         facts("Шкіряна сумка Італія", price=1800), False),
        (facts("Шкіряні черевики в ідеальному стані 37 розміру", sizes=["37"]),
         facts("Шкіряна сумка 37"), False),
        # не звужуємо: «нові» ≈ «в ідеальному стані», 38,5 ≈ 38, з брендом ціна ±40 %
        (facts("Нові кросівки Pavers 45 розміру", "Pavers", sizes=["45"]),
         facts("Шкіряні кросівки Pavers в ідеальному стані 45 розміру", sizes=["45"]), True),
        (facts("Термо черевики Peter Storm 38,5 розміру", "Peter Storm", sizes=["38,5"]),
         facts("Термо черевики Peter Storm 38 розміру", sizes=["38"]), True),
        (facts("Черевики Ecco 38 розміру", "Ecco", price=1000, sizes=["38"]),
         facts("Черевики Ecco 38 розміру", price=1350, sizes=["38"]), True),
    ],
)  # fmt: skip
def test_duplicate_rule(ours, theirs, expected):
    assert dup.same(ours, theirs, brand_in_name=True) is expected


def test_model_numbers_skip_sizes_years_insole_and_price():
    title = "Кросівки Nike Air Max 90 38-39 розміру 2019 року, устілка 24,5 см, 1200грн"
    assert dup.model_numbers(title) == set()
    assert dup.model_numbers("New Balance 574 classic") == {"574"}


def test_other_model_of_same_brand_goes_to_queue(conn, root):
    item_id = add(
        conn, root, 7301, brand="New Balance", sizes=("44,5",), section="Чоловічий одяг / Взуття / "
        "Кросівки", title="Кросівки New Balance 237 44,5 розміру в ідеальному стані",
    )  # fmt: skip
    snapshot(conn, listings=[("Кросівки New Balance 530 оригінал 44,5 розміру", 1200)])
    assert states(queue.plan(conn, root, TODAY))[item_id] == (queue.QUEUED, None)
