"""Старий борг (CORE-005): пости з Експорту без папок → Картки з одним фото поста.

Список номерів робить Власник (`.txt`, один номер у рядку; порожні рядки й `#` — коментарі).
Фото поста копіюється в теку даних (`<дані>/borh/<номер>/1.jpg`) — флешка чи Експорт потім не
потрібні. Картка — `borh-<номер>` у статусі `new` (далі ШІ, як усі) або `problem` з причиною.
Номер, який уже є в базі (папка, бот, попередній імпорт), пропускається: повтор не дублює.
"""

import io
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from shafa import variants
from shafa.db import Status, now
from shafa.export import Post
from shafa.folders import OLD_PREFIX, Folder, item_folder, old_folder_name
from shafa.scan import find_problem, save_posts

PHOTO_NAME = "1.jpg"
JPEG_QUALITY = 95
_NUMBER = re.compile(r"\d{1,7}")


class ListError(ValueError):
    """У файлі-списку рядок, що не є номером поста."""


def read_list(path: Path) -> list[int]:
    """Номери постів зі списку в порядку файлу, без повторів."""
    numbers: list[int] = []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not _NUMBER.fullmatch(line):
            raise ListError(f"рядок {n}: «{line[:40]}» — не номер поста")
        if int(line) not in numbers:
            numbers.append(int(line))
    return numbers


@dataclass
class ImportResult:
    created: list[int] = field(default_factory=list)
    existed: list[int] = field(default_factory=list)
    reordered: int = 0  # неопублікованим старим змінено місце в черзі
    not_in_export: list[int] = field(default_factory=list)
    no_photo: list[int] = field(default_factory=list)
    problems: list[tuple[int, str]] = field(default_factory=list)  # створено, але в Проблемах

    def text(self) -> str:
        summary = (
            f"Створено: {len(self.created)} · вже було: {len(self.existed)} · "
            f"немає в експорті: {len(self.not_in_export)} · без фото: {len(self.no_photo)}"
        )
        if self.reordered:
            summary += f" · порядок оновлено: {self.reordered}"
        lines = [summary]
        if self.not_in_export:
            lines.append("Немає в експорті: " + ", ".join(map(str, self.not_in_export)))
        if self.problems:
            lines.append("У Проблемах:")
            lines += [f"  {post_id}: {text}" for post_id, text in self.problems]
        return "\n".join(lines)


def _known(conn: sqlite3.Connection, post_id: int) -> bool:
    """Пост уже в роботі: є Картка (папка, бот, попередній імпорт) або пост від бота."""
    if conn.execute("SELECT 1 FROM items WHERE post_id = ?", (post_id,)).fetchone():
        return True
    row = conn.execute("SELECT source FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    return row is not None and row[0] == "bot"


def copy_photo(source: Path, target: Path) -> bool:
    """Фото поста → `target` (JPEG). Не відкривається — False, нічого не лишає."""
    try:
        with Image.open(source) as original:
            fmt = original.format
            image = ImageOps.exif_transpose(original).convert("RGB")
    except (OSError, UnidentifiedImageError):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    if fmt == "JPEG":
        shutil.copyfile(source, temp)  # як у Telegram, без перестискання
    else:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        temp.write_bytes(buffer.getvalue())
    temp.replace(target)
    return True


_UNPUBLISHED = (Status.NEW, Status.PROBLEM, Status.REVIEW, Status.APPROVED)


def _set_position(conn: sqlite3.Connection, post_id: int, position: int) -> bool:
    """Стара Картка, ще не опублікована, — нове місце в черзі. Опублікованих і нових не чіпає."""
    with conn:
        cursor = conn.execute(
            f"UPDATE items SET queue_pos = ? WHERE post_id = ? AND folder_name LIKE '{OLD_PREFIX}%' "
            f"AND status IN ({', '.join('?' * len(_UNPUBLISHED))}) "
            "AND queue_pos IS NOT ?",
            (position, post_id, *_UNPUBLISHED, position),
        )
    return cursor.rowcount > 0


def import_old(
    conn: sqlite3.Connection, posts: dict[int, Post], export_dir: Path, numbers: list[int]
) -> ImportResult:
    """Картки для номерів зі списку; позиція в списку — місце в черзі. Повтор не дублює."""
    result = ImportResult()
    for position, post_id in enumerate(numbers, start=1):
        post = posts.get(post_id)
        if post is None:
            result.not_in_export.append(post_id)
            continue
        if _known(conn, post_id):
            result.existed.append(post_id)
            if _set_position(conn, post_id, position):
                result.reordered += 1
            continue
        name = old_folder_name(post_id)
        target = item_folder(Path(), name) / PHOTO_NAME
        source = export_dir / post.photo if post.photo else None
        has_photo = source is not None and source.is_file() and copy_photo(source, target)
        folder = Folder(name, post_id, (PHOTO_NAME,) if has_photo else ())
        with conn:
            save_posts(conn, {post_id: post})
            problem = find_problem(folder, {post_id: post.text}, {post_id: [name]})
            if problem and problem[0] == "no_photos":
                problem = ("no_photos", "У пості немає фото")
            stamp = now()
            item_id = conn.execute(
                """
                INSERT INTO items (folder_name, post_id, status, problem_code, problem_text,
                                   created_at, updated_at, queue_pos)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, post_id, Status.PROBLEM if problem else Status.NEW,
                 *(problem or (None, None)), stamp, stamp, position),
            ).lastrowid  # fmt: skip
            conn.executemany(
                "INSERT INTO photos (item_id, position, file_name) VALUES (?, ?, ?)",
                [(item_id, i, n) for i, n in enumerate(folder.photos, start=1)],
            )
            variants.split_card(conn, item_id)  # кілька розмірів — Картка на розмір (CORE-014)
        result.created.append(post_id)
        if not has_photo:
            result.no_photo.append(post_id)
        elif problem:
            result.problems.append((post_id, problem[1]))
    return result


def recheck(conn: sqlite3.Connection, item_id: int) -> str | None:
    """«Перевірити ще раз» для старого поста: фото в теці даних і текст з бази → new / problem."""
    row = conn.execute("SELECT folder_name, post_id FROM items WHERE id = ?", (item_id,)).fetchone()
    name, post_id = row["folder_name"], row["post_id"]
    has_photo = (item_folder(Path(), name) / PHOTO_NAME).is_file()
    folder = Folder(name, post_id, (PHOTO_NAME,) if has_photo else ())
    texts = {r[0]: r[1] for r in conn.execute("SELECT post_id, text FROM posts WHERE post_id = ?",
                                               (post_id,))}  # fmt: skip
    problem = find_problem(folder, texts, {post_id: [name]})
    if problem and problem[0] == "no_photos":
        problem = ("no_photos", "У пості немає фото")
    if problem and problem[0] == "post_not_found":
        problem = ("post_not_found", f"Поста {post_id} немає в базі")
    if problem is None and post_id in texts:
        problem = variants.gone(name, texts[post_id])  # розмір варіанта прибрано (CORE-014)
    status = Status.PROBLEM if problem else Status.NEW
    code, text = problem or (None, None)
    conn.execute(
        "UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ? "
        "WHERE id = ?",
        (status, code, text, now(), item_id),
    )
    conn.execute("DELETE FROM photos WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO photos (item_id, position, file_name) VALUES (?, ?, ?)",
        [(item_id, i, n) for i, n in enumerate(folder.photos, start=1)],
    )
    variants.split_card(conn, item_id)
    return conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]
