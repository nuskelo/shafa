from pathlib import Path

import pytest

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


def test_data_dir_default_outside_repo(monkeypatch):
    monkeypatch.delenv("SHAFA_DATA_DIR", raising=False)
    path = data_dir()
    assert path.name == "shafa"
    assert not path.resolve().is_relative_to(Path(__file__).resolve().parents[1])
