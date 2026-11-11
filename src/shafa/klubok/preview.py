"""KLB-002 (D11): пробний прогін черги Клубку на копії бази — нічого не пише.

Файл відкривається лише для читання (`mode=ro`), копіюється в пам'ять (`Connection.backup`) і
мігрується **в пам'яті** тим самим кодом, що й `db.connect` (`db.migrate`): так працює й копія
живої бази старшої версії (17), а сам файл не змінюється.
"""

import collections
import sqlite3
from datetime import datetime
from pathlib import Path

from shafa import db
from shafa.klubok import queue
from shafa.review import load_card

SHOW = 30  # Карток черги у виводі
NO_PHOTOS_SHOW = 10
NAME_MAX = 48
LINES_MAX = 60


class CopyError(Exception):
    """Базу не відкрито (немає, пошкоджена, новіша за програму)."""


def open_copy(path: Path) -> sqlite3.Connection:
    """Копія бази в пам'яті, доведена до поточної схеми. Файл лише читається."""
    try:
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        memory = sqlite3.connect(":memory:")
        try:
            source.backup(memory)
        finally:
            source.close()
    except sqlite3.Error as error:
        raise CopyError(f"Базу не відкрито лише для читання: {error}") from None
    memory.row_factory = sqlite3.Row
    memory.execute("PRAGMA foreign_keys = ON")
    try:
        db.migrate(memory)
    except RuntimeError as error:  # новіша за програму
        memory.close()
        raise CopyError(str(error)) from None
    return memory


def _short(text: str) -> str:
    return text if len(text) <= NAME_MAX else text[: NAME_MAX - 1] + "…"


def text(conn: sqlite3.Connection, root: Path, today: datetime) -> list[str]:
    """Звіт ≤ 60 рядків: знімок, ліміт, перші 30 у черзі, «не піде» за кодами, дублі, без фото."""
    read = conn.execute("SELECT read_at, count FROM klubok_listings_read WHERE id = 1").fetchone()
    snap = "немає (KLB-003 перечитає перед сесією)" if read is None else (
        f"прочитано {read['read_at']}, оголошень {read['count']}"
        + ("" if queue.snapshot_fresh(conn, today) else " — старший за добу")
    )  # fmt: skip
    started, limit = queue.started_today(conn, today), queue.daily_limit()
    verdicts = queue.plan(conn, root, today)
    queued = [v for v in verdicts if v.state == queue.QUEUED]
    skipped = [v for v in verdicts if v.state == queue.SKIPPED]
    dups = [v for v in verdicts if v.code == queue.DUPLICATE]
    state = "увімкнено" if queue.enabled() else "вимкнено"
    lines = [
        (
            f"Клубок {state} · пости > {queue.from_post()} · "
            f"сьогодні ({today:%d.%m.%Y}) почато {started} з {limit}"
        ),
        f"Знімок її оголошень: {snap}",
        (
            f"Кандидатів {len(verdicts)}: у черзі {len(queued)} · не піде {len(skipped)} · "
            f"дублів {len(dups)}"
        ),
        f"Черга (перші {min(SHOW, len(queued))}):",
    ]
    for v in queued[:SHOW]:
        if v.ad is None:
            continue
        where = " / ".join(v.ad.leaf.path[-2:])
        lines.append(f"  {v.post_id} · {_short(v.ad.name)} · {where} · фото {len(v.ad.photos)}")
    codes = collections.Counter(v.code for v in skipped)
    if codes:
        lines.append("Не піде:")
        lines += [f"  {queue.REASONS.get(c or '', c)}: {n}" for c, n in codes.most_common()]
    if dups:
        lines.append("Схоже на те, що вже є на Клубку:")
        lines += [f"  {v.post_id} ≈ {_short(v.dup.name)}" for v in dups[:5] if v.dup]
        if len(dups) > 5:
            lines.append(f"  … ще {len(dups) - 5}")
    gone = [v for v in skipped if v.code == "klubok_no_photos"]
    if gone:
        lines.append("Без фото (пост, назва):")
        for v in gone[:NO_PHOTOS_SHOW]:
            card = load_card(conn, v.item_id)
            title = (card.fields["title"].value or card.folder_name) if card else "?"
            lines.append(f"  {v.post_id} · {_short(title)}")
        if len(gone) > NO_PHOTOS_SHOW:
            lines.append(f"  … ще {len(gone) - NO_PHOTOS_SHOW}")
    if len(lines) > LINES_MAX:
        lines = [*lines[: LINES_MAX - 1], f"… ще {len(lines) - LINES_MAX + 1} рядків"]
    return lines
