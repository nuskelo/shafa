"""Кілька розмірів однієї пари в одному пості (CORE-014): окрема Картка на кожен розмір.

Картка-варіант — `<папка>~<розмір>` (`3654~43`, `borh-3655~36`, `folders.variant_name`). Фото —
спільна папка поста. Правила й ШІ бачать текст варіанта: спільний текст + лише її пункт
(`rules.size_variants`). ШІ — один запит на пост (`one_per_post`): відповідь розходиться сестрам
(`spread`), розмір і назва з розміром наприкінці в кожної свої (`answer_for`).
"""

import json
import re
import sqlite3
from typing import Any

from shafa import ai, rules
from shafa.db import Status, now
from shafa.folders import base_name, variant_name, variant_size

# FIX-CORE-014-off (21.10, рішення Власника): пости з кількома розмірами не розбиваємо на Картки —
# `many_items`, як до CORE-014. Код варіантів лишився; True вмикає його знову.
SPLIT_SIZES = False

GONE = "size_gone"
GONE_TEXT = "Розмір {size} прибрано з поста — можливо, продано"


def size_variants(text: str) -> list[rules.SizeVariant] | None:
    """Варіанти поста для створення Карток; None, поки `SPLIT_SIZES` вимкнено."""
    return rules.size_variants(text) if SPLIT_SIZES else None


def find(folder_name: str, text: str) -> rules.SizeVariant | None:
    """Варіант Картки в поточному тексті поста; None — не варіант або її розмір з поста прибрано."""
    size = variant_size(folder_name)
    if size is None:
        return None
    return next((v for v in rules.size_variants(text) or [] if v.size == size), None)


def text_for(folder_name: str, text: str) -> str:
    """Текст поста для правил і ШІ: у варіанта — спільний текст + лише її пункт."""
    found = find(folder_name, text)
    return found.text if found else text


def gone(folder_name: str, text: str) -> tuple[str, str] | None:
    """Варіант, чийого розміру в пості вже немає → («size_gone», текст для людини)."""
    size = variant_size(folder_name)
    if size is None or find(folder_name, text) is not None:
        return None
    return GONE, GONE_TEXT.format(size=size)


# Розміри в назві від ШІ («43 розміру», «36 і 37 розміру», «розмір 43») — ШІ бачило весь пост.
_NUMBERS = r"\d{2}(?:[.,]5)?(?:\s*(?:,|і|й|та|-|–|/)\s*\d{2}(?:[.,]5)?)*"
_TITLE_SIZES = re.compile(
    rf"\s*(?:(?<![\w,.]){_NUMBERS}\s*(?:-?(?:го|й|ий)\s*)?розмір\w*|розмір\w*\s*{_NUMBERS})"
    r"(?![\w,.])",
    re.IGNORECASE,
)


def with_size(title: str | None, size: str) -> str | None:
    """Назва з розміром наприкінці — оголошення сестер на Шафі відрізняються. Розміри, які ШІ
    вписало в назву з усього поста, прибираються."""
    if not title:
        return title
    title = re.sub(r"\s{2,}", " ", _TITLE_SIZES.sub("", title)).strip(" ,.-–")
    if re.search(rf"(?<![\d,.]){re.escape(size)}$", title):
        return title
    return f"{title} {size}"


def answer_for(answer: dict[str, Any], size: str) -> dict[str, Any]:
    """Спільна відповідь ШІ → відповідь для варіанта: свій розмір (європейський), назва з ним.
    Сумніви ШІ про розмір — не про цей варіант."""
    doubts = answer.get("doubts")
    return {
        **answer,
        "title": with_size(answer.get("title"), size),
        "sizes": [size],
        "size_system": "EU",
        "doubts": [
            d
            for d in (doubts if isinstance(doubts, list) else [])
            if not (isinstance(d, dict) and d.get("field") == "sizes")
        ],
    }


def check_answer(
    answer: dict[str, Any], names: list[str], folder_name: str, text: str, edited: str = ""
) -> list[ai.Field]:
    """`ai.check_answer` для Картки: варіант — зі своїм текстом, розміром і назвою."""
    size = variant_size(folder_name)
    if size is None:
        return ai.check_answer(answer, names, text, edited)
    return ai.check_answer(answer_for(answer, size), names, text_for(folder_name, text), edited)


def sisters(conn: sqlite3.Connection, folder_name: str, post_id: int | None) -> list[sqlite3.Row]:
    """Усі варіанти того самого поста (разом із цією Карткою), без очищеного архіву."""
    if variant_size(folder_name) is None or post_id is None:
        return []
    prefix = variant_name(folder_name, "")
    return [
        r
        for r in conn.execute(
            "SELECT id, folder_name, status, shafa_id FROM items "
            "WHERE post_id = ? AND purged_at IS NULL ORDER BY id",
            (post_id,),
        )
        if r["folder_name"].startswith(prefix)
    ]


def _size_key(folder_name: str) -> float:
    try:
        return float((variant_size(folder_name) or "0").replace(",", "."))
    except ValueError:
        return 0.0


def place(conn: sqlite3.Connection, folder_name: str, post_id: int | None) -> tuple[int, int]:
    """«1 з 3» для екрана: місце Картки серед сестер за розміром і скільки їх. (0, 0) — не варіант."""
    names = sorted((r["folder_name"] for r in sisters(conn, folder_name, post_id)), key=_size_key)
    if folder_name not in names:
        return 0, 0
    return names.index(folder_name) + 1, len(names)


def sister_shafa_ids(conn: sqlite3.Connection, item_id: int) -> set[int]:
    """Оголошення сестер на Шафі: схожі між собою (спільні фото), але не дублі (DUP-001)."""
    row = conn.execute("SELECT folder_name, post_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return set()
    return {
        r["shafa_id"]
        for r in sisters(conn, row["folder_name"], row["post_id"])
        if r["id"] != item_id and r["shafa_id"] is not None
    }


def all_published(conn: sqlite3.Connection, folder_name: str, post_id: int | None) -> bool:
    """Чи опубліковано всі сестри — лише тоді спільна папка може переїхати в «Опубліковано»."""
    return all(r["status"] == Status.PUBLISHED for r in sisters(conn, folder_name, post_id))


# --- ШІ: один запит на пост ---


def one_per_post(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Черга ШІ без зайвих запитів: з сестер — лише перша, і жодної, поки сестра в пакеті."""
    from shafa.fill import IN_BATCH  # fill бере звідси перевірку відповіді

    waiting = {
        (r["post_id"], base_name(r["folder_name"]))
        for r in conn.execute(f"SELECT folder_name, post_id FROM items WHERE id IN ({IN_BATCH})")
        if variant_size(r["folder_name"]) is not None
    }
    kept, seen = [], set()
    for row in rows:
        if variant_size(row["folder_name"]) is None:
            kept.append(row)
            continue
        key = (row["post_id"], base_name(row["folder_name"]))
        if key in seen or key in waiting:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def spread(conn: sqlite3.Connection, item_id: int, answer: dict[str, Any]) -> list[int]:
    """Відповідь ШІ однієї сестри → бланки інших сестер, що досі чекають ШІ (`new`, не в пакеті).
    Без запитів і без запису вартості. Повертає, кому дісталося."""
    from shafa import fill  # fill бере звідси перевірку відповіді

    row = conn.execute("SELECT folder_name, post_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return []
    text = conn.execute("SELECT text FROM posts WHERE post_id = ?", (row["post_id"],)).fetchone()
    pending = {r[0] for r in conn.execute(fill.IN_BATCH)}
    done = []
    for sister in sisters(conn, row["folder_name"], row["post_id"]):
        if sister["id"] == item_id or sister["status"] != Status.NEW or sister["id"] in pending:
            continue
        fields = check_answer(
            answer,
            fill.photo_names(conn, sister["id"]),
            sister["folder_name"],
            text[0] if text else "",
            rules.edit_text(fill.edits_of(conn, sister["id"])),
        )
        if fill.set_status(conn, sister["id"], None, fields):
            fill.save_blank(conn, sister["id"], fields, answer)
            done.append(sister["id"])
    return done


# --- Стара Картка поста з кількома розмірами → варіанти ---

# Стара Картка може стати варіантами, лише поки її не взяли в роботу: ШІ чи правила (`new`) або
# проблема даних. Схвалені, опубліковані й проблеми публікації — ніколи.
_SPLIT_STATUSES = (Status.NEW, Status.PROBLEM)


def _copy_rows(conn: sqlite3.Connection, table: str, source: int, target: int) -> None:
    names = ", ".join(
        r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] not in ("id", "item_id")
    )
    conn.execute(
        f"INSERT INTO {table} (item_id, {names}) SELECT ?, {names} FROM {table} WHERE item_id = ?",
        (target, source),
    )


def split_card(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """Картка без суфікса, а пост — кілька розмірів однозначно → варіанти (у транзакції виклику).

    Сама Картка стає першим варіантом (бланк, відповідь ШІ, правки й місце в черзі лишаються),
    сестри — її копії. Є відповідь ШІ — бланки правилами без ШІ (`review` чи «Проблеми»), немає —
    усі `new`, і ШІ спитають один раз. Правка розміру не копіюється: у кожної свій. Картку з
    `shafa_id`, схвалену чи в роботі — не чіпає. Повертає id варіантів."""
    if not SPLIT_SIZES:
        return []
    from shafa import fill  # fill бере звідси перевірку відповіді

    row = conn.execute(
        "SELECT * FROM items WHERE id = ? AND purged_at IS NULL", (item_id,)
    ).fetchone()
    if row is None or row["shafa_id"] is not None or row["status"] not in _SPLIT_STATUSES:
        return []
    if variant_size(row["folder_name"]) is not None or row["post_id"] is None:
        return []
    from shafa.scan import robot_problem  # scan бере звідси розбір папки

    if robot_problem(row["problem_code"]):
        return []
    text = conn.execute("SELECT text FROM posts WHERE post_id = ?", (row["post_id"],)).fetchone()
    found = rules.size_variants(text[0]) if text else None
    if not found:
        return []
    names = [variant_name(row["folder_name"], v.size) for v in found]
    taken = {
        r[0]
        for r in conn.execute(
            f"SELECT folder_name FROM items WHERE folder_name IN ({', '.join('?' * len(names))})",
            names,
        )
    }
    has_answer = fill.has_answer(conn, item_id)
    if has_answer and answer_json(conn, item_id) is None:  # бланк до CORE-009: відповідь з бланка
        fill.save_answer(conn, item_id, fill.answer_from_blank(conn, item_id), 1, True)
    stamp = now()
    ids = []
    conn.execute("DELETE FROM edits WHERE item_id = ? AND field = 'sizes'", (item_id,))
    for name in names:
        if name in taken:
            continue
        if not ids:
            conn.execute(
                "UPDATE items SET folder_name = ?, updated_at = ? WHERE id = ?",
                (name, stamp, item_id),
            )
            ids.append(item_id)
            continue
        new_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at, queue_pos) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, row["post_id"], Status.NEW, stamp, stamp, row["queue_pos"]),
        ).lastrowid
        for table in ("photos", "ai_answers", "edits"):
            _copy_rows(conn, table, item_id, new_id)
        ids.append(new_id)
    for variant_id in ids:
        if has_answer:
            result = fill.recalc(conn, variant_id, text[0])
            status, code, why = (
                (Status.PROBLEM, *result.problem) if result.problem else (Status.REVIEW, None, None)
            )
            fill.save_answer(conn, variant_id, result.answer, result.version, result.rebuilt)
            fill.save_blank(conn, variant_id, result.fields)
        else:
            status, code, why = Status.NEW, None, None
        conn.execute(
            "UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ? "
            "WHERE id = ?",
            (status, code, why, stamp, variant_id),
        )
    return ids


def split_waiting(conn: sqlite3.Connection) -> int:
    """Старі Картки `many_items` (і `new`), пост яких тепер розбирається на розміри, → варіанти.
    Робиться разом із «Перерахувати правилами» і на старті програми. Повертає, скільки розділено."""
    if not SPLIT_SIZES:
        return 0
    rows = conn.execute(
        "SELECT id, folder_name FROM items WHERE purged_at IS NULL AND shafa_id IS NULL "
        "AND post_id IS NOT NULL AND (problem_code = 'many_items' OR status = ?) ORDER BY id",
        (Status.NEW,),
    ).fetchall()
    done = 0
    with conn:
        for row in rows:
            if variant_size(row["folder_name"]) is None and split_card(conn, row["id"]):
                done += 1
    return done


def answer_json(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT answer FROM ai_answers WHERE item_id = ?", (item_id,)).fetchone()
    return json.loads(row[0]) if row else None
