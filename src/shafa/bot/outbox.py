"""Вихідна черга бота на сервері (OPS-003): що забрати компу.

Бот на сервері пише сюди кожен новий пост (з папкою фото), правку поста в каналі, «уже в черзі» й
команди Власника — у тій самій транзакції, що й запис у свою базу. Комп через SSH (примусова
команда в `authorized_keys`, лише дві дії) забирає «все після N» одним zip і підтверджує «до N».

- `seq` ніколи не повторюється; повторна віддача тих самих номерів дає той самий вміст — комп
  застосовує ідемпотентно (OPS-004).
- Пост записано, а папка ще пишеться (`ready = 0`) — черга віддається лише ДО нього: інакше комп
  підтвердив би номер, фото якого ще не було.
- Фото не видаляються, доки комп не підтвердив, і ще `KEEP_DAYS` днів після.
- Час подій — Unix (`at`): місцеву дату рахує комп, пояс сервера не важить.
"""

import hashlib
import io
import json
import re
import shutil
import sqlite3
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from shafa.db import now
from shafa.folders import PHOTO_SUFFIXES, natural_key

FORMAT = 1
INDEX = "outbox.json"
PHOTOS = "photos"
KINDS = ("post", "edit", "gone", "known", "owner_cmd")
LIMIT = 50  # записів за одну віддачу
MAX_BYTES = 64 * 1_048_576  # фото за одну віддачу (≥ 1 запис завжди)
KEEP_DAYS = 14  # стільки днів після підтвердження фото поста ще лежать на сервері
SSH_COMMAND = re.compile(r"(give|ack) (\d{1,12})")


class OutboxError(Exception):
    """Запит до черги неправильний. Текст — для людини."""


def add(
    conn: sqlite3.Connection,
    kind: str,
    at: int,
    post_id: int | None = None,
    data: dict[str, Any] | None = None,
    ready: bool = True,
) -> int:
    """Запис у чергу. Транзакцію відкриває той, хто кличе (разом зі своїм записом)."""
    if kind not in KINDS:
        raise ValueError(f"Невідомий вид запису черги: {kind}")
    cursor = conn.execute(
        "INSERT INTO outbox (kind, at, post_id, data, ready, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (kind, int(at), post_id, json.dumps(data or {}, ensure_ascii=False), int(ready), now()),
    )
    return int(cursor.lastrowid or 0)


def mark_ready(conn: sqlite3.Connection, seq: int, folder: str | None) -> None:
    """Папку записано (чи не вдалось — `folder` None): запис можна віддавати."""
    row = conn.execute("SELECT data FROM outbox WHERE seq = ?", (seq,)).fetchone()
    if row is None:
        return
    data = json.loads(row[0])
    data["folder"] = folder
    with conn:
        conn.execute(
            "UPDATE outbox SET data = ?, ready = 1 WHERE seq = ?",
            (json.dumps(data, ensure_ascii=False), seq),
        )


def settle(conn: sqlite3.Connection) -> list[tuple[int, str | None]]:
    """Після падіння: пости, чия папка не встигла записатись, — готові з тим, що є (папка з
    чернетки або жодної). Повертає (номер поста, папка) — щоб сказати Власнику."""
    done = []
    for row in conn.execute(
        "SELECT seq, post_id FROM outbox WHERE ready = 0 ORDER BY seq"
    ).fetchall():
        found = conn.execute(
            "SELECT folder_name FROM bot_drafts WHERE post_id = ? AND folder_name IS NOT NULL "
            "AND state IN ('published', 'queued') ORDER BY id DESC LIMIT 1",
            (row["post_id"],),
        ).fetchone()
        folder = found[0] if found else None
        mark_ready(conn, row["seq"], folder)
        done.append((row["post_id"], folder))
    return done


def post_folder(root: Path, name: Any) -> Path | None:
    """Тека поста в Головній — лише пряма підтека, не символьне посилання."""
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    path = root / name
    if path.is_symlink() or not path.is_dir():
        return None
    return path


def photo_files(folder: Path) -> list[Path]:
    return sorted(
        (
            p for p in folder.iterdir()
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in PHOTO_SUFFIXES
            and not p.name.startswith(".")
        ),
        key=lambda p: natural_key(p.name),
    )  # fmt: skip


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Given:
    last: int  # останній віданий `seq` (= `after`, якщо нічого)
    count: int
    more: bool  # є ще готові записи — забрати ще раз


def _ready_rows(conn: sqlite3.Connection, after: int) -> Iterable[sqlite3.Row]:
    for row in conn.execute(
        "SELECT seq, kind, at, post_id, data, ready FROM outbox WHERE seq > ? ORDER BY seq",
        (after,),
    ).fetchall():
        if not row["ready"]:
            return  # далі — лише після того, як цей стане готовим
        yield row


def give(
    conn: sqlite3.Connection,
    root: Path,
    after: int,
    out: BinaryIO,
    limit: int = LIMIT,
    max_bytes: int = MAX_BYTES,
) -> Given:
    """Zip з записами `seq > after` (до першого неготового) і фото їхніх папок → `out`."""
    if after < 0:
        raise OutboxError("Номер має бути ≥ 0")
    records: list[dict[str, Any]] = []
    files: list[tuple[str, Path]] = []
    size = 0
    more = False
    for row in _ready_rows(conn, after):
        if len(records) >= limit:
            more = True
            break
        data = json.loads(row["data"])
        listed: list[dict[str, Any]] = []
        folder = post_folder(root, data.get("folder")) if row["kind"] == "post" else None
        photos = photo_files(folder) if folder else []
        extra = sum(p.stat().st_size for p in photos)
        if records and size + extra > max_bytes:
            more = True
            break
        for path in photos:
            name = f"{PHOTOS}/{row['seq']}/{path.name}"
            listed.append({"name": path.name, "sha256": _sha256(path), "size": path.stat().st_size})
            files.append((name, path))
        size += extra
        records.append({
            "seq": row["seq"], "kind": row["kind"], "at": row["at"], "post_id": row["post_id"],
            "data": data, "files": listed,
        })  # fmt: skip
    last = records[-1]["seq"] if records else after
    status = conn.execute("SELECT heartbeat_at, error FROM bot_status WHERE id = 1").fetchone()
    index = {
        "format": FORMAT,
        "after": after,
        "last": last,
        "more": more,
        "records": records,
        "bot": dict(status) if status else None,  # пульс бота на сервері — для панелі на компі
        "at": now(),
    }
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(INDEX, json.dumps(index, ensure_ascii=False, indent=1))
        for name, path in files:
            zf.write(path, name, compress_type=zipfile.ZIP_STORED)  # JPEG не стискається
    if records:
        with conn:
            conn.execute(
                "UPDATE outbox SET served_at = ? WHERE seq > ? AND seq <= ? AND served_at IS NULL",
                (now(), after, last),
            )
    return Given(last, len(records), more)


def give_bytes(conn: sqlite3.Connection, root: Path, after: int, **kw: Any) -> tuple[bytes, Given]:
    buffer = io.BytesIO()
    given = give(conn, root, after, buffer, **kw)
    return buffer.getvalue(), given


@dataclass
class Acked:
    upto: int
    count: int  # скільки записів підтверджено цим разом (повтор — 0)
    pruned: list[str]  # прибрані теки фото


def ack(conn: sqlite3.Connection, upto: int, root: Path, moment: datetime | None = None) -> Acked:
    """Комп забрав і застосував усе до `upto`. Підтвердити можна лише віддане."""
    served = conn.execute("SELECT max(seq) FROM outbox WHERE served_at IS NOT NULL").fetchone()[0]
    if upto < 0 or upto > (served or 0):
        raise OutboxError(f"Не можна підтвердити {upto}: віддано лише до {served or 0}")
    with conn:
        cursor = conn.execute(
            "UPDATE outbox SET acked_at = ? WHERE seq <= ? AND acked_at IS NULL", (now(), upto)
        )
    return Acked(upto, cursor.rowcount, prune(conn, root, moment))


def prune(
    conn: sqlite3.Connection, root: Path, moment: datetime | None = None, keep_days: int = KEEP_DAYS
) -> list[str]:
    """Теки фото постів, підтверджених комп'ютером давніше за `keep_days` днів, — геть.

    Лише пряма підтека Головної з тією самою назвою, не посилання; тека, на яку посилається ще не
    підтверджений запис, лишається."""
    moment = moment or datetime.now().astimezone()
    cutoff = (moment - timedelta(days=keep_days)).astimezone().isoformat(timespec="seconds")
    pending = {
        json.loads(r[0]).get("folder")
        for r in conn.execute("SELECT data FROM outbox WHERE kind = 'post' AND acked_at IS NULL")
    }
    removed = []
    for row in conn.execute(
        "SELECT seq, data FROM outbox WHERE kind = 'post' AND acked_at IS NOT NULL "
        "AND acked_at < ? AND pruned_at IS NULL",
        (_utc(cutoff),),
    ).fetchall():
        name = json.loads(row["data"]).get("folder")
        if name is not None and name in pending:
            continue  # ту саму теку ще не забрано під іншим записом
        folder = post_folder(root, name)
        if folder is not None and folder.parent.resolve() == root.resolve():
            shutil.rmtree(folder)
            removed.append(folder.name)
        with conn:
            conn.execute("UPDATE outbox SET pruned_at = ? WHERE seq = ?", (now(), row["seq"]))
    return removed


def _utc(stamp: str) -> str:
    """Межа в тому ж форматі, що `now()` (UTC ISO), — рядки порівнюються."""
    return datetime.fromisoformat(stamp).astimezone(UTC).isoformat(timespec="seconds")


def parse_ssh(command: str | None) -> tuple[str, int]:
    """`SSH_ORIGINAL_COMMAND` примусової команди: лише «give N» або «ack N», нічого більше."""
    match = SSH_COMMAND.fullmatch(command or "")
    if match is None:
        raise OutboxError("Дозволено лише «give N» або «ack N»")
    return match.group(1), int(match.group(2))
