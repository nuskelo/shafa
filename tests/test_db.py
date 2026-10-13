import sqlite3

import pytest

from shafa import db


def test_schema_created_with_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 5
    assert tables(conn) == {
        "posts", "items", "photos", "blanks", "ai_requests", "edits", "sessions"
    }  # fmt: skip
    assert {"archived_at", "purged_at", "shafa_id", "shafa_url", "publish_started_at",
            "published_at", "session_id", "duplicate_ok_at", "duplicate_url"} <= columns(
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
