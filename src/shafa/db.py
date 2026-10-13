"""База SQLite: пости, Картки, фото Карток."""

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 1


class Status(StrEnum):
    """Статуси Картки. Єдиний список у коді."""

    NEW = "new"  # папка знайдена, чекає обробки
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHED = "published"
    PROBLEM = "problem"
    ARCHIVED = "archived"


_STATUSES = ", ".join(f"'{s}'" for s in Status)

SCHEMA = f"""
CREATE TABLE posts (
    post_id  INTEGER PRIMARY KEY,
    date     TEXT NOT NULL,
    text     TEXT NOT NULL,
    source   TEXT NOT NULL CHECK (source IN ('export', 'bot')),
    added_at TEXT NOT NULL
);

CREATE TABLE items (
    id           INTEGER PRIMARY KEY,
    folder_name  TEXT NOT NULL UNIQUE,
    post_id      INTEGER,
    status       TEXT NOT NULL CHECK (status IN ({_STATUSES})),
    problem_code TEXT,
    problem_text TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE photos (
    item_id   INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    PRIMARY KEY (item_id, position)
);
"""


def now() -> str:
    """Поточний час UTC у форматі ISO, до секунд."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    """Відкрити базу; при першому відкритті створити схему."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        with conn:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"Невідома версія бази {version} (очікується {SCHEMA_VERSION})")
    return conn
