"""Кнопки під тривогами Власнику (BOT-011): вирішувати Проблеми прямо в Telegram.

Тривога про Проблему (`alerts`) приходить з кнопками; натискання обробляє `handle`. Усе, що кнопки
роблять із Карткою, — ті самі функції, що й на екрані (`review.recheck`, `publish.to_archive`,
`publish.other_item`): жодної власної логіки статусів. Схвалення Карток тут немає — у Telegram не
видно бланка.

`callback_data` — `дія:id Картки:код проблеми` (≤ 64 байти). Код — те, що було в тривозі: змінився
стан (уже вирішили на екрані) — «Уже вирішено», нічого не робиться. Кнопки — лише для
`TELEGRAM_OWNER_CHAT_ID`. Модуль не знає про `core`: `bot` — будь-що з `conn`, `tg`, `settings`,
`log`, `_answer`, `_drop_buttons`, `say`.
"""

import sqlite3
from pathlib import Path
from typing import Any

from shafa import config, photos, review
from shafa.bot import report
from shafa.bot.telegram import TelegramError, Upload
from shafa.db import Status
from shafa.folders import item_folder
from shafa.robot import control, publish
from shafa.robot import listings as listings_mod
from shafa.scan import STICKY

RECHECK, ARCHIVE, ARCHIVE_YES, ARCHIVE_NO = "rc", "ar", "ay", "an"
DIFFERENT, OPEN, WHAT = "od", "op", "ws"
ACTIONS = (RECHECK, ARCHIVE, ARCHIVE_YES, ARCHIVE_NO, DIFFERENT, OPEN, WHAT)

MULTI_MAX = 5  # у повідомленні з кількома Проблемами кнопки — для перших стількох
LINE_MAX = 160  # символів на одну проблему в тексті тривоги
NOTE_MAX = 190  # відповідь на кнопку (`answerCallbackQuery`) — до 200 символів
TEXT_KEEP = 3500  # скільки лишаємо від тексту тривоги, коли дописуємо результат (ліміт — 4096)
SESSION_ITEMS = 5  # Проблем сесії в «Що сталось»

TEXTS = {
    "denied": "Немає доступу",
    "gone": "Уже вирішено",
    "robot": "Робот зараз працює з цією Карткою",
    "no_action": "Для цієї Картки кнопки немає — відкрийте програму",
    "confirm": "Точно в архів? Через 7 днів Картка зникне.",
    "failed": "Не вийшло перевірити — відкрийте програму",
}
STATUS_NAMES = {
    Status.REVIEW: "На перевірку",
    Status.APPROVED: "Схвалені",
    Status.NEW: "Нові (чекає на ШІ)",
    Status.ARCHIVED: "Архів",
}


# Проблеми публікації, які вирішує людина на екрані («Перевірити ще раз» їх не чіпає); «фото зникли»
# теж липка, але в «На перевірку» її повертає саме «Перевірити ще раз»; дубль має свої кнопки.
HUMAN_ONLY = tuple(c for c in STICKY if c not in ("photos_missing", "possible_duplicate"))


def kind(code: str, shafa_id: object) -> str | None:
    """Які кнопки в Картки з такою Проблемою; None — жодних (її вирішують на екрані).

    Картка з `shafa_id` уже на Шафі: дія лише одна — «В архів» для відхиленої Шафою."""
    if code == listings_mod.DECLINED_CODE:
        return "declined"
    if shafa_id is not None or code in HUMAN_ONLY:
        return None
    return "duplicate" if code == "possible_duplicate" else "recheck"


def post_name(row: sqlite3.Row) -> str:
    return str(row["post_id"] or row["folder_name"])


def card_text(row: sqlite3.Row, limit: int = LINE_MAX) -> str:
    """«Пост N: назва проблеми — подробиці» — рядок про одну Картку в тривозі."""
    what = f"Пост {post_name(row)}: {report.problem_name(row['problem_code'])}"
    if why := " ".join(str(row["problem_text"] or "").split()):
        what += f" — {why}"
    return report.clip(what, limit)


def _data(action: str, row: sqlite3.Row) -> str:
    return f"{action}:{row['id']}:{row['problem_code'] or ''}"


def _button(text: str, action: str, row: sqlite3.Row) -> dict:
    return {"text": text, "callback_data": _data(action, row)}


def buttons(row: sqlite3.Row) -> list[list[dict]]:
    """Кнопки під повідомленням про одну Картку."""
    match kind(row["problem_code"] or "", row["shafa_id"]):
        case "recheck":
            return [[_button("🔁 Перевірити ще раз", RECHECK, row)]]
        case "duplicate":
            return [
                [_button("✅ Різні пари", DIFFERENT, row), _button("🗄 Дубль", ARCHIVE, row)],
            ]
        case "declined":
            link = shafa_link(row["shafa_url"])
            open_ = [[{"text": "🔗 Відкрити на Шафі", "url": link}]] if link else []
            return [*open_, [_button("🗄 В архів", ARCHIVE, row)]]
    return []


def shafa_link(url: str | None) -> str | None:
    """Посилання лише якщо це справді shafa.ua (як на екрані)."""
    return url if url and url.startswith("https://shafa.ua/") else None


def markup(rows: list[sqlite3.Row]) -> dict | None:
    """Одна Картка — її кнопки. Кілька — «📂 Пост N» для перших 5: кожна відкриває окреме повідомлення
    з Карткою (фото для дубля, свої кнопки); решту — у програмі."""
    if len(rows) == 1:
        keyboard = buttons(rows[0])
    else:
        keyboard = [
            [_button(f"📂 Пост {post_name(row)}", OPEN, row)]
            for row in rows[:MULTI_MAX]
            if kind(row["problem_code"] or "", row["shafa_id"])
        ]
    return {"inline_keyboard": keyboard} if keyboard else None


def session_markup(session_id: int) -> dict:
    return {
        "inline_keyboard": [[{"text": "📋 Що сталось", "callback_data": f"{WHAT}:{session_id}"}]]
    }


def confirm_markup(row: sqlite3.Row) -> dict:
    return {
        "inline_keyboard": [
            [_button("Так, в архів", ARCHIVE_YES, row), _button("Ні, назад", ARCHIVE_NO, row)]
        ]
    }


# --- Надсилання ---


def album(conn: sqlite3.Connection, root: Path, item_id: int) -> list[Upload]:
    """Фото нашої Картки (перше) і схожого оголошення (його зберіг робот) — що є з цього."""
    files: list[Upload] = []
    row = conn.execute(
        "SELECT i.folder_name, p.file_name FROM items i "
        "LEFT JOIN photos p ON p.item_id = i.id WHERE i.id = ? ORDER BY p.position LIMIT 1",
        (item_id,),
    ).fetchone()
    base = conn.execute("PRAGMA database_list").fetchone()["file"]
    if row and row["file_name"]:
        files += _load(
            lambda: photos.prepare(item_folder(root, row["folder_name"]) / row["file_name"])
        )
    if base:
        other = Path(base).parent / listings_mod.PHOTOS_DIR / f"{item_id}.jpg"
        files += _load(lambda: photos.prepare_bytes(other.read_bytes(), other.name))
    return files


def _load(read: Any) -> list[Upload]:
    try:
        prepared = read()
    except Exception:  # noqa: BLE001 — немає файла чи це не картинка: просто без цього фото
        return []
    return [Upload(prepared.file_name or "photo.jpg", prepared.data)]


def deliver(bot: Any, chat_id: int, text: str, keyboard: dict | None, album_of: int | None) -> bool:
    """Повідомлення Власнику; `album_of` — Картка, чиї фото йдуть перед ним (збій фото не заважає)."""
    if album_of is not None:
        files = album(bot.conn, bot.settings.root, album_of)
        try:
            if len(files) >= 2:
                bot.tg.send_media_group(chat_id, files, None)
            elif files:
                bot.tg.send_photo(chat_id, files[0], "")
        except TelegramError as error:
            bot.log(f"Фото до тривоги не надіслано: {error}")
    return bot.say(chat_id, text, keyboard) is not None


# --- Натискання ---


def _finish(bot: Any, query: dict, note: str, keyboard: dict | None = None) -> None:
    """Відповісти на кнопку й дописати результат у те саме повідомлення. Кнопки: `keyboard` чи жодних."""
    note = report.clip(report.scrub(note), TEXT_KEEP)
    bot._answer(query.get("id", ""), report.clip(note, NOTE_MAX))
    message = query.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    if chat_id is None or message_id is None:
        return
    text = report.clip(message.get("text") or "", TEXT_KEEP) + "\n\n" + note
    try:
        bot.tg.edit_text(chat_id, message_id, text, keyboard)
    except TelegramError as error:
        bot.log(f"Результат кнопки не дописано: {error}")
        bot._drop_buttons(chat_id, message_id)


def _swap_buttons(bot: Any, query: dict, keyboard: dict, note: str = "") -> None:
    """Той самий текст, інші кнопки (підтвердження архіву і «Ні, назад»)."""
    bot._answer(query.get("id", ""), note)
    message = query.get("message") or {}
    try:
        bot.tg.edit_text(
            message["chat"]["id"], message["message_id"], message.get("text") or "", keyboard
        )
    except (TelegramError, KeyError) as error:
        bot.log(f"Кнопки не змінено: {error}")


def _is_owner(bot: Any, query: dict) -> bool:
    owner = bot.settings.owner_chat
    chat = (query.get("message") or {}).get("chat") or {}
    user = query.get("from") or {}
    return (
        owner is not None
        and chat.get("type") == "private"
        and chat.get("id") == owner
        and user.get("id") == owner
    )


ALLOWED = {  # дія → які Картки (`kind`) її мають
    RECHECK: {"recheck"},
    ARCHIVE: {"duplicate", "declined"},
    ARCHIVE_YES: {"duplicate", "declined"},
    ARCHIVE_NO: {"duplicate", "declined"},
    DIFFERENT: {"duplicate"},
    OPEN: {"recheck", "duplicate", "declined"},
}


def _refusal(row: sqlite3.Row | None, code: str, action: str) -> str | None:
    """Чому кнопка нічого не робить: Картка вже не в тому стані, що був у тривозі, чи працює робот."""
    if row is None:
        return TEXTS["gone"]
    if row["status"] == Status.PUBLISHING:
        return TEXTS["robot"]
    if row["status"] != Status.PROBLEM or (row["problem_code"] or "") != code:
        return TEXTS["gone"]
    if kind(code, row["shafa_id"]) not in ALLOWED[action]:
        return TEXTS["no_action"]
    return None


def handle(bot: Any, query: dict) -> None:
    """Одне натискання кнопки тривоги. Чужий — «Немає доступу», нічого не змінюється."""
    if not _is_owner(bot, query):
        bot._answer(query.get("id", ""), TEXTS["denied"])
        return
    action, _, rest = str(query.get("data") or "").partition(":")
    if action == WHAT:
        _what(bot, query, rest)
        return
    raw_id, _, code = rest.partition(":")
    row = None
    if raw_id.isdigit():
        row = bot.conn.execute(
            "SELECT id, status, problem_code, problem_text, shafa_id, shafa_url, post_id, "
            "folder_name FROM items WHERE id = ?",
            (int(raw_id),),
        ).fetchone()
    if action not in ALLOWED:
        refusal = TEXTS["no_action"]
    else:
        refusal = _refusal(row, code, action)
    if refusal:
        _finish(bot, query, refusal, _without(query) if action == OPEN else None)
        return
    handler = {
        RECHECK: _recheck, ARCHIVE: _ask_archive, ARCHIVE_YES: _archive,
        ARCHIVE_NO: _back, DIFFERENT: _different, OPEN: _open,
    }[action]  # fmt: skip
    try:
        handler(bot, query, row)
    except sqlite3.Error as error:  # база зайнята екраном надовше за тайм-аут: бот не падає
        bot.log(f"Кнопка {action}: {type(error).__name__}: {str(error)[:200]}")
        _finish(bot, query, TEXTS["failed"])


def _without(query: dict) -> dict | None:
    """Кнопки повідомлення без того ряду, який щойно натиснули (для списку з кількома Картками)."""
    pressed = query.get("data")
    rows = (((query.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard")) or []
    left = [r for r in rows if not any(b.get("callback_data") == pressed for b in r)]
    return {"inline_keyboard": left} if left else None


def _recheck(bot: Any, query: dict, row: sqlite3.Row) -> None:
    try:
        result = review.recheck(
            bot.conn, row["id"], bot.settings.root, config.setting("TELEGRAM_CHANNEL_ID")
        )
    except Exception as error:  # noqa: BLE001 — кнопка не ламає бота
        bot.log(f"Кнопка «Перевірити ще раз»: {type(error).__name__}: {str(error)[:200]}")
        _finish(bot, query, TEXTS["failed"])
        return
    after = bot.conn.execute(
        "SELECT status, problem_text FROM items WHERE id = ?", (row["id"],)
    ).fetchone()
    name = post_name(row)
    if result.code == "sticky" or after is None:
        note = TEXTS["no_action"] if after else TEXTS["gone"]
    elif after["status"] == Status.PROBLEM:  # і коли перевірка нічого не знайшла (`stale`)
        why = " ".join(str(after["problem_text"] or "").split())
        note = f"Пост {name} → досі Проблема" + (f": {why}" if why else "")
    elif result.code == "stale":
        note = TEXTS["gone"]
    else:
        note = f"Пост {name} → {STATUS_NAMES.get(after['status'], after['status'])}"
    _finish(bot, query, note)


def _ask_archive(bot: Any, query: dict, row: sqlite3.Row) -> None:
    _swap_buttons(bot, query, confirm_markup(row), TEXTS["confirm"])


def _back(bot: Any, query: dict, row: sqlite3.Row) -> None:
    _swap_buttons(bot, query, {"inline_keyboard": buttons(row)})


def _archive(bot: Any, query: dict, row: sqlite3.Row) -> None:
    if publish.to_archive(bot.conn, row["id"]) != "duplicate_archived":
        _finish(bot, query, TEXTS["gone"])
        return
    _finish(bot, query, f"Пост {post_name(row)} → Архів (через {review.ARCHIVE_DAYS} днів зникне)")


def _different(bot: Any, query: dict, row: sqlite3.Row) -> None:
    if publish.other_item(bot.conn, row["id"]) != "other_item":
        _finish(bot, query, TEXTS["gone"])
        return
    _finish(bot, query, f"Пост {post_name(row)} → Схвалені (різні пари; робот опублікує)")


def _open(bot: Any, query: dict, row: sqlite3.Row) -> None:
    """Список з кількома Картками: окреме повідомлення про одну з них, її ряд зі списку знімається."""
    chat_id = (query.get("message") or {}).get("chat", {}).get("id")
    code = row["problem_code"] or ""
    deliver(
        bot, chat_id, "⚠️ " + card_text(row), markup([row]),
        row["id"] if code == "possible_duplicate" else None,
    )  # fmt: skip
    _finish(bot, query, f"Пост {post_name(row)} — окреме повідомлення нижче", _without(query))


def _what(bot: Any, query: dict, raw_id: str) -> None:
    """Зупинка робота: чому і які Проблеми були в цій сесії."""
    session = None
    if raw_id.isdigit():
        session = bot.conn.execute("SELECT * FROM sessions WHERE id = ?", (int(raw_id),)).fetchone()
    if session is None:
        _finish(bot, query, TEXTS["gone"])
        return
    reason = control.LAST.get(session["stop_code"] or "", "") or session["stop_reason"] or ""
    lines = [f"📋 Сесія {session['id']}: {reason.strip().rstrip('.') or 'причина невідома'}."]
    rows = bot.conn.execute(
        "SELECT id, status, problem_code, problem_text, shafa_id, shafa_url, post_id, folder_name "
        "FROM items WHERE status = ? AND (session_id = ? OR (updated_at >= ? AND updated_at <= ?)) "
        "ORDER BY updated_at DESC LIMIT ?",
        (
            Status.PROBLEM, session["id"], session["started_at"],
            session["ended_at"] or "9999", SESSION_ITEMS,
        ),
    ).fetchall()  # fmt: skip
    if rows:
        lines.append("Проблеми цієї сесії:")
        lines += ["• " + card_text(r) for r in rows]
    else:
        lines.append("Проблем у цій сесії немає.")
    _finish(bot, query, "\n".join(lines))
