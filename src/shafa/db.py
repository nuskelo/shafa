"""База SQLite: пости, Картки, фото Карток, бланки ШІ, ручні правки і вартість запитів."""

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 4


class Status(StrEnum):
    """Статуси Картки. Єдиний список у коді."""

    NEW = "new"  # папка знайдена, чекає обробки
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHING = "publishing"  # запис перед кліком «Додати річ»; далі published або problem
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

# Версія 4 (ROBOT-002): статус `publishing` (SQLite не міняє CHECK — таблиця `items`
# перебудовується), поля публікації й сесії робота. `command` — Пауза/Стоп ззовні (UI-002).
ITEMS_V4 = f"""
CREATE TABLE items_v4 (
    id                 INTEGER PRIMARY KEY,
    folder_name        TEXT NOT NULL UNIQUE,
    post_id            INTEGER,
    status             TEXT NOT NULL CHECK (status IN ({_STATUSES})),
    problem_code       TEXT,
    problem_text       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    archived_at        TEXT,
    purged_at          TEXT,
    shafa_id           INTEGER,
    shafa_url          TEXT,
    publish_started_at TEXT,
    published_at       TEXT,
    session_id         INTEGER REFERENCES sessions (id)
);
INSERT INTO items_v4 (id, folder_name, post_id, status, problem_code, problem_text, created_at,
                      updated_at, archived_at, purged_at)
SELECT id, folder_name, post_id, status, problem_code, problem_text, created_at, updated_at,
       archived_at, purged_at
FROM items;
DROP TABLE items;
ALTER TABLE items_v4 RENAME TO items;
"""

SESSIONS = """
CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    published   INTEGER NOT NULL DEFAULT 0,
    problems    INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    command     TEXT CHECK (command IN ('pause', 'stop'))
);
"""


def _rebuild_items(conn: sqlite3.Connection) -> None:
    """Перебудова `items` без втрати фото, бланків і правок (вони посилаються на items)."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # інакше DROP TABLE items видалить фото каскадом
    try:
        conn.executescript("BEGIN;" + SESSIONS + ITEMS_V4 + "PRAGMA user_version = 4; COMMIT;")
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Після перебудови бази порушено зв'язки таблиць")


# Що виконати, щоб перейти з версії N на N + 1 (текст SQL або функція, що сама ставить версію).
MIGRATIONS = {1: BLANKS, 2: EDITS, 3: _rebuild_items}


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
    if version == 0:  # нова база: схема версії 3, далі — ті самі переходи, що й для старих
        with conn:
            conn.executescript(SCHEMA + BLANKS + EDITS)
            conn.execute("PRAGMA user_version = 3")
        version = 3
    while version < SCHEMA_VERSION:  # старіша база: таблиці додаються, дані лишаються
        step = MIGRATIONS[version]
        if callable(step):
            step(conn)
            version += 1
            continue
        with conn:
            conn.executescript(step)
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
    return conn
