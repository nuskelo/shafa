"""OPS-001: перенесення пілоту — `transfer-pack` на Mac, `transfer-unpack` на її Windows.

Дві «машини» — дві тимчасові теки (тека даних, `.env`, Головна папка); справжніх місць тести не
бачать.
"""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from shafa import __main__ as cli
from shafa import config, db, transfer
from shafa.robot import lock

TOKEN = "123456:ABC-секретний-токен-бота"
CLAUDE = "sk-ant-секретний-ключ-claude"
SITE = "site-секретний-ключ"


def machine(monkeypatch, base: Path, env: dict[str, str] | None = None) -> tuple[Path, Path]:
    """«Увімкнути» комп'ютер: тека даних, `.env` і Головна папка в `base`."""
    data = base / "Telegram Desktop" / "дані"
    root = base / "Шафа"
    data.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SHAFA_DATA_DIR", str(data))
    monkeypatch.setattr(config, "ENV_FILE", None)
    monkeypatch.delenv("SHAFA_ROOT_DIR", raising=False)
    values = {"SHAFA_ROOT_DIR": str(root), **(env or {})}
    (data / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8"
    )
    return data, root


MAC_ENV = {
    "TELEGRAM_BOT_TOKEN": TOKEN,
    "ANTHROPIC_API_KEY": CLAUDE,
    "SITE_INTEGRATION_KEY": SITE,
    "TELEGRAM_CHANNEL_ID": "2394827824",
    "TELEGRAM_BOT_CHANNEL_ID": "100500",
    "SHAFA_DAILY_LIMIT": "30",
    "SHAFA_DAILY_OLD": "10",
}


def add_item(conn, folder: str, status: str, *, post: int | None = None, source="bot") -> None:
    now = db.now()
    if post is not None:
        conn.execute(
            "INSERT INTO posts (post_id, date, text, source, added_at) VALUES (?, ?, ?, ?, ?)",
            (post, now, "Текст поста", source, now),
        )
    conn.execute(
        "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (folder, post, status, now, now),
    )


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Mac Власника: база з Картками, фото бота в Головній, фото старого поста, секрети."""
    data, root = machine(monkeypatch, tmp_path / "mac", MAC_ENV)
    conn = db.connect(data / "shafa.sqlite")
    with conn:
        add_item(conn, "7001", "review", post=7001)
        add_item(conn, "7002-бот", "approved", post=7002)
        add_item(conn, "borh-55", "published")
        add_item(conn, "7003", "published", post=7003)
    conn.close()
    for name in ("7001", "7002-бот"):
        (root / name).mkdir()
        (root / name / "1.jpg").write_bytes(b"jpg-" + name.encode())
    (root / "Опубліковано" / "7003").mkdir(parents=True)
    (root / "Опубліковано" / "7003" / "1.jpg").write_bytes(b"pub")
    (data / "borh" / "55").mkdir(parents=True)
    (data / "borh" / "55" / "1.jpg").write_bytes(b"old")
    (data / "duplicates").mkdir()
    (data / "duplicates" / "9.jpg").write_bytes(b"dup")
    (data / "alerts-last.json").write_text('{"x": 1}', encoding="utf-8")
    (data / "report-last.json").write_text('{"day": "2026-10-27"}', encoding="utf-8")
    # те, що їхати не має
    (data / "robot-profile").mkdir()
    (data / "robot-profile" / "Cookies").write_bytes(b"cookie-" + SITE.encode())
    (data / "thumbs").mkdir()
    (data / "thumbs" / "1.jpg").write_bytes(b"t")
    (data / "robot-screens").mkdir()
    (data / "robot-screens" / "a.png").write_bytes(b"s")
    (data / "sync-cache").mkdir()
    (data / "sync-cache" / "c.json").write_text("{}", encoding="utf-8")
    (data / "robot-run.log").write_text("лог", encoding="utf-8")
    (data / "shafa.sqlite.до-оновлення").write_bytes(b"old-db")
    (data / "shafa.sqlite.bak-core013").write_bytes(b"bak")
    (data / "bot.json").write_text("{}", encoding="utf-8")
    (data / "ui.url").write_text("http://127.0.0.1:8765", encoding="utf-8")
    (root / "7001" / ".DS_Store").write_bytes(b"junk")
    return data, root


def packed(mac, tmp_path: Path) -> Path:
    out = tmp_path / "перенос" / "pilot.zip"
    transfer.pack(out)
    return out


def names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


# --- pack ---


def test_pack_refuses_while_app_runs(mac, tmp_path):
    data, _ = mac
    with lock.hold("ui", data / "ui.lock"), pytest.raises(transfer.TransferError, match="Вийти"):
        transfer.pack(tmp_path / "a.zip")
    assert not (tmp_path / "a.zip").exists()
    with lock.hold("bot", data / "bot.lock"), pytest.raises(transfer.TransferError, match="бот"):
        transfer.pack(tmp_path / "a.zip")
    with (
        lock.hold("robot", data / "robot.lock"),
        pytest.raises(transfer.TransferError, match="робот"),
    ):
        transfer.pack(tmp_path / "a.zip")


def test_pack_contents_and_exclusions(mac, tmp_path):
    out = packed(mac, tmp_path)
    got = names(out)
    assert {
        "manifest.json",
        "data/shafa.sqlite",
        "data/alerts-last.json",
        "data/report-last.json",
        "data/borh/55/1.jpg",
        "data/duplicates/9.jpg",
        "root/7001/1.jpg",
        "root/7002-бот/1.jpg",
        "root/Опубліковано/7003/1.jpg",
    } <= got
    for name in got:
        for banned in (".env", "robot-profile", "thumbs", "robot-screens", "sync-cache", ".lock"):
            assert banned not in name, name
        assert not name.endswith((".log", ".url", "bot.json", "до-оновлення", "bak-core013"))
        assert ".DS_Store" not in name
    assert not list(out.parent.glob("*.part"))  # тимчасове прибрано


def test_pack_has_no_secrets_anywhere(mac, tmp_path):
    out = packed(mac, tmp_path)
    with zipfile.ZipFile(out) as zf:
        everything = b"".join(zf.read(name) for name in zf.namelist() if not name.endswith("/"))
    for secret in (TOKEN, CLAUDE, SITE):
        assert secret.encode() not in everything
    manifest = json.loads(zipfile.ZipFile(out).read("manifest.json"))
    assert manifest["settings"] == {
        "SHAFA_DAILY_LIMIT": "30",
        "SHAFA_DAILY_OLD": "10",
        "TELEGRAM_BOT_CHANNEL_ID": "100500",
        "TELEGRAM_CHANNEL_ID": "2394827824",
    }


def test_pack_manifest_counts(mac, tmp_path):
    out = packed(mac, tmp_path)
    manifest = json.loads(zipfile.ZipFile(out).read("manifest.json"))
    assert manifest["schema"] == db.SCHEMA_VERSION
    assert manifest["statuses"] == {"approved": 1, "published": 2, "review": 1}
    assert manifest["files"]["count"] == 7  # 2 json + borh + duplicates + 3 фото Головної
    assert manifest["photo_dirs"] == 4  # 7001, 7002-бот, Опубліковано/7003, borh/55
    assert manifest["db_sha256"]


def test_pack_does_not_overwrite_and_leaves_pilot_alone(mac, tmp_path):
    data, root = mac
    out = tmp_path / "pilot.zip"
    out.write_bytes(b"x")
    with pytest.raises(transfer.TransferError, match="уже є"):
        transfer.pack(out)
    assert out.read_bytes() == b"x"
    before = (data / "shafa.sqlite").read_bytes()
    transfer.pack(tmp_path / "other.zip")
    assert (data / "shafa.sqlite").read_bytes() == before  # знімок, база не змінена
    assert (root / "7001" / "1.jpg").exists()


def test_pack_zip_inside_root_is_not_packed_into_itself(mac, tmp_path):
    _, root = mac
    out = root / "pilot.zip"
    transfer.pack(out)
    assert not any("pilot.zip" in name for name in names(out))


# --- unpack ---


def test_unpack_into_empty_machine(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    data, root = machine(monkeypatch, tmp_path / "win", {})
    (data / ".env").unlink()  # зовсім нова: ні бази, ні налаштувань
    monkeypatch.setenv("SHAFA_ROOT_DIR", str(root))
    result = transfer.unpack(out)
    assert result.statuses == {"approved": 1, "published": 2, "review": 1}
    assert result.missing == [] and result.side_by_side == [] and result.aside == []
    conn = db.connect(data / "shafa.sqlite")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4
    finally:
        conn.close()
    assert (root / "7002-бот" / "1.jpg").read_bytes() == "jpg-7002-бот".encode()
    assert (root / "Опубліковано" / "7003" / "1.jpg").read_bytes() == b"pub"
    assert (data / "borh" / "55" / "1.jpg").read_bytes() == b"old"
    assert (data / "duplicates" / "9.jpg").read_bytes() == b"dup"
    assert json.loads((data / "alerts-last.json").read_text(encoding="utf-8")) == {"x": 1}
    assert not list(data.glob("*" + transfer.TMP))
    assert "опубліковано 2" in result.text() and "два боти" in result.text()


def test_unpack_over_existing_db_renames_not_deletes(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    data, _ = machine(monkeypatch, tmp_path / "win", {})
    old = db.connect(data / "shafa.sqlite")
    with old:
        add_item(old, "999", "new", post=999)
    old.close()
    old_bytes = (data / "shafa.sqlite").read_bytes()
    (data / "alerts-last.json").write_text('{"old": true}', encoding="utf-8")
    result = transfer.unpack(out)
    aside = [p for p in data.iterdir() if p.name.startswith("shafa.sqlite" + transfer.ASIDE)]
    assert len(aside) == 1 and aside[0].read_bytes() == old_bytes
    kept = [p for p in data.iterdir() if p.name.startswith("alerts-last.json" + transfer.ASIDE)]
    assert len(kept) == 1 and json.loads(kept[0].read_text(encoding="utf-8")) == {"old": True}
    assert json.loads((data / "alerts-last.json").read_text(encoding="utf-8")) == {"x": 1}
    assert len(result.aside) == 2
    conn = db.connect(data / "shafa.sqlite")
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM items WHERE folder_name = '999'").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_unpack_same_folder_number_goes_next_to_it(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    data, root = machine(monkeypatch, tmp_path / "win", {})
    (root / "7001").mkdir()
    (root / "7001" / "її.jpg").write_bytes(b"hers")
    (root / "Опубліковано" / "7003").mkdir(parents=True)
    (root / "Опубліковано" / "7003" / "її.jpg").write_bytes(b"hers-pub")
    (data / "borh" / "55").mkdir(parents=True)
    result = transfer.unpack(out)
    assert (root / "7001" / "її.jpg").read_bytes() == b"hers"
    assert not (root / "7001" / "1.jpg").exists()  # не змішано
    assert (root / "7001-перенос" / "1.jpg").read_bytes() == b"jpg-7001"
    assert (root / "Опубліковано" / "7003-перенос" / "1.jpg").read_bytes() == b"pub"
    assert (data / "borh" / "55-перенос" / "1.jpg").read_bytes() == b"old"
    assert (root / "7002-бот" / "1.jpg").exists()  # без конфлікту — на своєму місці
    assert len(result.side_by_side) == 3 and "7001-перенос" in result.text()
    # другий раз — ще одна копія поруч, жодна не затерта
    shutil.rmtree(data / "borh")
    result = transfer.unpack(out)
    assert (root / "7001-перенос2" / "1.jpg").exists() and (root / "7001-перенос").exists()
    assert result.side_by_side


def test_unpack_env_keeps_secrets_and_her_root(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    her = {
        "TELEGRAM_BOT_TOKEN": "her-token",
        "ANTHROPIC_API_KEY": "her-claude",
        "SHAFA_DAILY_LIMIT": "10",
        "TELEGRAM_CHANNEL_ID": "2394827824",
    }
    data, root = machine(monkeypatch, tmp_path / "win", her)
    result = transfer.unpack(out)
    env = config.read_env_file(data / ".env")
    assert env["TELEGRAM_BOT_TOKEN"] == "her-token" and env["ANTHROPIC_API_KEY"] == "her-claude"
    assert "SITE_INTEGRATION_KEY" not in env
    assert env["SHAFA_ROOT_DIR"] == str(root)  # її, не з Mac
    assert env["SHAFA_DAILY_LIMIT"] == "30" and env["SHAFA_DAILY_OLD"] == "10"
    assert env["TELEGRAM_BOT_CHANNEL_ID"] == "100500"
    assert (data / ".env").stat().st_mode & 0o077 == 0
    changed = {key: (old, new) for key, old, new in result.changed}
    assert changed == {
        "SHAFA_DAILY_LIMIT": ("10", "30"),
        "SHAFA_DAILY_OLD": (None, "10"),
        "TELEGRAM_BOT_CHANNEL_ID": (None, "100500"),
    }
    assert "SHAFA_DAILY_LIMIT: 10 → 30" in result.text()
    assert "her-token" not in result.text()


def test_unpack_ignores_dangerous_manifest_settings(mac, tmp_path, monkeypatch):
    """Рукотворний manifest не може вписати секрет чи чужий шлях у її `.env`."""
    out = packed(mac, tmp_path)
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(forged, "w") as dst:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest["settings"] = {
                    "ANTHROPIC_API_KEY": "evil",
                    "TELEGRAM_BOT_TOKEN": "evil",
                    "SHAFA_ROOT_DIR": "C:/evil",
                    "SHAFA_API_KEY": "evil",
                    "SHAFA_X": "a\nANTHROPIC_API_KEY=evil",
                    "PATH": "evil",
                    "SHAFA_DAILY_LIMIT": "5",
                }
                payload = json.dumps(manifest).encode()
            dst.writestr(item, payload)
    data, root = machine(monkeypatch, tmp_path / "win", {"ANTHROPIC_API_KEY": "mine"})
    transfer.unpack(forged)
    env = config.read_env_file(data / ".env")
    assert env == {
        "ANTHROPIC_API_KEY": "mine",
        "SHAFA_ROOT_DIR": str(root),
        "SHAFA_DAILY_LIMIT": "5",
    }


def test_unpack_refuses_newer_schema(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(newer, "w") as dst:
        for item in src.infolist():
            payload = src.read(item.filename)
            if item.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest["schema"] = db.SCHEMA_VERSION + 1
                payload = json.dumps(manifest).encode()
            dst.writestr(item, payload)
    data, root = machine(monkeypatch, tmp_path / "win", {})
    with pytest.raises(transfer.TransferError, match="онови програму"):
        transfer.unpack(newer)
    assert not (data / "shafa.sqlite").exists() and not any(root.iterdir())


def test_unpack_refuses_while_app_runs(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    data, root = machine(monkeypatch, tmp_path / "win", {})
    with lock.hold("ui", data / "ui.lock"), pytest.raises(transfer.TransferError, match="Вийти"):
        transfer.unpack(out)
    assert not (data / "shafa.sqlite").exists() and not any(root.iterdir())


def test_unpack_rejects_corrupt_or_foreign_archives(mac, tmp_path, monkeypatch):
    out = packed(mac, tmp_path)
    data, root = machine(monkeypatch, tmp_path / "win", {})
    garbage = tmp_path / "x.zip"
    garbage.write_bytes("не zip".encode())
    with pytest.raises(transfer.TransferError, match="не zip"):
        transfer.unpack(garbage)
    slip = tmp_path / "slip.zip"
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(slip, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("root/../../evil.txt", b"x")
    with pytest.raises(transfer.TransferError, match="підозрілий"):
        transfer.unpack(slip)
    assert not (tmp_path / "evil.txt").exists() and not (data / "shafa.sqlite").exists()
    assert not any(root.iterdir())


def test_unpack_rolls_back_what_it_created(mac, tmp_path, monkeypatch):
    """Збій у розпаковці фото: нових тек не лишається, стара база не чіпана."""
    out = packed(mac, tmp_path)
    data, root = machine(monkeypatch, tmp_path / "win", {})
    old = db.connect(data / "shafa.sqlite")
    old.close()
    before = (data / "shafa.sqlite").read_bytes()
    calls = {"n": 0}
    real = transfer._extract

    def flaky(zf, member, target):
        calls["n"] += 1
        if calls["n"] == 4:  # база + кілька файлів — і збій
            raise OSError("диск повний")
        real(zf, member, target)

    monkeypatch.setattr(transfer, "_extract", flaky)
    with pytest.raises(OSError, match="диск"):
        transfer.unpack(out)
    assert (data / "shafa.sqlite").read_bytes() == before
    assert not any(root.iterdir()) and not (data / "borh").exists()
    assert not list(data.glob("*" + transfer.TMP))


def test_unpack_lists_cards_without_photo_folder(mac, tmp_path, monkeypatch):
    _, root = mac
    shutil.rmtree(root / "7002-бот")
    out = packed(mac, tmp_path)
    machine(monkeypatch, tmp_path / "win", {})
    result = transfer.unpack(out)
    assert result.missing == ["7002-бот"]
    assert "7002-бот" in result.text()


def test_cyrillic_and_spaces_in_paths(mac, tmp_path, monkeypatch):
    _, root = mac
    (root / "Фото з телефону Влада").mkdir()
    (root / "Фото з телефону Влада" / "Знімок 1.jpg").write_bytes(b"x")
    out = tmp_path / "Мій архів" / "пілот 1.zip"
    transfer.pack(out)
    assert "root/Фото з телефону Влада/Знімок 1.jpg" in names(out)
    _, root2 = machine(monkeypatch, tmp_path / "Нова Шафа", {})
    transfer.unpack(out)
    assert (root2 / "Фото з телефону Влада" / "Знімок 1.jpg").read_bytes() == b"x"


# --- команди ---


def test_cli_roundtrip_and_error_code(mac, tmp_path, monkeypatch, capsys):
    out = tmp_path / "cli.zip"
    assert cli.main(["transfer-pack", "--out", str(out)]) == 0
    assert "Готово" in capsys.readouterr().out
    assert cli.main(["transfer-pack", "--out", str(out)]) == 2  # вже є
    assert "уже є" in capsys.readouterr().err
    machine(monkeypatch, tmp_path / "win", {})
    assert cli.main(["transfer-unpack", "--zip", str(out)]) == 0
    assert "Перенесено" in capsys.readouterr().out
