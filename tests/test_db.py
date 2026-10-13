import sqlite3

import pytest

from shafa import db


def test_schema_created_with_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 2
    assert tables(conn) == {"posts", "items", "photos", "blanks", "ai_requests"}


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_version_1_migrated_and_data_kept(tmp_path):
    path = tmp_path / "база.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA)
    raw.execute("INSERT INTO posts VALUES (1, '2025-01-01T00:00:00', 'текст', 'export', 'x')")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert {"blanks", "ai_requests"} <= tables(conn)
    assert conn.execute("SELECT text FROM posts").fetchone()[0] == "текст"
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
