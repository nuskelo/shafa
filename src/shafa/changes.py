"""Пост чи фото змінено після того, як Картка вже зроблена (CORE-015, TD-004).

Ціну, стан, опис, устілку й мембрану `review.load_card` рахує з `posts.text` на льоту — досить
оновити текст. Решту полів бланка, що залежать від тексту (сезон, стать, розмір з ярлика, розділ),
перераховує `fill.recalc` зі збереженої відповіді ШІ: без запитів до ШІ. Власнику лишається
примітка на Картці «Пост змінено <дата>: <що>» (`review.NOTE_CODES`, у `problem_text`).
"""

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from shafa import ai, events, fill, review, rules, scan, variants
from shafa.db import Status, now
from shafa.folders import Folder, base_name, is_old

LOG_NAME = "post-changes.json"  # у теці даних: зміни постів, що вже на Шафі (для звіту, BOT-009)
LOG_KEEP = 200
SHOWN_MAX = 40
NAMES_MAX = 3
FIXED = (Status.REVIEW, Status.APPROVED)  # Картки, які вона вже бачила

# Поля, що змінюються самі слідом за іншими: у примітці їх не називаємо.
_SKIP = ("photo_order", "keywords")

NO_FOLDER = "Папку з фото не знайдено — поверніть її на місце"
NO_PHOTOS = "У папці не лишилось фото .jpg"


@dataclass(frozen=True)
class TextChange:
    cards: list[int] = field(default_factory=list)  # Картки `review`/`approved`, яких торкнулось
    published: list[tuple[int, str]] = field(default_factory=list)  # (пост, що змінилось)


@dataclass(frozen=True)
class PhotoSync:
    changed: list[tuple[int | None, str]] = field(default_factory=list)  # (пост, що змінилось)
    lost: list[tuple[int | None, str]] = field(default_factory=list)  # (пост, чому Проблема)


def _date(moment: datetime | None) -> str:
    return (moment or datetime.now(UTC)).astimezone().strftime("%d.%m.%Y")


def _shown(value: object) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    else:
        text = "" if value is None else str(value)
    text = text or "—"
    return text if len(text) <= SHOWN_MAX else text[: SHOWN_MAX - 1] + "…"


def _diff(before: review.Card, after: review.Card) -> str:
    """Що змінилось на Картці: «Ціна на Шафі, грн: 1380 → 1150; опис»."""
    parts = []
    for spec in review.SPECS:
        if spec.name in _SKIP:
            continue
        was, became = before.fields[spec.name].value, after.fields[spec.name].value
        if was == became:
            continue
        if spec.name == "description":
            parts.append("опис")
        else:
            parts.append(f"{spec.label}: {_shown(was)} → {_shown(became)}")
    return "; ".join(parts) or "текст поста змінено"


def _combined(conn: sqlite3.Connection, item_id: int, note: str) -> str:
    """Нова примітка після ще не знятої старої: вона побачить обидві."""
    row = conn.execute(
        "SELECT problem_code, problem_text FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row and row["problem_code"] in review.NOTE_CODES and row["problem_text"]:
        return f"{row['problem_text']} · {note}"
    return note


def _put_back(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    code: str,
    note: str,
    problem: tuple[str, str] | None = None,
) -> None:
    """Картка `review`/`approved` → «На перевірку» з приміткою або, якщо правила тепер бачать
    проблему, у «Проблеми» (причина й примітка разом)."""
    if problem:
        new_status, new_code, text = Status.PROBLEM, problem[0], f"{problem[1]}. {note}"
    else:
        new_status, new_code, text = Status.REVIEW, code, _combined(conn, item_id, note)
    conn.execute(
        "UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ? "
        "WHERE id = ? AND status = ?",
        (new_status, new_code, text, now(), item_id, status),
    )


def _has_blank(conn: sqlite3.Connection, item_id: int) -> bool:
    return conn.execute("SELECT 1 FROM blanks WHERE item_id = ?", (item_id,)).fetchone() is not None


def text_changed(
    conn: sqlite3.Connection,
    post_id: int,
    text: str,
    *,
    moment: datetime | None = None,
    allow_old: bool = True,
) -> TextChange:
    """Текст поста в каналі змінили. `allow_old=False` — пости з Експорту не чіпаємо (канал бота —
    не той, звідки Експорт: номери могли збігтися випадково). Те саме редагування — нічого."""
    row = conn.execute("SELECT text, source FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if row is None or row["text"] == text or (row["source"] == "export" and not allow_old):
        return TextChange()
    rows = conn.execute(
        "SELECT id, folder_name, status FROM items WHERE post_id = ? AND purged_at IS NULL "
        "ORDER BY id",
        (post_id,),
    ).fetchall()
    seen = [r for r in rows if r["status"] in (*FIXED, Status.PUBLISHED)]
    before = {r["id"]: review.load_card(conn, r["id"]) for r in seen}
    cards: list[int] = []
    published: list[tuple[int, str]] = []
    when = _date(moment)
    with conn:
        conn.execute("UPDATE posts SET text = ? WHERE post_id = ?", (text, post_id))
        for item in seen:
            item_id, status = item["id"], item["status"]
            old = before[item_id]
            if old is None:
                continue
            if status == Status.PUBLISHED:
                if gone := variants.gone(item["folder_name"], text):  # CORE-014: статус той самий
                    published.append((post_id, gone[1]))
                    continue
                after = review.load_card(conn, item_id)
                if after is not None:
                    published.append((post_id, _diff(old, after)))
                continue
            problem = None
            if _has_blank(conn, item_id):
                result = fill.recalc(conn, item_id, text)
                problem = result.problem  # і «розмір прибрано з поста» (CORE-014)
                needs = (not problem or problem[0] != "many_items") and ai.needs_ai(
                    result.version, result.answer, text
                )
                fill.save_answer(
                    conn, item_id, result.answer, result.version, result.rebuilt, needs
                )
                fill.save_blank(conn, item_id, result.fields)
            if problem is None and rules.price_from_text(text) is None:
                problem = ("no_price", f"У поста {post_id} немає ціни")
            after = review.load_card(conn, item_id)
            what = _diff(old, after) if after is not None else "текст поста змінено"
            _put_back(
                conn, item_id, status, review.POST_CHANGED, f"Пост змінено {when}: {what}", problem
            )
            cards.append(item_id)
    _announce_text(conn, post_id, cards, published)
    return TextChange(cards, published)


def _announce_text(
    conn: sqlite3.Connection, post_id: int, cards: list[int], published: list[tuple[int, str]]
) -> None:
    """BOT-013: «✏️ 7015: текст змінено → знову «На перевірку»» (Картки в роботі) і зміни поста,
    що вже на Шафі (там правлять руками)."""
    if cards:
        marks = ",".join("?" * len(cards))
        statuses = [
            r[0] for r in conn.execute(f"SELECT status FROM items WHERE id IN ({marks})", cards)
        ]
        stuck = statuses.count(Status.PROBLEM)
        where = "у «Проблемах»" if stuck == len(statuses) else "знову «На перевірку»"
        if 0 < stuck < len(statuses):
            where += f", у «Проблемах» {stuck} з {len(statuses)}"
        events.emit("post.changed", f"{post_id}: текст змінено → {where}")
    for number, what in published:
        events.emit(
            "post.changed", f"{number}: текст змінено після публікації на Шафі ({what}) — там "
            "виправ руками"
        )  # fmt: skip


def _announce_photos(changed: list[tuple[int | None, str, bool]]) -> None:
    """BOT-013: «✏️ 7015: фото змінено (було 5, стало 6) → знову «На перевірку»»."""
    changed = list(dict.fromkeys(changed))  # сестри-варіанти ділять папку — один рядок
    if len(changed) > scan.NEW_LINES:
        numbers = ", ".join(str(c[0]) for c in changed[: scan.NEW_LINES])
        events.emit("post.changed", f"фото змінено в {len(changed)} постах ({numbers} …)")
        return
    for post_id, what, problem in changed:
        where = "у «Проблемах»" if problem else "знову «На перевірку»"
        events.emit("post.changed", f"{post_id}: фото змінено ({what}) → {where}")


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:NAMES_MAX])
    return shown + (f" і ще {len(names) - NAMES_MAX}" if len(names) > NAMES_MAX else "")


def _photo_what(old: list[str], new: list[str]) -> str:
    added = [n for n in new if n not in old]
    removed = [n for n in old if n not in new]
    parts = [f"було {len(old)}, стало {len(new)}"]
    if added:
        parts.append(f"додано {_names(added)}")
    if removed:
        parts.append(f"прибрано {_names(removed)}")
    if not added and not removed:
        parts.append("інший порядок файлів")
    return parts[0] + (" (" + "; ".join(parts[1:]) + ")" if parts[1:] else "")


def sync_photos(
    conn: sqlite3.Connection, folders: list[Folder], *, moment: datetime | None = None
) -> PhotoSync:
    """TD-004: Картки `review`/`approved` — список фото проти папки (лише назви файлів: розмір чи
    час файлу в базі не зберігаються). Змінилось — список оновлено, «На перевірку» з приміткою;
    папки чи фото немає — «Проблеми» `photos_missing` (бланк і правки лишаються).

    `folders` порожній — Головна, схоже, недоступна: нічого не міняємо."""
    if not folders:
        return PhotoSync()
    by_name = {f.name: f for f in folders}
    rows = conn.execute(
        "SELECT id, folder_name, post_id, status FROM items WHERE status IN (?, ?) "
        "AND purged_at IS NULL ORDER BY id",
        FIXED,
    ).fetchall()
    changed: list[tuple[int | None, str]] = []
    shown: list[tuple[int | None, str, bool]] = []  # те саме + «пішло в Проблеми» (журнал)
    lost: list[tuple[int | None, str]] = []
    when = _date(moment)
    with conn:
        for row in rows:
            if is_old(row["folder_name"]):  # фото старих постів — у теці даних, не в Головній
                continue
            item_id, status = row["id"], row["status"]
            folder = by_name.get(base_name(row["folder_name"]))  # варіанти ділять папку (CORE-014)
            if folder is None or not folder.photos:
                why = NO_FOLDER if folder is None else NO_PHOTOS
                conn.execute(
                    "UPDATE items SET status = ?, problem_code = 'photos_missing', "
                    "problem_text = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (Status.PROBLEM, why, now(), item_id, status),
                )
                lost.append((row["post_id"], why))
                continue
            old = [
                r[0]
                for r in conn.execute(
                    "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
                )
            ]
            new = list(folder.photos)
            if old == new:
                continue
            conn.execute("DELETE FROM photos WHERE item_id = ?", (item_id,))
            conn.executemany(
                "INSERT INTO photos (item_id, position, file_name) VALUES (?, ?, ?)",
                [(item_id, i, name) for i, name in enumerate(new, start=1)],
            )
            problem = None
            if len(new) > scan.MAX_PHOTOS:
                problem = ("too_many_photos", f"Більше {scan.MAX_PHOTOS} фото")
            if _has_blank(conn, item_id):  # «порядок фото» — за новими файлами
                fill.save_blank(conn, item_id, fill.recalc(conn, item_id, _text(conn, row)).fields)
            what = _photo_what(old, new)
            _put_back(
                conn, item_id, status, review.PHOTOS_CHANGED, f"Фото змінено {when}: {what}",
                problem,
            )  # fmt: skip
            changed.append((row["post_id"], what))
            shown.append((row["post_id"], what, problem is not None))
    _announce_photos(shown)
    return PhotoSync(changed, lost)


def _text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    found = conn.execute("SELECT text FROM posts WHERE post_id = ?", (row["post_id"],)).fetchone()
    return found[0] if found else ""


# --- Зміни постів, що вже на Шафі: там правимо вручну, тож лишаємо слід для звіту ---


def record_published(
    data_dir: Path, post_id: int, what: str, moment: datetime | None = None
) -> None:
    items = _read(data_dir)
    stamp = (moment or datetime.now(UTC)).astimezone().isoformat(timespec="seconds")
    items.append({"post": post_id, "at": stamp, "what": what})
    data_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(items[-LOG_KEEP:], out, ensure_ascii=False)
        os.replace(name, data_dir / LOG_NAME)
    except OSError:
        Path(name).unlink(missing_ok=True)
        raise


def _read(data_dir: Path) -> list[dict]:
    try:
        data = json.loads((data_dir / LOG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def published_on(data_dir: Path, day: date) -> list[tuple[int, str]]:
    """Зміни постів після публікації за місцеву добу `day`: (пост, що змінилось)."""
    result = []
    for item in _read(data_dir):
        try:
            stamp = datetime.fromisoformat(item["at"]).astimezone().date()
            if stamp == day:
                result.append((int(item["post"]), str(item["what"])))
        except (KeyError, ValueError, TypeError):
            continue
    return result
