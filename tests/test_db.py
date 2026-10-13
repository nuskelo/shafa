import sqlite3

import pytest

from shafa import db


def test_schema_created_with_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 3
    assert tables(conn) == {"posts", "items", "photos", "blanks", "ai_requests", "edits"}
    assert {"archived_at", "purged_at"} <= columns(conn, "items")


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

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
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
        "published",
        "problem",
        "archived",
    ]
