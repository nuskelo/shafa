"""CORE-020: літнє взуття чекає весни — на ШІ лише з дати з налаштувань."""

from datetime import date, datetime, timedelta, timezone

import pytest

from shafa import batch, config, summer, web
from shafa.db import Status
from test_batch import TODAY as OCTOBER
from test_fill import add_item
from test_options import form
from test_web import PORT, TOKEN, get, html, plain, post

KYIV = timezone(timedelta(hours=3))
NOVEMBER = datetime(2026, 11, 3, 12, 0, tzinfo=KYIV)
MARCH = datetime(2027, 3, 1, 12, 0, tzinfo=KYIV)
SANDALS = "Босоніжки Zara 38 розмір. Шкіра. 300грн"


# --- Ознака «літня пара» ---


@pytest.mark.parametrize(
    "text",
    [
        "Босоніжки Zara 38 розмір. 300грн",
        "Босоножки кожаные 37, ідуть на 36",
        "Нові шльопанці!! 40 розмір.",
        "В'єтнамки Havaianas 39",
        "Вʼєтнамки сині 38",
        "Сандалі шкіряні 36",
        "Сандалії дитячі 28",
        "Сабо шкіряні 38. Устілка 24см",
        "Crocs сині W7 350грн",
        "Крокси жовті 37",
        "Балетки шкіряні 37, ідуть на 36",
        "Мюлі шкіряні 38",
        "Еспадрильї 37, на платформі",
        "Аквашузи 40. Нові",
        "Сабо, шльопанці і т.д. Нові. 500грн",
        "Нові! Босоніжки Aldo 38",  # у першому реченні типу немає — шукаємо в усьому тексті
    ],
)
def test_summer_shoes(text):
    assert summer.is_summer(text)


@pytest.mark.parametrize(
    "text",
    [
        "Утеплені босоніжки 38",  # ознака зими
        "Сабо Crocs зимові з хутром 37",
        "Балетки на флісі 36",
        "Шльопанці на хутрі 39",
        "Кросівки Nike 38. Замість босоніжок на літо",  # перший тип — кросівки
        "Кеди-сабо 37",
        "Черевики 38, під сандалі не підходять",
        "Туфлі Zara 37, балетки в комплекті не йдуть",
        "Сумка шкіряна, колір як босоніжки",
        "Черевики Ecco 40. Мембрана",
        "Рукавички шкіряні",
        "",
        "Просто текст без речі",
    ],
)
def test_not_summer(text):
    assert not summer.is_summer(text)


# --- Дати ---


@pytest.mark.parametrize(
    ("raw", "point"),
    [("01.03", (3, 1)), ("1.3", (3, 1)), (" 15.10 ", (10, 15)), ("29.02", (2, 29))],
)
def test_parse(raw, point):
    assert summer.parse(raw) == point


@pytest.mark.parametrize("raw", ["", "03.01.2027", "31.02", "32.01", "00.03", "1 березня", "01-03"])
def test_parse_rejects(raw):
    with pytest.raises(summer.DateError):
        summer.parse(raw)


@pytest.mark.parametrize(
    ("day", "until", "waits"),
    [
        (date(2026, 8, 31), (3, 1), False),  # до 1 вересня — літнє йде як звичайно
        (date(2026, 9, 1), (3, 1), True),
        (date(2026, 12, 31), (3, 1), True),
        (date(2027, 1, 1), (3, 1), True),
        (date(2027, 2, 28), (3, 1), True),
        (date(2027, 3, 1), (3, 1), False),  # у день дати — вже йде
        (date(2027, 7, 15), (3, 1), False),
        (date(2026, 10, 14), (10, 15), True),  # дата в тій самій осені
        (date(2026, 10, 15), (10, 15), False),
        (date(2026, 11, 3), (10, 15), False),
        (date(2026, 11, 3), (9, 1), False),  # дата = початку — відкладати нічого
        (date(2026, 11, 3), None, False),  # порожнє налаштування
    ],
)
def test_waiting_window(day, until, waits):
    assert summer.waiting(day, until) is waits


# --- Черга ---


def set_text(conn, post_id, text):
    with conn:
        conn.execute("UPDATE posts SET text = ? WHERE post_id = ?", (text, post_id))


def two_cards(conn, root, summer_text=SANDALS):
    """Літня й звичайна старі Картки `new` (пости з 2025 року, чекання 7 днів минуло)."""
    sandals = add_item(conn, root, folder="1", post_id=1)
    sneakers = add_item(conn, root, folder="2", post_id=2)
    set_text(conn, 1, summer_text)
    set_text(conn, 2, "Кросівки Nike 38 розмір. 500грн")
    return sandals, sneakers


def ids(queue):
    return [r["id"] for r in queue.ready]


def test_november_summer_is_not_ready(conn, root):
    _, sneakers = two_cards(conn, root)
    q = batch.queue(conn, NOVEMBER, 7, (3, 1))
    assert ids(q) == [sneakers]
    assert (q.summer, q.young) == (1, 0)


def test_first_of_march_summer_is_ready(conn, root):
    sandals, sneakers = two_cards(conn, root)
    q = batch.queue(conn, MARCH, 7, (3, 1))
    assert sorted(ids(q)) == [sandals, sneakers]
    assert q.summer == 0


def test_day_before_date_still_waits(conn, root):
    two_cards(conn, root)
    q = batch.queue(conn, datetime(2027, 2, 28, 12, 0, tzinfo=KYIV), 7, (3, 1))
    assert q.summer == 1


def test_empty_setting_does_not_hold_summer(conn, root):
    sandals, sneakers = two_cards(conn, root)
    q = batch.queue(conn, NOVEMBER, 7, None)
    assert sorted(ids(q)) == [sandals, sneakers]
    assert q.summer == 0


def test_winter_sandals_are_not_held(conn, root):
    sandals, _ = two_cards(conn, root, "Утеплені босоніжки 38 розмір. На хутрі")
    assert sandals in ids(batch.queue(conn, NOVEMBER, 7, (3, 1)))


def test_refill_card_is_not_held(conn, root):
    sandals, _ = two_cards(conn, root)
    with conn:
        conn.execute("UPDATE items SET refill_at = 'x' WHERE id = ?", (sandals,))
    q = batch.queue(conn, NOVEMBER, 7, (3, 1))
    assert sandals in ids(q) and q.summer == 0


def test_only_new_cards_are_touched(conn, root):
    sandals, _ = two_cards(conn, root)
    with conn:
        conn.execute("UPDATE items SET status = ? WHERE id = ?", (Status.REVIEW, sandals))
    q = batch.queue(conn, NOVEMBER, 7, (3, 1))
    assert q.summer == 0  # уже заповнена: ні в черзі, ні серед відкладених


def test_young_bot_post_waits_for_both_rules(conn, root):
    """Новий літній пост: 7 днів і літня дата діють разом."""
    sandals, _ = two_cards(conn, root)
    with conn:
        conn.execute("UPDATE posts SET date = '2026-11-01T10:00:00' WHERE post_id = 1")
    assert sandals not in ids(batch.queue(conn, NOVEMBER, 7, None))  # молодий
    assert sandals not in ids(batch.queue(conn, NOVEMBER, 7, (3, 1)))  # і літній
    assert batch.queue(conn, NOVEMBER, 7, (3, 1)).young == 0  # літні в «молодих» не рахуються
    after = datetime(2027, 3, 2, 12, 0, tzinfo=KYIV)
    assert sandals in ids(batch.queue(conn, after, 7, (3, 1)))


def test_prepare_does_not_send_summer(conn, root):
    _, sneakers = two_cards(conn, root)
    lim = batch.Limits(summer_until=(3, 1))
    plan = batch.prepare(conn, root, NOVEMBER, lim)
    assert [p.item_id for p in plan.planned] == [sneakers]
    assert plan.summer == 1
    assert batch.prepare(conn, root, MARCH, lim).summer == 0


def test_window_starts_on_first_of_september(conn, root):
    """Рішення Власника: з 1 вересня літнє вже чекає; 31 серпня — ще йде."""
    two_cards(conn, root)
    assert batch.queue(conn, OCTOBER, 7, (3, 1)).summer == 1
    assert batch.queue(conn, datetime(2026, 8, 31, 12, tzinfo=KYIV), 7, (3, 1)).summer == 0


# --- Налаштування ---


@pytest.fixture
def options_app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def test_setting_default_off_and_bad(monkeypatch):
    assert batch.limits().summer_until == (3, 1)
    monkeypatch.setenv(summer.SETTING, "15.10")
    assert batch.limits().summer_until == (10, 15)
    monkeypatch.setenv(summer.SETTING, "off")
    assert batch.limits().summer_until is None
    monkeypatch.setenv(summer.SETTING, "завтра")
    with pytest.raises(batch.SetupError, match="SHAFA_SUMMER_FROM"):
        batch.limits()


def test_options_page_shows_default_date(options_app):
    page = html(get(options_app, "/settings"))
    assert 'name="SHAFA_SUMMER_FROM" value="01.03"' in page
    assert "Літнє взуття (босоніжки, шльопанці…) — на Шафу з" in page


def test_options_save_date_and_empty(options_app, root):
    post(options_app, "/settings", **form(root, SHAFA_SUMMER_FROM="1.4"))
    assert batch.limits().summer_until == (4, 1)
    assert config.setting(summer.SETTING) == "01.04"

    page = html(get(options_app, "/settings"))
    assert 'name="SHAFA_SUMMER_FROM" value="01.04"' in page

    post(options_app, "/settings", **form(root, SHAFA_SUMMER_FROM=""))
    assert batch.limits().summer_until is None  # порожньо — не відкладати, а не «типово»
    assert 'name="SHAFA_SUMMER_FROM" value=""' in html(get(options_app, "/settings"))


@pytest.mark.parametrize("value", ["31.02", "березень", "01.03.2027", "13.13"])
def test_options_bad_date_saves_nothing(options_app, root, value):
    page = html(post(options_app, "/settings", **form(root, SHAFA_SUMMER_FROM=value)))
    assert "Не збережено" in page and f'value="{value}"' in page
    assert config.setting("SHAFA_DAILY_LIMIT") is None


# --- Панель ---


@pytest.fixture
def november_app(conn, tmp_path, root):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        today=lambda: NOVEMBER,
    )  # fmt: skip


def test_panel_counts_summer_separately(conn, root, november_app):
    two_cards(conn, root)
    page = html(get(november_app, "/review"))
    assert "Літнє взуття чекає до 01.03: 1" in page
    assert "Готові йти на ШІ 1" in plain(page)  # звичайна — так, літня — ні


def test_panel_no_summer_line_when_nothing_waits(conn, root, november_app, monkeypatch):
    two_cards(conn, root)
    monkeypatch.setenv(summer.SETTING, "off")
    page = html(get(november_app, "/review"))
    assert "Літнє взуття чекає" not in page
    assert "Готові йти на ШІ 2" in plain(page)


def test_summer_text():
    assert batch.summer_text(163, (3, 1)) == "Літнє взуття чекає до 01.03: 163"
    assert batch.summer_text(0, (3, 1)) is None
    assert batch.summer_text(5, None) is None
