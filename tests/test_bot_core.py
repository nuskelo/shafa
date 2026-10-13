import io
from datetime import UTC, datetime, timedelta, timezone

import pytest
from PIL import Image

from shafa.bot import core
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import TelegramError, Upload

KYIV = timezone(timedelta(hours=3))
SELLER, OWNER, STRANGER = 111, 222, 999
CHANNEL = -1002394827824
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

    def _record(self, method, **params):
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        self.next_id += 1

    def send_message(self, chat_id, text, markup=None):
        self._record("sendMessage", chat_id=chat_id, text=text, markup=markup)
        return {"message_id": self.next_id}

    def send_photo(self, chat_id, photo, caption, markup=None):
        self._record("sendPhoto", chat_id=chat_id, photo=photo, caption=caption, markup=markup)
        if chat_id == CHANNEL:
            self.channel_posts.append((photo, caption))
            return {"message_id": 1400 + len(self.channel_posts), "date": 1_791_960_000}
        if markup is not None:
            self.last_preview = self.next_id
        return {"message_id": self.next_id}

    def send_document(self, chat_id, file_id, caption, markup=None):
        self._record(
            "sendDocument", chat_id=chat_id, file_id=file_id, caption=caption, markup=markup
        )
        self.last_preview = self.next_id
        return {"message_id": self.next_id}

    def get_file(self, file_id):
        self._record("getFile", file_id=file_id)
        return {"file_path": file_id}

    def download(self, file_path, limit):
        self._record("download", file_path=file_path)
        return self.files[file_path]

    def answer_callback(self, callback_id, text):
        self._record("answerCallbackQuery", text=text)

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
    settings = Settings(CHANNEL, SELLER, OWNER, root)
    return Bot(conn, tg, settings, clock=clock, log=lambda text: None)


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


def test_caption_like_her_posts():
    assert core.caption_of([TEXT]) == TEXT + "\n\nПишіть сюди 👉 @viktoriaviktoriatg"
    assert core.caption_of(["  Кеди  ", "", "38 розмір\n"]) == (
        "Кеди\n38 розмір\n\nПишіть сюди 👉 @viktoriaviktoriatg"
    )
    signed = "Кеди\n\nПишіть сюди 👉 @viktoriaviktoriatg"
    assert core.caption_of([signed]) == signed  # уже є — не дублюємо


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
    assert preview["caption"] == core.caption_of([TEXT])
    buttons = preview["markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["pub:1", "cancel:1"]
    assert tg.channel_posts == []  # без натискання нічого не публікується

    press(upd, bot, tg)

    ((photo_sent, caption),) = tg.channel_posts
    assert isinstance(photo_sent, Upload) and photo_sent.data == tg.files["f1"]
    assert caption == core.caption_of([TEXT])
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


def test_more_after_preview_makes_new_preview(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock, files=1)
    first_preview = last_preview_id(tg)
    tg.files["f9"] = jpeg()
    bot.handle(upd(message=photo(20, "f9")))
    assert state(conn) == "collecting"

    press(upd, bot, tg, preview=first_preview)  # стара кнопка, поки дописує
    assert tg.answers()[-1] == "Ти ще дописуєш — за мить покажу нове прев'ю."
    clock.pass_(seconds=4)
    bot.tick()
    assert ("editMessageReplyMarkup", {"chat_id": SELLER, "message_id": first_preview}) in tg.calls
    press(upd, bot, tg, preview=first_preview)
    assert tg.answers()[-1] == "Це старе прев'ю — натисни кнопку під останнім."
    assert tg.channel_posts == []

    press(upd, bot, tg)
    assert len(tg.channel_posts) == 1
    assert "Збережено фото для Шафи: 2." in tg.texts(SELLER)[-1]


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
    assert [m for m, p in tg.calls if m == "sendDocument"]  # прев'ю — тим самим файлом
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


@pytest.mark.parametrize(
    ("name", "mime"), [("IMG_1.HEIC", "image/heic"), ("фото.webp", "image/webp"), ("a.pdf", "")]
)
def test_unsupported_file_explained(conn, bot, tg, upd, name, mime):
    bot.handle(upd(message=document(11, "d1", name, mime)))
    assert tg.texts(SELLER) == ["Цей файл я не прочитаю. Надішли це фото як звичайне фото або JPG."]
    assert conn.execute("SELECT count(*) FROM bot_draft_parts").fetchone()[0] == 0


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


def test_too_long_text(conn, bot, tg, upd, clock):
    signature = len("\n\n" + core.SIGNATURE) + 1  # 👉 — 2 символи для Telegram
    send_post(bot, tg, upd, clock, files=1, text="я" * (1024 - signature + 1))
    assert state(conn) == "failed"
    assert tg.texts(SELLER)[-1].startswith("Текст задовгий: 1025 символів")


def test_longest_text_fits(conn, bot, tg, upd, clock):
    signature = len("\n\n" + core.SIGNATURE) + 1
    send_post(bot, tg, upd, clock, files=1, text="я" * (1024 - signature))
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
    send_post(bot, tg, upd, clock, files=1, text="Кеди Ecco 38. 900грн")
    press(upd, bot, tg, draft_id=2)

    export = {1402: Post(1402, "2020-01-01T00:00:00", "старий текст з Експорту")}
    result = scan(conn, export, list_folders(root, None))

    cards = {r["folder_name"]: r for r in conn.execute("SELECT * FROM items")}
    assert cards["1402"]["status"] == "new"
    assert cards["1401-бот"]["problem_code"] == "bad_folder_name"
    post = conn.execute("SELECT source, text FROM posts WHERE post_id = 1402").fetchone()
    assert tuple(post) == ("bot", core.caption_of(["Кеди Ecco 38. 900грн"]))  # Експорт не затер
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
