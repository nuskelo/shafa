"""Налаштування з файлу `.env` у теці даних. Змінна середовища має пріоритет над файлом.

`.env` лежить у теці даних (`data_dir()`, на Windows `%LOCALAPPDATA%\\shafa\\.env`), а не в теці
програми: оновлення програми його не чіпає (WIN-001 D5). Старий `.env` з кореня проєкту / теки
програми переїжджає туди один раз сам (`migrate_env`). Права на файл — лише її користувач.

Тести (FIX-001, інцидент 15.10.2026: процеси, запущені тестами, перенесли справжній `.env` у
тимчасові теки pytest): `SHAFA_TESTING=1` — тоді тека даних і «старий» `.env` беруться ЛИШЕ зі
змінних `SHAFA_DATA_DIR` / `SHAFA_LEGACY_ENV`, а звернення до справжніх місць — помилка.
Змінні успадковують і процеси, які запускає тест.

Значення секретів ніде не друкуються — ні у виводі, ні в помилках.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ENV_FILE: Path | None = None  # інший файл (тести); None — `.env` у теці даних
TESTING = "SHAFA_TESTING"
LEGACY_ENV = "SHAFA_LEGACY_ENV"
_REAL_LEGACY_ENV = Path(__file__).resolve().parents[2] / ".env"  # корінь проєкту / тека програми


class RealPathInTests(RuntimeError):
    """Тест (чи процес, який він запустив) звернувся до справжньої теки даних або `.env`."""


def testing() -> bool:
    return os.environ.get(TESTING) == "1"


def legacy_env_file() -> Path:
    """Старе місце `.env` — корінь проєкту / тека програми (у тестах — лише зі змінної)."""
    custom = os.environ.get(LEGACY_ENV)
    if custom:
        return Path(custom)
    if testing():
        raise RealPathInTests(f"Тест звернувся до справжнього .env: задайте {LEGACY_ENV}")
    return _REAL_LEGACY_ENV


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


def env_file() -> Path:
    return ENV_FILE or data_dir() / ".env"


def setting(name: str, path: Path | None = None) -> str | None:
    """Налаштування: змінна середовища, інакше `.env`; порожнє значення = немає."""
    return os.environ.get(name) or read_env_file(path or env_file()).get(name) or None


def api_key(path: Path | None = None) -> str:
    """Ключ Claude API або ConfigError із текстом без жодного значення."""
    key = setting(API_KEY, path)
    if key is None:
        raise ConfigError(f"Немає {API_KEY} у .env")
    return key


DATA_DIR = "SHAFA_DATA_DIR"


def data_dir() -> Path:
    """Тека даних програми поза текою програми: `.env`, база, профіль робота, журнали.

    `SHAFA_DATA_DIR` — лише змінна середовища (`.env` сам лежить у теці даних), інакше Windows
    `%LOCALAPPDATA%\\shafa`, Mac `~/Library/Application Support/shafa`, інші `~/.local/share/shafa`.
    """
    custom = os.environ.get(DATA_DIR)
    if custom:
        return Path(custom).expanduser()  # `~/shafa-viktoria` у лапках оболонка не розгортає
    if testing():
        raise RealPathInTests(f"Тест звернувся до справжньої теки даних: задайте {DATA_DIR}")
    return default_data_dir()


def default_data_dir() -> Path:
    """Справжня тека даних (без `SHAFA_DATA_DIR`)."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "shafa"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "shafa"
    return Path.home() / ".local" / "share" / "shafa"


# --- Запис `.env`: лише її користувач ---


def restrict(path: Path) -> None:
    """Права на файл — лише поточний користувач (Windows: `icacls`, без успадкування)."""
    if os.name == "nt":
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}".lstrip("\\")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=True, capture_output=True,
        )  # fmt: skip
    else:
        path.chmod(0o600)


def write_private(path: Path, text: str) -> None:
    """Записати файл із секретами: спершу права «лише я», потім вміст."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    else:
        path.touch()
    restrict(path)
    path.write_text(text, encoding="utf-8")


def save_settings(values: dict[str, str], path: Path | None = None) -> None:
    """Записати налаштування в `.env`: наявні рядки замінюються, решта (і коментарі) лишаються."""
    path = path or env_file()
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    left = dict(values)
    result = []
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else None
        )
        if key in left:
            result.append(f"{key}={left.pop(key)}")
        else:
            result.append(line)
    result += [f"{key}={value}" for key, value in left.items()]
    write_private(path, "\n".join(result) + "\n")


def _inside_temp(path: Path) -> bool:
    temp = Path(tempfile.gettempdir()).resolve()
    return path.resolve().is_relative_to(temp)


def migrate_env(allow_temp: bool = False) -> str | None:
    """Старий `.env` (корінь проєкту / тека програми) → тека даних, один раз. Повертає пояснення.

    Обережно (FIX-001): з тимчасових тек не переносить (`allow_temp` — лише для тестів самого
    переносу); спершу резервна копія поруч (`.env.backup-<час>`), потім запис і звірка вмісту, і
    лише тоді старий файл видаляється. Будь-що не вдалось — старий лишається.
    """
    target, legacy = env_file(), legacy_env_file()
    if not legacy.is_file() or legacy.resolve() == target.resolve():
        return None
    if not allow_temp and (_inside_temp(legacy) or _inside_temp(target)):
        return None
    if target.exists():
        return (
            f"Є два файли .env: у {legacy.parent} і в теці даних. Програма читає теку даних; "
            "старий видаліть."
        )
    text = legacy.read_text(encoding="utf-8-sig")
    backup = legacy.with_name(f".env.backup-{datetime.now().astimezone():%Y%m%d-%H%M%S}")
    try:
        shutil.copy2(legacy, backup)
        restrict(backup)
        write_private(target, text)
        if target.read_text(encoding="utf-8") != text:
            raise OSError("вміст у теці даних не збігається")
    except OSError as error:
        target.unlink(missing_ok=True)  # недописаний новий файл не лишаємо — читався б він
        return f"Налаштування (.env) НЕ перенесено ({error}) — лишились на старому місці"
    legacy.unlink()
    return f"Налаштування (.env) перенесено в теку даних: {target.parent}. Резервна копія: {backup}"
