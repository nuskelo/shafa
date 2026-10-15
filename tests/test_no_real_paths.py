"""FIX-001: тести й процеси, які вони запускають, ніколи не бачать справжніх `.env` і теки даних.

Інцидент 15.10.2026: процеси, запущені тестами WIN-001, виконали `migrate_env` зі справжнім
`.env` у корені проєкту й перенесли його в тимчасову теку pytest (потім стерту).
"""

import os
import subprocess
import sys

import pytest

from shafa import config

SECRET = "sk-тестовий-секрет-FIX001"


def child(code: str, env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ) if env is None else env
    env["PYTHONIOENCODING"] = "utf-8"  # шляхи з кирилицею; консоль Windows — cp1252
    return subprocess.run(
        [sys.executable, "-c", code], env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )  # fmt: skip


def test_tests_never_point_to_real_places():
    assert config.data_dir() != config.default_data_dir()
    assert config.legacy_env_file() != config._REAL_LEGACY_ENV


def test_child_process_sees_only_temporary_places():
    result = child(
        "from shafa import config; print(config.data_dir()); print(config.legacy_env_file())"
    )
    data, legacy = result.stdout.splitlines()
    assert data == os.environ["SHAFA_DATA_DIR"]
    assert legacy == os.environ["SHAFA_LEGACY_ENV"]
    assert data != str(config.default_data_dir())
    assert legacy != str(config._REAL_LEGACY_ENV)


@pytest.mark.parametrize("name", ["SHAFA_DATA_DIR", "SHAFA_LEGACY_ENV"])
def test_guard_fails_loudly_in_test(monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(config.RealPathInTests, match="справжн"):
        config.data_dir() if name == "SHAFA_DATA_DIR" else config.legacy_env_file()


@pytest.mark.parametrize("name", ["SHAFA_DATA_DIR", "SHAFA_LEGACY_ENV"])
def test_guard_fails_loudly_in_child_process(name):
    env = {k: v for k, v in os.environ.items() if k != name}
    result = child("import sys, runpy; sys.argv = ['shafa', 'quit']; "
                   "runpy.run_module('shafa', run_name='__main__')", env)  # fmt: skip
    assert result.returncode != 0
    assert "RealPathInTests" in result.stderr


def test_main_in_child_never_moves_temporary_env():
    legacy = config.legacy_env_file()
    legacy.write_text(f"ANTHROPIC_API_KEY={SECRET}\n", encoding="utf-8")

    result = child("import sys, runpy; sys.argv = ['shafa', 'quit']; "
                   "runpy.run_module('shafa', run_name='__main__')")  # fmt: skip

    assert result.returncode == 0, result.stderr
    assert legacy.read_text(encoding="utf-8") == f"ANTHROPIC_API_KEY={SECRET}\n"
    assert not (config.data_dir() / ".env").exists()
    assert "перенесено" not in result.stdout
    assert SECRET not in result.stdout + result.stderr


def test_migrate_refuses_temporary_folders():
    legacy = config.legacy_env_file()
    legacy.write_text("X=1\n", encoding="utf-8")
    assert config.migrate_env() is None
    assert legacy.exists()


# --- Перенос: резервна копія, звірка, видалення лише після успіху ---


@pytest.fixture
def places(monkeypatch, tmp_path):
    data, program = tmp_path / "дані", tmp_path / "Програма"
    program.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(data))
    monkeypatch.setenv("SHAFA_LEGACY_ENV", str(program / ".env"))
    monkeypatch.setattr(config, "ENV_FILE", None)
    (program / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET}\n", encoding="utf-8")
    return data / ".env", program / ".env"


def backups(folder):
    return sorted(folder.glob(".env.backup-*"))


def test_backup_next_to_old_before_removing(places):
    target, legacy = places

    note = config.migrate_env(allow_temp=True)

    (backup,) = backups(legacy.parent)
    assert backup.read_text(encoding="utf-8") == f"ANTHROPIC_API_KEY={SECRET}\n"
    assert target.read_text(encoding="utf-8") == f"ANTHROPIC_API_KEY={SECRET}\n"
    assert not legacy.exists()
    assert str(backup) in note and SECRET not in note
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("broken", ["copy2", "write", "verify"])
def test_any_failure_keeps_old_file(places, monkeypatch, broken):
    target, legacy = places
    if broken == "copy2":
        monkeypatch.setattr(
            config.shutil, "copy2", lambda *a: (_ for _ in ()).throw(OSError("диск"))
        )
    elif broken == "write":
        monkeypatch.setattr(
            config, "write_private", lambda *a: (_ for _ in ()).throw(OSError("диск"))
        )
    else:
        real = config.write_private
        monkeypatch.setattr(config, "write_private", lambda path, text: real(path, text[:-3]))

    note = config.migrate_env(allow_temp=True)

    assert note.startswith("Налаштування (.env) НЕ перенесено")
    assert legacy.read_text(encoding="utf-8") == f"ANTHROPIC_API_KEY={SECRET}\n"
    assert not target.exists()  # недописаний новий файл не лишився


def test_backups_are_not_in_git():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ignored = subprocess.run(
        ["git", "check-ignore", ".env.backup-20261015-120000", ".env"], cwd=root,
        capture_output=True, text=True, check=False,
    ).stdout.split()  # fmt: skip
    assert ignored == [".env.backup-20261015-120000", ".env"]
