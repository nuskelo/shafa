"""BOT-016: «📦 Усі лише в чергу Шафи» у зведенні чернеток. Підставний Telegram, усе в tmp_path."""

import pytest

from shafa.bot import channel, core
from shafa.bot.core import Bot
from shafa.bot.telegram import TelegramError
from test_bot015 import DATE, Telegram, posts, sent_to_channel
from test_bot_core import (
    CHANNEL,
    OWNER,
    SELLER,
    Clock,
    Settings,
    Updates,
    press,
    press_all,
    state,
    summary,
    three_previews,
)

TEXTS = [
    "Кросівки Lowa 37 розміру. 1200грн",
    "Черевики Ecco 38 розмір. 1500грн",
    "Шкіряна сумка Furla. 1800грн",
]


@pytest.fixture
def tg():
    return Telegram()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def upd():
    return Updates()


@pytest.fixture
def bot(conn, tg, root, clock):
    settings = Settings(CHANNEL, SELLER, OWNER, root, -1009990001112)
    return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)


@pytest.fixture(autouse=True)
def site_on(monkeypatch):
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")


def old_post(conn):
    """Є від чого відштовхнутись перегляду каналу."""
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    conn.commit()


def in_channel(tg, numbers=(7001, 7002, 7003), texts=TEXTS):
    tg.channel = {n: (f"{t}\n#VS_37_розмір", DATE + i * 60)
                  for i, (n, t) in enumerate(zip(numbers, texts))}  # fmt: skip


def queue_all(upd, bot, tg, conn):
    return press_all(upd, bot, tg, conn, action="queueall")


def last_text(tg):
    return tg.texts(SELLER)[-1]


def test_summary_has_third_button_old_two_unchanged(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    (shown,) = [p for p in summary(tg)]
    assert shown["markup"]["inline_keyboard"] == [
        [
            {"text": "Опублікувати всі", "callback_data": "puball:0"},
            {"text": "Скасувати всі", "callback_data": "cancelall:0"},
        ],
        [{"text": "📦 Усі лише в чергу Шафи", "callback_data": "queueall:0"}],
    ]


def test_all_found_queue_all_without_touching_channel_twice(conn, bot, tg, upd, clock, root):
    old_post(conn)
    in_channel(tg)
    three_previews(upd, bot, tg, clock)

    queue_all(upd, bot, tg, conn)

    assert [state(conn, i) for i in (1, 2, 3)] == ["queued"] * 3
    rows = posts(conn)
    assert [(r["post_id"], r["source"]) for r in rows][1:] == [(7001, "bot"), (7002, "bot"),
                                                               (7003, "bot")]  # fmt: skip
    assert [r["date"] for r in rows][1:] == [core.local_date(DATE + i * 60) for i in range(3)]
    for number in (7001, 7002, 7003):
        assert sorted(p.name for p in (root / str(number)).iterdir()) == ["1.jpg", "2.jpg"]
    jobs = conn.execute("SELECT post_id, state FROM site_jobs ORDER BY post_id").fetchall()
    assert [tuple(j) for j in jobs] == [(7001, "waiting"), (7002, "waiting"), (7003, "waiting")]
    assert sent_to_channel(tg) == [] and tg.channel_posts == []  # у канал нічого
    assert len(tg.forwards) == len(set(tg.forwards))  # канал переглянуто один раз
    assert len(tg.deleted()) == tg.forwarded_copies() > 0  # копії не лишились
    texts = tg.texts(SELLER)
    assert any(t.startswith("Шукаю 3 постів у каналі") and "до 5 хв" in t for t in texts)
    assert texts[-1] == "У черзі Шафи: 3 (7001, 7002, 7003)."
    assert not any(t.startswith("Пост 70") for t in texts)  # одиничних «Пост N … у черзі» немає
    assert conn.execute("SELECT count(*) FROM bot_summaries").fetchone()[0] == 0


def test_second_and_next_drafts_come_from_memory(conn, bot, tg, upd, clock):
    old_post(conn)
    for n, text in zip((7001, 7002, 7003), TEXTS):
        channel.remember(conn, n, DATE, f"{text}\n#VS_37_розмір")
    three_previews(upd, bot, tg, clock)

    queue_all(upd, bot, tg, conn)

    assert [state(conn, i) for i in (1, 2, 3)] == ["queued"] * 3
    assert tg.forwards == []  # усе вже в `channel_posts` — канал не чіпали


def test_not_found_stays_preview_and_is_named(conn, bot, tg, upd, clock):
    old_post(conn)
    in_channel(tg, numbers=(7001, 7002), texts=TEXTS[:2])  # сумки в каналі немає
    three_previews(upd, bot, tg, clock)

    queue_all(upd, bot, tg, conn)

    assert [state(conn, i) for i in (1, 2, 3)] == ["queued", "queued", "preview"]
    assert len(tg.forwards) == len(set(tg.forwards))  # навіть не знайдена — без другого перегляду
    answer = last_text(tg)
    assert "У черзі Шафи: 2 (7001, 7002)." in answer
    assert "Не знайшов: 1 — «Шкіряна сумка Furla. 1800грн» — перешли ці пости з каналу" in answer
    assert "натисни кнопку в їхньому прев'ю" in answer
    assert [r["post_id"] for r in posts(conn)] == [7000, 7001, 7002]
    assert not (conn.execute("SELECT 1 FROM site_jobs WHERE post_id = 7003").fetchone())


def test_second_press_while_running_does_nothing(conn, bot, tg, upd, clock):
    old_post(conn)
    in_channel(tg)
    three_previews(upd, bot, tg, clock)
    with conn:
        conn.execute("UPDATE bot_summaries SET running = 1")  # перше натискання ще триває

    queue_all(upd, bot, tg, conn)

    assert tg.answers()[-1] == "Уже виконую — зачекай"
    assert [state(conn, i) for i in (1, 2, 3)] == ["preview"] * 3
    assert tg.forwards == [] and posts(conn)[-1]["post_id"] == 7000


def test_browse_dropped_stops_search_and_keeps_buttons(conn, bot, tg, upd, clock):
    old_post(conn)
    in_channel(tg)
    tg.forward_errors = {  # Telegram не відповідає: кожен запит — збій, не «немає поста»
        n: [TelegramError("x", code=502, uncertain=True)] * 3 for n in range(7001, 7100)
    }
    three_previews(upd, bot, tg, clock)

    queue_all(upd, bot, tg, conn)

    assert [state(conn, i) for i in (1, 2, 3)] == ["preview"] * 3
    done = len(tg.forwards)
    assert 0 < done < 20  # перегляд кинуто, решту чернеток не шукали заново
    answer = last_text(tg)
    assert answer.startswith("Telegram не відповідає — спробуй пізніше. Не шукав ще 3")
    assert "У черзі Шафи" not in answer
    assert posts(conn)[-1]["post_id"] == 7000 and sent_to_channel(tg) == []


def test_editing_draft_is_skipped_and_named(conn, bot, tg, upd, clock):
    old_post(conn)
    in_channel(tg, numbers=(7001, 7002), texts=TEXTS[:2])
    three_previews(upd, bot, tg, clock)
    press(upd, bot, tg, "edit", draft_id=3, preview=conn.execute(
        "SELECT preview_message_id FROM bot_drafts WHERE id = 3").fetchone()[0])  # fmt: skip
    assert state(conn, 3) == "editing"

    queue_all(upd, bot, tg, conn)

    assert [state(conn, i) for i in (1, 2, 3)] == ["queued", "queued", "editing"]
    answer = last_text(tg)
    assert "У черзі Шафи: 2 (7001, 7002)." in answer
    assert "Чекають твоєї відповіді: «Шкіряна сумка Furla. 1800грн»." in answer
    assert "Не знайшов" not in answer


def test_publish_all_still_works(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    press_all(upd, bot, tg, conn)
    assert [state(conn, i) for i in (1, 2, 3)] == ["published"] * 3
