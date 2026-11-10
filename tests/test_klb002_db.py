"""KLB-002: схема 18 — таблиці Клубку і `sessions.site`."""

import hashlib
import sqlite3

import pytest

from shafa import db

KLUBOK_TABLES = {"klubok_ads", "klubok_listings", "klubok_listings_read"}


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _v17(path, monkeypatch):
    """База версії 17 з даними: Картки, фото, сесії, «Мої речі», вихідна черга."""
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA_VERSION", 17)
        conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO posts VALUES (7301, '2026-11-01T10:00:00', 'т', 'export', 'x')")
        conn.execute(
            "INSERT INTO items (id, folder_name, post_id, status, created_at, updated_at) "
            "VALUES (1, '7301', 7301, 'approved', 'a', 'b')"
        )
        conn.execute("INSERT INTO photos (item_id, position, file_name) VALUES (1, 1, '1.jpg')")
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, published, stop_code) "
            "VALUES (3, 's', 'e', 2, 'done')"
        )
        conn.execute("INSERT INTO sessions (id, started_at) VALUES (4, 's2')")
        conn.execute(
            "INSERT INTO my_listings (id, name, slug, sizes, url, colors, added_at) "
            "VALUES (5, 'Кеди', 'w', '[]', 'u', '[]', 'a')"
        )
        conn.execute(
            "INSERT INTO outbox (kind, at, post_id, data, created_at) "
            "VALUES ('post', 1, 7301, '{}', 'c')"
        )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
    conn.close()


def _dump(conn) -> dict:
    names = ("posts", "items", "photos", "my_listings", "outbox")
    return {n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n} ORDER BY 1")] for n in names}


def test_migration_17_to_18_keeps_data(tmp_path, monkeypatch):
    path = tmp_path / "v17.sqlite"
    _v17(path, monkeypatch)
    raw = sqlite3.connect(path)
    before = _dump(raw)
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 18
    assert KLUBOK_TABLES <= _tables(conn)
    assert _dump(conn) == before
    assert [tuple(r) for r in conn.execute("SELECT id, site, stop_code FROM sessions")] == [
        (3, "shafa", "done"), (4, "shafa", None),
    ]  # fmt: skip
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sessions (started_at, site) VALUES ('s', 'інше')")
    with pytest.raises(sqlite3.IntegrityError):  # стан — лише зі списку
        conn.execute(
            "INSERT INTO klubok_ads (item_id, state, created_at, updated_at) "
            "VALUES (1, 'інше', 'a', 'b')"
        )
    schema = [tuple(r) for r in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")]
    conn.close()

    conn = db.connect(path)  # повторне відкриття нічого не міняє
    assert [
        tuple(r) for r in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")
    ] == (schema)
    assert _dump(conn) == before
    conn.close()


def test_newer_base_refused(tmp_path):
    path = tmp_path / "v19.sqlite"
    db.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 19")
    raw.close()
    with pytest.raises(RuntimeError, match="Невідома версія бази 19"):
        db.connect(path)


def test_interrupted_step_leaves_17_and_next_start_migrates(tmp_path, monkeypatch):
    path = tmp_path / "v17.sqlite"
    _v17(path, monkeypatch)
    # обрив посеред кроку: таблиці Клубку вже створено, далі — помилка
    monkeypatch.setattr(db, "KLUBOK", db.KLUBOK + "CREATE TABLE klubok_ads (x);")
    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)

    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 17
    assert not KLUBOK_TABLES & _tables(raw)
    assert "site" not in {r[1] for r in raw.execute("PRAGMA table_info(sessions)")}
    raw.close()

    monkeypatch.undo()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    assert KLUBOK_TABLES <= _tables(conn)
    conn.close()


def test_migrate_in_memory_copy_leaves_file(tmp_path, monkeypatch):
    """`migrate` — той самий код для копії в пам'яті (klubok-queue): файл не змінюється."""
    path = tmp_path / "v17.sqlite"
    _v17(path, monkeypatch)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    memory = sqlite3.connect(":memory:")
    source.backup(memory)
    source.close()
    db.migrate(memory)

    assert memory.execute("PRAGMA user_version").fetchone()[0] == 18
    assert KLUBOK_TABLES <= _tables(memory)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    memory.close()
