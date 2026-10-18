"""База SQLite: пости, Картки, фото Карток, бланки ШІ, ручні правки і вартість запитів."""

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 13


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


# Версія 5 (UI-002): «Це інший товар» (двигун більше не перевіряє Картку на схожі оголошення),
# посилання на схоже оголошення, код причини зупинки сесії для екрана, коли почато поточний товар.
CONTROL = """
ALTER TABLE items ADD COLUMN duplicate_ok_at TEXT;
ALTER TABLE items ADD COLUMN duplicate_url TEXT;
ALTER TABLE sessions ADD COLUMN stop_code TEXT;
ALTER TABLE sessions ADD COLUMN item_started_at TEXT;
"""

# Версія 6 (CORE-004): ШІ пакетом. Пакет і рядок на кожну його Картку зі станом (рішення D3 — без
# нового статусу Картки): `pending` — чекає результату (вдруге не відправляється), `done` — бланк,
# `error` — помилка запиту, `returned` — не оброблено (expired/canceled/пакет не створено), Картка
# знову в черзі. `estimate_micro_usd` — оцінка до відправки (запобіжник грошей, D5).
# Стан пакета: `sending` — запис перед відправкою, `uncertain` — відповідь на створення не прийшла
# (пакет міг створитися), `in_progress`, `ended` — результати забрано, `failed` — пакета немає.
# У `ai_requests` — токени кешу і чи це пакет (вартість уже з урахуванням знижок).
BATCHES = """
CREATE TABLE ai_batches (
    id           INTEGER PRIMARY KEY,
    anthropic_id TEXT UNIQUE,
    status       TEXT NOT NULL
                 CHECK (status IN ('sending', 'uncertain', 'in_progress', 'ended', 'failed')),
    note         TEXT,
    created_at   TEXT NOT NULL,
    ended_at     TEXT
);

CREATE TABLE ai_batch_items (
    batch_id           INTEGER NOT NULL REFERENCES ai_batches (id),
    item_id            INTEGER NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    estimate_micro_usd INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('pending', 'done', 'error', 'returned')),
    PRIMARY KEY (batch_id, item_id)
);

ALTER TABLE ai_requests ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_requests ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_requests ADD COLUMN batch_id INTEGER REFERENCES ai_batches (id);
"""

# Версія 7 (BOT-001): бот. `bot_updates` — оброблені оновлення Telegram (повторна доставка нічого
# не робить). `bot_drafts` — чернетка поста з одного чату: `collecting` (збираємо, 2 хв) →
# `preview` (прев'ю з кнопками, дійсне 24 год) → `publishing` (запис ДО відправки в канал) →
# `published`; `uncertain` — відповіді каналу не було (не повторюємо), `failed` — не вийшло, нічого
# не опубліковано, `canceled`, `expired`. `bot_draft_parts` — повідомлення чернетки: текст і фото
# (`photo` — «як фото», `jpeg`/`png` — файлом). `bot_status` — стан процесу бота для екрана.
# `comments` — альбом усіх фото в коментарях під постом (≥ 2 фото): `waiting` — чекаємо пересланий
# пост у групі обговорення, `late` — не прийшов за 2 хв (їй і Власнику сказано), `sending` — запис
# ДО відправки альбому, `done`, `failed`. `editing` — вона виправляє текст («Редагувати»): наступний
# її текст замінює текст чернетки. `bot_summaries` — повідомлення «Чернеток: N» з кнопками
# «Опублікувати всі / Скасувати всі» в її чаті.
BOT = """
CREATE TABLE bot_updates (
    update_id  INTEGER PRIMARY KEY,
    handled_at TEXT NOT NULL
);

CREATE TABLE bot_drafts (
    id                 INTEGER PRIMARY KEY,
    chat_id            INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('collecting', 'preview', 'editing',
                       'publishing', 'published', 'uncertain', 'failed', 'canceled', 'expired')),
    asked              INTEGER NOT NULL DEFAULT 0,
    preview_message_id INTEGER,
    preview_at         TEXT,
    edit_message_id    INTEGER,  -- «Надішли виправлений текст» з кнопкою «Скасувати редагування»
    post_id            INTEGER,
    folder_name        TEXT,
    note               TEXT,
    published_at       TEXT,
    comments           TEXT CHECK (comments IN ('waiting', 'late', 'sending', 'done', 'failed')),
    created_at         TEXT NOT NULL,
    last_message_at    TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE bot_draft_parts (
    draft_id   INTEGER NOT NULL REFERENCES bot_drafts (id),
    message_id INTEGER NOT NULL,
    text       TEXT,
    file_id    TEXT,
    kind       TEXT CHECK (kind IN ('photo', 'jpeg', 'png')),
    media_group TEXT,  -- альбом Telegram: частини одного альбому — завжди одна чернетка
    PRIMARY KEY (draft_id, message_id)
);

CREATE TABLE bot_summaries (
    chat_id    INTEGER PRIMARY KEY,
    message_id INTEGER,
    shown      INTEGER NOT NULL,           -- скільки чернеток написано в повідомленні
    running    INTEGER NOT NULL DEFAULT 0  -- «Опублікувати всі» триває: вдруге не запуститься
);

CREATE TABLE bot_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    started_at   TEXT,
    heartbeat_at TEXT,
    error        TEXT
);
"""

# Що виконати, щоб перейти з версії N на N + 1 (текст SQL або функція, що сама ставить версію).
# Версія 8 (CORE-006): позиція старої Картки у списку `import-old` (менше — раніше).
QUEUE = """
ALTER TABLE items ADD COLUMN queue_pos INTEGER;
"""

# Версія 9 (SITE-001): черга на сайт — пост, опублікований ботом, і скільки фото вже на сайті.
# `state`: `fields` — поля ще не на сайті, `photos` — поля є, фото з `next_photo`, `done`, `failed`
# (400/409 — не повторюємо). `fields` — JSON полів (рахуються один раз, ШІ — теж один раз).
# `next_at` — не раніше (обрив, 5xx, 429). `site_cache` — список підкатегорій на добу.
SITE = """
CREATE TABLE site_jobs (
    post_id     INTEGER PRIMARY KEY,  -- номер поста в каналі
    folder_name TEXT NOT NULL,
    posted_at   TEXT NOT NULL,        -- дата поста в каналі з поясом
    text        TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('fields', 'photos', 'done', 'failed')),
    fields      TEXT,
    next_photo  INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE site_cache (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

# Версія 10 (CORE-007): знімок її товарів з кабінету «Мої речі» для перевірки дублів —
# перечитується раз на N днів, опубліковане роботом дописується одразу. `my_listings_read` — один
# рядок: коли кабінет прочитано (немає рядка — знімок застарів, «Перечитати зараз»).
# `items.duplicate_name` — назва схожого оголошення для картки Проблеми (фото — у теці даних).
LISTINGS = """
CREATE TABLE my_listings (
    id       INTEGER PRIMARY KEY,  -- id товару на Шафі
    name     TEXT NOT NULL,
    brand    TEXT,
    slug     TEXT NOT NULL,        -- розділ Шафи
    sizes    TEXT NOT NULL,        -- JSON
    url      TEXT NOT NULL,
    colors   TEXT NOT NULL,        -- JSON
    price    INTEGER,
    photo    TEXT,                 -- адреса фото на Шафі
    added_at TEXT NOT NULL
);

CREATE TABLE my_listings_read (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    read_at TEXT NOT NULL
);

ALTER TABLE items ADD COLUMN duplicate_name TEXT;
"""

# Версія 11 (CORE-008): за якою версією правил (`rules.RULES_VERSION`) заповнено бланк (NULL —
# до CORE-007: сірий ярлик «заповнено старими правилами»); `refill_at` — «Перезаповнити»:
# Картка знову `new` і йде в пакет без чекання 7 днів, правки (`edits`) лишаються.
REFILL = """
ALTER TABLE items ADD COLUMN rules_version INTEGER;
ALTER TABLE items ADD COLUMN refill_at TEXT;
"""

# Версія 12 (SITE-002): на сайт — через N днів після поста. Нові стани черги: `waiting` (чекає
# `not_before`, UTC), `cancelled` (пост видалено з каналу до виходу — на сайт не йде). Стан у CHECK —
# тому `site_jobs` перебудовується (дані ті самі). `post_checks` — чи пост ще є в каналі:
# `checked_at` — остання спроба, `unknown_since` — з якої пори поспіль «невідомо», `told_at` — Власнику
# сказано (3 доби «невідомо»), `gone_at` — пост зник (= продано), `sold_at` — сайт прийняв «Продано»;
# `old` — пара зі списку сайту (старі ~2093), не з нашої черги.
SITE_WAIT = """
CREATE TABLE site_jobs_v12 (
    post_id     INTEGER PRIMARY KEY,
    folder_name TEXT NOT NULL,
    posted_at   TEXT NOT NULL,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL
                CHECK (state IN ('waiting', 'fields', 'photos', 'done', 'failed', 'cancelled')),
    fields      TEXT,
    next_photo  INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    not_before  TEXT
);
INSERT INTO site_jobs_v12 (post_id, folder_name, posted_at, text, state, fields, next_photo,
                           attempts, next_at, error, created_at, updated_at)
SELECT post_id, folder_name, posted_at, text, state, fields, next_photo, attempts, next_at, error,
       created_at, updated_at
FROM site_jobs;
DROP TABLE site_jobs;
ALTER TABLE site_jobs_v12 RENAME TO site_jobs;

CREATE TABLE post_checks (
    post_id       INTEGER PRIMARY KEY,
    old           INTEGER NOT NULL DEFAULT 0,
    checked_at    TEXT,
    unknown_since TEXT,
    told_at       TEXT,
    gone_at       TEXT,
    sold_at       TEXT
);
"""


def _site_wait(conn: sqlite3.Connection) -> None:
    """Перебудова `site_jobs` одним записом: або вся, або ніяк."""
    conn.commit()
    try:
        conn.executescript("BEGIN;" + SITE_WAIT + "PRAGMA user_version = 12; COMMIT;")
    except sqlite3.Error:
        conn.rollback()
        raise


# Версія 13 (SITE-003): «Продано» на сайті. `hold` — «Продано» ще не слати: `night` — зник цієї
# ночі, чекає кінця ночі (запобіжник рахує), `fuse` — запобіжник спрацював, чекає Власника
# («Надіслати відкладені Продано»); `off_site_at` — пари немає в списку сайту (продано в адмінці) —
# не перевіряємо; `sold_note` — сайт «Продано» не прийняв (пари немає — 404 тощо), більше не шлемо.
# Зниклі до SITE-003 і ще не позначені — `night`: наступна ніч перевірить їх знову, через запобіжник.
SOLD = """
ALTER TABLE post_checks ADD COLUMN hold TEXT CHECK (hold IN ('night', 'fuse'));
ALTER TABLE post_checks ADD COLUMN off_site_at TEXT;
ALTER TABLE post_checks ADD COLUMN sold_note TEXT;
UPDATE post_checks SET hold = 'night' WHERE gone_at IS NOT NULL AND sold_at IS NULL;
"""

MIGRATIONS = {
    1: BLANKS, 2: EDITS, 3: _rebuild_items, 4: CONTROL, 5: BATCHES, 6: BOT, 7: QUEUE, 8: SITE,
    9: LISTINGS, 10: REFILL, 11: _site_wait, 12: SOLD,
}  # fmt: skip


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
