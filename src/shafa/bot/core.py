"""Логіка бота без мережі: повідомлення → чернетка → прев'ю → «Опублікувати» → канал + папка.

Рішення BOT-001:
- D2: без натискання «Опублікувати» нічого не публікується; прев'ю дійсне 24 год; повторне
  натискання й повторна доставка — один пост; «публікую» записується ДО відправки в канал;
  невизначена відповідь — не повторюємо, кажемо Власнику.
- D3: фото «як фото» і файлами; у папку — що прийшло (файл — оригінал), у канал — перше фото,
  завантажене як фото.
- D4: файлом лише JPG і PNG (PNG → JPG), інше — «Надішли як звичайне фото або JPG».
- D5 (уточнено Власником): кожен підписаний пост — фото з підписом або альбом, де є підпис, —
  окрема чернетка й окреме прев'ю (вона може переслати кілька готових постів поспіль). Частини
  одного альбому — завжди разом (`media_group_id`). Правило «2 хв» лише для неповного: фото без
  підпису чіпляються до тексту, надісланого окремо, і навпаки.
- D6: підпис — як у її постах: порожній рядок, «Пишіть сюди 👉 @…», без форматування.
- D7: дата поста — дата повідомлення в каналі, місцевий час без поясу (як в Експорті).
- D8: папка з номером уже є — фото в «<номер>-бот» і повідомлення Власнику.

Ітерація 2 (рішення Власника):
- Підпис: текст → `#VS_N_розмір` (окремий рядок, `hashtag.py`) → порожній рядок → «Пишіть сюди…» →
  порожній рядок → «Додаткові фото в коментарях ⬇️» (лише якщо фото ≥ 2). Незрозумілий розмір — питаємо.
- Фото ≥ 2: усі (і перше) — альбомом у коментарі під постом: відповідь на пересланий у групу
  обговорення пост. Не прийшов за 2 хв — їй «фото ще не додались», Власнику тривога, додаємо, коли
  прийде (до 24 год). Пост у канал удруге — ніколи.
- Прев'ю — точно як у каналі: фото-картинкою, з усіма рядками.
- «Редагувати»: бот надсилає лише її текст і чекає виправлений; наступний її текст (без фото) —
  новий текст чернетки, нове прев'ю, хештег перераховується. «Скасувати редагування» — прев'ю
  знову. Фото в цей час — нова чернетка.
- Чернеток ≥ 2 — повідомлення «Чернеток: N» з «Опублікувати всі / Скасувати всі». «Опублікувати
  всі» — по черзі, з паузою, кожна тим самим шляхом, що й одна кнопка (рівно один пост); ті, що
  чекають її відповіді, пропускає. Повторне натискання нічого не дублює.

BOT-006: кнопок під постом немає (з ними Telegram ховає «Коментарі»). Замість них — посилання в
тексті підпису (`caption_entities`, UTF-16): ціле «Пишіть сюди 👉 @…» — посилання «Запитати про цю
пару» (текст для поля вводу містить посилання на пост, яке відоме лише після `sendPhoto`, тож пост
іде з посиланням на чат, а `editMessageCaption` ставить правильне; помилка правки пост не ламає),
«🛍 Більше взуття на сайті⏎Viktoria Shoes 👈» (два рядки, одне посилання; BOT-008) — посилання на сайт, «Додаткові фото в коментарях ⬇️» —
звичайний рядок. BOT-007: між «Пишіть сюди…» і рядком сайту — порожній рядок. У базі (`posts.text`) — простий текст, як бачить покупець.
"""

import io
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from PIL import Image, ImageOps, UnidentifiedImageError

from shafa import config, rules, site
from shafa.bot import report
from shafa.bot.hashtag import ASK_KIND, decide
from shafa.bot.telegram import Telegram, TelegramError, Upload
from shafa.db import now

SELLER_USERNAME = "viktoriaviktoriatg"
SIGNATURE = f"Пишіть сюди 👉 @{SELLER_USERNAME}"  # як у її постах (звірено з Експортом)
SITE_URL = "https://viktoriashoes.com.ua"
SITE_TEXT = "🛍 Більше взуття на сайті\nViktoria Shoes 👈"
COMMENTS_TEXT = "Додаткові фото в коментарях ⬇️"  # лише коли фото ≥ 2
SITE_SETTING = (
    "SHAFA_SITE_BUTTON"  # рядок-посилання на сайт; типово так, «ні» — без нього (BOT-006)
)
ASK_SETTING = "SHAFA_ASK_BUTTON"  # «Пишіть сюди» — посилання «Запитати»; типово так, «ні» — текст
ASK_HELLO = "Вітаю! Цікавить ця пара: "
ASK_RETRY = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10))
WINDOW = timedelta(minutes=2)  # D5
ASK_WINDOW = timedelta(minutes=10)  # бот щось спитав — її відповідь чекаємо довше
COMMENTS_WAIT = timedelta(minutes=2)  # пересланий у групу пост
COMMENTS_GIVE_UP = timedelta(hours=24)
SETTLE = timedelta(seconds=3)  # тиша після останнього повідомлення — показуємо прев'ю
PREVIEW_VALID = timedelta(hours=24)  # D2
MAX_PHOTOS = 10
CAPTION_MAX = 1024  # Bot API: підпис фото 0–1024 символи
DOWNLOAD_MAX = 20 * 1024 * 1024  # Bot API: бот завантажує файли до 20 МБ
CHANNEL_PHOTO_MAX = 10 * 1024 * 1024  # Bot API: фото завантаженням до 10 МБ
CHANNEL_SIDE = 2560  # якщо оригінал не влазить у ліміти фото — зменшуємо копію для каналу

REPORT_COMMAND = "/zvit"  # звіт за сьогодні зараз; лише від Власника (BOT-009)
PUBLISH = "pub"
CANCEL = "cancel"
EDIT = "edit"
NO_EDIT = "noedit"
PUBLISH_ALL = "puball"
CANCEL_ALL = "cancelall"
PUBLISH_ALL_PAUSE = 3  # секунд між постами «Опублікувати всі»
SUMMARY_DELAY = timedelta(seconds=2)  # «Чернеток: N» — після останнього прев'ю, без мигтіння
OPEN = ("collecting", "preview", "editing")  # відкриті чернетки

TEXTS = {
    "help": "Надішли текст поста і фото (до 10) — я покажу, як буде в каналі, і чекатиму "
    "«Опублікувати».",
    "unsupported": "Цей файл я не прочитаю. Надішли це фото як звичайне фото або JPG.",
    "too_big": "Файл більший за 20 МБ — Telegram не дає його боту. Надішли як звичайне фото.",
    "not_photo": "Я приймаю лише текст і фото. Надішли фото як звичайне фото або JPG.",
    "need_photos": "Надішли фото до цього тексту (до 10).",
    "need_text": "Надішли текст поста до цих фото.",
    "need_size": "Не можу зрозуміти розмір. Допиши розмір (наприклад «38 розмір» або «ідуть на "
    "37») — і я покажу прев'ю.",
    "need_kind": "Не можу визначити хештег — що це за річ? Взуття — допиши розмір (наприклад «38 "
    "розмір»); сумка, гаманець, рюкзак чи ремінь — допиши це слово; хештег не потрібен — напиши "
    "«без хештега».",
    "too_many": "Забагато фото: {n}. Максимум 10 — надішли пост заново.",
    "too_long": "Текст задовгий: {n} символів разом із підписом, максимум 1024. Скороти й надішли "
    "пост заново.",
    "preview": "Так буде в каналі (фото: {n}; у канал — перше{comments}, у папку для Шафи — усі).\n"
    "{tag}.\nОпублікувати?",
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
    "editing": "Ти редагуєш текст — надішли новий текст або натисни «Скасувати редагування».",
    "edit_start": "Редагую",
    "edit_ask": "Надішли виправлений текст (хештег і «Пишіть сюди» я додам сам).",
    "edit_canceled": "Редагування скасовано — ось прев'ю знову.",
    "summary": "Чернеток: {n}",
    "all_busy": "Уже публікую всі — зачекай",
    "all_start": "Публікую всі…",
    "all_done": "Опубліковано {done}, лишилось {left}.",
    "all_waiting": "Чекають твоєї відповіді: {names}.",
    "all_canceled": "Скасовано чернеток: {n}. Нічого не опубліковано.",
    "comments_late": "Пост {link} є, але фото в коментарі ще не додались — додам, щойно зможу.",
    "comments_done": "Фото додано в коментарі до поста {link}.",
}


class PhotoError(Exception):
    """Фото не прочитати. Текст — для Продавчині."""


@dataclass(frozen=True)
class Settings:
    channel_id: int  # куди публікувати (у форматі Bot API: -100…)
    seller_chat: int | None
    owner_chat: int | None
    root: Path  # Головна папка
    discussion_id: int | None = None  # група обговорення каналу (коментарі)

    @property
    def chats(self) -> set[int]:
        return {c for c in (self.seller_chat, self.owner_chat) if c is not None}


NO_TAG = re.compile(r"^\s*без\s+хеш\s*тег\w*\s*[.!]?\s*$", re.IGNORECASE)
COMMENTS = rules.COMMENTS
SITE_LINE = rules.SITE_LINE
SHOP_LINE = rules.SHOP_LINE


@dataclass(frozen=True)
class Composed:
    """Підпис поста (`caption`), рядок про хештег для прев'ю (`tag_note`) або питання (`ask`)."""

    caption: str | None
    tag_note: str = ""
    ask: str | None = None


def compose(texts: list[str], photos: int = 1) -> Composed:
    """Підпис: текст → хештег → «Пишіть сюди…» → «🛍 Більше взуття на сайті⏎Viktoria Shoes 👈» → «Додаткові фото в
    коментарях» (фото ≥ 2). Хештег чи підпис уже є — не дублюємо. Хештег не визначено — питання,
    не мовчання; рядок «без хештега» від неї — явна згода без хештега (у пост він не йде)."""
    lines = "\n".join(t.strip() for t in texts if t and t.strip()).split("\n")
    no_tag = any(NO_TAG.match(line) for line in lines)
    # «Всі/Більше/Додаткові фото в коментарях» і «Більше взуття на сайті» з пересланого поста — прибираємо:
    # додамо самі
    lines = [
        line
        for line in lines
        if not (
            NO_TAG.match(line)
            or COMMENTS.match(line)
            or SITE_LINE.match(line)
            or SHOP_LINE.match(line)
        )
    ]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not body:
        return Composed(None, ask="text")
    decision = decide(body)
    if decision.ask and not no_tag:
        return Composed(None, ask=decision.ask)
    tag = None if no_tag else decision.tag
    note = decision.note
    if no_tag and not note.startswith("хештег твій"):
        note = "без хештега — як ти написала"
    lines = body.split("\n")
    signed = next((i for i, line in enumerate(lines) if rules.SIGNATURE.search(line)), None)
    head = lines if signed is None else lines[:signed]
    tail = [] if signed is None else lines[signed + 1 :]
    text = "\n".join(head).rstrip()
    if tag:  # хештег — перед її власним «Пишіть сюди…»
        text += f"\n{tag}"
    text += f"\n\n{SIGNATURE if signed is None else lines[signed].strip()}"
    if site_enabled():
        text += f"\n\n{SITE_TEXT}"
    if "\n".join(tail).strip():
        text += "\n\n" + "\n".join(tail).strip()
    if photos >= 2:
        text += f"\n\n{COMMENTS_TEXT}"
    return Composed(re.sub(r"\n{3,}", "\n\n", text).strip(), note)


def caption_of(texts: list[str], photos: int = 1) -> str | None:
    """Лише підпис (None — треба спитати її)."""
    return compose(texts, photos).caption


def caption_length(text: str) -> int:
    """Довжина, як її рахує Telegram (UTF-16: емодзі — 2)."""
    return len(text.encode("utf-16-le")) // 2


def post_link(channel_id: int, message_id: int) -> str:
    return f"https://t.me/c/{str(channel_id).removeprefix('-100')}/{message_id}"


def ask_enabled() -> bool:
    return (config.setting(ASK_SETTING) or "").strip().lower() not in ("0", "ні", "no", "false")


def site_enabled() -> bool:
    return (config.setting(SITE_SETTING) or "").strip().lower() not in ("0", "ні", "no", "false")


def is_not_modified(error: TelegramError) -> bool:
    return "not modified" in error.description.lower() or "not modified" in str(error).lower()


def ask_url(link: str | None = None) -> str:
    """Чат з нею; з `link` (посилання на пост) — ще й готовий текст у полі вводу (нічого не шле
    само). Посилання на пост відоме лише після публікації — до того лише чат."""
    url = f"https://t.me/{SELLER_USERNAME}"
    return f"{url}?text={quote(ASK_HELLO + link, safe='')}" if link else url


def caption_entities(caption: str, link: str | None = None) -> list[dict]:
    """Посилання в підписі: ціле «Пишіть сюди…» (перший такий рядок) — «Запитати» і рядок про
    сайт. Зсув і довжина — у UTF-16, як рахує Telegram (емодзі — 2); хештег Telegram знаходить сам.
    Без розмітки HTML: її текст може містити `<`, `&`, `_` — нічого не ламається."""
    found: list[dict] = []
    offset, asked = 0, False
    lines = caption.split("\n")
    skip = False
    for i, line in enumerate(lines):
        length = caption_length(line)
        if skip:  # другий рядок блоку сайту вже в посиланні
            skip = False
        elif not asked and rules.SIGNATURE.search(line):
            asked = True
            if ask_enabled():
                found.append(link_entity(offset, length, ask_url(link)))
        elif SITE_LINE.match(line) and site_enabled():
            # BOT-008: «🛍 Більше взуття на сайті» + «Viktoria Shoes 👈» — одне посилання на два рядки
            if i + 1 < len(lines) and SHOP_LINE.match(lines[i + 1]):
                length += 1 + caption_length(lines[i + 1])
                skip = True
            found.append(link_entity(offset, length, SITE_URL))
            if skip:
                offset += length - caption_length(lines[i + 1])
                continue
        offset += length + 1
    return found


def link_entity(offset: int, length: int, url: str) -> dict:
    return {"type": "text_link", "offset": offset, "length": length, "url": url}


def is_command(text: str, command: str) -> bool:
    """«/zvit», «/zvit@ім'я_бота» і «/zvit щось» — це команда; «/zvitt» — ні."""
    word = text.strip().split(maxsplit=1)[0]
    return word.split("@", 1)[0].lower() == command


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.conn, self.tg, self.settings, self.clock, self.log = conn, tg, settings, clock, log
        self.sleep = sleep
        self._username: str | None = None  # публічне ім'я каналу; "" — немає (кеш після getChat)
        # пост → (спроб було, наступна, підпис): посилання «Запитати» ще не виправлено
        self._links: dict[int, tuple[int, datetime, str]] = {}
        # BOT-009: за яку добу звіт Власнику вже надіслано і коли пробувати знову після помилки
        self._report_last: date | None | bool = False  # False — ще не читали файл
        self._report_retry: datetime | None = None
        self._report_bad: str | None = None  # неправильне значення, про яке вже написали

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
        elif "edited_channel_post" in update:
            self._channel_edit(update_id, update["edited_channel_post"])
        else:
            self._mark(update_id)

    def _channel_edit(self, update_id: int, post: Any) -> None:
        """Пост нашого каналу змінено: пара ще чекає сайту — піде новий текст (SITE-002)."""
        self._mark(update_id)
        if (
            not isinstance(post, dict)
            or (post.get("chat") or {}).get("id") != self.settings.channel_id
        ):
            return
        post_id, text = post.get("message_id"), post.get("caption", post.get("text"))
        if (
            isinstance(post_id, int)
            and isinstance(text, str)
            and site.edited(self.conn, post_id, text)
        ):
            self.log(f"Пост {post_id} змінено в каналі — на сайт піде новий текст")

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
        discussion = self.settings.discussion_id
        if discussion is not None and isinstance(chat, dict) and chat.get("id") == discussion:
            self._discussion(update_id, message)
            return
        if not self._allowed(chat, message.get("from")):  # чужі — мовчки
            self._mark(update_id)
            return
        chat_id = chat["id"]
        text = message.get("text") or message.get("caption")
        part, refusal = self._part(message)
        if text and text.strip().startswith("/") and part is None:
            self._mark(update_id)
            if chat_id == self.settings.owner_chat and is_command(text, REPORT_COMMAND):
                self._report_now(chat_id)  # позначку «надіслано» не міняє
            else:
                self.say(chat_id, TEXTS["help"])
            return
        if refusal or (part is None and not text):
            self._mark(update_id)
            self.say(chat_id, TEXTS[refusal or "not_photo"])
            return
        if part is None and self._edited(update_id, chat_id, message, text):
            return
        stamp = self._stamp()
        group = message.get("media_group_id")
        with self.conn:
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            draft = self._target_draft(chat_id, part is not None, bool(text), group)
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
                "INSERT OR IGNORE INTO bot_draft_parts VALUES (?, ?, ?, ?, ?, ?)",
                (draft_id, message.get("message_id"), text, file_id, kind, group),
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

    def _target_draft(
        self, chat_id: int, has_photo: bool, has_text: bool, group: str | None
    ) -> sqlite3.Row | None:
        """Чернетка, до якої додати повідомлення, або None — нова чернетка.

        - Частина альбому — до чернетки з цим альбомом.
        - Фото з підписом — новий пост, окрема чернетка.
        - Фото без підпису чи окремий текст — до останньої чернетки цього чату (не пізніше 2 хв; бот
          щось спитав — 10 хв), якщо їй бракує фото чи тексту або бот у ній щось спитав.
        """
        if group:
            row = self.conn.execute(
                "SELECT d.* FROM bot_drafts d JOIN bot_draft_parts p ON p.draft_id = d.id "
                "WHERE d.chat_id = ? AND p.media_group = ? AND d.state IN ('collecting', 'preview') "
                "ORDER BY d.id DESC LIMIT 1",
                (chat_id, group),
            ).fetchone()
            if row is not None:
                return row
        if has_photo and has_text:
            return None
        since = (self.clock() - WINDOW).isoformat(timespec="seconds")
        asked_since = (self.clock() - ASK_WINDOW).isoformat(timespec="seconds")
        latest = self.conn.execute(
            "SELECT * FROM bot_drafts WHERE chat_id = ? AND state IN ('collecting', 'preview') "
            "AND (last_message_at >= ? OR (state = 'collecting' AND asked = 1 "
            "AND last_message_at >= ?)) ORDER BY id DESC LIMIT 1",
            (chat_id, since, asked_since),
        ).fetchone()
        if latest is None:
            return None
        photos, texts = self.conn.execute(
            "SELECT count(file_id), count(text) FROM bot_draft_parts WHERE draft_id = ?",
            (latest["id"],),
        ).fetchone()
        if not photos or not texts or latest["asked"]:
            return latest
        return None

    # --- Прев'ю ---

    def waiting(self) -> bool:
        """Є чернетки, що чекають прев'ю, або щойно було прев'ю («Чернеток: N» має опуститись
        донизу) — опитувати Telegram частіше."""
        recent = (self.clock() - SUMMARY_DELAY * 5).isoformat(timespec="seconds")
        return bool(
            self.conn.execute(
                "SELECT 1 FROM bot_drafts WHERE (state = 'collecting' AND asked = 0) "
                "OR (state = 'preview' AND preview_at >= ?)",
                (recent,),
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
                "WHERE state IN ('collecting', 'preview', 'editing') AND last_message_at < ? "
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
        self._comments_overdue(now_)
        self._retry_links(now_)
        self._summaries()
        self._daily_report(now_)

    # --- Щоденний звіт Власнику (BOT-009) ---

    def _report_text(self, day: date) -> str:
        late = day != self.clock().astimezone().date()
        return report.build(self.conn, config.data_dir(), day, late=late)

    def _report_now(self, chat_id: int) -> None:
        """`/zvit`: звіт за сьогодні зараз. Позначку «надіслано» не чіпає."""
        try:
            text = self._report_text(self.clock().astimezone().date())
        except Exception as error:  # noqa: BLE001 — звіт не валить бота
            self.log(f"Звіт: {type(error).__name__}: {str(error)[:200]}")
            self.say(chat_id, "Не вдалося скласти звіт — подивись журнал бота.")
            return
        self.say(chat_id, text)

    def _daily_report(self, now_: datetime) -> None:
        """Раз на добу о `SHAFA_REPORT_AT` — звіт Власнику. Ноутбук спав — при першому `tick` тієї
        ж доби; пропущену добу — один раз «за вчора». Помилка Telegram — позначки немає, нова спроба
        не раніше ніж за 10 хв. Помилка звіту бота не зупиняє."""
        owner = self.settings.owner_chat
        if owner is None or (self._report_retry and now_ < self._report_retry):
            return
        try:
            at = report.at_time(self._report_log)
            if at is None:
                return
            data_dir = config.data_dir()
            if self._report_last is False:
                self._report_last = report.last_sent(data_dir)
            day = report.due(now_, self._report_last or None, at)
            if day is None:
                return
            if self.say(owner, self._report_text(day)) is None:
                self._report_retry = now_ + report.RETRY
                return
            # в пам'яті — навіть якщо файл не запишеться: вдруге не надішлемо
            self._report_last, self._report_retry = day, None
            report.mark_sent(data_dir, day, now_)
        except Exception as error:  # noqa: BLE001
            self._report_retry = now_ + report.RETRY
            self.log(f"Звіт: {type(error).__name__}: {str(error)[:200]}")

    def _report_log(self, text: str) -> None:
        if text != self._report_bad:
            self._report_bad = text
            self.log(text)

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
        composed = compose(texts, len(photos))
        caption = composed.caption
        if caption is None:  # хештег не визначено — питаємо, не публікуємо мовчки
            self._set(draft_id, asked=1)
            question = {"text": "need_text", ASK_KIND: "need_kind"}.get(composed.ask, "need_size")
            self.say(chat_id, TEXTS[question])
            return
        if caption_length(caption) > CAPTION_MAX:
            self._set(draft_id, state="failed", note="задовгий текст")
            self.say(chat_id, TEXTS["too_long"].format(n=caption_length(caption)))
            return
        if draft["preview_message_id"]:  # старе прев'ю цієї чернетки — без кнопок
            self._drop_buttons(chat_id, draft["preview_message_id"])
        markup = {
            "inline_keyboard": [
                [{"text": "Опублікувати", "callback_data": f"{PUBLISH}:{draft_id}"}],
                [
                    {"text": "Редагувати", "callback_data": f"{EDIT}:{draft_id}"},
                    {"text": "Скасувати", "callback_data": f"{CANCEL}:{draft_id}"},
                ],
            ]
        }
        comments = ", у коментарі — усі" if len(photos) >= 2 else ""
        tag_note = composed.tag_note[:1].upper() + composed.tag_note[1:]
        self.say(chat_id, TEXTS["preview"].format(n=len(photos), comments=comments, tag=tag_note))
        first = photos[0]
        try:  # прев'ю — як у каналі: фото-картинкою (файл — завантажуємо як фото)
            if first["kind"] == "photo":
                picture: Upload | str = first["file_id"]
            else:
                picture = Upload("1.jpg", for_channel(self._download(first)))
            sent = self.tg.send_photo(chat_id, picture, caption, markup, caption_entities(caption))
        except (TelegramError, PhotoError, OSError) as error:
            self.log(f"Прев'ю не надіслано: {error}")
            self._set(draft_id, asked=1)  # без кнопок публікувати нічого; вона надішле ще раз
            why = str(error) if isinstance(error, PhotoError) else "прев'ю не надіслалось"
            self.say(chat_id, TEXTS["failed"].format(why=why))
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
        if action in (PUBLISH_ALL, CANCEL_ALL):
            self._all(update_id, query, chat["id"], action)
            return
        draft = None
        if raw_id.isdigit():
            draft = self.conn.execute(
                "SELECT * FROM bot_drafts WHERE id = ? AND chat_id = ?", (int(raw_id), chat["id"])
            ).fetchone()
        if draft is None or action not in (PUBLISH, CANCEL, EDIT, NO_EDIT):
            self._mark(update_id)
            self._answer(query.get("id", ""), TEXTS["gone"])
            return
        handler = {
            CANCEL: self._cancel, PUBLISH: self._publish, EDIT: self._edit,
            NO_EDIT: self._no_edit,
        }[action]  # fmt: skip
        handler(update_id, query, draft)

    def _cancel(self, update_id: int, query: dict, draft: sqlite3.Row) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            cursor = self.conn.execute(
                "UPDATE bot_drafts SET state = 'canceled', updated_at = ? "
                "WHERE id = ? AND state IN ('collecting', 'preview', 'editing')",
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
        if row["state"] == "editing":
            return TEXTS["editing"]
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
        self._run_publish(draft)

    def _run_publish(self, draft: sqlite3.Row) -> None:
        """Чернетка вже `publishing` (записано в базі) → канал, папка, коментарі."""
        draft_id, chat_id = draft["id"], draft["chat_id"]
        self._drop_buttons(chat_id, draft["preview_message_id"])
        parts = self._parts(draft_id)
        files = [p for p in parts if p["file_id"]]
        caption = caption_of([p["text"] for p in parts if p["text"]], len(files))
        try:
            if caption is None:  # прев'ю пройшло, тож сюди не потрапляє; про всяк випадок
                raise PhotoError("розмір незрозумілий")
            photos = [self._download(p) for p in files]
            channel_photo = for_channel(photos[0])
        except (TelegramError, PhotoError, OSError) as error:
            why = str(error) if isinstance(error, PhotoError) else "фото не завантажились"
            self.log(f"Чернетка {draft_id}: {error}")
            self._set(draft_id, state="failed", note=why)
            self.say(chat_id, TEXTS["failed"].format(why=why))
            return
        try:
            sent = self.tg.send_photo(
                self.settings.channel_id, Upload("1.jpg", channel_photo), caption,
                entities=caption_entities(caption),
            )  # fmt: skip
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
        comments = "waiting" if len(photos) >= 2 else None
        with self.conn:
            self.conn.execute(
                "UPDATE bot_drafts SET state = 'published', post_id = ?, published_at = ?, "
                "comments = ?, updated_at = ? WHERE id = ?",
                (post_id, self._stamp(), comments, self._stamp(), draft_id),
            )
            self.conn.execute(
                "INSERT INTO posts (post_id, date, text, source, added_at) "
                "VALUES (?, ?, ?, 'bot', ?) ON CONFLICT (post_id) DO NOTHING",
                (post_id, date, caption, now()),
            )
        self._fix_link(post_id, caption)
        link = post_link(self.settings.channel_id, post_id)
        try:
            folder = self._save_folder(post_id, photos)
        except OSError as error:
            # без папки альбому для коментарів немає — Власник розбереться руками
            self._set(draft_id, note=f"папка не створена: {error}", comments=comments and "failed")
            self.say(chat_id, TEXTS["done"].format(link=link, n=0))
            self.tell_owner(f"пост {post_id} опубліковано, але папку не створено: {error}")
            return
        self._set(draft_id, folder_name=folder.name)
        self._to_site(post_id, folder.name, sent, caption)
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

    def _post_link(self, post_id: int) -> str:
        """`t.me/<ім'я>/<номер>`, якщо в каналу є публічне ім'я (getChat — один раз), інакше `t.me/c/…`."""
        if self._username is None:
            try:
                self._username = self.tg.get_chat(self.settings.channel_id).get("username") or ""
            except TelegramError as error:  # не кешуємо: спробуємо наступного разу
                self.log(f"getChat каналу: {error}")
        if self._username:
            return f"https://t.me/{self._username}/{post_id}"
        return post_link(self.settings.channel_id, post_id)

    def _fix_link(self, post_id: int, caption: str, tried: int = 0) -> None:
        """Пост пішов із посиланням «Пишіть сюди» на чат без тексту (номера ще не було): тепер
        `editMessageCaption` ставить посилання з готовим текстом про цю пару (BOT-006). Помилка
        публікацію не ламає: пост уже в каналі, лише повтор пізніше. Пост удруге — ніколи."""
        if not ask_enabled():  # посилання на пост у підписі нема — виправляти нічого
            return
        try:
            link = self._post_link(post_id)
            self.tg.edit_caption(
                self.settings.channel_id, post_id, caption, caption_entities(caption, link)
            )
        except TelegramError as error:
            if not is_not_modified(error):
                self._link_failed(post_id, caption, tried, error)
                return
        except Exception as error:  # noqa: BLE001 — канал важливіший за посилання
            self.log(f"Підпис поста {post_id}: {type(error).__name__}")
        self._links.pop(post_id, None)

    def _link_failed(self, post_id: int, caption: str, tried: int, error: TelegramError) -> None:
        tried += 1
        if tried > len(ASK_RETRY):
            self._links.pop(post_id, None)
            self.log(f"Підпис поста {post_id}: не вийшло ({error})")
            self.tell_owner(
                f"пост {post_id} опубліковано, але посилання «Пишіть сюди» не доповнено "
                "текстом про пару — воно веде просто в чат."
            )
            return
        wait = timedelta(seconds=error.retry_after) if error.retry_after else ASK_RETRY[tried - 1]
        self._links[post_id] = (tried, self.clock() + wait, caption)
        self.log(f"Підпис поста {post_id}: {error}; спробую ще раз")

    def _retry_links(self, now_: datetime) -> None:
        for post_id, (tried, when, caption) in list(self._links.items()):
            if when <= now_:
                self._fix_link(post_id, caption, tried)

    def _to_site(self, post_id: int, folder_name: str, sent: dict, caption: str) -> None:
        """Пост від бота → черга сайту (SITE-001). Лише запис у базу, мережі тут немає; будь-яка
        помилка сайту публікацію в канал не ламає."""
        try:
            unix = sent.get("date")
            moment = datetime.fromtimestamp(unix, UTC) if isinstance(unix, int) else self.clock()
            posted_at = moment.astimezone().isoformat(timespec="seconds")
            if site.enqueue(self.conn, post_id, folder_name, posted_at, caption):
                self.log(f"Пост {post_id} — у черзі на сайт")
        except Exception as error:  # noqa: BLE001 — канал важливіший за сайт
            self.log(f"Пост {post_id} не став у чергу сайту: {type(error).__name__}")

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
        """Після падіння: «публікую» — невідомо, чи пост у каналі; «надсилаю коментарі» —
        невідомо, чи альбом під постом. Не повторюємо (D2). «Опублікувати всі» перервалось —
        позначку знімаємо (чернетки, що встигли стати «публікую», — нижче, без повтору)."""
        with self.conn:
            self.conn.execute("DELETE FROM bot_summaries WHERE running = 1")
        for row in self.conn.execute(
            "SELECT id, post_id FROM bot_drafts WHERE comments = 'sending'"
        ).fetchall():
            self._set(row["id"], comments="failed")
            self.tell_owner(
                f"бот зупинився, коли додавав фото в коментарі до поста {row['post_id']} — "
                "перевір коментарі; повторно не додаю."
            )
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

    # --- Коментарі (альбом усіх фото під постом) ---

    def _discussion(self, update_id: int, message: dict) -> None:
        """Група обговорення: цікавить лише пересланий туди наш пост — на нього відповідаємо
        альбомом. Решту повідомлень групи пропускаємо мовчки."""
        origin = message.get("forward_origin") or {}
        post_id = origin.get("message_id")
        ours = (
            message.get("is_automatic_forward") is True
            and origin.get("type") == "channel"
            and (origin.get("chat") or {}).get("id") == self.settings.channel_id
            and isinstance(post_id, int)
        )
        if not ours:
            self._mark(update_id)
            return
        with self.conn:  # «надсилаю» — ДО відправки альбому: вдруге не надішлемо
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            draft = self.conn.execute(
                "SELECT * FROM bot_drafts WHERE post_id = ? AND state = 'published' "
                "AND comments IN ('waiting', 'late')",
                (post_id,),
            ).fetchone()
            if draft is None:
                return
            self.conn.execute(
                "UPDATE bot_drafts SET comments = 'sending', updated_at = ? WHERE id = ?",
                (self._stamp(), draft["id"]),
            )
        self._send_comments(draft, message["message_id"])

    def _album(self, folder_name: str) -> list[Upload]:
        folder = self.settings.root / folder_name
        files = sorted(folder.glob("*.jpg"), key=lambda f: int(f.stem) if f.stem.isdigit() else 0)
        return [Upload(f.name, for_channel(f.read_bytes())) for f in files[:MAX_PHOTOS]]

    def _send_comments(self, draft: sqlite3.Row, forwarded_id: int) -> None:
        link = post_link(self.settings.channel_id, draft["post_id"])
        try:
            album = self._album(draft["folder_name"] or "")
            if len(album) < 2:
                raise PhotoError("у папці поста менше двох фото")
            self.tg.send_media_group(self.settings.discussion_id, album, forwarded_id)
        except (TelegramError, PhotoError, OSError) as error:
            self.log(f"Коментарі до поста {draft['post_id']}: {error}")
            self._set(draft["id"], comments="failed")
            sure = isinstance(error, TelegramError) and error.uncertain
            self.tell_owner(
                f"фото в коментарі до поста {link} "
                + ("могли не додатись — перевір; повторно не додаю" if sure else "не додались")
                + f" ({error})."
            )
            return
        self._set(draft["id"], comments="done")
        if draft["comments"] == "late":
            self.say(draft["chat_id"], TEXTS["comments_done"].format(link=link))

    def _comments_overdue(self, now_: datetime) -> None:
        """Пересланого поста немає 2 хв — їй і Власнику; немає добу — здаємось."""
        wait = (now_ - COMMENTS_WAIT).isoformat(timespec="seconds")
        give_up = (now_ - COMMENTS_GIVE_UP).isoformat(timespec="seconds")
        for row in self.conn.execute(
            "SELECT * FROM bot_drafts WHERE comments = 'waiting' AND published_at <= ?", (wait,)
        ).fetchall():
            link = post_link(self.settings.channel_id, row["post_id"])
            self._set(row["id"], comments="late")
            self.say(row["chat_id"], TEXTS["comments_late"].format(link=link))
            self.tell_owner(
                f"пост {link}: пересланий у групу обговорення пост не прийшов за 2 хв — фото в "
                "коментарях ще немає. Додам, щойно прийде (до 24 год)."
            )
        for row in self.conn.execute(
            "SELECT * FROM bot_drafts WHERE comments = 'late' AND published_at <= ?", (give_up,)
        ).fetchall():
            self._set(row["id"], comments="failed")
            self.tell_owner(
                f"пост {post_link(self.settings.channel_id, row['post_id'])}: за добу фото в "
                "коментарі так і не додались — додай руками з папки поста."
            )

    # --- «Редагувати» ---

    def _edit(self, update_id: int, query: dict, draft: sqlite3.Row) -> None:
        pressed = (query.get("message") or {}).get("message_id")
        if draft["state"] != "preview" or draft["preview_message_id"] != pressed:
            self._mark(update_id)
            refusal = "old_preview" if draft["state"] == "preview" else None
            self._answer(query.get("id", ""), TEXTS[refusal] if refusal else self._state_text(
                draft["id"]))  # fmt: skip
            return
        with self.conn:
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            others = self.conn.execute(
                "SELECT * FROM bot_drafts WHERE chat_id = ? AND state = 'editing'",
                (draft["chat_id"],),
            ).fetchall()
            cursor = self.conn.execute(
                "UPDATE bot_drafts SET state = 'editing', updated_at = ? "
                "WHERE id = ? AND state = 'preview'",
                (self._stamp(), draft["id"]),
            )
        if cursor.rowcount == 0:
            self._answer(query.get("id", ""), self._state_text(draft["id"]))
            return
        for other in others:  # редагувати можна одну чернетку — іншій повертаємо прев'ю
            self._back_to_preview(other)
        self._answer(query.get("id", ""), TEXTS["edit_start"])
        self._drop_buttons(draft["chat_id"], pressed)
        self.say(draft["chat_id"], own_text([p["text"] for p in self._parts(draft["id"])]))
        markup = {"inline_keyboard": [[
            {"text": "Скасувати редагування", "callback_data": f"{NO_EDIT}:{draft['id']}"}
        ]]}  # fmt: skip
        prompt = self.say(draft["chat_id"], TEXTS["edit_ask"], markup)
        self._set(draft["id"], edit_message_id=(prompt or {}).get("message_id"))

    def _back_to_preview(self, draft: sqlite3.Row) -> None:
        """Редагування скінчилось — прев'ю знову (на найближчому `tick`)."""
        self._set(draft["id"], state="collecting", asked=0, edit_message_id=None)
        if draft["edit_message_id"]:
            self._drop_buttons(draft["chat_id"], draft["edit_message_id"])

    def _no_edit(self, update_id: int, query: dict, draft: sqlite3.Row) -> None:
        self._mark(update_id)
        fresh = self.conn.execute(
            "SELECT * FROM bot_drafts WHERE id = ?", (draft["id"],)
        ).fetchone()
        if fresh["state"] != "editing":
            self._answer(query.get("id", ""), self._state_text(draft["id"]))
            return
        self._back_to_preview(fresh)
        self._answer(query.get("id", ""), TEXTS["edit_canceled"])
        self.say(draft["chat_id"], TEXTS["edit_canceled"])

    def _edited(self, update_id: int, chat_id: int, message: dict, text: str | None) -> bool:
        """Текст без фото, поки чернетка в редагуванні, — її новий текст. True — оброблено."""
        draft = self.conn.execute(
            "SELECT * FROM bot_drafts WHERE chat_id = ? AND state = 'editing' "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if draft is None or not text:
            return False
        stamp = self._stamp()
        with self.conn:
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            self.conn.execute(
                "UPDATE bot_draft_parts SET text = NULL WHERE draft_id = ?", (draft["id"],)
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_draft_parts VALUES (?, ?, ?, NULL, NULL, NULL)",
                (draft["id"], message.get("message_id"), text),
            )
            self.conn.execute(
                "UPDATE bot_drafts SET state = 'collecting', asked = 0, edit_message_id = NULL, "
                "last_message_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, draft["id"]),
            )
        if draft["edit_message_id"]:
            self._drop_buttons(chat_id, draft["edit_message_id"])
        return True

    # --- «Опублікувати всі» / «Скасувати всі» ---

    def _open_drafts(self, chat_id: int) -> list[sqlite3.Row]:
        marks = ", ".join("?" * len(OPEN))
        return self.conn.execute(
            f"SELECT * FROM bot_drafts WHERE chat_id = ? AND state IN ({marks}) ORDER BY id",
            (chat_id, *OPEN),
        ).fetchall()

    def _summary_markup(self) -> dict:
        return {"inline_keyboard": [[
            {"text": "Опублікувати всі", "callback_data": f"{PUBLISH_ALL}:0"},
            {"text": "Скасувати всі", "callback_data": f"{CANCEL_ALL}:0"},
        ]]}  # fmt: skip

    def _summaries(self) -> None:
        """«Чернеток: N» з кнопками, коли відкритих ≥ 2 — завжди НИЖЧЕ за останнє прев'ю.

        Нові прев'ю ще йдуть (чернетка чекає прев'ю або від останнього < 2 с) — чекаємо. Після
        повідомлення з'явилось нове прев'ю — старе видаляємо, нове внизу. Змінилось лише N —
        редагуємо на місці; < 2 — без кнопок.
        """
        marks = ", ".join("?" * len(OPEN))
        chats = {
            r[0]
            for r in self.conn.execute(
                f"SELECT chat_id FROM bot_drafts WHERE state IN ({marks}) "
                "UNION SELECT chat_id FROM bot_summaries WHERE running = 0",
                OPEN,
            )
        }
        settled = (self.clock() - SUMMARY_DELAY).isoformat(timespec="seconds")
        for chat_id in sorted(chats):
            row = self.conn.execute(
                "SELECT * FROM bot_summaries WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is not None and row["running"]:
                continue
            drafts = self._open_drafts(chat_id)
            count = len(drafts)
            text = TEXTS["summary"].format(n=count)
            if count < 2:
                if row is not None:
                    self._edit_summary(chat_id, row["message_id"], text, None)
                    with self.conn:
                        self.conn.execute("DELETE FROM bot_summaries WHERE chat_id = ?", (chat_id,))
                continue
            coming = any(d["state"] == "collecting" and not d["asked"] for d in drafts)
            latest = max((d["preview_at"] or "" for d in drafts), default="")
            last_preview = max((d["preview_message_id"] or 0 for d in drafts), default=0)
            below = row is not None and (row["message_id"] or 0) > last_preview
            if row is not None and below:
                if row["shown"] != count:
                    self._edit_summary(chat_id, row["message_id"], text, self._summary_markup())
                    with self.conn:
                        self.conn.execute(
                            "UPDATE bot_summaries SET shown = ? WHERE chat_id = ?",
                            (count, chat_id),
                        )
                continue
            if coming or latest > settled:  # прев'ю ще йдуть — не мигтимо
                continue
            if row is not None and row["message_id"]:
                try:
                    self.tg.delete_message(chat_id, row["message_id"])
                except TelegramError as error:  # старше 48 год тощо — лишаємо без кнопок
                    self.log(f"«Чернеток: N» не видалено: {error}")
                    self._drop_buttons(chat_id, row["message_id"])
            sent = self.say(chat_id, text, self._summary_markup())
            with self.conn:
                self.conn.execute(
                    "INSERT INTO bot_summaries (chat_id, message_id, shown) VALUES (?, ?, ?) "
                    "ON CONFLICT (chat_id) DO UPDATE SET message_id = excluded.message_id, "
                    "shown = excluded.shown",
                    (chat_id, (sent or {}).get("message_id"), count),
                )

    def _edit_summary(
        self, chat_id: int, message_id: int | None, text: str, markup: dict | None
    ) -> None:
        if message_id is None:
            return
        try:
            self.tg.edit_text(chat_id, message_id, text, markup)
        except TelegramError as error:
            self.log(f"«Чернеток: N» не оновлено: {error}")

    def _all(self, update_id: int, query: dict, chat_id: int, action: str) -> None:
        callback_id = query.get("id", "")
        pressed = (query.get("message") or {}).get("message_id")
        with self.conn:  # «публікую всі» — позначка ДО роботи: друге натискання нічого не робить
            self.conn.execute("INSERT INTO bot_updates VALUES (?, ?)", (update_id, now()))
            cursor = self.conn.execute(
                "UPDATE bot_summaries SET running = 1 "
                "WHERE chat_id = ? AND message_id = ? AND running = 0",
                (chat_id, pressed),
            )
        if cursor.rowcount == 0:
            busy = self.conn.execute(
                "SELECT running FROM bot_summaries WHERE chat_id = ? AND message_id = ?",
                (chat_id, pressed),
            ).fetchone()
            self._answer(callback_id, TEXTS["all_busy"] if busy else TEXTS["gone"])
            return
        self._drop_buttons(chat_id, pressed)
        try:
            if action == CANCEL_ALL:
                self._answer(callback_id, TEXTS["canceled"])
                self._cancel_all(chat_id)
            else:
                self._answer(callback_id, TEXTS["all_start"])
                self._publish_all(chat_id)
        finally:
            with self.conn:
                self.conn.execute("DELETE FROM bot_summaries WHERE chat_id = ?", (chat_id,))

    def _cancel_all(self, chat_id: int) -> None:
        drafts = self._open_drafts(chat_id)
        marks = ", ".join("?" * len(OPEN))
        with self.conn:
            self.conn.execute(
                f"UPDATE bot_drafts SET state = 'canceled', updated_at = ? "
                f"WHERE chat_id = ? AND state IN ({marks})",
                (self._stamp(), chat_id, *OPEN),
            )
        for draft in drafts:
            for message_id in (draft["preview_message_id"], draft["edit_message_id"]):
                if message_id:
                    self._drop_buttons(chat_id, message_id)
        self.say(chat_id, TEXTS["all_canceled"].format(n=len(drafts)))

    def _publish_all(self, chat_id: int) -> None:
        """Готові прев'ю — по черзі (як вона їх надіслала), той самий захист, що й одна кнопка."""
        fresh = (self.clock() - PREVIEW_VALID).isoformat(timespec="seconds")
        done = 0
        for n, draft in enumerate(self._open_drafts(chat_id)):
            if draft["state"] != "preview" or (draft["preview_at"] or "") < fresh:
                continue
            with self.conn:
                cursor = self.conn.execute(
                    "UPDATE bot_drafts SET state = 'publishing', updated_at = ? "
                    "WHERE id = ? AND state = 'preview'",
                    (self._stamp(), draft["id"]),
                )
            if cursor.rowcount == 0:
                continue
            if n and done:
                self.sleep(PUBLISH_ALL_PAUSE)
            self._run_publish(draft)
            state = self.conn.execute(
                "SELECT state FROM bot_drafts WHERE id = ?", (draft["id"],)
            ).fetchone()[0]
            done += state == "published"
        left = self._open_drafts(chat_id)
        text = TEXTS["all_done"].format(done=done, left=len(left))
        if left:
            names = ", ".join(f"«{first_line(d['id'], self)}»" for d in left)
            text += "\n" + TEXTS["all_waiting"].format(names=names)
        self.say(chat_id, text)


def first_line(draft_id: int, bot: "Bot") -> str:
    """Початок тексту чернетки — щоб вона впізнала, про яку мова."""
    texts = [p["text"] for p in bot._parts(draft_id) if p["text"]]
    line = texts[0].strip().split("\n")[0] if texts else "без тексту"
    return line if len(line) <= 50 else line[:50].rstrip() + "…"


def own_text(texts: list[str]) -> str:
    """Лише її текст — без хештега, «Пишіть сюди…», рядків про сайт і коментарі та «без хештега»."""
    lines = "\n".join(t.strip() for t in texts if t and t.strip()).split("\n")
    kept = [
        line
        for line in lines
        if not (
            rules.SIGNATURE.search(line)
            or COMMENTS.match(line)
            or SITE_LINE.match(line)
            or SHOP_LINE.match(line)
            or NO_TAG.match(line)
            or re.fullmatch(r"\s*(#VS_\S+\s*)+", line, re.IGNORECASE)
        )
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() or "(тексту немає)"
