"""Налаштування з файлу `.env` у корені проєкту. Змінна середовища має пріоритет над файлом.

Значення секретів ніде не друкуються — ні у виводі, ні в помилках.
"""

import os
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


def setting(name: str, env_file: Path = ENV_FILE) -> str | None:
    """Налаштування: змінна середовища, інакше `.env`; порожнє значення = немає."""
    return os.environ.get(name) or read_env_file(env_file).get(name) or None


def api_key(env_file: Path = ENV_FILE) -> str:
    """Ключ Claude API або ConfigError із текстом без жодного значення."""
    key = setting(API_KEY, env_file)
    if key is None:
        raise ConfigError(f"Немає {API_KEY} у .env")
    return key
