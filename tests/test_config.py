import os
import subprocess
from pathlib import Path

import pytest

from shafa import config
from shafa.config import API_KEY, ConfigError, api_key, data_dir, read_env_file, setting

FAKE_KEY = "test-key-vyhadanyi-123"


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv(API_KEY, raising=False)


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_key_value_comments_and_blank_lines(tmp_path):
    path = write_env(
        tmp_path,
        "# коментар\n\nANTHROPIC_API_KEY="
        + FAKE_KEY
        + "\n  SHAFA_ROOT_DIR = C:\\Шафа \nбез_рівності\n",
    )
    assert read_env_file(path) == {API_KEY: FAKE_KEY, "SHAFA_ROOT_DIR": "C:\\Шафа"}


def test_value_keeps_equals_and_quotes(tmp_path):
    path = write_env(tmp_path, 'A=b=c\nB="в лапках"\n')
    assert read_env_file(path) == {"A": "b=c", "B": '"в лапках"'}


def test_bom_from_windows_notepad(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"{API_KEY}={FAKE_KEY}\n", encoding="utf-8-sig")
    assert api_key(path) == FAKE_KEY


def test_missing_file_is_empty(tmp_path):
    assert read_env_file(tmp_path / "немає.env") == {}


def test_environment_wins_over_file(tmp_path, monkeypatch):
    path = write_env(tmp_path, f"{API_KEY}=з-файлу\n")
    monkeypatch.setenv(API_KEY, FAKE_KEY)
    assert api_key(path) == FAKE_KEY


def test_empty_values_mean_missing(tmp_path, monkeypatch):
    path = write_env(tmp_path, f"{API_KEY}=\n")
    monkeypatch.setenv(API_KEY, "")
    assert setting(API_KEY, path) is None


def test_no_key_error_has_no_values(tmp_path):
    path = write_env(tmp_path, "OTHER_SECRET=" + FAKE_KEY + "\n")
    with pytest.raises(ConfigError) as error:
        api_key(path)
    assert str(error.value) == "Немає ANTHROPIC_API_KEY у .env"
    assert FAKE_KEY not in repr(error.value)


def test_data_dir_from_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    assert data_dir() == tmp_path / "дані"


def test_data_dir_default_outside_repo():
    path = config.default_data_dir()  # лише шлях, без звернення
    assert path.name == "shafa"
    assert not path.resolve().is_relative_to(Path(__file__).resolve().parents[1])


# --- `.env` у теці даних (WIN-001 D5) ---

SECRET = "sk-тестовий-секрет-123"


@pytest.fixture
def real_paths(monkeypatch, tmp_path):
    """`.env` — як у встановленій програмі: тека даних і стара тека програми."""
    data, program = tmp_path / "дані", tmp_path / "Програма"
    program.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(data))
    monkeypatch.setattr(config, "ENV_FILE", None)
    monkeypatch.setenv("SHAFA_LEGACY_ENV", str(program / ".env"))
    return data / ".env", program / ".env"


def private(path: Path) -> bool:
    if os.name == "nt":
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True)
        return "BUILTIN\\Users" not in acl.stdout and "Everyone" not in acl.stdout
    return path.stat().st_mode & 0o777 == 0o600


def test_env_file_is_in_data_dir(real_paths):
    target, _ = real_paths
    assert config.env_file() == target


def test_old_env_moves_once_and_is_private(real_paths):
    target, legacy = real_paths
    legacy.write_text(f"ANTHROPIC_API_KEY={SECRET}\nSHAFA_DAILY_LIMIT=20\n", encoding="utf-8")

    note = config.migrate_env(allow_temp=True)

    assert note.startswith("Налаштування (.env) перенесено в теку даних")
    assert SECRET not in note
    assert not legacy.exists()
    assert config.setting("ANTHROPIC_API_KEY") == SECRET
    assert config.setting("SHAFA_DAILY_LIMIT") == "20"
    assert private(target)
    assert config.migrate_env(allow_temp=True) is None  # удруге — нічого


def test_both_env_files_data_dir_wins_old_kept(real_paths):
    target, legacy = real_paths
    target.parent.mkdir(parents=True)
    target.write_text("SHAFA_DAILY_LIMIT=10\n", encoding="utf-8")
    legacy.write_text("SHAFA_DAILY_LIMIT=20\n", encoding="utf-8")

    note = config.migrate_env(allow_temp=True)

    assert "Є два файли .env" in note
    assert legacy.exists() and config.setting("SHAFA_DAILY_LIMIT") == "10"


def test_update_of_program_folder_keeps_settings(real_paths):
    """Оновлення замінює теку програми — `.env` у теці даних лишається."""
    _, legacy = real_paths
    config.save_settings({"ANTHROPIC_API_KEY": SECRET})
    for child in legacy.parent.iterdir():
        child.unlink()
    legacy.parent.rmdir()
    assert config.setting("ANTHROPIC_API_KEY") == SECRET


def test_save_settings_merges_and_keeps_comments(real_paths):
    target, _ = real_paths
    target.parent.mkdir(parents=True)
    target.write_text("# мої налаштування\nSHAFA_DAILY_LIMIT=30\nOTHER=x\n", encoding="utf-8")

    config.save_settings({"SHAFA_DAILY_LIMIT": "25", "SHAFA_AI_DAILY_USD": "2"})

    assert target.read_text(encoding="utf-8") == (
        "# мої налаштування\nSHAFA_DAILY_LIMIT=25\nOTHER=x\nSHAFA_AI_DAILY_USD=2\n"
    )
    assert private(target)
