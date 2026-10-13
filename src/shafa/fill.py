"""Заповнення Карток ШІ-бланком: `new` → запит до Claude → `review` або `problem` (`ai_error`)."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import UnidentifiedImageError

from shafa import ai
from shafa.db import Status, now
from shafa.photos import PreparedPhoto, prepare

# Захист від випадкових витрат: більше запитів за один запуск не буває.
MAX_PER_RUN = 10


@dataclass(frozen=True)
class Planned:
    """Картка, для якої зібрано запит (ще не надісланий)."""

    item_id: int
    folder_name: str
    post_id: int
    post_text: str
    photos: list[PreparedPhoto]
    request: dict[str, Any]
    estimate: ai.Usage


@dataclass(frozen=True)
class Skipped:
    folder_name: str
    text: str


@dataclass(frozen=True)
class Filled:
    folder_name: str
    usage: ai.Usage | None
    yellow: int = 0
    error: str | None = None


def pick_items(conn: sqlite3.Connection, post_id: int | None, limit: int) -> list[sqlite3.Row]:
    """Картки `new` з постом: одна за номером поста або перші `limit` за порядком створення."""
    query = "SELECT id, folder_name, post_id FROM items WHERE status = ? AND post_id IS NOT NULL"
    params: list[Any] = [Status.NEW]
    if post_id is not None:
        query += " AND post_id = ?"
        params.append(post_id)
    query += " ORDER BY id LIMIT ?"
    params.append(min(limit, MAX_PER_RUN))
    return conn.execute(query, params).fetchall()


def plan(conn: sqlite3.Connection, root: Path, row: sqlite3.Row) -> Planned | Skipped:
    """Зібрати запит для Картки: текст поста й підготовлені фото. Нічого не надсилає."""
    text = conn.execute("SELECT text FROM posts WHERE post_id = ?", (row["post_id"],)).fetchone()
    if text is None:
        return Skipped(row["folder_name"], f"Поста {row['post_id']} немає в базі")
    names = [
        r[0]
        for r in conn.execute(
            "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (row["id"],)
        )
    ]
    photos = []
    for name in names:
        try:
            photos.append(prepare(root / row["folder_name"] / name))
        except (OSError, UnidentifiedImageError):
            return Skipped(row["folder_name"], f"Фото не відкривається: {name}")
    request = ai.build_request(text[0], photos)
    estimate = ai.estimate(request, photos)
    return Planned(
        row["id"], row["folder_name"], row["post_id"], text[0], photos, request, estimate
    )


def save_blank(conn: sqlite3.Connection, item_id: int, fields: list[ai.Field]) -> None:
    conn.execute("DELETE FROM blanks WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO blanks (item_id, field, value, yellow, reason) VALUES (?, ?, ?, ?, ?)",
        [
            (item_id, f.name, json.dumps(f.value, ensure_ascii=False), int(f.yellow), f.reason)
            for f in fields
        ],
    )


def log_request(
    conn: sqlite3.Connection, item_id: int, usage: ai.Usage | None, error: str | None
) -> None:
    usage = usage or ai.Usage(0, 0)
    conn.execute(
        """
        INSERT INTO ai_requests (item_id, model, input_tokens, output_tokens, cost_micro_usd,
                                 error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            ai.MODEL,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_micro_usd,
            error,
            now(),
        ),
    )


def _set_status(conn: sqlite3.Connection, item_id: int, error: str | None) -> bool:
    """Новий статус лише для Картки, що досі `new`. Повертає, чи змінено."""
    status, code = (Status.PROBLEM, "ai_error") if error else (Status.REVIEW, None)
    cursor = conn.execute(
        """
        UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ?
        WHERE id = ? AND status = ?
        """,
        (status, code, error, now(), item_id, Status.NEW),
    )
    return cursor.rowcount == 1


def fill(conn: sqlite3.Connection, client: Any, planned: Planned) -> Filled:
    """Надіслати запит, перевірити відповідь, зберегти бланк, вартість і новий статус."""
    try:
        answer, usage = ai.call(client, planned.request)
    except ai.AiError as error:
        with conn:
            log_request(conn, planned.item_id, error.usage, str(error))
            _set_status(conn, planned.item_id, str(error))
        return Filled(planned.folder_name, error.usage, error=str(error))
    fields = ai.check_answer(answer, [p.file_name for p in planned.photos], planned.post_text)
    with conn:
        log_request(conn, planned.item_id, usage, None)
        if _set_status(conn, planned.item_id, None):  # Картку вже взяли в роботу — бланк не чіпаємо
            save_blank(conn, planned.item_id, fields)
    return Filled(planned.folder_name, usage, yellow=sum(f.yellow for f in fields))
