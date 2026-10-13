"""Замок «один робот на профіль» (UI-002, D1): файловий замок ОС.

Його беруть усі режими, що відкривають профіль Chrome робота (`--publish`, `--dry-fill`, `--login`,
`--sync-catalog`, запуск з екрана). Замок тримає сам процес робота; помре процес — ОС зніме замок
сама (POSIX `flock`; Windows `LockFile`: «If a process terminates with a portion of a file locked …
the locks are unlocked by the operating system»), тож «завислого» замка після падіння немає. Сон
ноутбука — процес живий, замок тримається. Перевірок «чи живий процес» за PID немає: на Windows
`os.kill(pid, 0)` процес убиває.

Поруч із замком — файл стану (що саме робить робот), його читає екран.
"""

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO

from shafa import config

if os.name == "nt":  # pragma: no cover — перевіряється в windows-tests
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Busy(Exception):
    """Робот уже працює (замок тримає інший процес)."""

    def __str__(self) -> str:
        return "Робот уже працює — другий запуск нічого не чіпає"


def lock_path() -> Path:
    return config.data_dir() / "robot.lock"


def state_path(lock: Path) -> Path:
    return lock.with_suffix(".json")


class Held:
    """Узятий замок. `mode` — що робить робот (для екрана)."""

    def __init__(self, path: Path, handle: IO[bytes], mode: str) -> None:
        self.path, self.handle, self.mode = path, handle, mode
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        state_path(self.path).unlink(missing_ok=True)
        try:
            _unlock(self.handle)
        finally:
            self.handle.close()


def acquire(mode: str, path: Path | None = None, wait: float = 2.0) -> Held:
    """Узяти замок або `Busy`. Чекає лише `wait` с (екран міг саме пробувати замок)."""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 — живе, доки тримаємо замок
    deadline = time.monotonic() + wait
    while not _try_lock(handle):
        if time.monotonic() >= deadline:
            handle.close()
            raise Busy
        time.sleep(0.1)
    state = {"mode": mode, "pid": os.getpid(), "since": datetime.now().astimezone().isoformat()}
    state_path(path).write_text(json.dumps(state), encoding="utf-8")
    return Held(path, handle, mode)


@contextmanager
def hold(mode: str, path: Path | None = None, wait: float = 2.0) -> Iterator[Held]:
    held = acquire(mode, path, wait)
    try:
        yield held
    finally:
        held.release()


def busy(path: Path | None = None) -> dict | None:
    """Стан робота, якщо замок узято, інакше None. Пробує замок і одразу відпускає."""
    path = path or lock_path()
    if not path.exists():
        return None
    with open(path, "a+b") as handle:
        if _try_lock(handle):
            _unlock(handle)
            return None
    try:
        return json.loads(state_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "?"}
