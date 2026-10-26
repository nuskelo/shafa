import io
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest
from PIL import Image

from shafa.bot import core
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import TelegramError, Upload

KYIV = timezone(timedelta(hours=3))
SELLER, OWNER, STRANGER = 111, 222, 999
CHANNEL = -1002394827824
GROUP = -1009990001112  # група обговорення каналу
TEXT = "Шкіряні кросівки Lowa 37 розміру. 1200грн\n#VS_37_розмір"


def jpeg(size=(400, 300), color="red", exif_turn=False) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if exif_turn:
        exif[0x0112] = 6
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (300, 400), (0, 0, 255, 128)).save(out, "PNG")
    return out.getvalue()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 14, 10, 0, tzinfo=KYIV)

    def __call__(self):
        return self.now

    def pass_(self, **delta):
        self.now += timedelta(**delta)


class FakeTelegram:
    """Підставний Telegram: запам'ятовує виклики, файли за `file_id`, помилки на замовлення."""

    def __init__(self):
        self.calls = []
        self.files = {}
        self.errors = {}  # метод → TelegramError
        self.next_id = 500
        self.channel_posts = []
        self.last_preview = None
        self.albums = []
        self.album_first = None  # id першого фото останнього альбому
        self.chat_info = {}  # відповідь getChat каналу (username тощо)

    def _record(self, method, **params):
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        self.next_id += 1

    def send_message(self, chat_id, text, markup=None):
        self._record("sendMessage", chat_id=chat_id, text=text, markup=markup)
        return {"message_id": self.next_id}

    def send_photo(self, chat_id, photo, caption, markup=None, entities=None):
        self._record("sendPhoto", chat_id=chat_id, photo=photo, caption=caption, markup=markup,
                     entities=entities)  # fmt: skip
        if chat_id == CHANNEL:
            self.channel_posts.append((photo, caption))
            return {"message_id": 1400 + len(self.channel_posts), "date": 1_791_960_000}
        if markup is not None:
            self.last_preview = self.next_id
        return {"message_id": self.next_id}

    def send_media_group(self, chat_id, photos, reply_to):
        self._record("sendMediaGroup", chat_id=chat_id, photos=photos, reply_to=reply_to)
        self.albums.append((chat_id, photos, reply_to))
        first = self.album_first = self.next_id
        self.next_id += len(photos)
        return [{"message_id": first + i} for i in range(len(photos))]

    def get_file(self, file_id):
        self._record("getFile", file_id=file_id)
        return {"file_path": file_id}

    def download(self, file_path, limit):
        self._record("download", file_path=file_path)
        return self.files[file_path]

    def answer_callback(self, callback_id, text):
        self._record("answerCallbackQuery", text=text)

    def edit_text(self, chat_id, message_id, text, markup=None):
        self._record("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                     markup=markup)  # fmt: skip
        return {"message_id": message_id}

    def delete_message(self, chat_id, message_id):
        self._record("deleteMessage", chat_id=chat_id, message_id=message_id)

    def get_chat(self, chat_id):
        self._record("getChat", chat_id=chat_id)
        return {"id": chat_id, **self.chat_info}

    def edit_caption(self, chat_id, message_id, caption, entities=None):
        self._record("editMessageCaption", chat_id=chat_id, message_id=message_id,
                     caption=caption, entities=entities)  # fmt: skip

    def drop_buttons(self, chat_id, message_id):
        self._record("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id)

    def texts(self, chat_id):
        return [p["text"] for m, p in self.calls if m == "sendMessage" and p["chat_id"] == chat_id]

    def answers(self):
        return [p["text"] for m, p in self.calls if m == "answerCallbackQuery"]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def bot(conn, tg, root, clock):
    settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
    return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)


class Updates:
    def __init__(self):
        self.next = 1

    def __call__(self, **body):
        self.next += 1
        return {"update_id": self.next, **body}


@pytest.fixture
def upd():
    return Updates()


def chat(chat_id=SELLER):
    return {"id": chat_id, "type": "private"}


def message(msg_id, chat_id=SELLER, **body):
    return {"message_id": msg_id, "chat": chat(chat_id), "from": {"id": chat_id}, **body}


def photo(msg_id, file_id, caption=None, chat_id=SELLER, group=None):
    body = {"photo": [{"file_id": file_id + "-small", "width": 90, "height": 67},
                      {"file_id": file_id, "width": 1280, "height": 960}]}  # fmt: skip
    if caption:
        body["caption"] = caption
    if group:
        body["media_group_id"] = group
    return message(msg_id, chat_id, **body)


def document(msg_id, file_id, name, mime, size=1000):
    body = {"file_id": file_id, "file_name": name, "mime_type": mime, "file_size": size}
    return message(msg_id, document=body)


def press(upd, bot, tg, action="pub", draft_id=1, chat_id=SELLER, preview=None):
    preview = preview or last_preview_id(tg)
    query = {
        "id": "cb", "from": {"id": chat_id}, "data": f"{action}:{draft_id}",
        "message": {"message_id": preview, "chat": chat(chat_id)},
    }  # fmt: skip
    bot.handle(upd(callback_query=query))


def last_preview_id(tg):
    return tg.last_preview


def send_post(bot, tg, upd, clock, files=3, text=TEXT):
    """Альбом з підписом на першому фото → тиша → прев'ю."""
    for i in range(1, files + 1):
        tg.files[f"f{i}"] = jpeg(color=("red", "green", "blue", "white")[i % 4])
        bot.handle(upd(message=photo(10 + i, f"f{i}", caption=text if i == 1 else None, group="g")))
    clock.pass_(seconds=4)
    bot.tick()


def state(conn, draft_id=1):
    return conn.execute("SELECT state FROM bot_drafts WHERE id = ?", (draft_id,)).fetchone()[0]


# --- Підпис і дрібниці ---


SIGN = "Пишіть сюди 👉 @viktoriaviktoriatg"
SITE = "🛍 Більше взуття на сайті\nViktoria Shoes 👈"
OLD_SITE = "🛍 Більше взуття на сайті 👈"  # формат до BOT-008 — старі пости в каналі
MORE = "Додаткові фото в коментарях ⬇️"
TAIL = f"{SIGN}\n\n{SITE}"  # кінець підпису одного фото
TAIL_ALBUM = f"{TAIL}\n\n{MORE}"  # фото ≥ 2


def test_caption_like_her_posts():
    text = "Кросівки Lowa 37 розміру. 1200грн"
    assert core.caption_of([text]) == f"{text}\n#VS_37_розмір\n\n{TAIL}"
    assert core.caption_of([text], 2) == f"{text}\n#VS_37_розмір\n\n{TAIL_ALBUM}"
    assert core.caption_of(["  Кеди  ", "", "38 розмір\n"]) == (
        f"Кеди\n38 розмір\n#VS_38_розмір\n\n{TAIL}"
    )


def test_caption_does_not_duplicate_her_hashtag_or_signature():
    assert core.caption_of([TEXT]) == f"{TEXT}\n\n{TAIL}"  # хештег уже є
    signed = f"Кеди 38 розмір. 800грн\n\n{SIGN}"
    assert core.caption_of([signed]) == f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{TAIL}"
    assert core.caption_of([f"Сумка шкіряна. 900грн\n#VS_сумки\n\n{SIGN}"], 3) == (
        f"Сумка шкіряна. 900грн\n#VS_сумки\n\n{TAIL_ALBUM}"
    )


def test_caption_other_things_without_hashtag_and_unclear_size():
    assert core.caption_of(["Рукавиці шкіряні. 300грн"]) == f"Рукавиці шкіряні. 300грн\n\n{TAIL}"
    assert core.caption_of(["Кросівки Nike. 1200грн"]) is None


def test_caption_site_line_follows_signature_and_comments_line_is_last():
    caption = core.caption_of(["Кеди 38 розмір. 800грн"], 2)
    assert caption.split("\n")[-7:] == ["", SIGN, "", *SITE.split("\n"), "", MORE]
    assert core.caption_of(["Кеди 38 розмір. 800грн"], 1).endswith(f"{SIGN}\n\n{SITE}")
    assert MORE not in core.caption_of(["Кеди 38 розмір. 800грн"], 1)


def test_caption_site_line_from_forwarded_post_not_duplicated():
    text = f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{SIGN}\n\n{SITE}\n\n{MORE}"
    caption = core.caption_of([text], 3)
    assert caption.count(SITE) == 1 and caption.count(MORE) == 1
    assert caption == f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{TAIL_ALBUM}"


def test_caption_site_line_switched_off(monkeypatch):
    monkeypatch.setenv("SHAFA_SITE_BUTTON", "ні")
    caption = core.caption_of([f"Кеди 38 розмір. 800грн\n\n{SITE}"], 2)
    assert SITE not in caption
    assert caption.endswith(f"{SIGN}\n\n{MORE}")


def utf16(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def test_entities_cover_whole_signature_and_site_lines_in_utf16():
    # емодзі 🔥 і 🛍 — по 2 одиниці UTF-16: зміщення рахується не в символах
    caption = core.caption_of(["🔥 Кеди 38 розмір. 800грн 🔥"], 2)
    link = "https://t.me/c/2394827824/1400"
    ask, site = core.caption_entities(caption, link)
    assert utf16(caption, ask["offset"], ask["length"]) == SIGN
    assert utf16(caption, site["offset"], site["length"]) == SITE
    assert ask["type"] == site["type"] == "text_link"
    assert site["url"] == core.SITE_URL
    # у символах Python зміщення було б іншим — тест ловить рахунок «по символах»
    assert caption.index(SIGN) != ask["offset"]


def test_entities_ask_url_has_hello_and_post_link_percent_encoded():
    caption = core.caption_of(["Кеди 38 розмір. 800грн"])
    (ask, _) = core.caption_entities(caption, "https://t.me/viktoria/1400")
    assert ask["url"].startswith("https://t.me/viktoriaviktoriatg?text=")
    assert unquote(ask["url"].split("?text=")[1]) == (
        "Вітаю! Цікавить ця пара: https://t.me/viktoria/1400"
    )
    assert " " not in ask["url"] and ":" not in ask["url"].split("?text=")[1]


def test_entities_without_post_link_point_to_chat_only():
    caption = core.caption_of(["Кеди 38 розмір. 800грн"])
    (ask, _) = core.caption_entities(caption)
    assert ask["url"] == "https://t.me/viktoriaviktoriatg"


def test_entities_ignore_her_markup_characters_and_hashtag_stays_plain():
    text = "Кеди <b>x</b> & Ко_Ко *y* [1](x). 38 розмір. 800грн"
    caption = core.caption_of([text], 2)
    assert text in caption  # її текст — як є, без екранування й розмітки
    ents = core.caption_entities(caption, "https://t.me/c/1/2")
    assert [e["type"] for e in ents] == ["text_link", "text_link"]  # хештег — не наша сутність
    assert utf16(caption, ents[0]["offset"], ents[0]["length"]) == SIGN


def test_entities_only_first_signature_line_and_switches(monkeypatch):
    caption = core.caption_of([f"Кеди 38 розмір. 800грн\n\n{SIGN}\nДодатково"], 1)
    ents = core.caption_entities(caption, "https://t.me/c/1/2")
    assert len(ents) == 2 and caption.count("Пишіть сюди") == 1
    monkeypatch.setenv("SHAFA_ASK_BUTTON", "ні")
    (only,) = core.caption_entities(caption, "https://t.me/c/1/2")
    assert only["url"] == core.SITE_URL
    monkeypatch.setenv("SHAFA_SITE_BUTTON", "ні")
    assert core.caption_entities(caption) == []


def test_caption_length_counts_like_telegram():
    assert core.caption_length("👉") == 2
    assert core.caption_length("Кеди") == 4


def test_local_date_like_export(monkeypatch):
    stamp = int(datetime(2026, 10, 14, 7, 0, tzinfo=UTC).timestamp())
    assert core.local_date(stamp) == datetime.fromtimestamp(stamp, UTC).astimezone().strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    assert len(core.local_date(stamp)) == 19 and "+" not in core.local_date(stamp)


def test_post_link():
    assert core.post_link(CHANNEL, 1342) == "https://t.me/c/2394827824/1342"


# --- Повний шлях ---


def test_album_preview_then_publish(conn, bot, tg, upd, clock, root):
    send_post(bot, tg, upd, clock)

    assert state(conn) == "preview"
    preview = [(m, p) for m, p in tg.calls if m == "sendPhoto"][-1][1]
    assert preview["chat_id"] == SELLER and preview["photo"] == "f1"  # найбільший розмір першого
    assert preview["caption"] == core.caption_of([TEXT], 3)
    assert preview["caption"].endswith(f"{TAIL_ALBUM}")  # фото ≥ 2 — рядок про коментарі
    buttons = [b["callback_data"] for row in preview["markup"]["inline_keyboard"] for b in row]
    assert buttons == ["pub:1", "edit:1", "cancel:1", "queue:1"]
    assert tg.channel_posts == []  # без натискання нічого не публікується

    press(upd, bot, tg)

    ((photo_sent, caption),) = tg.channel_posts
    assert isinstance(photo_sent, Upload) and photo_sent.data == tg.files["f1"]
    assert caption == preview["caption"]  # прев'ю = канал
    (sent,) = channel_sends(tg)
    assert sent["markup"] is None  # кнопок під постом немає
    assert sent["entities"] == core.caption_entities(caption)
    assert preview["entities"] == sent["entities"]  # у прев'ю ті самі посилання, що в каналі
    assert state(conn) == "published"
    folder = root / "1401"
    assert sorted(p.name for p in folder.iterdir()) == ["1.jpg", "2.jpg", "3.jpg"]
    assert [(folder / f"{i}.jpg").read_bytes() for i in (1, 2, 3)] == [
        tg.files["f1"], tg.files["f2"], tg.files["f3"]
    ]  # fmt: skip
    post = conn.execute("SELECT * FROM posts WHERE post_id = 1401").fetchone()
    assert (post["source"], post["text"]) == ("bot", caption)
    assert post["date"] == core.local_date(1_791_960_000)
    assert tg.texts(SELLER)[-1] == (
        "Опубліковано: https://t.me/c/2394827824/1401\nЗбережено фото для Шафи: 3."
    )


def test_album_parts_out_of_order_keep_send_order(conn, bot, tg, upd, clock, root):
    for msg_id, file_id in ((13, "f3"), (11, "f1"), (12, "f2")):
        tg.files[file_id] = jpeg(color={"f1": "red", "f2": "green", "f3": "blue"}[file_id])
        caption = TEXT if file_id == "f2" else None  # підпис не на першому
        bot.handle(upd(message=photo(msg_id, file_id, caption=caption, group="g")))
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg)

    assert tg.channel_posts[0][0].data == tg.files["f1"]
    assert (root / "1401" / "1.jpg").read_bytes() == tg.files["f1"]
    assert (root / "1401" / "3.jpg").read_bytes() == tg.files["f3"]


def test_text_and_photos_sent_separately_within_two_minutes(conn, bot, tg, upd, clock):
    bot.handle(upd(message=message(10, text=TEXT)))
    clock.pass_(seconds=4)
    bot.tick()
    assert tg.texts(SELLER) == ["Надішли фото до цього тексту (до 10)."]

    clock.pass_(seconds=100)  # ще в межах 2 хв від останнього
    tg.files["f1"] = jpeg()
    bot.handle(upd(message=photo(11, "f1")))
    clock.pass_(seconds=4)
    bot.tick()

    assert state(conn) == "preview"
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 1


def test_message_after_two_minutes_starts_new_draft(conn, bot, tg, upd, clock):
    bot.handle(upd(message=message(10, text=TEXT)))
    clock.pass_(minutes=2, seconds=1)
    bot.handle(upd(message=photo(11, "f1")))
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 2


def test_photos_without_text_ask_for_text(conn, bot, tg, upd, clock):
    bot.handle(upd(message=photo(11, "f1")))
    clock.pass_(seconds=4)
    bot.tick()
    bot.tick()
    assert tg.texts(SELLER) == ["Надішли текст поста до цих фото."]
    assert state(conn) == "collecting"


def test_preview_waits_for_silence(conn, bot, tg, upd, clock):
    bot.handle(upd(message=photo(11, "f1", caption=TEXT)))
    clock.pass_(seconds=1)
    bot.tick()
    assert state(conn) == "collecting" and tg.calls == []


def test_late_album_part_makes_new_preview(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=2)  # альбом «g», підпис на першій частині
    first_preview = last_preview_id(tg)
    tg.files["f9"] = jpeg()
    bot.handle(upd(message=photo(20, "f9", group="g")))  # запізніла частина того ж альбому
    assert state(conn) == "collecting"

    press(upd, bot, tg, preview=first_preview)  # стара кнопка, поки альбом ще приходить
    assert tg.answers()[-1] == "Ти ще дописуєш — за мить покажу нове прев'ю."
    clock.pass_(seconds=4)
    bot.tick()
    assert ("editMessageReplyMarkup", {"chat_id": SELLER, "message_id": first_preview}) in tg.calls
    press(upd, bot, tg, preview=first_preview)
    assert tg.answers()[-1] == "Це старе прев'ю — натисни кнопку під останнім."
    assert tg.channel_posts == []

    press(upd, bot, tg)
    assert len(tg.channel_posts) == 1
    assert "Збережено фото для Шафи: 3." in tg.texts(SELLER)[-1]


def test_photo_without_caption_after_ready_post_is_separate(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    first_preview = last_preview_id(tg)
    tg.files["f9"] = jpeg()
    bot.handle(upd(message=photo(20, "f9")))
    clock.pass_(seconds=4)
    bot.tick()

    assert [state(conn, 1), state(conn, 2)] == ["preview", "collecting"]
    assert "Надішли текст поста до цих фото." in tg.texts(SELLER)
    press(upd, bot, tg, preview=first_preview)
    assert len(tg.channel_posts) == 1 and "Збережено фото для Шафи: 1." in tg.texts(SELLER)[-1]


# --- Один пост ---


def test_double_click_and_redelivery_publish_once(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    preview = last_preview_id(tg)
    query = {
        "id": "cb", "from": {"id": SELLER}, "data": "pub:1",
        "message": {"message_id": preview, "chat": chat()},
    }  # fmt: skip
    update = upd(callback_query=query)

    bot.handle(update)
    bot.handle(update)  # та сама доставка вдруге
    bot.handle(upd(callback_query=query))  # друге натискання

    assert len(tg.channel_posts) == 1
    assert tg.answers()[-1] == "Цей пост уже опубліковано: https://t.me/c/2394827824/1401"


def test_redelivered_messages_do_not_duplicate_parts(conn, bot, tg, upd, clock):
    update = upd(message=photo(11, "f1", caption=TEXT))
    bot.handle(update)
    bot.handle(update)
    assert conn.execute("SELECT count(*) FROM bot_draft_parts").fetchone()[0] == 1


def test_uncertain_channel_answer_not_repeated(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    tg.errors["sendPhoto"] = TelegramError("Telegram не відповів вчасно (sendPhoto)", True)

    press(upd, bot, tg)
    tg.errors.clear()
    press(upd, bot, tg)

    assert state(conn) == "uncertain"
    assert tg.channel_posts == []
    assert sum(1 for m, p in tg.calls if m == "sendPhoto" and p["chat_id"] == CHANNEL) == 1
    assert "перевір канал" in tg.texts(SELLER)[-1]
    assert any("не впевнений" in t for t in tg.texts(OWNER))
    assert tg.answers()[-1] == "Уже публікую — зачекай"
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0


def test_channel_refusal_publishes_nothing(conn, bot, tg, upd, clock, root):
    send_post(bot, tg, upd, clock)
    tg.errors["sendPhoto"] = TelegramError("Telegram відмовив (sendPhoto, 403): not admin")

    press(upd, bot, tg)

    assert state(conn) == "failed"
    assert list(root.iterdir()) == []
    assert "канал не прийняв пост" in tg.texts(SELLER)[-1]
    assert any("not admin" in t for t in tg.texts(OWNER))


def test_crash_while_publishing_is_uncertain_on_start(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    with conn:
        conn.execute("UPDATE bot_drafts SET state = 'publishing'")

    bot.recover()
    press(upd, bot, tg)

    assert state(conn) == "uncertain"
    assert tg.channel_posts == []
    assert any("зупинився під час публікації" in t for t in tg.texts(OWNER))


def test_preview_expires_after_a_day(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    clock.pass_(hours=24, seconds=1)

    press(upd, bot, tg)

    assert tg.channel_posts == []
    assert state(conn) == "expired"
    assert tg.texts(SELLER)[-1] == "Прев'ю застаріло — надішли пост ще раз."


def test_preview_valid_just_under_a_day(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    clock.pass_(hours=23, minutes=59)
    bot.tick()
    press(upd, bot, tg)
    assert len(tg.channel_posts) == 1
    assert "(отримано о 10:00, опубліковано із запізненням)" in tg.texts(SELLER)[-1]


def test_cancel(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg, action="cancel")
    press(upd, bot, tg)
    assert state(conn) == "canceled"
    assert tg.channel_posts == []
    assert tg.texts(SELLER)[-1] == "Скасовано. Нічого не опубліковано."


# --- Чужі ---


def test_stranger_is_ignored_silently(conn, bot, tg, upd, clock):
    bot.handle(upd(message=photo(11, "f1", caption=TEXT, chat_id=STRANGER)))
    bot.handle(upd(message={**message(12, text="hi"), "chat": {"id": SELLER, "type": "group"}}))
    clock.pass_(seconds=4)
    bot.tick()
    assert tg.calls == []
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM bot_updates").fetchone()[0] == 2


def test_stranger_cannot_press_her_buttons(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg, chat_id=STRANGER)
    assert tg.channel_posts == [] and state(conn) == "preview"


def test_owner_can_post_too(conn, bot, tg, upd, clock):
    tg.files["f1"] = jpeg()
    bot.handle(upd(message=photo(11, "f1", caption=TEXT, chat_id=OWNER)))
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg, chat_id=OWNER)
    assert len(tg.channel_posts) == 1


# --- Файли ---


def test_jpeg_file_saved_as_original_and_uploaded_to_channel(conn, bot, tg, upd, clock, root):
    tg.files["d1"] = jpeg(size=(4032, 3024))
    bot.handle(upd(message={**document(11, "d1", "IMG_1.JPG", "image/jpeg"), "caption": TEXT}))
    clock.pass_(seconds=4)
    bot.tick()
    (preview,) = [p for m, p in tg.calls if m == "sendPhoto" and p["markup"]]
    assert isinstance(preview["photo"], Upload)  # прев'ю файлу — фото-картинкою, як у каналі
    assert preview["photo"].data == tg.files["d1"]
    press(upd, bot, tg)

    assert (root / "1401" / "1.jpg").read_bytes() == tg.files["d1"]
    assert tg.channel_posts[0][0].data == tg.files["d1"]


def test_png_file_converted_to_jpeg(conn, bot, tg, upd, clock, root):
    tg.files["d1"] = png()
    bot.handle(upd(message={**document(11, "d1", "скрін.png", "image/png"), "caption": TEXT}))
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg)

    with Image.open(root / "1401" / "1.jpg") as image:
        assert (image.format, image.size) == ("JPEG", (300, 400))


@pytest.mark.parametrize(("name", "mime"), [("фото.webp", "image/webp"), ("a.pdf", "")])
def test_unsupported_file_explained(conn, bot, tg, upd, name, mime):
    bot.handle(upd(message=document(11, "d1", name, mime)))
    assert tg.texts(SELLER) == ["Цей файл я не прочитаю. Надішли це фото як звичайне фото або JPG."]
    assert conn.execute("SELECT count(*) FROM bot_draft_parts").fetchone()[0] == 0


HEIC = Path(__file__).parent / "fixtures" / "photo.heic"  # 48×32, EXIF-поворот 6 (→ 32×48)


@pytest.mark.parametrize(
    ("name", "mime"),
    [("IMG_1377.HEIC", "image/heic"), ("IMG_1.heif", ""), ("фото", "image/heif")],
)
def test_heic_file_converted_to_jpeg(conn, bot, tg, upd, clock, root, name, mime):
    tg.files["d1"] = HEIC.read_bytes()
    bot.handle(upd(message={**document(11, "d1", name, mime), "caption": TEXT}))
    clock.pass_(seconds=4)
    bot.tick()
    preview = tg.last_preview
    assert preview is not None and tg.texts(OWNER) == []
    press(upd, bot, tg)

    with Image.open(root / "1401" / "1.jpg") as image:
        assert (image.format, image.size) == ("JPEG", (32, 48))  # повернуто за EXIF
        assert image.getexif().get(0x0112, 1) == 1
    assert tg.channel_posts and state(conn) == "published"


def test_heic_as_photo_content_converted(conn, bot, tg, upd, clock, root):
    """Telegram зазвичай сам робить JPEG, але HEIC під видом фото теж приймаємо."""
    tg.files["f1"] = HEIC.read_bytes()
    bot.handle(upd(message=photo(11, "f1", caption=TEXT)))
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg)

    with Image.open(root / "1401" / "1.jpg") as image:
        assert image.format == "JPEG"


def test_broken_heic_explained_to_seller_and_owner(conn, bot, tg, upd, clock, root):
    tg.files["d1"] = HEIC.read_bytes()[:200]
    bot.handle(upd(message={**document(11, "d1", "IMG_2.HEIC", "image/heic"), "caption": TEXT}))
    clock.pass_(seconds=4)
    bot.tick()

    assert tg.texts(SELLER)[-1] == (
        "Не можу відкрити це фото — надішліть, будь ласка, як звичайне фото."
    )
    assert any("HEIC-фото чернетки 1" in t for t in tg.texts(OWNER))
    assert tg.channel_posts == [] and not (root / "1401").exists()


def test_broken_heic_on_publish_publishes_nothing(conn, bot, tg, upd, clock):
    tg.files["d1"] = HEIC.read_bytes()
    bot.handle(upd(message={**document(11, "d1", "IMG_2.HEIC", "image/heic"), "caption": TEXT}))
    clock.pass_(seconds=4)
    bot.tick()
    tg.files["d1"] = HEIC.read_bytes()[:200]  # між прев'ю й «Опублікувати» файл не відкривається
    press(upd, bot, tg)

    assert tg.channel_posts == [] and state(conn) == "failed"
    assert tg.texts(SELLER)[-1].startswith("Не можу відкрити це фото")
    assert any("HEIC-фото" in t for t in tg.texts(OWNER))


def test_too_big_file(conn, bot, tg, upd):
    bot.handle(upd(message=document(11, "d1", "a.jpg", "image/jpeg", size=21 * 1024 * 1024)))
    assert "більший за 20 МБ" in tg.texts(SELLER)[0]


def test_video_and_command(conn, bot, tg, upd):
    bot.handle(upd(message=message(11, video={"file_id": "v"})))
    bot.handle(upd(message=message(12, text="/start")))
    assert tg.texts(SELLER) == [core.TEXTS["not_photo"], core.TEXTS["help"]]
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 0


def test_broken_photo_publishes_nothing(conn, bot, tg, upd, clock, root):
    send_post(bot, tg, upd, clock)
    tg.files["f2"] = b"not a jpeg"
    press(upd, bot, tg)
    assert tg.channel_posts == [] and state(conn) == "failed"
    assert tg.texts(SELLER)[-1] == (
        "Не вийшло: фото не відкривається. Нічого не опубліковано — надішли пост ще раз."
    )


def test_download_error_publishes_nothing(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    tg.errors["getFile"] = TelegramError("Немає з'єднання з Telegram (getFile)")
    press(upd, bot, tg)
    assert tg.channel_posts == [] and state(conn) == "failed"


def test_channel_copy_turned_by_exif_and_shrunk():
    turned = jpeg(size=(400, 300), exif_turn=True)
    with Image.open(io.BytesIO(core.for_channel(turned))) as image:
        assert image.size == (300, 400)
    plain = jpeg()
    assert core.for_channel(plain) == plain
    huge = jpeg(size=(8000, 3000))
    with Image.open(io.BytesIO(core.for_channel(huge))) as image:
        assert max(image.size) == core.CHANNEL_SIDE


# --- Межі ---


def test_too_many_photos(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=11)
    assert state(conn) == "failed"
    assert tg.texts(SELLER)[-1] == "Забагато фото: 11. Максимум 10 — надішли пост заново."


def test_ten_photos_fine(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=10)
    assert state(conn) == "preview"


def long_post(length):
    """Пост про кросівки (одне фото), підпис якого (хештег, «Пишіть сюди», рядок про сайт) —
    рівно `length` символів за рахунком Telegram (UTF-16: 👉 і 🛍 — по 2)."""
    start = "Кросівки 38 розмір. "
    extra = core.caption_length(f"\n#VS_38_розмір\n\n{core.SIGNATURE}\n\n{core.SITE_TEXT}")
    return start + "я" * (length - extra - len(start))


def test_too_long_text(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text=long_post(1025))
    assert state(conn) == "failed"
    assert tg.texts(SELLER)[-1].startswith("Текст задовгий: 1025 символів")


def test_longest_text_fits(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text=long_post(1024))
    assert state(conn) == "preview"


# --- Папка (D8) ---


def test_existing_folder_not_overwritten(conn, bot, tg, upd, clock, root):
    (root / "1401").mkdir()
    (root / "1401" / "старе.jpg").write_bytes(b"old")
    send_post(bot, tg, upd, clock, files=2)

    press(upd, bot, tg)

    assert (root / "1401" / "старе.jpg").read_bytes() == b"old"
    assert sorted(p.name for p in (root / "1401-бот").iterdir()) == ["1.jpg", "2.jpg"]
    assert any("«1401-бот»" in t for t in tg.texts(OWNER))
    assert len(tg.channel_posts) == 1


def test_bot_post_becomes_card_and_survives_new_export(conn, bot, tg, upd, clock, root):
    from shafa.export import Post
    from shafa.folders import list_folders
    from shafa.scan import scan

    (root / "1401").mkdir()  # заважає: фото підуть у «1401-бот»
    (root / "1401" / "a.jpg").write_bytes(jpeg())
    send_post(bot, tg, upd, clock, files=2)
    press(upd, bot, tg)
    send_post(bot, tg, upd, clock, files=1, text="Кеди Ecco 38 розмір. 900грн")
    press(upd, bot, tg, draft_id=2)

    export = {1402: Post(1402, "2020-01-01T00:00:00", "старий текст з Експорту")}
    result = scan(conn, export, list_folders(root, None))

    cards = {r["folder_name"]: r for r in conn.execute("SELECT * FROM items")}
    assert cards["1402"]["status"] == "new"
    assert cards["1401-бот"]["problem_code"] == "bad_folder_name"
    post = conn.execute("SELECT source, text FROM posts WHERE post_id = 1402").fetchone()
    assert tuple(post) == (
        "bot",
        core.caption_of(["Кеди Ecco 38 розмір. 900грн"]),
    )  # Експорт не затер
    assert result.folders == 3


def test_button_in_her_chat_pressed_by_someone_else(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    query = {
        "id": "cb", "from": {"id": STRANGER}, "data": "pub:1",
        "message": {"message_id": last_preview_id(tg), "chat": chat(SELLER)},
    }  # fmt: skip
    bot.handle(upd(callback_query=query))
    assert tg.channel_posts == [] and state(conn) == "preview"


def test_largest_photo_size_taken_whatever_the_order(conn, bot, tg, upd, clock):
    sizes = [{"file_id": "big", "width": 1280, "height": 960},
             {"file_id": "small", "width": 90, "height": 67}]  # fmt: skip
    bot.handle(upd(message=message(11, photo=sizes, caption=TEXT)))
    assert conn.execute("SELECT file_id FROM bot_draft_parts").fetchone()[0] == "big"


# --- Незрозумілий розмір ---


def test_unclear_size_asks_and_waits_ten_minutes_for_answer(conn, bot, tg, upd, clock):
    tg.files["f1"] = jpeg()
    bot.handle(upd(message=photo(11, "f1", caption="Кросівки Nike в ідеальному стані. 1200грн")))
    clock.pass_(seconds=4)
    bot.tick()
    assert tg.texts(SELLER)[-1].startswith("Не можу зрозуміти розмір")
    assert state(conn) == "collecting" and tg.last_preview is None

    clock.pass_(minutes=8)  # відповіла не одразу — та сама чернетка
    bot.handle(upd(message=message(12, text="42 розмір")))
    clock.pass_(seconds=4)
    bot.tick()

    assert state(conn) == "preview"
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 1
    preview = [p for m, p in tg.calls if m == "sendPhoto" and p["markup"]][-1]
    assert "#VS_42_розмір" in preview["caption"]


def test_ask_window_ends_after_ten_minutes(conn, bot, tg, upd, clock):
    bot.handle(upd(message=photo(11, "f1", caption="Кросівки Nike. 1200грн")))
    clock.pass_(seconds=4)
    bot.tick()
    clock.pass_(minutes=10, seconds=1)
    bot.handle(upd(message=message(12, text="42 розмір")))
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 2


# --- Коментарі ---


def forwarded(msg_id, post_id, channel=CHANNEL, automatic=True, chat_id=GROUP):
    return {
        "message_id": msg_id,
        "chat": {"id": chat_id, "type": "supergroup"},
        "is_automatic_forward": automatic,
        "forward_origin": {"type": "channel", "chat": {"id": channel}, "message_id": post_id},
    }


def published(bot, tg, upd, clock, files=3):
    send_post(bot, tg, upd, clock, files=files)
    press(upd, bot, tg)


def comments(conn, draft_id=1):
    return conn.execute("SELECT comments FROM bot_drafts WHERE id = ?", (draft_id,)).fetchone()[0]


def test_all_photos_go_to_comments_as_album_reply(conn, bot, tg, upd, clock, root):
    published(bot, tg, upd, clock, files=3)
    assert comments(conn) == "waiting"
    assert tg.albums == []

    bot.handle(upd(message=forwarded(77, 1401)))

    ((chat_id, photos, reply_to),) = tg.albums
    assert (chat_id, reply_to) == (GROUP, 77)
    assert [p.data for p in photos] == [tg.files["f1"], tg.files["f2"], tg.files["f3"]]
    assert comments(conn) == "done"


def test_one_photo_no_comments(conn, bot, tg, upd, clock):
    published(bot, tg, upd, clock, files=1)
    bot.handle(upd(message=forwarded(77, 1401)))
    assert comments(conn) is None and tg.albums == []
    assert "коментар" not in tg.channel_posts[0][1]  # одне фото — рядка про коментарі нема


def test_forward_redelivered_or_repeated_album_once(conn, bot, tg, upd, clock):
    published(bot, tg, upd, clock)
    update = upd(message=forwarded(77, 1401))
    bot.handle(update)
    bot.handle(update)
    bot.handle(upd(message=forwarded(78, 1401)))
    assert len(tg.albums) == 1


@pytest.mark.parametrize(
    "msg",
    [
        forwarded(77, 1401, automatic=False),  # хтось переслав руками
        forwarded(77, 1401, channel=-1005555),  # пост іншого каналу
        forwarded(77, 9999),  # не наш пост
        {**message(77, text="Купую!"), "chat": {"id": GROUP, "type": "supergroup"}},
    ],
)
def test_other_group_messages_ignored_silently(conn, bot, tg, upd, clock, msg):
    published(bot, tg, upd, clock)
    calls = len(tg.calls)
    bot.handle(upd(message=msg))
    assert tg.calls[calls:] == [] and comments(conn) == "waiting"


def test_forward_late_seller_told_owner_alarmed_album_added_later(conn, bot, tg, upd, clock):
    published(bot, tg, upd, clock)
    clock.pass_(minutes=1, seconds=59)
    bot.tick()
    assert comments(conn) == "waiting"

    clock.pass_(seconds=2)
    bot.tick()
    bot.tick()

    assert comments(conn) == "late"
    assert tg.texts(SELLER)[-1] == (
        "Пост https://t.me/c/2394827824/1401 є, але фото в коментарі ще не додались — додам, "
        "щойно зможу."
    )
    assert sum("не прийшов за 2 хв" in t for t in tg.texts(OWNER)) == 1

    clock.pass_(hours=3)
    bot.handle(upd(message=forwarded(77, 1401)))
    assert len(tg.albums) == 1 and comments(conn) == "done"
    assert (
        tg.texts(SELLER)[-1] == "Фото додано в коментарі до поста https://t.me/c/2394827824/1401."
    )
    assert len(tg.channel_posts) == 1  # пост у канал удруге — ніколи


def test_forward_never_came_gives_up_after_a_day(conn, bot, tg, upd, clock):
    published(bot, tg, upd, clock)
    clock.pass_(minutes=3)
    bot.tick()
    clock.pass_(hours=24)
    bot.tick()
    assert comments(conn) == "failed"
    assert any("так і не додались" in t for t in tg.texts(OWNER))
    bot.handle(upd(message=forwarded(77, 1401)))
    assert tg.albums == []


@pytest.mark.parametrize("uncertain", [False, True])
def test_album_error_not_repeated_owner_told(conn, bot, tg, upd, clock, uncertain):
    published(bot, tg, upd, clock)
    tg.errors["sendMediaGroup"] = TelegramError("Telegram відмовив (sendMediaGroup)", uncertain)

    bot.handle(upd(message=forwarded(77, 1401)))
    tg.errors.clear()
    bot.handle(upd(message=forwarded(78, 1401)))

    assert comments(conn) == "failed" and tg.albums == []
    text = tg.texts(OWNER)[-1]
    assert ("могли не додатись" in text) is uncertain


def test_crash_while_sending_album_is_not_repeated(conn, bot, tg, upd, clock):
    published(bot, tg, upd, clock)
    with conn:
        conn.execute("UPDATE bot_drafts SET comments = 'sending'")
    bot.recover()
    bot.handle(upd(message=forwarded(77, 1401)))
    assert comments(conn) == "failed" and tg.albums == []
    assert any("перевір коментарі" in t for t in tg.texts(OWNER))


def test_png_preview_is_jpeg_picture(conn, bot, tg, upd, clock):
    tg.files["d1"] = png()
    bot.handle(upd(message={**document(11, "d1", "IMG_1377.PNG", "image/png"), "caption": TEXT}))
    clock.pass_(seconds=4)
    bot.tick()
    (preview,) = [p for m, p in tg.calls if m == "sendPhoto" and p["markup"]]
    assert isinstance(preview["photo"], Upload)
    with Image.open(io.BytesIO(preview["photo"].data)) as image:
        assert image.format == "JPEG"


# --- Хештег ніколи не зникає мовчки ---

BAG_TEXT = "Тест 5: сумка шкіряна Furla. Розмір 30см*20см. 1800грн"


def preview_header(tg):
    return [t for t in tg.texts(SELLER) if t.startswith("Так буде в каналі")][-1]


def test_live_bag_text_gets_bag_hashtag_in_preview_and_post(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=3, text=BAG_TEXT)

    preview = [p for m, p in tg.calls if m == "sendPhoto" and p["markup"]][-1]
    assert preview["caption"] == (f"{BAG_TEXT}\n#VS_сумки\n\n{TAIL_ALBUM}")
    assert "Хештег: #VS_сумки." in preview_header(tg)
    press(upd, bot, tg)
    assert tg.channel_posts[0][1] == preview["caption"]


def test_unknown_thing_asks_then_her_no_hashtag_is_explicit(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text="Тест 6: щось гарне. 500грн")
    assert tg.texts(SELLER)[-1].startswith("Не можу визначити хештег — що це за річ?")
    assert tg.last_preview is None

    bot.handle(upd(message=message(30, text="Без хештега")))
    clock.pass_(seconds=4)
    bot.tick()

    preview = [p for m, p in tg.calls if m == "sendPhoto" and p["markup"]][-1]
    assert preview["caption"] == f"Тест 6: щось гарне. 500грн\n\n{TAIL}"  # фраза не в пості
    assert "Без хештега — як ти написала." in preview_header(tg)


def test_other_thing_preview_says_without_hashtag(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text="Рукавиці шкіряні. 300грн")
    assert "Без хештега (рукавиці, шарфи тощо — за правилом без нього)." in preview_header(tg)


def test_her_hashtag_shown_in_preview(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    assert "Хештег твій: #VS_37_розмір." in preview_header(tg)


def test_only_no_hashtag_phrase_is_not_a_text(conn, bot, tg, upd, clock):
    tg.files["f1"] = jpeg()
    bot.handle(upd(message=photo(11, "f1", caption="без хештега")))
    clock.pass_(seconds=4)
    bot.tick()
    assert tg.texts(SELLER)[-1] == "Надішли текст поста до цих фото."
    assert tg.last_preview is None


# --- Кілька готових постів поспіль (переслані зі «Збережених») ---


def forwarded_post(upd, bot, tg, first_msg, group, caption, files, caption_at=0):
    """Пересланий альбом: частини з одним `media_group_id`, підпис на одній із частин."""
    origin = {"type": "user", "sender_user": {"id": SELLER}, "date": 1_791_000_000}
    for i in range(files):
        file_id = f"{group}-{i + 1}"
        tg.files[file_id] = jpeg(color=("red", "green", "blue")[i % 3])
        body = photo(first_msg + i, file_id, caption=caption if i == caption_at else None,
                     group=group if files > 1 else None)  # fmt: skip
        bot.handle(upd(message={**body, "forward_origin": origin}))


def previews(tg):
    return [p for m, p in tg.calls if m == "sendPhoto" and p["markup"]]


def test_three_forwarded_albums_three_previews_in_order(conn, bot, tg, upd, clock):
    texts = [
        "Кросівки Lowa 37 розміру. 1200грн",
        "Черевики Ecco 38 розмір. 1500грн",
        "Шкіряна сумка Furla. 1800грн",
    ]
    for n, text in enumerate(texts):
        forwarded_post(upd, bot, tg, 10 + n * 10, f"g{n}", text, files=3, caption_at=n % 3)
    clock.pass_(seconds=4)
    bot.tick()

    shown = previews(tg)
    assert [p["caption"].split("\n")[0] for p in shown] == texts
    assert [p["markup"]["inline_keyboard"][0][0]["callback_data"] for p in shown] == [
        "pub:1", "pub:2", "pub:3"
    ]  # fmt: skip
    assert [p["photo"] for p in shown] == ["g0-1", "g1-1", "g2-1"]  # перше фото свого альбому
    assert conn.execute("SELECT count(*) FROM bot_draft_parts").fetchone()[0] == 9

    # друга кнопка публікує лише другий пост
    query = {"id": "cb", "from": {"id": SELLER}, "data": "pub:2",
             "message": {"message_id": 0, "chat": chat()}}  # fmt: skip
    preview_ids = [
        r[0] for r in conn.execute("SELECT preview_message_id FROM bot_drafts ORDER BY id")
    ]
    query["message"]["message_id"] = preview_ids[1]
    bot.handle(upd(callback_query=query))

    assert len(tg.channel_posts) == 1
    assert tg.channel_posts[0][1].startswith(texts[1])
    assert [state(conn, i) for i in (1, 2, 3)] == ["preview", "published", "preview"]


def test_forwarded_single_photos_each_its_own_preview(conn, bot, tg, upd, clock):
    forwarded_post(upd, bot, tg, 10, "a", "Кеди 38 розмір. 800грн", files=1)
    forwarded_post(upd, bot, tg, 11, "b", "Кеди 39 розмір. 900грн", files=1)
    clock.pass_(seconds=4)
    bot.tick()
    assert [p["caption"].split("\n")[1] for p in previews(tg)] == [
        "#VS_38_розмір", "#VS_39_розмір"
    ]  # fmt: skip


@pytest.mark.parametrize("text_first", [False, True])
def test_album_without_caption_plus_separate_text_one_preview(
    conn, bot, tg, upd, clock, text_first
):
    if text_first:
        bot.handle(upd(message=message(9, text="Черевики Ecco 38 розмір. 1500грн")))
    forwarded_post(upd, bot, tg, 10, "g", None, files=3)
    if not text_first:
        clock.pass_(seconds=30)
        bot.handle(upd(message=message(20, text="Черевики Ecco 38 розмір. 1500грн")))
    clock.pass_(seconds=4)
    bot.tick()

    (shown,) = previews(tg)
    assert shown["caption"].startswith("Черевики Ecco 38 розмір. 1500грн\n#VS_38_розмір")
    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 1


@pytest.mark.parametrize("files", [1, 3])
def test_forwarded_ready_post_no_duplicates(conn, bot, tg, upd, clock, files):
    ready = f"Кросівки Lowa 37 розміру. 1200грн\n#VS_37_розмір\n\n{SIGN}"
    forwarded_post(upd, bot, tg, 10, "g", ready, files=files)
    clock.pass_(seconds=4)
    bot.tick()

    (shown,) = previews(tg)
    caption = shown["caption"]
    assert caption.count("#VS_") == 1 and caption.count("Пишіть сюди") == 1
    assert caption.count(SITE) == 1  # її рядка про сайт тут не було — наш один
    assert caption.count("фото в коментарях") == (1 if files > 1 else 0)
    assert caption.startswith(f"Кросівки Lowa 37 розміру. 1200грн\n#VS_37_розмір\n\n{TAIL}")
    assert "\n\n\n" not in caption


def test_ready_post_after_photos_without_caption_is_separate(conn, bot, tg, upd, clock):
    tg.files["f1"] = jpeg()
    bot.handle(upd(message=photo(9, "f1")))  # фото без підпису — чекає текст
    forwarded_post(upd, bot, tg, 10, "a", "Кеди 38 розмір. 800грн", files=1)
    clock.pass_(seconds=4)
    bot.tick()

    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 2
    assert [state(conn, 1), state(conn, 2)] == ["collecting", "preview"]
    assert "Надішли текст поста до цих фото." in tg.texts(SELLER)


def test_comments_line_in_the_middle_leaves_no_gap(conn, bot, tg, upd, clock):
    text = f"Кеди 38 розмір. 800грн\n\nВсі фото в коментарях ⬇️\n\n{SIGN}"
    forwarded_post(upd, bot, tg, 10, "g", text, files=2)
    clock.pass_(seconds=4)
    bot.tick()
    (shown,) = previews(tg)
    assert shown["caption"] == (f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{TAIL_ALBUM}")


@pytest.mark.parametrize(
    "old_line",
    ["Всі фото в коментарях ⬇️", "Більше фото в коментарях ⬇️", "Додаткові фото в коментарях ⬇️"],
)
def test_forwarded_comments_line_is_not_duplicated(conn, bot, tg, upd, clock, old_line):
    """Старий рядок («Всі…» / «Більше…») з пересланого поста прибрано, наш — один, в кінці."""
    text = f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{SIGN}\n\n{old_line}"
    forwarded_post(upd, bot, tg, 10, "g", text, files=2)
    clock.pass_(seconds=4)
    bot.tick()
    (shown,) = previews(tg)
    assert shown["caption"].count("фото в коментарях") == 1
    assert shown["caption"].endswith(TAIL_ALBUM)


def test_comments_line_between_paragraphs_leaves_no_gap(conn, bot, tg, upd, clock):
    text = "Кеди 38 розмір. 800грн\n\nВсі фото в коментарях ⬇️\n\nВідправка Новою поштою"
    forwarded_post(upd, bot, tg, 10, "g", text, files=2)
    clock.pass_(seconds=4)
    bot.tick()
    (shown,) = previews(tg)
    assert shown["caption"].startswith("Кеди 38 розмір. 800грн\n\nВідправка Новою поштою\n#VS_")


# --- «Редагувати» ---


def edit_query(upd, bot, tg, conn, action="edit", draft_id=1, message_id=None):
    if message_id is None:
        message_id = conn.execute(
            "SELECT preview_message_id FROM bot_drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0]
    query = {"id": "cb", "from": {"id": SELLER}, "data": f"{action}:{draft_id}",
             "message": {"message_id": message_id, "chat": chat()}}  # fmt: skip
    bot.handle(upd(callback_query=query))


def test_edit_sends_only_her_text_then_new_text_new_preview(conn, bot, tg, upd, clock):
    ready = f"Черевики Ecco 38 розмір. 1500грн\n#VS_38_розмір\n\n{SIGN}"
    forwarded_post(upd, bot, tg, 10, "g", ready, files=2)
    clock.pass_(seconds=4)
    bot.tick()
    old_preview = last_preview_id(tg)

    edit_query(upd, bot, tg, conn)

    assert state(conn) == "editing"
    assert ("editMessageReplyMarkup", {"chat_id": SELLER, "message_id": old_preview}) in tg.calls
    assert tg.texts(SELLER)[-2] == "Черевики Ecco 38 розмір. 1500грн"  # лише її текст
    assert tg.texts(SELLER)[-1].startswith("Надішли виправлений текст")
    press(upd, bot, tg, preview=old_preview)  # стара кнопка — нічого
    assert tg.answers()[-1].startswith("Ти редагуєш текст")
    assert tg.channel_posts == []

    bot.handle(upd(message=message(30, text="Черевики Ecco 39 розмір. 1700грн")))
    clock.pass_(seconds=4)
    bot.tick()

    assert state(conn) == "preview"
    new = previews(tg)[-1]
    assert new["caption"] == (f"Черевики Ecco 39 розмір. 1700грн\n#VS_39_розмір\n\n{TAIL_ALBUM}")
    press(upd, bot, tg)
    assert tg.channel_posts[0][1] == new["caption"]


def test_edit_keeps_her_own_new_hashtag(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text="Кеди 38 розмір. 800грн")
    edit_query(upd, bot, tg, conn)
    bot.handle(upd(message=message(30, text="Кеди 38 розмір. 800грн\n#VS_37_розмір")))
    clock.pass_(seconds=4)
    bot.tick()
    caption = previews(tg)[-1]["caption"]
    assert caption.count("#VS_") == 1 and "#VS_37_розмір" in caption


def test_cancel_edit_returns_same_preview(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    before = previews(tg)[-1]["caption"]
    edit_query(upd, bot, tg, conn)
    prompt = [p for m, p in tg.calls if m == "sendMessage" and p["markup"]][-1]
    assert prompt["markup"]["inline_keyboard"][0][0]["callback_data"] == "noedit:1"

    edit_query(upd, bot, tg, conn, action="noedit", message_id=0)
    clock.pass_(seconds=4)
    bot.tick()

    assert state(conn) == "preview"
    assert previews(tg)[-1]["caption"] == before
    assert "Редагування скасовано — ось прев'ю знову." in tg.texts(SELLER)


def test_photo_while_editing_is_new_draft_text_still_edits(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text="Кеди 38 розмір. 800грн")
    edit_query(upd, bot, tg, conn)
    tg.files["n1"] = jpeg()
    bot.handle(upd(message=photo(40, "n1")))  # фото — нова чернетка
    bot.handle(upd(message=message(41, text="Кеди 39 розмір. 900грн")))  # текст — редагування

    assert conn.execute("SELECT count(*) FROM bot_drafts").fetchone()[0] == 2
    draft1 = [p["text"] for p in bot._parts(1) if p["text"]]
    draft2 = bot._parts(2)
    assert draft1 == ["Кеди 39 розмір. 900грн"]
    assert [(p["file_id"], p["text"]) for p in draft2] == [("n1", None)]


def test_own_text_strips_what_bot_adds():
    texts = [f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{SIGN}"]
    assert core.own_text(texts) == "Кеди 38 розмір. 800грн"


# --- «Опублікувати всі» / «Скасувати всі» ---


def three_previews(upd, bot, tg, clock, texts=None):
    texts = texts or [
        "Кросівки Lowa 37 розміру. 1200грн",
        "Черевики Ecco 38 розмір. 1500грн",
        "Шкіряна сумка Furla. 1800грн",
    ]
    for n, text in enumerate(texts):
        forwarded_post(upd, bot, tg, 10 + n * 10, f"g{n}", text, files=2)
    clock.pass_(seconds=4)
    bot.tick()  # прев'ю
    clock.pass_(seconds=3)
    bot.tick()  # «Чернеток: N» — після них
    return texts


def summary(tg):
    return [p for m, p in tg.calls if m == "sendMessage" and p["text"].startswith("Чернеток")]


def press_all(upd, bot, tg, conn, action="puball", update=None):
    message_id = conn.execute("SELECT message_id FROM bot_summaries").fetchone()
    query = {
        "id": "cb",
        "from": {"id": SELLER},
        "data": f"{action}:0",
        "message": {"message_id": message_id[0] if message_id else 999, "chat": chat()},
    }
    update = update or upd(callback_query=query)  # fmt: skip
    bot.handle(update)
    return update


def test_summary_after_previews_with_buttons(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    (sent,) = summary(tg)
    assert sent["text"] == "Чернеток: 3"
    assert [b["callback_data"] for b in sent["markup"]["inline_keyboard"][0]] == [
        "puball:0", "cancelall:0"
    ]  # fmt: skip
    last_call = [m for m, p in tg.calls if m in ("sendMessage", "sendPhoto")][-1]
    assert last_call == "sendMessage"  # після всіх прев'ю


def test_one_draft_no_summary(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    assert summary(tg) == []


def test_publish_all_in_order_with_pause_each_once(conn, bot, tg, upd, clock):
    sleeps = []
    bot.sleep = sleeps.append
    texts = three_previews(upd, bot, tg, clock)

    update = press_all(upd, bot, tg, conn)
    bot.handle(update)  # повторна доставка
    press_all(upd, bot, tg, conn)  # друге натискання (кнопки вже немає)

    assert [c.split("\n")[0] for _, c in tg.channel_posts] == texts
    assert sleeps == [core.PUBLISH_ALL_PAUSE, core.PUBLISH_ALL_PAUSE]
    assert "Опубліковано 3, лишилось 0." in tg.texts(SELLER)
    assert [state(conn, i) for i in (1, 2, 3)] == ["published"] * 3


def test_publish_all_skips_drafts_waiting_for_her(conn, bot, tg, upd, clock):
    bot.sleep = lambda s: None
    three_previews(upd, bot, tg, clock, texts=[
        "Кросівки Lowa 37 розміру. 1200грн",
        "Кросівки Nike в ідеальному стані. 1500грн",  # розмір незрозумілий — питання
        "Шкіряна сумка Furla. 1800грн",
    ])  # fmt: skip
    assert summary(tg)[-1]["text"] == "Чернеток: 3"

    press_all(upd, bot, tg, conn)

    assert len(tg.channel_posts) == 2
    assert state(conn, 2) == "collecting"
    result = [t for t in tg.texts(SELLER) if t.startswith("Опубліковано 2")][-1]
    assert result == (
        "Опубліковано 2, лишилось 1.\n"
        "Чекають твоєї відповіді: «Кросівки Nike в ідеальному стані. 1500грн»."
    )


def test_publish_all_does_not_republish_published(conn, bot, tg, upd, clock):
    bot.sleep = lambda s: None
    three_previews(upd, bot, tg, clock)
    press(upd, bot, tg, draft_id=2, preview=conn.execute(
        "SELECT preview_message_id FROM bot_drafts WHERE id = 2").fetchone()[0])  # fmt: skip
    bot.tick()
    press_all(upd, bot, tg, conn)
    assert len(tg.channel_posts) == 3


def test_summary_updates_and_disappears(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    summary_id = conn.execute("SELECT message_id FROM bot_summaries").fetchone()[0]
    for draft_id in (1, 2):
        preview = conn.execute(
            "SELECT preview_message_id FROM bot_drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0]
        press(upd, bot, tg, draft_id=draft_id, preview=preview)
        bot.tick()
    edits = [p for m, p in tg.calls if m == "editMessageText"]
    assert [(e["message_id"], e["text"]) for e in edits] == [
        (summary_id, "Чернеток: 2"), (summary_id, "Чернеток: 1")
    ]  # fmt: skip
    assert edits[0]["markup"] is not None and edits[1]["markup"] is None  # одна — без кнопок
    assert conn.execute("SELECT count(*) FROM bot_summaries").fetchone()[0] == 0


def test_cancel_all(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    press_all(upd, bot, tg, conn, action="cancelall")
    assert [state(conn, i) for i in (1, 2, 3)] == ["canceled"] * 3
    assert tg.channel_posts == []
    assert "Скасовано чернеток: 3. Нічого не опубліковано." in tg.texts(SELLER)
    dropped = [p["message_id"] for m, p in tg.calls if m == "editMessageReplyMarkup"]
    previews_ids = [r[0] for r in conn.execute("SELECT preview_message_id FROM bot_drafts")]
    assert set(previews_ids) <= set(dropped)


def test_publish_all_already_running_does_nothing(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    with conn:
        conn.execute("UPDATE bot_summaries SET running = 1")  # інший запуск триває
    press_all(upd, bot, tg, conn)
    assert tg.channel_posts == []
    assert tg.answers()[-1] == "Уже публікую всі — зачекай"


def test_crash_during_publish_all_summary_comes_back(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    with conn:
        conn.execute("UPDATE bot_summaries SET running = 1")
    bot.recover()
    bot.tick()
    assert [p["text"] for p in summary(tg)] == ["Чернеток: 3", "Чернеток: 3"]


def test_publish_all_skips_expired_preview(conn, bot, tg, upd, clock):
    bot.sleep = lambda s: None
    three_previews(upd, bot, tg, clock)
    with conn:  # прев'ю першої — старше доби (tick ще не встиг позначити)
        conn.execute("UPDATE bot_drafts SET preview_at = '2020-01-01T00:00:00+03:00' WHERE id = 1")
    press_all(upd, bot, tg, conn)
    assert len(tg.channel_posts) == 2 and state(conn, 1) == "preview"


def test_photo_with_caption_while_editing_is_new_post(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1, text="Кеди 38 розмір. 800грн")
    edit_query(upd, bot, tg, conn)
    tg.files["n1"] = jpeg()
    bot.handle(upd(message=photo(40, "n1", caption="Кеди 40 розмір. 1000грн")))
    assert state(conn, 1) == "editing"
    assert [p["text"] for p in bot._parts(2)] == ["Кеди 40 розмір. 1000грн"]


def test_edit_second_draft_returns_first_to_preview(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    edit_query(upd, bot, tg, conn, draft_id=1)
    edit_query(upd, bot, tg, conn, draft_id=2)
    assert [state(conn, 1), state(conn, 2)] == ["collecting", "editing"]
    clock.pass_(seconds=4)
    bot.tick()
    assert state(conn, 1) == "preview"


def test_edit_on_old_preview_refused(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    edit_query(upd, bot, tg, conn, message_id=12345)
    assert state(conn) == "preview"
    assert tg.answers()[-1] == "Це старе прев'ю — натисни кнопку під останнім."


# --- «Чернеток: N» — завжди внизу ---


def summary_id(conn):
    return conn.execute("SELECT message_id FROM bot_summaries").fetchone()[0]


def last_preview_message(conn):
    return conn.execute("SELECT max(preview_message_id) FROM bot_drafts").fetchone()[0]


def test_summary_waits_until_previews_settle(conn, bot, tg, upd, clock):
    for n in range(2):
        forwarded_post(upd, bot, tg, 10 + n * 10, f"g{n}", f"Кеди {38 + n} розмір. 800грн", 2)
    clock.pass_(seconds=4)
    bot.tick()
    assert len(previews(tg)) == 2 and summary(tg) == []  # щойно прев'ю — ще не
    clock.pass_(seconds=1)
    bot.tick()
    assert summary(tg) == []
    clock.pass_(seconds=1)
    bot.tick()
    assert [p["text"] for p in summary(tg)] == ["Чернеток: 2"]
    assert summary_id(conn) > last_preview_message(conn)


def test_new_previews_move_summary_to_bottom(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    old = summary_id(conn)

    forwarded_post(upd, bot, tg, 100, "late", "Кеди 40 розмір. 900грн", files=2)
    clock.pass_(seconds=1)
    bot.tick()  # ще збирається — не чіпаємо
    assert [m for m, p in tg.calls if m == "deleteMessage"] == []
    clock.pass_(seconds=3)
    bot.tick()  # прев'ю
    clock.pass_(seconds=3)
    bot.tick()  # «Чернеток: 4» — унизу

    assert ("deleteMessage", {"chat_id": SELLER, "message_id": old}) in tg.calls
    assert [p["text"] for p in summary(tg)] == ["Чернеток: 3", "Чернеток: 4"]
    assert summary_id(conn) > last_preview_message(conn)
    assert conn.execute("SELECT count(*) FROM bot_summaries").fetchone()[0] == 1


def test_ten_previews_one_summary_no_flicker(conn, bot, tg, upd, clock):
    for n in range(10):  # пости надходять один за одним, прев'ю — у різних тактах
        forwarded_post(upd, bot, tg, 10 + n * 10, f"g{n}", f"Кеди {36 + n} розмір. 800грн", 1)
        clock.pass_(seconds=4)
        bot.tick()
    clock.pass_(seconds=3)
    bot.tick()
    assert len(previews(tg)) == 10
    assert [p["text"] for p in summary(tg)] == ["Чернеток: 10"]
    assert [m for m, p in tg.calls if m in ("deleteMessage", "editMessageText")] == []


def test_count_change_edits_in_place_without_moving(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    old = summary_id(conn)
    preview = conn.execute("SELECT preview_message_id FROM bot_drafts WHERE id = 3").fetchone()[0]
    press(upd, bot, tg, draft_id=3, preview=preview)
    clock.pass_(seconds=3)
    bot.tick()
    edits = [p for m, p in tg.calls if m == "editMessageText"]
    assert [(e["message_id"], e["text"]) for e in edits] == [(old, "Чернеток: 2")]
    assert [m for m, p in tg.calls if m == "deleteMessage"] == []
    assert summary_id(conn) == old


def test_old_summary_not_deletable_loses_buttons(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    old = summary_id(conn)
    tg.errors["deleteMessage"] = TelegramError("Telegram відмовив (deleteMessage, 400)")
    forwarded_post(upd, bot, tg, 100, "late", "Кеди 40 розмір. 900грн", files=2)
    clock.pass_(seconds=4)
    bot.tick()
    clock.pass_(seconds=3)
    bot.tick()
    assert ("editMessageReplyMarkup", {"chat_id": SELLER, "message_id": old}) in tg.calls
    assert summary_id(conn) != old and summary_id(conn) > last_preview_message(conn)


def test_summary_moves_once_when_previews_follow_each_other(conn, bot, tg, upd, clock):
    three_previews(upd, bot, tg, clock)
    forwarded_post(upd, bot, tg, 100, "a", "Кеди 40 розмір. 900грн", files=1)
    clock.pass_(seconds=4)
    bot.tick()  # прев'ю A; підсумок тепер вище за нього
    forwarded_post(upd, bot, tg, 110, "b", "Кеди 41 розмір. 900грн", files=1)
    clock.pass_(seconds=2, milliseconds=500)
    bot.tick()  # B ще збирається — підсумок не чіпаємо
    clock.pass_(seconds=1)
    bot.tick()  # прев'ю B
    clock.pass_(seconds=3)
    bot.tick()
    assert len([m for m, p in tg.calls if m == "deleteMessage"]) == 1
    assert summary(tg)[-1]["text"] == "Чернеток: 5"
    assert summary_id(conn) > last_preview_message(conn)


def test_polls_fast_right_after_previews(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    assert bot.waiting()  # «Чернеток: N» може знадобитись опустити
    clock.pass_(seconds=11)
    assert not bot.waiting()


# --- Сайт (SITE-001): бот лише ставить пост у чергу, мережі до сайту тут немає ---


@pytest.fixture
def site_on(monkeypatch):
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")


def site_jobs(conn):
    return conn.execute(
        "SELECT post_id, folder_name, posted_at, text, state FROM site_jobs"
    ).fetchall()


def test_published_post_queued_for_site_with_channel_date_and_zone(
    conn, bot, tg, upd, clock, site_on, monkeypatch
):
    monkeypatch.setattr(core.site, "Site", None)  # будь-яка спроба мережі до сайту — падіння
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg)

    ((post_id, folder, posted_at, text, job_state),) = site_jobs(conn)
    assert (post_id, folder, job_state) == (1401, "1401", "waiting")  # SITE-002: через 7 днів
    assert text == tg.channel_posts[0][1]  # текст поста як у каналі
    moment = datetime.fromisoformat(posted_at)
    assert moment.utcoffset() is not None and moment.timestamp() == 1_791_960_000
    not_before = conn.execute("SELECT not_before FROM site_jobs").fetchone()[0]
    assert datetime.fromisoformat(not_before).timestamp() == 1_791_960_000 + 7 * 86400
    assert state(conn) == "published"


def test_site_switched_off_nothing_queued(conn, bot, tg, upd, clock, monkeypatch):
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg)

    assert site_jobs(conn) == []
    assert state(conn) == "published"


def test_site_queue_failure_never_breaks_channel_publish(
    conn, bot, tg, upd, clock, site_on, monkeypatch
):
    def broken(*args):
        raise RuntimeError("база зайнята")

    monkeypatch.setattr(core.site, "enqueue", broken)
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg)

    assert state(conn) == "published"
    assert any(t.startswith("Опубліковано:") for t in tg.texts(SELLER))


def test_edited_channel_post_updates_waiting_site_text_only_for_our_channel(
    conn, bot, tg, upd, clock, site_on
):
    """SITE-002: пара ще чекає сайту — правка поста в каналі дає новий текст; чужий канал — ні."""
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg)
    other = {"chat": {"id": -100555}, "message_id": 1401, "caption": "чужий"}
    bot.handle(upd(edited_channel_post=other))
    assert site_jobs(conn)[0][3] == tg.channel_posts[0][1]

    ours = {"chat": {"id": CHANNEL}, "message_id": 1401, "caption": "Кросівки Lowa 37. 900грн"}
    bot.handle(upd(edited_channel_post=ours))

    assert site_jobs(conn)[0][3] == "Кросівки Lowa 37. 900грн"
    assert conn.execute("SELECT count(*) FROM bot_updates").fetchone()[0] >= 2  # не повторюємо


def test_channel_refusal_not_queued_for_site(conn, bot, tg, upd, clock, site_on):
    send_post(bot, tg, upd, clock)
    tg.errors["sendPhoto"] = TelegramError("ні", code=400)
    press(upd, bot, tg)

    assert site_jobs(conn) == []


# --- Посилання в тексті замість кнопок (BOT-006) ---


def edits(tg):
    """Виправлення підпису поста каналу (посилання «Запитати» з номером поста)."""
    return [p for m, p in tg.calls if m == "editMessageCaption"]


def channel_sends(tg):
    return [p for m, p in tg.calls if m == "sendPhoto" and p["chat_id"] == CHANNEL]


def publish_one(bot, tg, upd, clock, files=3):
    send_post(bot, tg, upd, clock, files=files)
    press(upd, bot, tg)


def ask_link(post):
    return (
        "https://t.me/viktoriaviktoriatg?text="
        "%D0%92%D1%96%D1%82%D0%B0%D1%8E%21%20%D0%A6%D1%96%D0%BA%D0%B0%D0%B2%D0%B8%D1%82%D1%8C"
        "%20%D1%86%D1%8F%20%D0%BF%D0%B0%D1%80%D0%B0%3A%20" + post
    )


def test_post_goes_with_chat_link_then_caption_gets_post_link(conn, bot, tg, upd, clock):
    publish_one(bot, tg, upd, clock)

    (sent,) = channel_sends(tg)
    ask, site = sent["entities"]
    assert ask["url"] == "https://t.me/viktoriaviktoriatg"  # номера поста ще немає
    assert site["url"] == "https://viktoriashoes.com.ua"
    (fix,) = edits(tg)
    assert (fix["chat_id"], fix["message_id"]) == (CHANNEL, 1401)
    assert fix["caption"] == sent["caption"]  # текст той самий, міняється лише посилання
    ask, site = fix["entities"]
    assert ask["url"] == ask_link("https%3A%2F%2Ft.me%2Fc%2F2394827824%2F1401")  # закритий канал
    assert utf16(fix["caption"], ask["offset"], ask["length"]) == SIGN
    assert utf16(fix["caption"], site["offset"], site["length"]) == SITE


def test_no_buttons_under_the_post_and_no_reply_markup_calls_on_channel(conn, bot, tg, upd, clock):
    publish_one(bot, tg, upd, clock)

    (sent,) = channel_sends(tg)
    assert sent["markup"] is None
    assert not [
        p
        for m, p in tg.calls
        if m in ("setButton", "editMessageReplyMarkup") and p.get("chat_id") == CHANNEL
    ]
    assert not hasattr(core, "ask_markup") and not hasattr(core, "PHOTO_SETTING")


def test_album_comments_add_no_buttons_and_no_second_edit(conn, bot, tg, upd, clock):
    publish_one(bot, tg, upd, clock)
    bot.handle(upd(message=forwarded(77, 1401)))  # пересланий у групу пост → альбом
    assert comments(conn) == "done"
    assert len(edits(tg)) == 1  # «Коментарі» лишаються: кнопок не ставимо, підпис не чіпаємо
    assert not [m for m, _ in tg.calls if m == "setButton"]


def test_post_text_in_database_is_plain_without_markup(conn, bot, tg, upd, clock):
    publish_one(bot, tg, upd, clock)

    text = conn.execute("SELECT text FROM posts WHERE post_id = 1401").fetchone()[0]
    assert text == edits(tg)[0]["caption"] == core.caption_of([TEXT], 3)
    assert "<" not in text and "](" not in text  # без HTML і markdown
    assert text.endswith(f"{TAIL_ALBUM}")


def test_one_photo_post_has_site_line_but_no_comments_line(conn, bot, tg, upd, clock):
    publish_one(bot, tg, upd, clock, files=1)

    (sent,) = channel_sends(tg)
    assert sent["caption"].endswith(TAIL) and MORE not in sent["caption"]
    assert len(sent["entities"]) == 2


def test_uses_public_username_and_asks_getchat_once(conn, bot, tg, upd, clock):
    tg.chat_info = {"username": "viktoriashoes"}
    publish_one(bot, tg, upd, clock)
    clock.pass_(minutes=5)
    tg.files["g"] = jpeg()
    bot.handle(upd(message=photo(21, "g", caption=TEXT)))
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg, draft_id=2)

    first, second = edits(tg)
    assert unquote(first["entities"][0]["url"]).endswith("https://t.me/viktoriashoes/1401")
    assert second["message_id"] == 1402
    assert sum(1 for m, _ in tg.calls if m == "getChat") == 1


def test_caption_fix_error_never_breaks_publication_and_retries(conn, bot, tg, upd, clock):
    tg.errors["editMessageCaption"] = TelegramError("мережа", uncertain=True)
    publish_one(bot, tg, upd, clock)

    assert state(conn) == "published"
    assert len(tg.channel_posts) == 1
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
    assert any(t.startswith("Опубліковано:") for t in tg.texts(SELLER))

    tg.errors.clear()
    bot.tick()  # ще рано
    assert len(edits(tg)) == 1
    clock.pass_(seconds=31)
    bot.tick()
    assert len(edits(tg)) == 2
    assert "t.me%2Fc%2F2394827824%2F1401" in edits(tg)[-1]["entities"][0]["url"]
    clock.pass_(hours=1)
    bot.tick()
    assert len(edits(tg)) == 2  # виправлено — більше не пробуємо
    assert len(tg.channel_posts) == 1  # пост удруге — ніколи


def test_caption_fix_429_waits_retry_after_then_gives_up_to_owner(conn, bot, tg, upd, clock):
    tg.errors["editMessageCaption"] = TelegramError("забагато", code=429, retry_after=90)
    publish_one(bot, tg, upd, clock)
    clock.pass_(seconds=60)
    bot.tick()
    assert len(edits(tg)) == 1  # retry_after 90 с ще не минуло
    for _ in range(5):
        clock.pass_(minutes=15)
        bot.tick()
    assert len(edits(tg)) == 4  # перша + 3 повтори, далі здаємось
    assert any("Пишіть сюди" in t for t in tg.texts(OWNER))
    assert state(conn) == "published" and len(tg.channel_posts) == 1


def test_caption_fix_not_modified_counts_as_done(conn, bot, tg, upd, clock):
    tg.errors["editMessageCaption"] = TelegramError(
        "400", code=400, description="Bad Request: message is not modified"
    )
    publish_one(bot, tg, upd, clock)
    clock.pass_(hours=1)
    bot.tick()
    assert len(edits(tg)) == 1
    assert not any("Пишіть сюди" in t for t in tg.texts(OWNER))


def test_redelivery_does_not_duplicate_post_or_caption_fix(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    query = {
        "id": "cb", "from": {"id": SELLER}, "data": "pub:1",
        "message": {"message_id": last_preview_id(tg), "chat": chat()},
    }  # fmt: skip
    update = upd(callback_query=query)
    bot.handle(update)
    bot.handle(update)
    bot.handle(upd(callback_query=query))

    assert len(tg.channel_posts) == 1
    assert len(edits(tg)) == 1


def test_ask_switched_off_signature_stays_text_and_nothing_to_fix(
    conn, bot, tg, upd, clock, monkeypatch
):
    monkeypatch.setenv("SHAFA_ASK_BUTTON", "ні")
    publish_one(bot, tg, upd, clock)

    (sent,) = channel_sends(tg)
    assert SIGN in sent["caption"]
    assert [e["url"] for e in sent["entities"]] == ["https://viktoriashoes.com.ua"]
    assert edits(tg) == []  # посилання «Запитати» немає — виправляти нічого


def test_site_switched_off_no_site_line_only_ask_link(conn, bot, tg, upd, clock, monkeypatch):
    monkeypatch.setenv("SHAFA_SITE_BUTTON", "ні")
    publish_one(bot, tg, upd, clock)

    (sent,) = channel_sends(tg)
    assert SITE not in sent["caption"] and SIGN in sent["caption"]
    (ask,) = sent["entities"]
    assert ask["url"] == "https://t.me/viktoriaviktoriatg"
    (fix,) = edits(tg)
    assert len(fix["entities"]) == 1 and "%3A%20" in fix["entities"][0]["url"]


def test_both_off_plain_caption_without_entities(conn, bot, tg, upd, clock, monkeypatch):
    monkeypatch.setenv("SHAFA_ASK_BUTTON", "ні")
    monkeypatch.setenv("SHAFA_SITE_BUTTON", "ні")
    publish_one(bot, tg, upd, clock)

    (sent,) = channel_sends(tg)
    assert not sent["entities"] and SITE not in sent["caption"]
    assert edits(tg) == [] and state(conn) == "published"


def test_forwarded_ready_post_gets_links_once(conn, bot, tg, upd, clock):
    ready = f"Кросівки Lowa 37 розміру. 1200грн\n#VS_37_розмір\n\n{SIGN}\n\n{SITE}\n\n{MORE}"
    forwarded_post(upd, bot, tg, 10, "g", ready, files=2)
    clock.pass_(seconds=4)
    bot.tick()
    press(upd, bot, tg)

    (sent,) = channel_sends(tg)
    assert sent["caption"].count(SITE) == 1 and sent["caption"].count(MORE) == 1
    assert sent["caption"].count("Пишіть сюди") == 1 and len(sent["entities"]) == 2


def test_edit_after_our_caption_fix_changes_nothing(conn, tg, upd, clock, root, site_on):
    """Telegram присилає `edited_channel_post` і після нашого редагування підпису: той самий текст —
    без дії (оновлення не йде в чергу сайту, у журналі нічого)."""
    log = []
    settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
    bot = Bot(conn, tg, settings, clock=clock, log=log.append, sleep=lambda s: None)
    publish_one(bot, tg, upd, clock)
    before = conn.execute("SELECT updated_at, text FROM site_jobs").fetchone()
    log.clear()
    clock.pass_(minutes=1)

    same = {"chat": {"id": CHANNEL}, "message_id": 1401, "caption": tg.channel_posts[0][1]}
    bot.handle(upd(edited_channel_post=same))

    assert not any("змінено" in line for line in log)
    assert tuple(conn.execute("SELECT updated_at, text FROM site_jobs").fetchone()) == tuple(before)


# --- BOT-008: «Viktoria Shoes» у посиланні на сайт ---


def test_bot008_site_block_is_two_lines_one_link_without_neighbours():
    caption = core.caption_of(["🔥 Кеди 38 розмір. 800грн"], 2)
    assert SITE == "🛍 Більше взуття на сайті\nViktoria Shoes 👈"
    assert f"\n\n{SITE}\n\n{MORE}" in caption
    ents = [
        e for e in core.caption_entities(caption, "https://t.me/c/1/2") if e["url"] == core.SITE_URL
    ]
    (site,) = ents  # одна сутність на обидва рядки
    assert utf16(caption, site["offset"], site["length"]) == SITE  # без порожніх рядків і сусідів
    assert core.caption_length(SITE) == site["length"]


def test_bot008_old_one_line_site_is_replaced_not_duplicated():
    text = f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{SIGN}\n\n{OLD_SITE}\n\n{MORE}"
    caption = core.caption_of([text], 3)
    assert caption == f"Кеди 38 розмір. 800грн\n#VS_38_розмір\n\n{TAIL_ALBUM}"
    assert "Viktoria Shoes" in caption and caption.count("Більше взуття") == 1


def test_bot008_old_format_alone_still_one_link():
    old = f"Кеди 38 розмір\n\n{SIGN}\n\n{OLD_SITE}"
    (_, site) = core.caption_entities(old)
    assert utf16(old, site["offset"], site["length"]) == OLD_SITE


def test_bot008_own_text_drops_both_formats():
    for block in (SITE, OLD_SITE):
        assert (
            core.own_text([f"Кеди 38 розмір. 800грн\n\n{SIGN}\n\n{block}"])
            == "Кеди 38 розмір. 800грн"
        )


def test_bot008_switched_off_has_neither_line(monkeypatch):
    monkeypatch.setenv("SHAFA_SITE_BUTTON", "ні")
    caption = core.caption_of([f"Кеди 38 розмір. 800грн\n\n{SITE}"], 2)
    assert "Viktoria Shoes" not in caption and "Більше взуття" not in caption
    assert all(e["url"] != core.SITE_URL for e in core.caption_entities(caption))
