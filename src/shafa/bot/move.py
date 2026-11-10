"""Перехід бота з компа на сервер (OPS-003): `bot-pack` на компі → `bot-unpack` на сервері.

Їде лише те, що потрібно боту: оброблені оновлення (`bot_updates` — інакше останню пачку, яку комп
обробив, але не підтвердив Telegram, сервер обробив би вдруге), чернетки з частинами (ті самі id —
кнопки старих прев'ю працюють), зведення, пам'ять каналу (`channel_posts`), номери всіх постів
(«вже в черзі» й старт перегляду каналу; без текстів) і теки постів, чий альбом у коментарях ще
чекає. Секретів, Карток, сайту немає.

Розпаковка не створює записів вихідної черги (ці пости комп уже має) і повтор нічого не міняє: усе
`INSERT OR IGNORE`, тека, що вже є з тими самими файлами, пропускається. Конфлікт (чернетка з тим
самим номером інша, тека з іншими фото) — відмова до будь-якого запису.
"""

import hashlib
import json
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from shafa import config, db
from shafa.bot import outbox, run
from shafa.robot import lock

FORMAT = 1
INDEX = "bot.json"
FOLDERS = "folders"
TMP = ".bot-unpack-tmp"
TABLES = ("bot_updates", "bot_drafts", "bot_draft_parts", "bot_summaries", "channel_posts")
LABELS = {
    "bot_updates": "оновлень Telegram",
    "bot_drafts": "чернеток",
    "bot_draft_parts": "частин чернеток",
    "bot_summaries": "зведень",
    "channel_posts": "постів каналу",
    "posts": "номерів постів",
}


class MoveError(Exception):
    """Перенести не можна. Текст — для людини."""


def _refuse_if_running() -> None:
    if lock.busy(config.data_dir() / run.LOCK_NAME) is not None:
        raise MoveError(
            "Бот зараз працює — спершу «Вийти» (чи `python -m shafa quit`), дочекайся й повтори."
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@dataclass
class Packed:
    out: Path
    counts: dict[str, int]
    folders: list[str]

    def text(self) -> str:
        parts = [f"{LABELS[t]} {n}" for t, n in self.counts.items()]
        return (
            f"Готово: {self.out}\nУ архіві: {', '.join(parts)}; тек з фото: {len(self.folders)}.\n"
            "Секретів немає: токен і налаштування на сервері вводяться окремо."
        )


def pack(db_path: Path, root: Path, out: Path) -> Packed:
    _refuse_if_running()
    if not db_path.is_file():
        raise MoveError(f"Базу не знайдено: {db_path}")
    if out.exists():
        raise MoveError(f"{out} уже є — вибери іншу назву")
    conn = db.connect(db_path)
    try:
        tables = {t: _rows(conn, t) for t in TABLES}
        tables["posts"] = [
            dict(r)
            for r in conn.execute("SELECT post_id, date, source FROM posts ORDER BY post_id")
        ]
        waiting = [
            r[0] for r in conn.execute(
                "SELECT folder_name FROM bot_drafts WHERE comments = 'waiting' "
                "AND folder_name IS NOT NULL ORDER BY id"
            )
        ]  # fmt: skip
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    folders: dict[str, list[dict[str, Any]]] = {}
    files: list[tuple[str, Path]] = []
    for name in waiting:
        folder = outbox.post_folder(root, name)
        if folder is None or name in folders:
            continue
        listed = []
        for path in outbox.photo_files(folder):
            listed.append({"name": path.name, "sha256": _sha256(path.read_bytes())})
            files.append((f"{FOLDERS}/{name}/{path.name}", path))
        folders[name] = listed
    index = {"format": FORMAT, "schema": schema, "tables": tables, "folders": folders}
    tmp = out.with_name(out.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(INDEX, json.dumps(index, ensure_ascii=False))
            for name, path in files:
                zf.write(path, name, compress_type=zipfile.ZIP_STORED)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return Packed(out, {t: len(rows) for t, rows in tables.items()}, list(folders))


@dataclass
class Unpacked:
    added: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)  # уже були — не чіпали
    folders: list[str] = field(default_factory=list)
    folders_kept: list[str] = field(default_factory=list)

    def text(self) -> str:
        added = ", ".join(f"{LABELS[t]} {n}" for t, n in self.added.items() if n) or "нічого"
        lines = [f"Додано: {added}; тек з фото: {len(self.folders)}."]
        kept = ", ".join(f"{LABELS[t]} {n}" for t, n in self.kept.items() if n)
        if kept or self.folders_kept:
            lines.append(f"Уже було (не чіпав): {kept or '—'}; тек: {len(self.folders_kept)}.")
        lines.append("Черга для компа не поповнилась: ці пости комп уже має.")
        return "\n".join(lines)


def _safe_member(name: str) -> tuple[str, str] | None:
    """Ім'я в архіві: `bot.json` → None; `folders/<тека>/<файл>` → (тека, файл); інше — відмова."""
    if name == INDEX:
        return None
    path = PurePosixPath(name)
    parts = path.parts
    bad = (
        path.is_absolute() or "\\" in name or ":" in name or len(parts) != 3
        or parts[0] != FOLDERS or any(p in (".", "..") or p.startswith(".") for p in parts[1:])
    )  # fmt: skip
    if bad:
        raise MoveError(f"У архіві підозрілий шлях «{name}» — це не архів `bot-pack`.")
    return parts[1], parts[2]


def _read(zip_path: Path) -> tuple[dict, dict[str, dict[str, bytes]]]:
    if not zip_path.is_file():
        raise MoveError(f"Архів не знайдено: {zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if zf.testzip() is not None:
                raise MoveError("Архів пошкоджено.")
            names = {m.filename for m in zf.infolist() if not m.is_dir()}
            if INDEX not in names:
                raise MoveError(f"В архіві немає {INDEX} — це не архів `bot-pack`.")
            index = json.loads(zf.read(INDEX).decode("utf-8"))
            files: dict[str, dict[str, bytes]] = {}
            for name in sorted(names):
                place = _safe_member(name)
                if place is not None:
                    files.setdefault(place[0], {})[place[1]] = zf.read(name)
    except (zipfile.BadZipFile, ValueError, KeyError) as error:
        raise MoveError("Архів не читається.") from error
    if index.get("format") != FORMAT or not isinstance(index.get("tables"), dict):
        raise MoveError("Архів зроблено іншою версією — онови програму.")
    if not isinstance(index.get("schema"), int) or index["schema"] > db.SCHEMA_VERSION:
        raise MoveError("Архів з новішої версії програми — онови програму на сервері.")
    for folder, listed in (index.get("folders") or {}).items():
        got = files.get(folder, {})
        want = {f["name"]: f["sha256"] for f in listed}
        if set(got) != set(want) or any(_sha256(got[n]) != want[n] for n in want):
            raise MoveError(f"Фото теки {folder} не збігаються з описом (архів пошкоджено).")
    if set(files) - set(index.get("folders") or {}):
        raise MoveError("В архіві зайві теки — це не архів `bot-pack`.")
    return index, files


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _same_folder(path: Path, files: dict[str, bytes]) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    present = {p.name: p for p in path.iterdir() if p.is_file()}
    return set(present) == set(files) and all(
        _sha256(present[n].read_bytes()) == _sha256(files[n]) for n in files
    )


def unpack(zip_path: Path, db_path: Path, root: Path) -> Unpacked:
    _refuse_if_running()
    index, files = _read(zip_path)
    tables: dict[str, list[dict[str, Any]]] = index["tables"]
    conn = db.connect(db_path)
    try:
        allowed = {t: _columns(conn, t) for t in (*TABLES, "posts")}
        for table, rows in tables.items():
            if table not in allowed or not isinstance(rows, list):
                raise MoveError(f"Невідома таблиця в архіві: {table}")
            for row in rows:
                if not isinstance(row, dict) or not set(row) <= allowed[table]:
                    raise MoveError(f"Невідомі поля таблиці {table} в архіві.")
        for row in tables.get("bot_drafts", []):
            have = conn.execute(
                "SELECT chat_id, created_at FROM bot_drafts WHERE id = ?", (row.get("id"),)
            ).fetchone()
            if have is not None and tuple(have) != (row.get("chat_id"), row.get("created_at")):
                raise MoveError(
                    f"На сервері вже є інша чернетка {row.get('id')} — розпаковувати в цю базу "
                    "не можна (потрібна нова база бота)."
                )
        result = Unpacked()
        for name, content in files.items():
            target = root / name
            if target.exists() or target.is_symlink():
                if not _same_folder(target, content):
                    raise MoveError(f"Тека {name} уже є з іншими фото — розберись руками.")
                result.folders_kept.append(name)
        for name, content in files.items():  # тимчасова назва → перейменування
            if name in result.folders_kept:
                continue
            tmp = root / f".{name}{TMP}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for file_name, data in content.items():
                (tmp / file_name).write_bytes(data)
            tmp.rename(root / name)
            result.folders.append(name)
        stamp = db.now()
        with conn:
            for table in (*TABLES, "posts"):
                added = 0
                rows = tables.get(table, [])
                for row in rows:
                    if table == "posts":  # лише номер, дата й джерело: текст боту не потрібен
                        row = {**row, "text": "", "added_at": stamp}
                    cols = list(row)
                    added += conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "
                        f"VALUES ({', '.join('?' * len(cols))})",
                        [row[c] for c in cols],
                    ).rowcount
                result.added[table] = added
                result.kept[table] = len(rows) - added
    finally:
        conn.close()
    return result
