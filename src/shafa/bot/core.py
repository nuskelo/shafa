"""Логіка бота без мережі: повідомлення → чернетка → прев'ю → «Опублікувати» → канал + папка.

Рішення BOT-001:
- D2: без натискання «Опублікувати» нічого не публікується; прев'ю дійсне 24 год; повторне
  натискання й повторна доставка — один пост; «публікую» записується ДО відправки в канал;
  невизначена відповідь — не повторюємо, кажемо Власнику.
- D3: фото «як фото» і файлами; у папку — що прийшло (файл — оригінал), у канал — перше фото,
  завантажене як фото.
- D4: файлом лише JPG і PNG (PNG → JPG), інше — «Надішли як звичайне фото або JPG».
- D5: усе з одного чату протягом 2 хв від останнього повідомлення — одна чернетка.
- D6: підпис — як у її постах: порожній рядок, «Пишіть сюди 👉 @…», без форматування.
- D7: дата поста — дата повідомлення в каналі, місцевий час без поясу (як в Експорті).
- D8: папка з номером уже є — фото в «<номер>-бот» і повідомлення Власнику.
"""

import io
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from shafa import rules
from shafa.bot.telegram import Telegram, TelegramError, Upload
from shafa.db import now

SIGNATURE = "Пишіть сюди 👉 @viktoriaviktoriatg"  # як у її постах (звірено з Експортом)
WINDOW = timedelta(minutes=2)  # D5
SETTLE = timedelta(seconds=3)  # тиша після останнього повідомлення — показуємо прев'ю
PREVIEW_VALID = timedelta(hours=24)  # D2
MAX_PHOTOS = 10
CAPTION_MAX = 1024  # Bot API: підпис фото 0–1024 символи
DOWNLOAD_MAX = 20 * 1024 * 1024  # Bot API: бот завантажує файли до 20 МБ
CHANNEL_PHOTO_MAX = 10 * 1024 * 1024  # Bot API: фото завантаженням до 10 МБ
CHANNEL_SIDE = 2560  # якщо оригінал не влазить у ліміти фото — зменшуємо копію для каналу

PUBLISH = "pub"
CANCEL = "cancel"

TEXTS = {
    "help": "Надішли текст поста і фото (до 10) — я покажу, як буде в каналі, і чекатиму "
    "«Опублікувати».",
    "unsupported": "Цей файл я не прочитаю. Надішли це фото як звичайне фото або JPG.",
    "too_big": "Файл більший за 20 МБ — Telegram не дає його боту. Надішли як звичайне фото.",
    "not_photo": "Я приймаю лише текст і фото. Надішли фото як звичайне фото або JPG.",
    "need_photos": "Надішли фото до цього тексту (до 10).",
    "need_text": "Надішли текст поста до цих фото.",
    "too_many": "Забагато фото: {n}. Максимум 10 — надішли пост заново.",
    "too_long": "Текст задовгий: {n} символів разом із підписом, максимум 1024. Скороти й надішли "
    "пост заново.",
    "preview": "Так буде в каналі (фото: {n}; у канал — перше, у папку для Шафи — усі). "
    "Опублікувати?",
    "publishing": "Публікую…",
    "already": "Цей пост уже опубліковано: {link}",
    "busy": "Уже публікую — зачекай",
    "uncertain_seller": "Не впевнений, чи пост з'явився в каналі — перевір канал. Повторно я не "
    "публікую, Власник уже знає.",
    "expired": "Прев'ю застаріло — надішли пост ще раз.",
    "canceled": "Скасовано. Нічого не опубліковано.",
    "gone": "Цієї чернетки вже немає — надішли пост ще раз.",
    "old_preview": "Це старе прев'ю — натисни кнопку під останнім.",
    "updating": "Ти ще дописуєш — за мить покажу нове прев'ю.",
    "failed": "Не вийшло: {why}. Нічого не опубліковано — надішли пост ще раз.",
    "done": "Опубліковано: {link}\nЗбережено фото для Шафи: {n}.",
    "late": "(отримано о {time}, опубліковано із запізненням)",
}


class PhotoError(Exception):
    """Фото не прочитати. Текст — для Продавчині."""


@dataclass(frozen=True)
class Settings:
    channel_id: int  # куди публікувати (у форматі Bot API: -100…)
    seller_chat: int | None
    owner_chat: int | None
    root: Path  # Головна папка

    @property
    def chats(self) -> set[int]:
        return {c for c in (self.seller_chat, self.owner_chat) if c is not None}


def caption_of(texts: list[str]) -> str:
    """Текст поста + підпис, як у її постах (D6). Підпис уже є — не дублюємо."""
    body = "\n".join(t.strip() for t in texts if t and t.strip())
    if rules.SIGNATURE.search(body):
        return body
    return f"{body}\n\n{SIGNATURE}"


def caption_length(text: str) -> int:
    """Довжина, як її рахує Telegram (UTF-16: емодзі — 2)."""
    return len(text.encode("utf-16-le")) // 2


def post_link(channel_id: int, message_id: int) -> str:
    return f"https://t.me/c/{str(channel_id).removeprefix('-100')}/{message_id}"


def local_date(unix: int) -> str:
    """Дата Telegram → місцевий час без поясу, як в Експорті (D7)."""
    return datetime.fromtimestamp(unix, UTC).astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def as_jpeg(data: bytes, kind: str) -> bytes:
    """Фото для папки: JPG як є (оригінал), PNG → JPG (D4). Бите → PhotoError."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if kind != "png":
                if image.format != "JPEG":
                    raise PhotoError("фото не JPG")
                return data
            background = Image.new("RGB", image.size, "white")
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.getchannel("A"))
            out = io.BytesIO()
            background.save(out, "JPEG", quality=95)
            return out.getvalue()
    except (UnidentifiedImageError, OSError) as error:
        raise PhotoError("фото не відкривається") from error


def for_channel(data: bytes) -> bytes:
    """Копія першого фото для каналу: у межах лімітів фото Bot API (10 МБ, сума сторін ≤ 10000)
    і з поворотом за EXIF. Оригінал у папці не міняється."""
    with Image.open(io.BytesIO(data)) as image:
        turned = image.getexif().get(0x0112, 1) not in (None, 1)
        fits = len(data) <= CHANNEL_PHOTO_MAX and sum(image.size) <= 10000
        if fits and not turned:
            return data
        copy = ImageOps.exif_transpose(image).convert("RGB")
        copy.thumbnail((CHANNEL_SIDE, CHANNEL_SIDE))
        out = io.BytesIO()
        copy.save(out, "JPEG", quality=90)
        return out.getvalue()


class Bot:
    def __init__(
        self,
        conn: sqlite3.Connection,
        tg: Telegram,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        log: Callable[[str], None] = print,
    ) -> None:
        self.conn, self.tg, self.settings, self.clock, self.log = conn, tg, settings, clock, log

    # --- Надсилання без падінь (відповідь не дійшла — не привід ламати обробку) ---

    def say(self, chat_id: int | None, text: str, markup: dict | None = None) -> dict | None:
        if chat_id is None:
            return None
        try:
            return self.tg.send_message(chat_id, text, markup)
        except TelegramError as error:
            self.log(f"Не вдалося відповісти в чат: {error}")
            return None

    def tell_owner(self, text: str) -> None:
        self.say(self.settings.owner_chat, "Бот: " + text)

    def _stamp(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    # --- Оновлення ---

    def handle(self, update: dict) -> None:
        """Одне оновлення Telegram. Уже оброблене (повторна доставка) — нічого не робить."""
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        if self.conn.execute(
            "SELECT 1 FROM bot_updates WHERE update_id = ?", (update_id,)
        ).fetchone():
            return
        if "callback_query" in update:
            self._callback(update_id, update["callback_query"])
        elif "message" in update:
            self._message(update_id, update["message"])
        else:
            self._mark(update_id)

    def _mark(self, update_id: int) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO bot_updates VALUES (?, ?)", (update_id, now()))

    def _allowed(self, chat: Any, user: Any = None) -> bool:
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return False
        if chat.get("id") not in self.settings.chats:
            return False
        return user is None or (isinstance(user, dict) and user.get("id") == chat.get("id"))

    def _message(self, update_id: int, message: dict) -> None:
        chat = message.get("chat")
        if not self._allowed(chat, message.get("from")):  # чужі — мовчки
            self._mark(update_id)
            return
        chat_id = chat["id"]
        text = message.get("text") or message.get("caption")
        part, refusal = self._part(message)
        if text and text.strip().startswith("/") and part is None:
            self._mark(update_id)
            self.say(chat_id, TEXTS["help"])
            return
        if refusal or (part is None and not text):
            self._mark(update_id)
            self.say(chat_id, TEXTS[refusal or "not_photo"])
            return
        stamp = self._stamp()
        with self.conn:
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            draft = self._open_draft(chat_id)
            if draft is None:
                draft_id = self.conn.execute(
                    "INSERT INTO bot_drafts (chat_id, state, created_at, last_message_at, "
                    "updated_at) VALUES (?, 'collecting', ?, ?, ?)",
                    (chat_id, stamp, stamp, stamp),
                ).lastrowid
            else:
                draft_id = draft["id"]
                self.conn.execute(
                    "UPDATE bot_drafts SET state = 'collecting', asked = 0, last_message_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (stamp, stamp, draft_id),
                )
            file_id, kind = part or (None, None)
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_draft_parts VALUES (?, ?, ?, ?, ?)",
                (draft_id, message.get("message_id"), text, file_id, kind),
            )

    def _part(self, message: dict) -> tuple[tuple[str, str] | None, str | None]:
        """Фото повідомлення: ((file_id, вид), None) або (None, код відмови); без фото — (None, None)."""
        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            largest = max(photo, key=lambda p: p.get("width", 0) * p.get("height", 0))
            return (largest["file_id"], "photo"), None
        document = message.get("document")
        if isinstance(document, dict):
            mime = (document.get("mime_type") or "").lower()
            name = (document.get("file_name") or "").lower()
            if (document.get("file_size") or 0) > DOWNLOAD_MAX:
                return None, "too_big"
            if mime == "image/jpeg" or name.endswith((".jpg", ".jpeg")):
                return (document["file_id"], "jpeg"), None
            if mime == "image/png" or name.endswith(".png"):
                return (document["file_id"], "png"), None
            return None, "unsupported"
        if any(k in message for k in ("video", "sticker", "voice", "audio", "animation")):
            return None, "not_photo"
        return None, None

    def _open_draft(self, chat_id: int) -> sqlite3.Row | None:
        """Чернетка, до якої додається повідомлення: остання в цьому чаті не пізніше 2 хв тому."""
        since = (self.clock() - WINDOW).isoformat(timespec="seconds")
        return self.conn.execute(
            "SELECT * FROM bot_drafts WHERE chat_id = ? AND state IN ('collecting', 'preview') "
            "AND last_message_at >= ? ORDER BY id DESC LIMIT 1",
            (chat_id, since),
        ).fetchone()

    # --- Прев'ю ---

    def waiting(self) -> bool:
        """Є чернетки, що чекають прев'ю (опитувати Telegram частіше)."""
        return bool(
            self.conn.execute(
                "SELECT 1 FROM bot_drafts WHERE state = 'collecting' AND asked = 0"
            ).fetchone()
        )

    def tick(self) -> None:
        """Чернетки, де настала тиша: прев'ю або прохання дослати фото/текст."""
        now_ = self.clock()
        settled = (now_ - SETTLE).isoformat(timespec="seconds")
        stale = (now_ - PREVIEW_VALID).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "UPDATE bot_drafts SET state = 'expired', updated_at = ? "
                "WHERE state IN ('collecting', 'preview') AND last_message_at < ? "
                "AND coalesce(preview_at, last_message_at) < ?",
                (self._stamp(), stale, stale),
            )
        drafts = self.conn.execute(
            "SELECT * FROM bot_drafts WHERE state = 'collecting' AND asked = 0 "
            "AND last_message_at <= ? ORDER BY id",
            (settled,),
        ).fetchall()
        for draft in drafts:
            self._preview(draft)

    def _parts(self, draft_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM bot_draft_parts WHERE draft_id = ? ORDER BY message_id", (draft_id,)
        ).fetchall()

    def _set(self, draft_id: int, **fields: Any) -> None:
        names = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(
                f"UPDATE bot_drafts SET {names}, updated_at = ? WHERE id = ?",
                (*fields.values(), self._stamp(), draft_id),
            )

    def _preview(self, draft: sqlite3.Row) -> None:
        chat_id, draft_id = draft["chat_id"], draft["id"]
        parts = self._parts(draft_id)
        photos = [p for p in parts if p["file_id"]]
        texts = [p["text"] for p in parts if p["text"]]
        if not photos or not texts:
            self._set(draft_id, asked=1)
            self.say(chat_id, TEXTS["need_photos" if not photos else "need_text"])
            return
        if len(photos) > MAX_PHOTOS:
            self._set(draft_id, state="failed", note="забагато фото")
            self.say(chat_id, TEXTS["too_many"].format(n=len(photos)))
            return
        caption = caption_of(texts)
        if caption_length(caption) > CAPTION_MAX:
            self._set(draft_id, state="failed", note="задовгий текст")
            self.say(chat_id, TEXTS["too_long"].format(n=caption_length(caption)))
            return
        if draft["preview_message_id"]:  # старе прев'ю цієї чернетки — без кнопок
            self._drop_buttons(chat_id, draft["preview_message_id"])
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Опублікувати", "callback_data": f"{PUBLISH}:{draft_id}"},
                    {"text": "Скасувати", "callback_data": f"{CANCEL}:{draft_id}"},
                ]
            ]
        }
        self.say(chat_id, TEXTS["preview"].format(n=len(photos)))
        first = photos[0]
        try:
            if first["kind"] == "photo":
                sent = self.tg.send_photo(chat_id, first["file_id"], caption, markup)
            else:
                sent = self.tg.send_document(chat_id, first["file_id"], caption, markup)
        except TelegramError as error:
            self.log(f"Прев'ю не надіслано: {error}")
            self._set(draft_id, asked=1)  # без кнопок публікувати нічого; вона надішле ще раз
            self.say(chat_id, TEXTS["failed"].format(why="прев'ю не надіслалось"))
            return
        self._set(
            draft_id, state="preview", preview_message_id=sent.get("message_id"),
            preview_at=self._stamp(),
        )  # fmt: skip

    def _drop_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            self.tg.drop_buttons(chat_id, message_id)
        except TelegramError as error:
            self.log(f"Кнопки не прибрано: {error}")

    # --- Кнопки ---

    def _answer(self, callback_id: str, text: str) -> None:
        try:
            self.tg.answer_callback(callback_id, text)
        except TelegramError as error:
            self.log(f"Відповідь на кнопку не дійшла: {error}")

    def _callback(self, update_id: int, query: dict) -> None:
        message = query.get("message") or {}
        chat = message.get("chat")
        if not self._allowed(chat, query.get("from")):
            self._mark(update_id)
            return
        action, _, raw_id = str(query.get("data") or "").partition(":")
        draft = None
        if raw_id.isdigit():
            draft = self.conn.execute(
                "SELECT * FROM bot_drafts WHERE id = ? AND chat_id = ?", (int(raw_id), chat["id"])
            ).fetchone()
        if draft is None or action not in (PUBLISH, CANCEL):
            self._mark(update_id)
            self._answer(query.get("id", ""), TEXTS["gone"])
            return
        if action == CANCEL:
            self._cancel(update_id, query, draft)
        else:
            self._publish(update_id, query, draft)

    def _cancel(self, update_id: int, query: dict, draft: sqlite3.Row) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            cursor = self.conn.execute(
                "UPDATE bot_drafts SET state = 'canceled', updated_at = ? "
                "WHERE id = ? AND state IN ('collecting', 'preview')",
                (self._stamp(), draft["id"]),
            )
        if cursor.rowcount == 1:
            self._answer(query.get("id", ""), TEXTS["canceled"])
            self._drop_buttons(draft["chat_id"], query["message"]["message_id"])
            self.say(draft["chat_id"], TEXTS["canceled"])
        else:
            self._answer(query.get("id", ""), self._state_text(draft["id"]))

    def _state_text(self, draft_id: int) -> str:
        row = self.conn.execute(
            "SELECT state, post_id FROM bot_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row["state"] == "published":
            return TEXTS["already"].format(link=post_link(self.settings.channel_id, row["post_id"]))
        if row["state"] in ("publishing", "uncertain"):
            return TEXTS["busy"]
        if row["state"] == "expired":
            return TEXTS["expired"]
        return TEXTS["gone"]

    # --- Публікація ---

    def _publish(self, update_id: int, query: dict, draft: sqlite3.Row) -> None:
        callback_id = query.get("id", "")
        draft_id, chat_id = draft["id"], draft["chat_id"]
        pressed = (query.get("message") or {}).get("message_id")
        fresh = (self.clock() - PREVIEW_VALID).isoformat(timespec="seconds")
        refusal = None
        if draft["state"] == "collecting":
            refusal = "updating"  # після прев'ю прийшло ще щось — буде нове прев'ю
        elif draft["state"] == "preview" and draft["preview_message_id"] != pressed:
            refusal = "old_preview"
        elif draft["state"] == "preview" and (draft["preview_at"] or "") < fresh:
            refusal = "expired"
            self._set(draft_id, state="expired")
        if refusal:
            self._mark(update_id)
            self._answer(callback_id, TEXTS[refusal])
            if refusal == "expired":
                self._drop_buttons(chat_id, pressed)
                self.say(chat_id, TEXTS["expired"])
            return
        with self.conn:  # «публікую» — ДО відправки в канал (D2); вдруге сюди не потрапить
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            cursor = self.conn.execute(
                "UPDATE bot_drafts SET state = 'publishing', updated_at = ? "
                "WHERE id = ? AND state = 'preview'",
                (self._stamp(), draft_id),
            )
        if cursor.rowcount == 0:
            self._answer(callback_id, self._state_text(draft_id))
            return
        self._answer(callback_id, TEXTS["publishing"])
        self._drop_buttons(chat_id, draft["preview_message_id"])

        parts = self._parts(draft_id)
        caption = caption_of([p["text"] for p in parts if p["text"]])
        try:
            photos = [self._download(p) for p in parts if p["file_id"]]
            channel_photo = for_channel(photos[0])
        except (TelegramError, PhotoError, OSError) as error:
            why = str(error) if isinstance(error, PhotoError) else "фото не завантажились"
            self.log(f"Чернетка {draft_id}: {error}")
            self._set(draft_id, state="failed", note=why)
            self.say(chat_id, TEXTS["failed"].format(why=why))
            return
        try:
            sent = self.tg.send_photo(
                self.settings.channel_id, Upload("1.jpg", channel_photo), caption
            )
        except TelegramError as error:
            self._channel_failed(draft_id, chat_id, error)
            return
        self._published(draft, sent, caption, photos)

    def _download(self, part: sqlite3.Row) -> bytes:
        info = self.tg.get_file(part["file_id"])
        if not isinstance(info, dict) or not info.get("file_path"):
            raise PhotoError("фото не завантажилось")
        data = self.tg.download(info["file_path"], DOWNLOAD_MAX)
        return as_jpeg(data, "jpeg" if part["kind"] == "photo" else part["kind"])

    def _channel_failed(self, draft_id: int, chat_id: int, error: TelegramError) -> None:
        self.log(f"Чернетка {draft_id}: канал — {error}")
        if error.uncertain:
            self._set(draft_id, state="uncertain", note=str(error))
            self.say(chat_id, TEXTS["uncertain_seller"])
            self.tell_owner(
                f"не впевнений, чи пост чернетки {draft_id} з'явився в каналі ({error}). "
                "Перевір канал; повторно не публікую."
            )
            return
        self._set(draft_id, state="failed", note=str(error))
        self.say(chat_id, TEXTS["failed"].format(why="канал не прийняв пост"))
        self.tell_owner(f"канал не прийняв пост чернетки {draft_id}: {error}")

    def _published(self, draft: sqlite3.Row, sent: Any, caption: str, photos: list[bytes]) -> None:
        draft_id, chat_id = draft["id"], draft["chat_id"]
        post_id = sent.get("message_id") if isinstance(sent, dict) else None
        if not isinstance(post_id, int):  # пост, мабуть, є, але номера не знаємо
            self._channel_failed(
                draft_id, chat_id, TelegramError("канал не повернув номер поста", uncertain=True)
            )
            return
        date = local_date(sent["date"]) if isinstance(sent.get("date"), int) else None
        date = date or self.clock().strftime("%Y-%m-%dT%H:%M:%S")
        with self.conn:
            self.conn.execute(
                "UPDATE bot_drafts SET state = 'published', post_id = ?, updated_at = ? "
                "WHERE id = ?",
                (post_id, self._stamp(), draft_id),
            )
            self.conn.execute(
                "INSERT INTO posts (post_id, date, text, source, added_at) "
                "VALUES (?, ?, ?, 'bot', ?) ON CONFLICT (post_id) DO NOTHING",
                (post_id, date, caption, now()),
            )
        link = post_link(self.settings.channel_id, post_id)
        try:
            folder = self._save_folder(post_id, photos)
        except OSError as error:
            self._set(draft_id, note=f"папка не створена: {error}")
            self.say(chat_id, TEXTS["done"].format(link=link, n=0))
            self.tell_owner(f"пост {post_id} опубліковано, але папку не створено: {error}")
            return
        self._set(draft_id, folder_name=folder.name)
        late = ""
        received = self.conn.execute(
            "SELECT last_message_at FROM bot_drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0]
        if self.clock() - datetime.fromisoformat(received) > timedelta(minutes=30):
            late = "\n" + TEXTS["late"].format(
                time=datetime.fromisoformat(received).strftime("%H:%M")
            )
        self.say(chat_id, TEXTS["done"].format(link=link, n=len(photos)) + late)
        if folder.name != str(post_id):
            self.tell_owner(
                f"папка {post_id} уже була — фото поста {post_id} збережено в «{folder.name}». "
                "Розберись, яка правильна."
            )

    def _save_folder(self, post_id: int, photos: list[bytes]) -> Path:
        """Фото в `<Головна>/<номер>` (1.jpg, 2.jpg … у порядку надсилання). Є — `<номер>-бот`."""
        root = self.settings.root
        folder = root / str(post_id)
        n = 1
        while folder.exists():
            folder = root / (f"{post_id}-бот" if n == 1 else f"{post_id}-бот-{n}")
            n += 1
        folder.mkdir(parents=True)
        for i, data in enumerate(photos, start=1):
            (folder / f"{i}.jpg").write_bytes(data)
        return folder

    # --- Старт ---

    def recover(self) -> None:
        """Після падіння: чернетки «публікую» — невідомо, чи пост у каналі. Не повторюємо (D2)."""
        rows = self.conn.execute(
            "SELECT id, chat_id FROM bot_drafts WHERE state = 'publishing'"
        ).fetchall()
        for row in rows:
            self._set(row["id"], state="uncertain", note="бот зупинився під час публікації")
            self.say(row["chat_id"], TEXTS["uncertain_seller"])
            self.tell_owner(
                f"бот зупинився під час публікації чернетки {row['id']} — перевір канал; "
                "повторно не публікую."
            )
