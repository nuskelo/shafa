"""Процес бота: налаштування з `.env`, перевірка прав у каналі, довге опитування Telegram.

Канал для публікації — окреме налаштування `TELEGRAM_BOT_CHANNEL_ID` (не `TELEGRAM_CHANNEL_ID`,
з якого беруться назви папок): спершу там тестовий канал Власника, її канал — лише з його дозволу.
"""

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from shafa import config, site
from shafa.bot import presence
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import Telegram, TelegramError
from shafa.db import now

TOKEN = "TELEGRAM_BOT_TOKEN"
CHANNEL = "TELEGRAM_BOT_CHANNEL_ID"
SELLER = "TELEGRAM_SELLER_CHAT_ID"
OWNER = "TELEGRAM_OWNER_CHAT_ID"
POLL = 25  # секунд довгого опитування, коли нічого не чекаємо
BUSY_POLL = 1  # коли чернетка чекає прев'ю
PAUSE = 5  # після помилки зв'язку
LOCK_NAME = "bot.lock"
LOG_NAME = "bot-run.log"


class SetupError(Exception):
    """Бот не може працювати. Текст — для людини, без значень секретів."""


def token() -> str:
    value = config.setting(TOKEN)
    if value is None:
        raise SetupError(f"Немає {TOKEN} у .env")
    return value


def channel_id(raw: str | None) -> int:
    """Id каналу як у Bot API: `-100…`. Приймає й короткий (`2394827824`), як у посиланні."""
    if raw is None:
        raise SetupError(f"Немає {CHANNEL} у .env (канал, куди бот публікує)")
    raw = raw.strip()
    if raw.isdigit():
        return int("-100" + raw)
    if raw.startswith("-100") and raw[4:].isdigit():
        return int(raw)
    raise SetupError(f"{CHANNEL} — id каналу, наприклад 2394827824 або -1002394827824")


def chat_id(name: str) -> int | None:
    raw = config.setting(name)
    if raw is None:
        return None
    if not raw.strip().lstrip("-").isdigit():
        raise SetupError(f"{name} — число (chat id)")
    return int(raw)


def export_channel() -> int | None:
    """Канал, з якого Експорт (`TELEGRAM_CHANNEL_ID`); немає чи неправильний — None."""
    raw = config.setting("TELEGRAM_CHANNEL_ID")
    if raw is None:
        return None
    try:
        return channel_id(raw)
    except SetupError:
        return None


def settings(root: Path) -> Settings:
    seller, owner = chat_id(SELLER), chat_id(OWNER)
    if seller is None and owner is None:
        raise SetupError(f"Немає {SELLER} і {OWNER} у .env — бот нікого не слухатиме")
    return Settings(
        channel_id(config.setting(CHANNEL)), seller, owner, root,
        export_channel_id=export_channel(),
    )  # fmt: skip


def _is_admin(member: dict, can_post: bool = False) -> bool:
    status = member.get("status")
    if status == "creator":
        return True
    return status == "administrator" and (not can_post or bool(member.get("can_post_messages")))


def check_rights(tg: Telegram, channel: int) -> tuple[str, int]:
    """Бот — адміністратор каналу з правом публікації; у каналу є група обговорення (коментарі),
    бот у ній — адміністратор, а учасники писати не можуть (рішення Власника: коментують лише
    вона й бот). Повертає ім'я бота й id групи."""
    try:
        me = tg.get_me()
        member = tg.get_chat_member(channel, me["id"])
        if not _is_admin(member, can_post=True):
            raise SetupError("Бот не адміністратор каналу з правом публікувати повідомлення")
        group = tg.get_chat(channel).get("linked_chat_id")
        if not isinstance(group, int):
            raise SetupError(
                "У каналу немає групи для коментарів: Керування каналом → Обговорення → "
                "прив'яжи групу; якщо щойно прив'язала — зачекай 5 хв і запусти ще раз"
            )
        if not _is_admin(tg.get_chat_member(group, me["id"])):
            raise SetupError(
                "Бот не адміністратор групи коментарів — зробіть його адміністратором групи"
            )
        permissions = tg.get_chat(group).get("permissions") or {}
        if permissions.get("can_send_messages") is not False:
            raise SetupError(
                "У групі коментарів учасники можуть писати — вимкни для учасників «Надсилати "
                "повідомлення» (Дозволи групи)"
            )
    except TelegramError as error:
        if error.code in (400, 403):
            raise SetupError(
                "Бот не бачить каналу або групи: додайте бота адміністратором"
            ) from None
        raise SetupError(f"Не вдалося перевірити бота: {error}") from None
    return me.get("username") or "бот", group


OFFLINE_TEXT = "Немає зв'язку з Telegram — перевірте інтернет, бот спробує сам"


BUSY_TEXT = "Telegram тимчасово не відповідає — бот спробує сам"


def set_status(conn: sqlite3.Connection, started: bool = False, error: str | None = None) -> None:
    stamp = now()
    with conn:
        conn.execute(
            "INSERT INTO bot_status (id, started_at, heartbeat_at, error) VALUES (1, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at, "
            "error = excluded.error, "
            "started_at = CASE WHEN ? THEN excluded.started_at ELSE bot_status.started_at END",
            (stamp, stamp, error, started),
        )


def poll(
    bot: Bot,
    tg: Telegram,
    conn: sqlite3.Connection,
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """Довге опитування, доки `stop()` не скаже зупинитись. Конфлікт (другий бот) — SetupError."""
    offset = None
    while not stop():
        try:
            updates = tg.get_updates(offset, BUSY_POLL if bot.waiting() else POLL)
        except TelegramError as error:
            if error.code == 409:
                raise SetupError(
                    "Бот зупинився: його вже запущено на іншому комп'ютері — скажіть Власнику"
                ) from None
            if error.code == 401:
                raise SetupError(
                    "Бот зупинився: Telegram не приймає ключ бота — скажіть Власнику"
                ) from None
            log(f"Опитування: {error}")
            # на екран — просто слова; метод і код лишаються в журналі (рядок вище)
            set_status(conn, error=OFFLINE_TEXT if error.code is None else BUSY_TEXT)
            sleep(error.retry_after or PAUSE)
            continue
        for update in sorted(updates or [], key=lambda u: u.get("update_id", 0)):
            bot.handle(update)
            if isinstance(update.get("update_id"), int):
                offset = update["update_id"] + 1  # підтверджуємо оброблене
        bot.tick()
        set_status(conn)


def start_site(db_path: Path, root: Path, token: str, settings: Settings) -> threading.Event:
    """Потік сайту (SITE-001) поруч із ботом. Повідомлення людям — своїм клієнтом Telegram (потік
    не ділить з'єднання з опитуванням); не дійшло — лише рядок у журналі."""
    tg = Telegram(token)

    def say(chat: int | None) -> Callable[[str], None]:
        def send(text: str) -> None:
            if chat is None:
                return
            try:
                tg.send_message(chat, text)
            except TelegramError as error:
                print(f"Сайт: повідомлення не надіслано ({error})")

        return send

    if site.key() is None:
        print(f"Сайт: немає {site.KEY} — інтеграція вимкнена")
    chat = presence.service_chat(settings.owner_chat, settings.seller_chat)
    checker = (
        None
        if chat is None
        else presence.Presence(tg, settings.channel_id, chat, data_dir=config.data_dir())
    )
    return site.start(
        db_path, root, site.Notify(say(settings.owner_chat), say(settings.seller_chat)),
        presence=checker,
    )  # fmt: skip
