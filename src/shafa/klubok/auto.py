"""KLB-014: Клубок виставляє сам щодня — налаштування, позначка доби, умови й рядок панелі.

Без браузера й мережі. Сам запуск — `robot/klubok_auto.Auto` (потік `app`). Автостарт — один на
місцеву добу: або після спокійного кінця сесії Шафи, або о `SHAFA_KLUBOK_AUTO_AT`.
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

from shafa import config
from shafa.klubok import queue, renew

AUTO_SETTING = "SHAFA_KLUBOK_AUTO"  # «Виставляти самому щодня»: немає — так, `0`/`ні`/`off` — ні
AT_SETTING = (
    "SHAFA_KLUBOK_AUTO_AT"  # «ГГ:ХХ»: немає — `AT_DEFAULT`, порожнє/`off` — лише після Шафи
)
AT_DEFAULT = "21:00"
OFF = renew.OFF
MARK = "klubok-auto.json"  # {"day", "at", "session"} — сьогодні вже запускали
CALM = ("done", "limit", "stop")  # як `publish._announce_end` і `bot.alerts.CALM`


class SetupError(Exception):
    pass


def enabled() -> bool:
    raw = (config.setting(AUTO_SETTING) or "").strip().lower()
    return not raw or raw not in OFF


def at_setting() -> time | None:
    """Час запасного запуску; None — вимкнено (тоді лише після сесії Шафи)."""
    if AT_SETTING in os.environ:
        raw = os.environ[AT_SETTING]
    else:
        raw = config.read_env_file(config.env_file()).get(AT_SETTING, AT_DEFAULT)
    raw = raw.strip()
    if raw.lower() in OFF:
        return None
    try:
        hours, minutes = raw.split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        raise SetupError(f"{AT_SETTING}={raw!r}: потрібен час «ГГ:ХХ» або порожнє") from None


# --- позначка доби ---


def _path(folder: Path | None) -> Path:
    return (folder or config.data_dir()) / MARK


def read_mark(folder: Path | None = None) -> dict | None:
    try:
        data = json.loads(_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def marked_today(now: datetime, folder: Path | None = None) -> dict | None:
    data = read_mark(folder)
    return data if data and data.get("day") == f"{now:%Y-%m-%d}" else None


def write_mark(now: datetime, folder: Path | None = None, session: int | None = None) -> None:
    path = _path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"day": f"{now:%Y-%m-%d}", "at": now.isoformat(timespec="seconds"), "session": session}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def attach_session(conn: sqlite3.Connection, now: datetime, folder: Path | None = None) -> None:
    """Автостарт не знає номера сесії до її початку — дописуємо, коли сесія з'явилась."""
    data = marked_today(now, folder)
    if not data or data.get("session") is not None:
        return
    try:
        began = datetime.fromisoformat(data["at"])
    except (KeyError, ValueError):
        return
    found = None
    for row in conn.execute(  # від нових до старих: лишається найраніша після запуску
        "SELECT id, started_at FROM sessions WHERE site = 'klubok' ORDER BY id DESC LIMIT 10"
    ):
        started = _local(row["started_at"], now)
        if started is not None and started >= began:
            found = row["id"]
    if found is not None:
        write_mark(began, folder, found)


# --- умови ---


def _local(value: str | None, now: datetime) -> datetime | None:
    """Момент із бази (UTC-рядок) у місцевому часі `now`."""
    try:
        moment = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(now.tzinfo)


def shafa_calm_today(conn: sqlite3.Connection, now: datetime) -> bool:
    """Сьогодні (місцева доба) закінчилась сесія Шафи спокійно — Клубок іде одразу після неї."""
    for row in conn.execute(
        "SELECT stop_code, stop_reason, ended_at FROM sessions "
        "WHERE site = 'shafa' AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 30"
    ):
        ended = _local(row["ended_at"], now)
        if ended is None or ended.date() != now.date():
            continue
        code, reason = row["stop_code"], (row["stop_reason"] or "").strip()
        if code in CALM or not (code or reason):
            return True
    return False


def loud_today(conn: sqlite3.Connection, now: datetime) -> str | None:
    """Код гучної зупинки останньої сесії Клубку за сьогодні (капча, немає входу, 429…) чи None."""
    from shafa.robot import klubok_publish

    row = conn.execute(
        "SELECT stop_code, ended_at FROM sessions WHERE site = 'klubok' AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ended = _local(row["ended_at"], now)
    if ended is None or ended.date() != now.date():
        return None
    return row["stop_code"] if row["stop_code"] in klubok_publish.LOUD else None


# --- рядок панелі ---


@dataclass(frozen=True)
class Status:
    line: str = ""
    warn: bool = False


def status(conn: sqlite3.Connection, now: datetime, folder: Path | None = None) -> Status:
    """«Автостарт: …» для панелі Клубку; порожньо, коли Клубок вимкнено."""
    if not queue.enabled():
        return Status()
    if not enabled():
        return Status("Автостарт: вимкнено")
    try:
        at = at_setting()
    except SetupError as error:
        return Status(f"Автостарт: {error}", warn=True)
    if (data := marked_today(now, folder)) is not None:
        started = datetime.fromisoformat(data["at"]) if data.get("at") else None
        when = f" о {started:%H:%M}" if started else ""
        return Status(f"Автостарт: запущено сьогодні{when}")
    if (code := loud_today(conn, now)) is not None:
        return Status(
            f"Автостарт: сьогодні не буде — сесія Клубку зупинилась з помилкою ({code})", warn=True
        )
    tail = f" або о {at:%H:%M}" if at is not None else ""
    return Status(f"Автостарт: сьогодні після сесії Шафи{tail}")
