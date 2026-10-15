"""Встановлення й оновлення на її ноутбуці (WIN-001 D6).

Програма — `%LOCALAPPDATA%\\shafa\\app\\` (вбудований Python, бібліотеки, іконка); дані — поруч у
`%LOCALAPPDATA%\\shafa\\`: `.env`, база, мініатюри, профіль робота, журнали. Оновлення замінює ЛИШЕ
теку `app` (стара тимчасово `app.old`, щоб повернути, якщо копіювання не вдалося), дані не чіпає.

Запуск — `Встановити.cmd` з розпакованого zip: `python.exe` з НОВОЇ теки (не встановленої — її
не можна замінити, поки з неї працює Python). Перший раз — потім майстер налаштування.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from shafa import config

APP = "app"
OLD = "app.old"


class InstallError(Exception):
    """Не встановлено. Текст — для Власника."""


def install(source: Path, data: Path | None = None) -> Path:
    """Скопіювати теку програми `source` у `<дані>/app`. Повертає встановлену теку."""
    data = data or config.data_dir()
    source = source.resolve()
    target = data / APP
    old = data / OLD
    if source == target.resolve():
        raise InstallError("Запускай «Встановити» з розпакованої нової версії, не з встановленої")
    if not (source / "python").is_dir() or not (source / "lib" / "shafa").is_dir():
        raise InstallError(f"У {source} немає програми (python, lib\\shafa)")
    data.mkdir(parents=True, exist_ok=True)
    if old.exists():
        shutil.rmtree(old)
    if target.exists():
        try:
            target.rename(old)
        except OSError as error:
            raise InstallError(
                "Програма ще працює — натисни «Вийти» на екрані (або закрий робота) і запусти ще раз"
            ) from error
    try:
        shutil.copytree(source, target)
    except OSError as error:
        shutil.rmtree(target, ignore_errors=True)
        if old.exists():
            old.rename(target)  # повертаємо стару версію
        raise InstallError(f"Не вдалося скопіювати програму: {error}") from error
    shutil.rmtree(old, ignore_errors=True)
    return target


def _stop_running(data: Path) -> None:
    """Перед оновленням — закрити запущену програму (екран і бота) її ж Python."""
    python = data / APP / "python" / "python.exe"
    if python.is_file():
        subprocess.run([str(python), "-m", "shafa", "quit"], check=False, timeout=60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Встановити")
    parser.add_argument("source", type=Path, help="тека app з розпакованого zip")
    parser.add_argument("--no-setup", action="store_true", help="не запускати майстер")
    args = parser.parse_args(argv)
    data = config.data_dir()
    first = not config.env_file().exists()
    _stop_running(data)
    try:
        target = install(args.source, data)
    except InstallError as error:
        print(f"НЕ ВСТАНОВЛЕНО: {error}")
        return 1
    version = (target / "VERSION").read_text(encoding="utf-8").strip() if (
        target / "VERSION").is_file() else "?"  # fmt: skip
    print(f"Встановлено «Шафа-помічник» {version} у {target}")
    print(f"Дані (налаштування, база, профіль робота) — у {data}, їх не чіпали")
    if first and not args.no_setup:
        print("Перше встановлення — запускаю майстер налаштування…")
        return subprocess.run(
            [str(target / "python" / "python.exe"), "-m", "shafa", "setup"], check=False
        ).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
