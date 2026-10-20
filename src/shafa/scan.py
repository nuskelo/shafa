"""Зіставлення папок товарів із постами: Картки й «Проблеми»."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from shafa import catalog
from shafa.bot.hashtag import accessory_group, item_kind
from shafa.db import Status, now
from shafa.export import Post
from shafa.folders import Folder, is_old, natural_key
from shafa.rules import many_items, many_sizes, price_from_text

MAX_PHOTOS = 10

# Статуси, які сканування може міняти. Картки в роботі (review і далі) не чіпає.
SCANNABLE = (Status.NEW, Status.PROBLEM)

# «Липкі» проблеми публікації (D2): товар міг уже з'явитися на Шафі. Сканування їх не чіпає —
# повертає лише людина, перевіривши Шафу. Інакше Картка пішла б у new → ШІ → друга публікація.
STICKY = (
    "publish_uncertain", "possible_duplicate", "publish_error", "already_published",
    "shafa_declined",  # ROBOT-011: Шафа відхилила вже опубліковане — друга публікація дала б дубль
)  # fmt: skip


def robot_problem(problem_code: str | None) -> bool:
    """Проблема публікації чи робота: бланк і правки лишаються, повертає лише людина."""
    return problem_code in STICKY or (problem_code or "").startswith("robot_")


def touchable(status: str, problem_code: str | None) -> bool:
    """Чи може сканування змінити Картку з таким статусом і кодом проблеми.

    Проблеми робота (`robot_…`) теж не чіпає: інакше Картка пішла б у new і ШІ заповнило б бланк
    знову (TD-015). «Перевірити ще раз» повертає їх у Схвалені (`review.robot_retry`).
    """
    return status in SCANNABLE and not robot_problem(problem_code)


@dataclass(frozen=True)
class Problem:
    folder_name: str
    code: str
    text: str


@dataclass(frozen=True)
class ScanResult:
    folders: int
    new: int
    problems: list[Problem]


def save_posts(conn: sqlite3.Connection, posts: dict[int, Post]) -> None:
    """Пости з Експорту → таблиця `posts`. Пости від бота не перезаписуються."""
    added_at = now()
    conn.executemany(
        """
        INSERT INTO posts (post_id, date, text, source, added_at)
        VALUES (?, ?, ?, 'export', ?)
        ON CONFLICT (post_id) DO UPDATE SET date = excluded.date, text = excluded.text
        WHERE posts.source = 'export'
        """,
        [(p.post_id, p.date, p.text, added_at) for p in posts.values()],
    )


def _join(names: list[str]) -> str:
    return ", ".join(names[:-1]) + " і " + names[-1]


NO_SECTION_TEXT = "Немає підходящого розділу на Шафі (окуляри, парасольки, шкарпетки тощо)"


def text_problem(text: str) -> tuple[str, str] | None:
    """Проблема, яку видно з самого тексту поста (ШІ тут не допоможе): кілька товарів, немає розділу.

    Перевіряє і «Перезаповнити» (QA-001): Картки, завантажені до нових правил, інакше їх обходять.
    """
    if many_items(text):
        return "many_items", "У пості кілька товарів з різними цінами"
    if many_sizes(text):  # «43 і 45» — два товари (CORE-007)
        return "many_items", "У пості кілька розмірів — схоже, кілька товарів"
    if item_kind(text) == "other":  # окуляри, парасольки… — розділу на Шафі немає
        # CORE-010: рукавички, шарфи, шапки — розділи є, коли гілку вже зібрано у знімку
        group = accessory_group(text)
        if group is None or not catalog.has_group(group):
            return "no_section", NO_SECTION_TEXT
    return None


def find_problem(
    folder: Folder, texts: dict[int, str], twins: dict[int, list[str]]
) -> tuple[str, str] | None:
    """Причина «Проблеми» для папки (код, текст для Продавчині) або None."""
    post_id = folder.post_id
    if post_id is None:
        return "bad_folder_name", "Назва папки не номер поста"
    if len(twins[post_id]) > 1:
        names = twins[post_id]
        count = "Дві папки" if len(names) == 2 else "Кілька папок"
        return "duplicate_post", f"{count} для одного поста: {_join(names)}"
    if post_id not in texts:
        return "post_not_found", f"Поста з номером {post_id} немає в експорті"
    if not texts[post_id].strip():
        return "no_text", f"У поста {post_id} немає тексту"
    if price_from_text(texts[post_id]) is None:
        return "no_price", f"У поста {post_id} немає ціни"
    if problem := text_problem(texts[post_id]):
        return problem
    if not folder.photos:
        return "no_photos", "У папці немає фото .jpg"
    if len(folder.photos) > MAX_PHOTOS:
        return "too_many_photos", f"Більше {MAX_PHOTOS} фото"
    return None


def _old_posts(conn: sqlite3.Connection) -> set[int]:
    """Пости, завантажені як старі (CORE-005): для них папка в Головній — дубль."""
    return {
        r[1]
        for r in conn.execute("SELECT folder_name, post_id FROM items WHERE post_id IS NOT NULL")
        if is_old(r[0])
    }


def folder_problem(
    folder: Folder, texts: dict[int, str], twins: dict[int, list[str]], old: set[int]
) -> tuple[str, str] | None:
    if folder.post_id in old:
        return "duplicate_post", f"Пост {folder.post_id} уже завантажено як старий (без папки)"
    return find_problem(folder, texts, twins)


def _save_item(conn: sqlite3.Connection, folder: Folder, problem: tuple[str, str] | None) -> None:
    status = Status.PROBLEM if problem else Status.NEW
    code, text = problem or (None, None)
    row = conn.execute(
        "SELECT id, post_id, status, problem_code, problem_text FROM items WHERE folder_name = ?",
        (folder.name,),
    ).fetchone()
    stamp = now()
    if row is None:
        item_id = conn.execute(
            """
            INSERT INTO items (folder_name, post_id, status, problem_code, problem_text,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (folder.name, folder.post_id, status, code, text, stamp, stamp),
        ).lastrowid
    else:
        if not touchable(row["status"], row["problem_code"]):
            return
        item_id = row["id"]
        photos = tuple(
            r[0]
            for r in conn.execute(
                "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
            )
        )
        same = (row["post_id"], row["status"], row["problem_code"], row["problem_text"]) == (
            folder.post_id,
            status,
            code,
            text,
        )
        if same and photos == folder.photos:
            return
        conn.execute(
            """
            UPDATE items SET post_id = ?, status = ?, problem_code = ?, problem_text = ?,
                             updated_at = ?
            WHERE id = ?
            """,
            (folder.post_id, status, code, text, stamp, item_id),
        )
        conn.execute("DELETE FROM photos WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO photos (item_id, position, file_name) VALUES (?, ?, ?)",
        [(item_id, i, name) for i, name in enumerate(folder.photos, start=1)],
    )


def _twins(folders: list[Folder]) -> dict[int, list[str]]:
    twins: dict[int, list[str]] = defaultdict(list)
    for folder in folders:
        if folder.post_id is not None:
            twins[folder.post_id].append(folder.name)
    return twins


def _mark_missing(conn: sqlite3.Connection, present: set[str]) -> None:
    rows = conn.execute(
        "SELECT id, folder_name, problem_code FROM items WHERE status IN (?, ?)", SCANNABLE
    ).fetchall()
    stamp = now()
    for row in rows:
        if row["folder_name"] in present or row["problem_code"] == "folder_missing":
            continue
        if is_old(row["folder_name"]):  # фото старого поста — у теці даних, не в Головній
            continue
        if robot_problem(row["problem_code"]):
            continue
        conn.execute(
            """
            UPDATE items SET status = ?, problem_code = 'folder_missing',
                             problem_text = 'Папку не знайдено', updated_at = ?
            WHERE id = ?
            """,
            (Status.PROBLEM, stamp, row["id"]),
        )


def _texts(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT post_id, text FROM posts")}


def scan(conn: sqlite3.Connection, posts: dict[int, Post], folders: list[Folder]) -> ScanResult:
    """Створити або оновити Картки для папок. Повторний запуск нічого не дублює."""
    with conn:
        save_posts(conn, posts)
        texts = _texts(conn)
        twins = _twins(folders)
        old = _old_posts(conn)
        for folder in folders:
            _save_item(conn, folder, folder_problem(folder, texts, twins, old))
        _mark_missing(conn, {f.name for f in folders})

    new = conn.execute("SELECT count(*) FROM items WHERE status = ?", (Status.NEW,)).fetchone()[0]
    problems = [
        Problem(r["folder_name"], r["problem_code"], r["problem_text"])
        for r in conn.execute(
            "SELECT folder_name, problem_code, problem_text FROM items WHERE status = ?",
            (Status.PROBLEM,),
        )
    ]
    problems.sort(key=lambda p: natural_key(p.folder_name))
    return ScanResult(len(folders), new, problems)


def rescan_folder(conn: sqlite3.Connection, folder_name: str, folders: list[Folder]) -> str | None:
    """Перевірити ще раз одну Картку `new`/`problem`: тексти — з бази, інші Картки не чіпає.

    `folders` — усі папки Головної зараз (для пошуку двох папок одного поста).
    Повертає новий статус або None, якщо Картки немає чи її перевіряти не можна.
    """
    with conn:
        row = conn.execute(
            "SELECT id, status, problem_code FROM items WHERE folder_name = ?", (folder_name,)
        ).fetchone()
        if row is None or not touchable(row["status"], row["problem_code"]):
            return None
        if is_old(folder_name):
            from shafa import borh  # borh бере правила звідси

            return borh.recheck(conn, row["id"])
        folder = next((f for f in folders if f.name == folder_name), None)
        if folder is None:
            conn.execute(
                """
                UPDATE items SET status = ?, problem_code = 'folder_missing',
                                 problem_text = 'Папку не знайдено', updated_at = ?
                WHERE id = ?
                """,
                (Status.PROBLEM, now(), row["id"]),
            )
            return Status.PROBLEM
        problem = folder_problem(folder, _texts(conn), _twins(folders), _old_posts(conn))
        _save_item(conn, folder, problem)
        return conn.execute("SELECT status FROM items WHERE id = ?", (row["id"],)).fetchone()[0]
