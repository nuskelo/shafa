"""База SQLite: пости, Картки, фото Карток, бланки ШІ, ручні правки і вартість запитів."""

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 3


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

# Версія 2 (CORE-003): поля бланка ШІ і кожен запит до ШІ з токенами й ціною.
BLANKS = """
CREATE TABLE blanks (
    item_id INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    field   TEXT NOT NULL,
    value   TEXT,               -- JSON
    yellow  INTEGER NOT NULL CHECK (yellow IN (0, 1)),
    reason  TEXT,
    PRIMARY KEY (item_id, field)
);

CREATE TABLE ai_requests (
    id             INTEGER PRIMARY KEY,
    item_id        INTEGER REFERENCES items (id) ON DELETE SET NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_micro_usd INTEGER NOT NULL,  -- мільйонні частки долара
    error          TEXT,
    created_at     TEXT NOT NULL
);
"""

# Версія 3 (UI-001): ручні правки Продавчині поверх бланка ШІ й полів коду (новий бланк ШІ їх
# не стирає); дата відхилення і дата очищення архіву (Картка лишається прихованим записом, щоб
# повторний `scan` не створив її знову).
EDITS = """
CREATE TABLE edits (
    item_id   INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    field     TEXT NOT NULL,
    value     TEXT,               -- JSON
    edited_at TEXT NOT NULL,
    PRIMARY KEY (item_id, field)
);

ALTER TABLE items ADD COLUMN archived_at TEXT;
ALTER TABLE items ADD COLUMN purged_at TEXT;
"""

# Що виконати, щоб перейти з версії N на N + 1.
MIGRATIONS = {1: BLANKS, 2: EDITS}


def now() -> str:
    """Поточний час UTC у форматі ISO, до секунд."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    """Відкрити базу; при першому відкритті створити схему."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"Невідома версія бази {version} (очікується {SCHEMA_VERSION})")
    if version == 0:
        with conn:
            conn.executescript(SCHEMA + BLANKS + EDITS)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return conn
    while version < SCHEMA_VERSION:  # старіша база: таблиці додаються, дані лишаються
        with conn:
            conn.executescript(MIGRATIONS[version])
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
    return conn
