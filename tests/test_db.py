import sqlite3

import pytest

from shafa import db

# BOT-015 (версія 16): прибрати додане, щоб база стала старшою версією
QUEUE_DROP = (
    "DROP TABLE channel_posts; ALTER TABLE bot_draft_parts DROP COLUMN origin_post; "
    "ALTER TABLE bot_draft_parts DROP COLUMN origin_date;"
)

# CORE-007 (версія 10): прибрати, щоб база стала старшою версією
LISTINGS_DROP = (
    "ALTER TABLE items DROP COLUMN rules_version; ALTER TABLE items DROP COLUMN refill_at; "
    "DROP TABLE my_listings; DROP TABLE my_listings_read; "
    "ALTER TABLE items DROP COLUMN duplicate_name; DROP TABLE post_checks; DROP TABLE ai_answers;"
)  # CORE-008 (версія 11): колонки rules_version і refill_at; SITE-002 (12): post_checks


def test_schema_created_with_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 16
    assert tables(conn) == {
        "posts", "items", "photos", "blanks", "ai_requests", "edits", "sessions", "ai_batches",
        "ai_batch_items", "bot_updates", "bot_drafts", "bot_draft_parts", "bot_status",
        "bot_summaries", "site_jobs", "site_cache", "my_listings", "my_listings_read",
        "post_checks", "ai_answers", "channel_posts",
    }  # fmt: skip
    assert {"cache_write_tokens", "cache_read_tokens", "batch_id"} <= columns(conn, "ai_requests")
    assert {"archived_at", "purged_at", "shafa_id", "shafa_url", "publish_started_at",
            "published_at", "session_id", "duplicate_ok_at", "duplicate_url",
            "duplicate_name"} <= columns(
        conn, "items"
    )  # fmt: skip
    assert {"stop_code", "item_started_at", "command"} <= columns(conn, "sessions")


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_version_1_migrated_and_data_kept(tmp_path):
    path = tmp_path / "база.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA)
    raw.execute("INSERT INTO posts VALUES (1, '2025-01-01T00:00:00', 'текст', 'export', 'x')")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert {"blanks", "ai_requests", "edits"} <= tables(conn)
    assert conn.execute("SELECT text FROM posts").fetchone()[0] == "текст"
    conn.close()


def test_version_2_migrated_and_blank_kept(tmp_path):
    path = tmp_path / "база.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA + db.BLANKS)
    raw.execute(
        "INSERT INTO items (folder_name, status, created_at, updated_at) "
        "VALUES ('1342', 'review', 'x', 'x')"
    )
    raw.execute("INSERT INTO blanks VALUES (1, 'title', '\"Кеди\"', 0, NULL)")
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "edits" in tables(conn)
    row = conn.execute("SELECT status, archived_at, purged_at FROM items").fetchone()
    assert tuple(row) == ("review", None, None)
    assert conn.execute("SELECT value FROM blanks").fetchone()[0] == '"Кеди"'
    conn.close()


def test_reopen_keeps_data(tmp_path):
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO posts VALUES (1, '2025-01-01T00:00:00', 'текст', 'export', 'x')")
    conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT text FROM posts").fetchone()[0] == "текст"
    conn.close()


def test_unknown_version_refused(tmp_path):
    path = tmp_path / "база.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(RuntimeError, match="99"):
        db.connect(path)


def test_status_outside_enum_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO items (folder_name, status, created_at, updated_at) "
            "VALUES ('1', 'draft', 'x', 'x')"
        )


def test_statuses():
    assert [s.value for s in db.Status] == [
        "new",
        "review",
        "approved",
        "publishing",
        "published",
        "problem",
        "archived",
    ]


def test_version_3_migrated_keeps_photos_blanks_and_edits(tmp_path):
    """Перебудова `items` (новий CHECK) не видаляє каскадом фото, бланки й правки."""
    path = tmp_path / "база.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = ON")
    old_statuses = "'new', 'review', 'approved', 'published', 'problem', 'archived'"
    raw.executescript(db.SCHEMA.replace(db._STATUSES, old_statuses) + db.BLANKS + db.EDITS)
    raw.execute(
        "INSERT INTO items (folder_name, post_id, status, created_at, updated_at, archived_at) "
        "VALUES ('1342', 1342, 'approved', 'x', 'x', NULL)"
    )
    raw.execute("INSERT INTO photos VALUES (1, 1, 'IMG_1.JPG')")
    raw.execute("INSERT INTO blanks VALUES (1, 'title', '\"Кеди\"', 0, NULL)")
    raw.execute("INSERT INTO edits VALUES (1, 'title', '\"Кеди Ecco\"', 'x')")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    with pytest.raises(sqlite3.IntegrityError):  # старий CHECK не знав нового статусу
        raw.execute("UPDATE items SET status = 'publishing'")
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM blanks").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM edits").fetchone()[0] == 1
    assert tuple(conn.execute("SELECT folder_name, post_id, status FROM items").fetchone()) == (
        "1342", 1342, "approved"
    )  # fmt: skip
    with conn:
        conn.execute("UPDATE items SET status = 'publishing'")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with conn:  # зв'язки працюють і після перебудови: видалення Картки прибирає її фото
        conn.execute("DELETE FROM items")
    assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 0
    conn.close()


def test_version_5_migrated_keeps_cost_records(tmp_path):
    """Схема 6 (CORE-004): старі записи вартості лишаються, токени кешу — 0; `items` не
    перебудовується (D3 — без нового статусу)."""
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES ('1342', 1342, 'review', 'x', 'x')"
        )
        conn.execute(
            "INSERT INTO ai_requests (item_id, model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES (1, 'm', 9000, 500, 23000, 'x')"
        )
        conn.executescript(
            "DROP TABLE bot_updates; DROP TABLE bot_draft_parts; DROP TABLE bot_drafts;"
            "DROP TABLE bot_status; DROP TABLE bot_summaries;"
            "DROP TABLE ai_batch_items; DROP TABLE ai_batches;"
            "ALTER TABLE ai_requests DROP COLUMN batch_id;"
            "ALTER TABLE ai_requests DROP COLUMN cache_write_tokens;"
            "ALTER TABLE ai_requests DROP COLUMN cache_read_tokens;"
            "ALTER TABLE items DROP COLUMN queue_pos;"
            "DROP TABLE site_jobs; DROP TABLE site_cache;"
        )
        conn.executescript(LISTINGS_DROP)
        conn.executescript("DROP TABLE channel_posts;")
        conn.execute("PRAGMA user_version = 5")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone()[0]
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT * FROM ai_requests").fetchone()
    assert (row["cost_micro_usd"], row["cache_write_tokens"], row["cache_read_tokens"]) == (
        23000, 0, 0
    )  # fmt: skip
    assert row["batch_id"] is None
    assert (
        conn.execute("SELECT sql FROM sqlite_master WHERE name = 'items'").fetchone()[0]
        == (
            sql[:-1]
            + ", queue_pos INTEGER, duplicate_name TEXT, rules_version INTEGER, refill_at TEXT)"  # лише нові колонки, без перебудови
        )
    )
    with conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ai_batches (status, created_at) VALUES ('waiting', 'x')")
    conn.close()


def test_version_6_migrated_adds_bot_tables(tmp_path):
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO posts VALUES (1342, '2025-01-01T10:00:00', 'Кеди', 'export', 'x')"
        )
        conn.executescript(
            "DROP TABLE bot_updates; DROP TABLE bot_draft_parts; DROP TABLE bot_drafts;"
            "DROP TABLE bot_status; DROP TABLE bot_summaries;"
            "ALTER TABLE items DROP COLUMN queue_pos;"
            "DROP TABLE site_jobs; DROP TABLE site_cache;"
        )
        conn.executescript(LISTINGS_DROP)
        conn.executescript("DROP TABLE channel_posts;")
        conn.execute("PRAGMA user_version = 6")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert {
        "bot_updates",
        "bot_drafts",
        "bot_draft_parts",
        "bot_status",
        "bot_summaries",
    } <= tables(conn)
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
    with conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bot_drafts (chat_id, state, created_at, last_message_at, updated_at) "
            "VALUES (1, 'sent', 'x', 'x', 'x')"
        )
    conn.close()


def test_version_7_migrated_adds_queue_pos_and_keeps_cards(tmp_path):
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES ('borh-5', 5, 'new', 'x', 'x')"
        )
        conn.execute("ALTER TABLE items DROP COLUMN queue_pos")
        conn.executescript("DROP TABLE site_jobs; DROP TABLE site_cache;")
        conn.executescript(LISTINGS_DROP)
        conn.executescript(QUEUE_DROP)
        conn.execute("PRAGMA user_version = 7")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT folder_name, queue_pos FROM items").fetchone()
    assert tuple(row) == ("borh-5", None)
    conn.close()


def test_version_8_migrated_adds_site_queue_and_keeps_posts(tmp_path):
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO posts (post_id, date, text, source, added_at) VALUES (5, 'x', 't', 'bot', 'x')"
        )
        conn.executescript("DROP TABLE site_jobs; DROP TABLE site_cache;")
        conn.executescript(LISTINGS_DROP)
        conn.executescript(QUEUE_DROP)
        conn.execute("PRAGMA user_version = 8")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT post_id FROM posts").fetchone()[0] == 5
    conn.close()


def test_version_9_migrated_adds_listings_snapshot(tmp_path):
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES ('7', 7, 'new', 'x', 'x')"
        )
        conn.executescript(LISTINGS_DROP)
        conn.executescript(QUEUE_DROP)
        conn.execute("PRAGMA user_version = 9")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM my_listings").fetchone()[0] == 0
    assert tuple(conn.execute("SELECT folder_name, duplicate_name FROM items").fetchone()) == (
        "7",
        None,
    )
    conn.close()


def test_version_11_migrated_site_queue_rebuilt_with_waiting_and_rows_kept(tmp_path):
    """Схема 12 (SITE-002): `site_jobs` зі старим CHECK перебудовується — рядки ті самі,
    нові стани `waiting`/`cancelled` і `not_before`; `post_checks` з'являється."""
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.executescript(
            "DROP TABLE site_jobs; DROP TABLE post_checks; DROP TABLE ai_answers;"
            + QUEUE_DROP
            + "ALTER TABLE my_listings DROP COLUMN state; ALTER TABLE my_listings DROP COLUMN reason;"
            + db.SITE.split("CREATE TABLE site_cache")[0]
        )
        conn.execute(
            "INSERT INTO site_jobs (post_id, folder_name, posted_at, text, state, fields, "
            "next_photo, created_at, updated_at) VALUES (9, '9', 'p', 'т', 'done', '{}', 3, 'c', 'u')"
        )
        conn.execute("PRAGMA user_version = 11")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT * FROM site_jobs").fetchone()
    assert (row["post_id"], row["state"], row["next_photo"], row["text"], row["not_before"]) == (
        9, "done", 3, "т", None,
    )  # fmt: skip
    with conn:
        conn.execute("UPDATE site_jobs SET state = 'waiting'")
        conn.execute("UPDATE site_jobs SET state = 'cancelled'")
    assert conn.execute("SELECT count(*) FROM post_checks").fetchone()[0] == 0
    conn.close()


def test_version_12_migrated_sold_columns_and_unsent_gone_rechecked(tmp_path):
    """Схема 13 (SITE-003): `hold`, `off_site_at`, `sold_note`; зниклі без «Продано» — `night`."""
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.executescript(
            "ALTER TABLE post_checks DROP COLUMN hold; ALTER TABLE post_checks DROP COLUMN "
            "off_site_at; ALTER TABLE post_checks DROP COLUMN sold_note; DROP TABLE ai_answers; "
            + QUEUE_DROP
            + "ALTER TABLE my_listings DROP COLUMN state; ALTER TABLE my_listings DROP COLUMN reason;"
        )
        conn.execute(
            "INSERT INTO post_checks (post_id, gone_at, sold_at) VALUES (1, 'g', NULL), "
            "(2, 'g', 's'), (3, NULL, NULL)"
        )
        conn.execute("PRAGMA user_version = 12")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    rows = conn.execute("SELECT post_id, hold, off_site_at, sold_note FROM post_checks ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, "night", None, None), (2, None, None, None),
                                        (3, None, None, None)]  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE post_checks SET hold = 'інше'")
    conn.close()


def test_version_13_migrated_adds_ai_answers(tmp_path):
    """Схема 14 (CORE-009): відповідь ШІ як є — для перерахунку правилами без ШІ."""
    path = tmp_path / "база.sqlite"
    conn = db.connect(path)
    with conn:
        conn.execute("DROP TABLE ai_answers")
        conn.executescript(QUEUE_DROP)
        conn.executescript(
            "ALTER TABLE my_listings DROP COLUMN state; ALTER TABLE my_listings DROP COLUMN reason;"
        )
        conn.execute("PRAGMA user_version = 13")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert {"answer", "prompt_version", "rebuilt", "needs_ai"} <= columns(conn, "ai_answers")
    conn.close()
