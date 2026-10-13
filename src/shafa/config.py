"""Налаштування з файлу `.env` у корені проєкту. Змінна середовища має пріоритет над файлом.

Значення секретів ніде не друкуються — ні у виводі, ні в помилках.
"""

import os
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
API_KEY = "ANTHROPIC_API_KEY"


class ConfigError(Exception):
    """Немає потрібного налаштування. Текст — для людини, без значень."""


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` з файлу. Порожні рядки й `#`-коментарі пропускаються, лапки не знімаються."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def setting(name: str, env_file: Path | None = None) -> str | None:
    """Налаштування: змінна середовища, інакше `.env`; порожнє значення = немає."""
    return os.environ.get(name) or read_env_file(env_file or ENV_FILE).get(name) or None


def api_key(env_file: Path | None = None) -> str:
    """Ключ Claude API або ConfigError із текстом без жодного значення."""
    key = setting(API_KEY, env_file)
    if key is None:
        raise ConfigError(f"Немає {API_KEY} у .env")
    return key


DATA_DIR = "SHAFA_DATA_DIR"


def data_dir() -> Path:
    """Тека даних програми поза репозиторієм: профіль робота, скріншоти.

    `SHAFA_DATA_DIR`, інакше Windows `%LOCALAPPDATA%\\shafa`, Mac `~/Library/Application Support/
    shafa`, інші `~/.local/share/shafa`.
    """
    custom = setting(DATA_DIR)
    if custom:
        return Path(custom)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "shafa"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "shafa"
    return Path.home() / ".local" / "share" / "shafa"
