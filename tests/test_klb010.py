"""KLB-010: Клубок на екрані = Шафа — дублі, хвилі, Експорт іншого чату, черга завжди актуальна."""

import pytest

from shafa import events, review
from shafa.db import Status
from shafa.klubok import dup, queue, refresh, waves
from test_klb002_queue import TODAY, ad_row, add, row, snapshot, states
from test_klb008 import klubok_card


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


# --- F10: хвилі ---

MITTENS = "Нові зимові рукавиці Snowlife Швейцарія. На 6-10 років. Дуже теплі. 650грн"
SLIPPERS = (
    "Нові натуральні тапочки балетки. Вказаний розмір 4/5. Ідуть на 36. Всередині хутро. 700грн"
)


@pytest.mark.parametrize(
    ("text", "kind", "wave"),
    [
        (MITTENS, "одяг", None),
        (SLIPPERS, "домашнє", 4),
        ("Хутряні тапочки UGG в новому стані. 37 розмір", "домашнє", 4),
        ("Хутряна жилетка Zana Італія.", "одяг", None),
        ("Кросівки Nike в ідеальному стані, гарно під джинси. 41 розмір", "спорт", 4),
        ("Зимові черевики на хутрі Ecco. 38 розмір", "зима", 1),
        ("Шкіряна сумка з шарфом у подарунок", "сумки", 1),
    ],
)
def test_wave_kind(text, kind, wave):
    assert (waves.kind(text), waves.wave_of(waves.kind(text))) == (kind, wave)


def test_reclassify_moves_slippers_and_archives_mittens_once(conn, root, monkeypatch):
    said = []
    monkeypatch.setattr(events, "emit", lambda *a, **k: said.append((a, k)))
    mittens = klubok_card(conn, root, 3800, text=MITTENS)
    slippers = klubok_card(conn, root, 4045, text=SLIPPERS)

    done = refresh.reclassify(conn)

    assert done.dropped == [3800] and done.archived == [mittens]
    assert done.moved == [(4045, "зима/1", "домашнє/4")]
    card = conn.execute(
        "SELECT status, problem_code FROM items WHERE id = ?", (mittens,)
    ).fetchone()
    assert tuple(card) == (Status.ARCHIVED, review.NOT_FOR_KLUBOK)
    assert conn.execute("SELECT status FROM items WHERE id = ?", (slippers,)).fetchone()[0] == (
        Status.REVIEW
    )
    assert queue.wave(conn, 3800) is None and queue.wave(conn, 4045) == 4
    assert said[0][0][0] == "klubok.waves"
    assert refresh.reclassify(conn) == refresh.Reclassified()  # повтор — нічого


def test_reclassify_leaves_card_already_on_klubok(conn, root):
    mittens = klubok_card(conn, root, 3800, text=MITTENS, status=Status.APPROVED)
    ad_row(conn, mittens, queue.PUBLISHED, klubok_id=7)
    assert refresh.reclassify(conn).archived == []


# --- F3: черга завжди актуальна ---


def test_refresh_writes_queue_without_button(conn, root):
    item_id = add(conn, root, 7301)
    assert queue.unsynced(conn) == 1
    assert refresh.refresh(conn, root, TODAY) == 1
    assert queue.unsynced(conn) == 0 and row(conn, item_id)["state"] == queue.QUEUED


def test_refresh_does_nothing_to_queue_when_klubok_off(conn, root, monkeypatch):
    monkeypatch.delenv(queue.FLAG)
    add(conn, root, 7301)
    assert refresh.refresh(conn, root, TODAY) is None and queue.unsynced(conn) == 1


def test_background_refresh_merges_requests(conn, root, tmp_path):
    from shafa import db

    path = conn.execute("PRAGMA database_list").fetchone()[2]
    add(conn, root, 7301)
    bg = refresh.Background(lambda: db.connect(path), lambda: root, print)
    bg.start()
    bg.start()
    bg.wait()
    bg.wait()
    assert queue.unsynced(conn) == 0
