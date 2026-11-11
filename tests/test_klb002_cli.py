"""KLB-002 (D11): `klubok-queue` — пробний прогін на копії бази; файл не змінюється."""

import hashlib
import sqlite3

from shafa import db
from shafa.__main__ import main
from shafa.db import Status
from test_klb002_queue import add


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(path):
    raw = sqlite3.connect(path)
    try:
        return raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()


def test_runs_on_v17_copy_and_leaves_file(tmp_path, root, monkeypatch, capsys):
    path = tmp_path / "копія.sqlite"
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA_VERSION", 17)
        conn = db.connect(path)
        for n in range(35):
            add(conn, root, 7301 + n, title=f"Черевики Ecco {n} 38 розміру")
        add(conn, root, 7400, status=Status.PUBLISHED, text="без ціни")
        conn.close()
    before = _digest(path)

    code = main(["klubok-queue", "--db", str(path), "--root", str(root), "--today", "2026-11-10"])

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert _digest(path) == before and _version(path) == 17
    assert len(out) <= 60
    assert out[0].startswith("Клубок вимкнено · пости > 7200 · сьогодні (10.11.2026) почато 0 з 10")
    assert "Кандидатів 36: у черзі 35 · не піде 1" in out[2]
    assert sum(line.startswith("  73") for line in out) == 30  # перші 30
    assert "  у пості немає ціни: 1" in out


def test_newer_base_refused_without_change(tmp_path, root, capsys):
    path = tmp_path / "нова.sqlite"
    db.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 19")
    raw.close()
    before = _digest(path)

    code = main(["klubok-queue", "--db", str(path), "--root", str(root)])

    assert code == 2
    assert "Невідома версія бази 19" in capsys.readouterr().out
    assert _digest(path) == before
