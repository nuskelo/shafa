"""BOT-015: «📦 Лише в чергу Шафи» — пост уже в каналі; бот пам'ятає пости каналу. Підставний Telegram."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from shafa import db
from shafa.bot import channel, core
from shafa.bot.core import Bot
from shafa.bot.telegram import TelegramError
from test_bot_core import (
    CHANNEL,
    OWNER,
    SELLER,
    STRANGER,
    Clock,
    FakeTelegram,
    Settings,
    Updates,
    jpeg,
    message,
    photo,
    press,
    state,
)

OWN = "Шкіряні кросівки Lowa 37 розміру. 1200грн"
DRAFT = f"{OWN}\n#VS_37_розмір"
DATE = 1_791_000_000  # дата поста в каналі
SERVICE = OWNER  # службовий чат: чат Власника


class Telegram(FakeTelegram):
    """+ `forwardMessage`: канал `posts` (номер → текст) і помилки на замовлення."""

    def __init__(self):
        super().__init__()
        self.channel = {}  # номер → (текст, дата)
        self.forward_errors = {}  # номер → список помилок (по одній на спробу)
        self.forwards = []

    def send_message(self, chat_id, text, markup=None, silent=False):
        return super().send_message(chat_id, text, markup)

    def forward_message(self, chat_id, from_chat_id, message_id):
        self.forwards.append(message_id)
        self.calls.append(("forwardMessage", {"chat_id": chat_id, "message_id": message_id}))
        errors = self.forward_errors.get(message_id)
        if errors:
            raise errors.pop(0)
        if message_id not in self.channel:
            raise TelegramError(
                "x", code=400, description="Bad Request: message to forward not found"
            )
        text, date = self.channel[message_id]
        self.next_id += 1
        return {
            "message_id": self.next_id, "caption": text, "photo": [{"file_id": "x"}],
            "forward_origin": {"type": "channel", "chat": {"id": CHANNEL},
                               "message_id": message_id, "date": date},
        }  # fmt: skip

    def deleted(self):
        return [p["message_id"] for m, p in self.calls if m == "deleteMessage"]

    def forwarded_copies(self):
        return len([n for n in self.forwards if n in self.channel])  # копія є лише в наявних


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


@pytest.fixture
def site_on(monkeypatch):
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")


def draft_with_photos(bot, tg, upd, clock, files=2, text=DRAFT):
    for i in range(1, files + 1):
        tg.files[f"f{i}"] = jpeg(color=("red", "green", "blue")[i % 3])
        bot.handle(upd(message=photo(10 + i, f"f{i}", caption=text if i == 1 else None, group="g")))
    clock.pass_(seconds=4)
    bot.tick()


def channel_post(post_id, text, chat=CHANNEL, date=DATE, **extra):
    return {"message_id": post_id, "chat": {"id": chat, "type": "channel"}, "date": date,
            "caption": text, **extra}  # fmt: skip


def known(conn, post_id, text, date=DATE):
    channel.remember(conn, post_id, date, text)


def posts(conn):
    return conn.execute("SELECT post_id, date, text, source FROM posts").fetchall()


def sent_to_channel(tg):
    return [c for c in tg.calls if c[0] in ("sendPhoto", "editMessageCaption")
            and c[1]["chat_id"] == CHANNEL]  # fmt: skip


def queue(upd, bot, tg):
    press(upd, bot, tg, "queue")


# --- A: кнопка ---


def test_preview_has_queue_button_and_publish_still_works(conn, bot, tg, upd, clock):
    draft_with_photos(bot, tg, upd, clock)
    markup = next(p["markup"] for m, p in tg.calls if m == "sendPhoto" and p["markup"])
    assert markup["inline_keyboard"][2] == [
        {"text": "📦 Пост уже в каналі — лише на Шафу", "callback_data": "queue:1"}
    ]
    press(upd, bot, tg, "pub")
    assert state(conn) == "published"
    assert len(tg.channel_posts) == 1 and posts(conn)[0]["source"] == "bot"


def test_found_in_memory_queues_without_touching_channel(conn, bot, tg, upd, clock, root, site_on):
    known(conn, 7010, f"{OWN}\nПишіть сюди 👉 @viktoriaviktoriatg\n#VS_37_розмір")
    draft_with_photos(bot, tg, upd, clock, files=3)
    before = len(tg.calls)

    queue(upd, bot, tg)

    (row,) = posts(conn)
    assert (row["post_id"], row["source"]) == (7010, "bot")
    assert row["date"] == core.local_date(DATE) and OWN in row["text"]  # дата й текст — з каналу
    assert sorted(p.name for p in (root / "7010").iterdir()) == ["1.jpg", "2.jpg", "3.jpg"]
    job = conn.execute("SELECT post_id, folder_name, state, not_before FROM site_jobs").fetchone()
    assert (job[0], job[1], job[2]) == (7010, "7010", "waiting")
    assert datetime.fromisoformat(job[3]).timestamp() == DATE + 7 * 86400  # від дати каналу
    assert state(conn) == "queued"
    assert conn.execute("SELECT post_id, folder_name FROM bot_drafts").fetchone()[:] == (
        7010,
        "7010",
    )
    assert sent_to_channel(tg) == [] and tg.forwards == []  # у канал нічого, перегляду немає
    posted = datetime.fromisoformat(core.local_date(DATE))
    answer = tg.texts(SELLER)[-1]
    assert answer == (
        f"Пост 7010 від {posted:%d.%m.%Y} — у черзі Шафи, фото: 3. "
        f"На Шафу — не раніше {posted + timedelta(days=7):%d.%m.%Y}."
    )
    assert len(tg.calls) > before  # відповідь їй була


def test_found_by_browsing_queues_deletes_copies_and_fills_cache(
    conn, bot, tg, upd, clock, root, site_on
):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    conn.commit()
    tg.channel = {7003: ("Інше\nпишіть сюди 👉 @viktoriaviktoriatg", DATE),
                  7004: (f"{OWN}\n#VS_37_розмір", DATE + 60)}  # fmt: skip
    draft_with_photos(bot, tg, upd, clock)

    queue(upd, bot, tg)

    assert [r["post_id"] for r in posts(conn)] == [7000, 7004]
    assert conn.execute("SELECT date FROM posts WHERE post_id = 7004").fetchone()[0] == (
        core.local_date(DATE + 60)
    )
    assert state(conn) == "queued" and (root / "7004" / "1.jpg").exists()
    assert len(tg.deleted()) == tg.forwarded_copies() > 0  # жодної копії не лишилось
    cached = [r[0] for r in conn.execute("SELECT post_id FROM channel_posts ORDER BY 1")]
    assert cached == [7003, 7004]  # знайдені тексти — в пам'яті
    assert sent_to_channel(tg) == []
    assert 7001 in tg.forwards and max(tg.forwards) == 7004 + channel.AHEAD_GONE
    assert min(tg.forwards) == 7000 - channel.BACK  # униз — не далі за межу


@pytest.mark.parametrize("case", ["none", "two", "other_price"])
def test_not_found_writes_nothing_and_asks_to_forward(conn, bot, tg, upd, clock, root, case):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    conn.commit()
    if case == "two":
        tg.channel = {7001: (DRAFT, DATE), 7002: (DRAFT, DATE + 5)}
    if case == "other_price":
        tg.channel = {7001: (DRAFT.replace("1200", "1300"), DATE)}
    draft_with_photos(bot, tg, upd, clock)

    queue(upd, bot, tg)

    assert [r["post_id"] for r in posts(conn)] == [7000]
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0
    assert not list(root.iterdir())
    assert state(conn) == "preview"  # чернетка лишається з кнопками
    assert tg.texts(SELLER)[-1].startswith("Не знайшов у каналі пост з таким описом. Перешли мені")
    assert sent_to_channel(tg) == []


def test_forwarded_channel_post_needs_no_browsing(conn, bot, tg, upd, clock, root):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    conn.commit()
    tg.files["f1"] = jpeg()
    forwarded = photo(11, "f1", caption=DRAFT)
    forwarded["forward_origin"] = {"type": "channel", "chat": {"id": CHANNEL}, "message_id": 7020,
                                   "date": DATE}  # fmt: skip
    bot.handle(upd(message=forwarded))
    clock.pass_(seconds=4)
    bot.tick()

    queue(upd, bot, tg)

    assert [(r["post_id"], r["date"]) for r in posts(conn)][1:] == [(7020, core.local_date(DATE))]
    assert tg.forwards == [] and state(conn) == "queued"


def test_forwarded_post_with_changed_text_is_refused(conn, bot, tg, upd, clock):
    tg.files["f1"] = jpeg()
    forwarded = photo(11, "f1", caption=DRAFT)
    forwarded["forward_origin"] = {"type": "channel", "chat": {"id": CHANNEL}, "message_id": 7020,
                                   "date": DATE}  # fmt: skip
    bot.handle(upd(message=forwarded))
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg, "edit")  # «Редагувати» → новий текст (інша ціна)
    bot.handle(upd(message=message(30, text=DRAFT.replace("1200", "900"))))
    clock.pass_(seconds=4)
    bot.tick()

    queue(upd, bot, tg)

    assert posts(conn) == [] and tg.forwards == []
    assert state(conn) == "preview"
    assert tg.texts(SELLER)[-1].startswith("Не знайшов")


def test_forward_from_other_channel_is_not_an_origin(conn, bot, tg, upd, clock):
    tg.files["f1"] = jpeg()
    forwarded = photo(11, "f1", caption=DRAFT)
    forwarded["forward_origin"] = {"type": "channel", "chat": {"id": -100555}, "message_id": 7020,
                                   "date": DATE}  # fmt: skip
    bot.handle(upd(message=forwarded))
    assert conn.execute("SELECT origin_post FROM bot_draft_parts").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM channel_posts").fetchone()[0] == 0


def test_post_already_in_posts_changes_nothing(conn, bot, tg, upd, clock, root):
    conn.execute("INSERT INTO posts VALUES (7010, '2026-10-01T10:00:00', 'було', 'bot', 'x')")
    conn.commit()
    known(conn, 7010, DRAFT)
    draft_with_photos(bot, tg, upd, clock)

    queue(upd, bot, tg)

    assert posts(conn)[0]["text"] == "було" and len(posts(conn)) == 1
    assert not list(root.iterdir()) and state(conn) == "preview"
    assert tg.texts(SELLER)[-1] == "Пост 7010 уже в черзі або опубліковано — нічого не змінюю."


def test_existing_folder_goes_to_bot_folder_and_owner_is_told(conn, bot, tg, upd, clock, root):
    (root / "7010").mkdir()
    known(conn, 7010, DRAFT)
    draft_with_photos(bot, tg, upd, clock)

    queue(upd, bot, tg)

    assert (root / "7010-бот" / "1.jpg").exists()
    assert any("7010-бот" in t for t in tg.texts(OWNER))


def test_second_press_after_queued_does_nothing(conn, bot, tg, upd, clock):
    known(conn, 7010, DRAFT)
    draft_with_photos(bot, tg, upd, clock)
    queue(upd, bot, tg)
    queue(upd, bot, tg)

    assert len(posts(conn)) == 1 and tg.answers()[-1] == "Цей пост уже в черзі Шафи."


# --- B: пам'ять про пости каналу ---


def test_channel_post_of_our_channel_is_remembered_other_chats_ignored(conn, bot, upd):
    bot.handle(upd(channel_post=channel_post(7011, "Текст поста", chat=-100777)))
    assert conn.execute("SELECT count(*) FROM channel_posts").fetchone()[0] == 0

    bot.handle(upd(channel_post=channel_post(7011, "Текст поста", media_group_id="g1")))
    bot.handle(upd(channel_post={**channel_post(7012, ""), "caption": None}))  # фото без підпису

    row = conn.execute("SELECT * FROM channel_posts").fetchone()
    assert tuple(row)[:4] == (7011, DATE, "Текст поста", "g1")
    assert conn.execute("SELECT count(*) FROM channel_posts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0  # більше нічого не робить
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0


def test_edited_channel_post_updates_memory(conn, bot, upd):
    bot.handle(upd(channel_post=channel_post(7011, "Старий текст")))
    bot.handle(upd(edited_channel_post=channel_post(7011, "Новий текст")))
    assert conn.execute("SELECT text FROM channel_posts").fetchone()[0] == "Новий текст"


def test_stranger_cannot_press_queue(conn, bot, tg, upd, clock):
    known(conn, 7010, DRAFT)
    draft_with_photos(bot, tg, upd, clock)
    press(upd, bot, tg, "queue", chat_id=STRANGER)
    assert posts(conn) == [] and state(conn) == "preview"


# --- C: перегляд каналу ---


def finder(conn, tg, pauses, limit=channel.LIMIT):
    from shafa.bot.presence import Presence

    looker = Presence(tg, CHANNEL, SERVICE, log=lambda t: None)
    return channel.Finder(conn, looker, pauses.append, log=lambda t: None, limit=limit)


def test_429_while_browsing_waits_and_retries_not_not_found(conn, tg):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    tg.channel = {7001: (DRAFT, DATE)}
    tg.forward_errors = {7001: [TelegramError("x", code=429, retry_after=7)]}
    pauses = []

    assert finder(conn, tg, pauses).find(OWN) == [7001]
    assert 7.0 in pauses and tg.forwards.count(7001) == 2


def test_browsing_stops_at_request_limit_and_finds_nothing(conn, tg):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    tg.channel = {n: ("Інше оголошення про щось ціна 100", DATE) for n in range(7001, 7500)}
    search = finder(conn, tg, [], limit=40)

    assert search.find(OWN) is None  # перегляд не завершився — не «не знайшов» і не вгадування
    assert len(tg.forwards) == 40


def test_unreachable_telegram_aborts_browsing(conn, tg):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    tg.forward_errors = {n: [TelegramError("x", code=500)] for n in range(7001, 7020)}
    assert finder(conn, tg, []).find(OWN) is None


def test_browse_goes_down_to_missed_posts_below_known(conn, tg):
    for n in (7000, 7010):
        conn.execute("INSERT INTO posts VALUES (?, '2026-10-01T10:00:00', 'старий', 'export', 'x')",
                     (n,))  # fmt: skip
    tg.channel = {7005: (DRAFT, DATE)}  # пропущений між відомими
    assert finder(conn, tg, []).find(OWN) == [7005]
    assert 7000 not in tg.forwards and 7010 not in tg.forwards  # відомі не перевіряються


def test_without_service_chat_browsing_is_skipped(conn, tg):
    conn.execute("INSERT INTO posts VALUES (7000, '2026-10-01T10:00:00', 'старий', 'export', 'x')")
    assert channel.Finder(conn, None, lambda s: None).find(OWN) == []


def test_bot_without_service_chat_still_refuses_politely(conn, tg, root, upd, clock):
    bot = Bot(conn, tg, Settings(CHANNEL, None, None, root), clock=clock, log=lambda t: None)
    assert bot._finder().looker is None


# --- Звірка тексту ---


@pytest.mark.parametrize(
    ("post", "same"),
    [
        (f"{OWN}\nПишіть сюди 👉 @viktoriaviktoriatg\n#VS_37_розмір", True),
        (f"✨ {OWN.upper()} ✨ https://t.me/x @nick #хештег", True),
        (OWN.replace("Шкіряні", "Шкіряні!"), True),
        (f"Продам. {OWN} Є дефект", True),
        (OWN.replace("1200", "1500"), False),  # ціна
        (OWN.replace("37", "38"), False),  # розмір
        ("Зовсім інший пост про сумку 1200 37", False),
    ],
)
def test_same_post(post, same):
    assert channel.same_post(channel.normalize(OWN), channel.normalize(post)) is same


def test_similarity_threshold_and_short_text():
    own = channel.normalize("Кросівки Nike Air Max 42 розмір, стан ідеальний, ціна 1500")
    post = channel.normalize("Кросівки Nike Air Max 42 розмір, стан ідеальний, ціна 1500 грн")
    assert channel.same_post(own, post)
    assert not channel.same_post("кеди 37", "кеди 37")  # занадто короткий опис нічого не доводить


# --- Міграція ---


def test_migration_keeps_drafts_and_accepts_queued(tmp_path):
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.executescript(
            "DROP TABLE channel_posts; ALTER TABLE bot_draft_parts DROP COLUMN origin_post; "
            "ALTER TABLE bot_draft_parts DROP COLUMN origin_date;"
        )
        conn.execute(
            "INSERT INTO bot_drafts (chat_id, state, created_at, last_message_at, updated_at) "
            "VALUES (1, 'preview', 'x', 'x', 'x')"
        )
        conn.execute("INSERT INTO bot_draft_parts (draft_id, message_id, text) VALUES (1, 5, 'т')")
        conn.execute("PRAGMA user_version = 15")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
    assert conn.execute("SELECT state FROM bot_drafts").fetchone()[0] == "preview"
    assert conn.execute("SELECT text, origin_post FROM bot_draft_parts").fetchone()[:] == (
        "т",
        None,
    )
    with conn:
        conn.execute("UPDATE bot_drafts SET state = 'queued'")
    with conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bot_drafts SET state = 'sent'")
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
