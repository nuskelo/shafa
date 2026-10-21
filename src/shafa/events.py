"""Журнал подій (BOT-012): усе, що відбувається у фоні (ШІ, робот, довідники), — рядком у файл
`events.jsonl` у теці даних. Бот читає його (`bot/alerts.py`) і шле Власнику в Telegram.

ШІ й робот можуть працювати в іншому процесі, ніж бот, тож між ними — лише файл. Новий вид події
(BOT-013) = один виклик `emit(...)` і один рядок у `KINDS`. Збій запису ніколи не ламає того, хто
пише: це лише журнал.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shafa import config

NAME = "events.jsonl"
OLD_NAME = "events-old.jsonl"  # попередня копія після ротації
MAX_BYTES = 2 * 1024 * 1024

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Kind:
    """Як бот показує подію цього виду."""

    emoji: str
    loud: bool = False  # гучне повідомлення одразу, інакше — тихо, у збірці раз на 5 хв
    skip: bool = False  # бот не шле: те саме вже йде іншим шляхом (Проблеми — з кнопками, BOT-011)


# Єдина таблиця видів: `kind` → emoji, гучність, пропускати чи ні.
KINDS = {
    # ШІ (`fill.py`, `batch.py`)
    "ai.start": Kind("🤖"),
    "ai.done": Kind("🤖"),
    "ai.stop": Kind("⛔", loud=True),  # бюджет, ключ, пакет не створено
    "ai.rules": Kind("🔁"),  # перерахунок правилами, «Перевірити» без ШІ: без запитів до ШІ
    # робот (`robot/publish.py`)
    "robot.start": Kind("▶️"),
    "robot.post": Kind("✅"),
    "robot.problem": Kind("⚠️", skip=True),  # її вже шле `alerts._problem_events` з кнопками
    "robot.end": Kind("⏹"),
    "robot.stop": Kind("⛔", loud=True, skip=True),  # її вже шле `alerts._session_events`
    # пробний прогін (`robot/rehearse.py`)
    "rehearse.start": Kind("🧪"),
    "rehearse.done": Kind("🧪"),
    "rehearse.bad": Kind("🧪", loud=True),  # не готова форма чи «Проблеми»
    # довідники Шафи (`robot/sync.py`)
    "catalog.start": Kind("📚"),
    "catalog.done": Kind("📚"),
    "catalog.error": Kind("⚠️", loud=True),
}
OTHER = Kind("ℹ️")  # вид, якого немає в таблиці: тихо


def info(kind: str) -> Kind:
    return KINDS.get(kind, OTHER)


def cards(n: int) -> str:
    """«1 Картка», «2 Картки», «5 Карток»."""
    tail = n % 100
    if n % 10 == 1 and tail != 11:
        word = "Картка"
    elif n % 10 in (2, 3, 4) and tail not in (12, 13, 14):
        word = "Картки"
    else:
        word = "Карток"
    return f"{n} {word}"


def clean_url(url: str) -> str:
    """Посилання без параметрів і якоря (як у `robot-run.log`)."""
    return url.split("?", 1)[0].split("#", 1)[0]


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(OLD_NAME))  # стару копію замінюємо
    except OSError:
        pass  # немає файла чи його тримає читач (Windows): ротація наступного разу


def emit(kind: str, text: str, *, loud: bool = False, data: dict[str, Any] | None = None) -> None:
    """Дописати подію в журнал. `loud` — гучно, навіть якщо вид тихий. Не кидає винятків."""
    try:
        from shafa.bot import report  # лише тут: журнал пишуть і процеси без бота

        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "text": report.scrub(text),
            "loud": loud,
            "data": data or {},
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = config.data_dir() / NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        with open(path, "ab") as stream:  # один `write` у режимі додавання: рядки не змішуються
            stream.write(line.encode("utf-8"))
    except Exception as error:  # noqa: BLE001 — журнал не ламає ШІ чи робота
        log.warning("Журнал подій: %s: %s", type(error).__name__, str(error)[:200])


# --- Читання (бот) ---


@dataclass(frozen=True)
class Record:
    """Один рядок журналу. `kind` порожній — рядок зіпсований (його пропускають). `inode` і `end` —
    де в файлі закінчується цей рядок (позиція читача)."""

    at: datetime | None
    kind: str
    text: str
    loud: bool
    data: dict
    inode: int
    end: int


def _parse(raw: bytes, inode: int, end: int) -> Record:
    try:
        data = json.loads(raw.decode("utf-8"))
        at = datetime.fromisoformat(str(data["at"])).astimezone()
        extra = data.get("data")
        return Record(
            at, str(data["kind"]), str(data["text"]), bool(data.get("loud")),
            extra if isinstance(extra, dict) else {}, inode, end,
        )  # fmt: skip
    except (ValueError, KeyError, TypeError, AttributeError):
        return Record(None, "", "", False, {}, inode, end)


def _read(path: Path, offset: int) -> list[Record]:
    """Повні рядки з `offset`; недописаний останній — наступного разу."""
    try:
        with open(path, "rb") as stream:
            inode = os.fstat(stream.fileno()).st_ino
            stream.seek(offset)
            chunk = stream.read()
    except OSError:
        return []
    chunk = chunk[: chunk.rfind(b"\n") + 1]
    records, end = [], offset
    for raw in chunk.splitlines(keepends=True):
        end += len(raw)
        if raw.strip():
            records.append(_parse(raw, inode, end))
    return records


def _inode(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_size


def read_new(data_dir: Path, position: dict | None) -> tuple[list[Record], bool]:
    """Нові рядки після `position` (`{"inode", "offset"}`; None — читача ще не було).

    Другий результат — `fresh`: позиції немає чи вона втрачена (файл зник чи став меншим), тож
    читаємо з початку, і старі події слати не треба. Ротація не втрата: файл, який бот читав,
    став `events-old.jsonl` — дочитуємо його й беремо новий з початку."""
    current, old = data_dir / NAME, data_dir / OLD_NAME
    here, before = _inode(current), _inode(old)
    try:
        saved, offset = int(position["inode"]), int(position["offset"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return (_read(current, 0) if here else []), True
    if here and here[0] == saved:
        if here[1] < offset:  # файл урізано — це вже інший файл
            return _read(current, 0), True
        return _read(current, offset), False
    if before and before[0] == saved:
        return _read(old, offset) + (_read(current, 0) if here else []), False
    return (_read(current, 0) if here else []), True
