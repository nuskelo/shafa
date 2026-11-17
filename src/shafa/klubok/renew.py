"""KLB-004: «Обновить все» на Клубку — правила без браузера й мережі.

Факт (Фаза 0): `GET /renew/all` оновлює лише **прострочені** оголошення; у кожного свій термін —
`renew_date` у `get-items` = мить оновлення + 7 діб. Тож оновлення буває й щодня малими групами.

* `due` — найраніший `renew_date` її активних у знімку; `overdue` — прострочені на мить `at`.
* `link` — посилання «Обновить все (N)» на сторінці керування (`/users/198135/manage`): немає —
  GET не робимо, навіть якщо знімок каже інакше (Ітерація 1, в).
* `judge` (Ітерація 1, б): прострочені до GET — P; успіх — ≥ 95 % з P мають `renew_date` > зараз
  + 6 діб **і** активних ≥ 90 % від знімка до. P порожнє, а посилання було — успіх, якщо після GET
  посилання зникло.
* Не частіше одного GET на місцеву добу: `klubok-renew.json` у теці даних (`day` пишеться **до**
  GET). Там же підсумок для панелі й тривоги.
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from shafa import config
from shafa.klubok import queue

STATE_NAME = "klubok-renew.json"
AT_SETTING = "SHAFA_KLUBOK_RENEW_AT"  # «ГГ:ХХ»; немає — `AT_DEFAULT`, порожнє чи `off` — вимкнено
AT_DEFAULT = "20:00"
OFF = ("", "off", "ні", "no", "0", "false")
FRESH = timedelta(days=6)  # після оновлення `renew_date` має бути далі, ніж зараз + 6 діб
SHARE = 0.95  # стільки з прострочених мають оновитись
KEEP = 0.9  # активних після — не менше 90 % від знімка до
AHEAD = timedelta(hours=1)  # щоденний запуск: є термін ≤ зараз + 1 год
STALE = timedelta(hours=24)  # … або знімок старший за добу
ALERT_AFTER = timedelta(hours=12)  # прострочено довше — жовте на панелі й тривога Власнику
LINK = re.compile(
    r"""<a\b[^>]*\bhref\s*=\s*["']/renew/all["'][^>]*>(.*?)</a>""", re.DOTALL | re.IGNORECASE
)
COUNT = re.compile(r"\((\d+)\)")


class SetupError(Exception):
    """Неправильне `SHAFA_KLUBOK_RENEW_AT`."""


def parse_date(value: object) -> datetime | None:
    """`renew_date` Клубку («2026-11-24T17:06:22Z») → datetime з поясом; погане — None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _active(conn: sqlite3.Connection) -> list[tuple[int, datetime | None]]:
    rows = conn.execute("SELECT id, renew_date FROM klubok_listings WHERE sold = 0").fetchall()
    return [(row[0], parse_date(row[1])) for row in rows]


def active_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM klubok_listings WHERE sold = 0").fetchone()[0]


def due(conn: sqlite3.Connection) -> datetime | None:
    """Найраніший термін її активних у знімку; знімка чи дат немає — None."""
    dates = [moment for _id, moment in _active(conn) if moment is not None]
    return min(dates) if dates else None


def overdue(conn: sqlite3.Connection, at: datetime) -> list[int]:
    return [i for i, moment in _active(conn) if moment is not None and moment <= at]


def renew_dates(conn: sqlite3.Connection) -> dict[int, datetime | None]:
    return dict(_active(conn))


def read_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT read_at FROM klubok_listings_read WHERE id = 1").fetchone()
    return parse_date(row[0]) if row else None


@dataclass(frozen=True)
class Link:
    present: bool
    count: int | None = None  # N з «Обновить все (N)»; None — посилання без числа чи немає


def link(html: str) -> Link:
    found = LINK.search(html or "")
    if found is None:
        return Link(False)
    number = COUNT.search(re.sub(r"<[^>]+>", " ", found.group(1)))
    return Link(True, int(number.group(1)) if number else None)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    text: str
    renewed: list[int] = field(default_factory=list)  # id з P, що оновились


def judge(
    before: list[int],
    active_before: int,
    after: dict[int, datetime | None],
    active_after: int,
    now: datetime,
    link_after: Link | None = None,
) -> Verdict:
    """Ітерація 1 (б). `before` — P (прострочені до GET), `after` — `renew_date` активних після."""
    if active_before and active_after < KEEP * active_before:
        return Verdict(False, f"активних було {active_before}, стало {active_after}")
    edge = now + FRESH
    renewed = [i for i in before if (moment := after.get(i)) is not None and moment > edge]
    if not before:
        if link_after is not None and not link_after.present:
            return Verdict(True, "посилання «Обновить все» зникло")
        return Verdict(False, "посилання «Обновить все» не зникло")
    if len(renewed) >= SHARE * len(before):
        return Verdict(True, f"оновлено {len(renewed)} з {len(before)}", renewed)
    return Verdict(False, f"оновлено лише {len(renewed)} з {len(before)} прострочених", renewed)


# --- Стан (тека даних) ---


def state_path(data_dir: Path | None = None) -> Path:
    return (data_dir or config.data_dir()) / STATE_NAME


def load(data_dir: Path | None = None) -> dict:
    try:
        data = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data_dir: Path | None = None, **fields) -> None:
    """Дописати поля стану. Збій запису оновлення не ламає (лише панель не побачить)."""
    path = state_path(data_dir)
    data = load(data_dir) | fields
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        pass


def done_on(day: date, data_dir: Path | None = None) -> bool:
    """Сьогодні (`day`, місцева доба) GET `/renew/all` уже був (чи міг бути)."""
    return load(data_dir).get("day") == day.isoformat()


def mark_ok(conn: sqlite3.Connection, renewed: list[int], at: datetime) -> int:
    """`klubok_ads.renewed_at` для `published`, чиє оголошення оновилось."""
    stamp = at.astimezone(UTC).isoformat(timespec="seconds")
    with conn:
        cursor = conn.executemany(
            "UPDATE klubok_ads SET renewed_at = ? WHERE state = ? AND klubok_id = ?",
            [(stamp, queue.PUBLISHED, klubok_id) for klubok_id in renewed],
        )
    return cursor.rowcount


# --- Налаштування й рядок панелі ---


def at_setting() -> time | None:
    """Час щоденного запуску; None — вимкнено. Немає налаштування — «20:00»."""
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


def _local(moment: datetime, now: datetime) -> datetime:
    return moment.astimezone(now.tzinfo)


@dataclass(frozen=True)
class Status:
    line: str = ""
    warn: bool = False
    since: datetime | None = None  # з якого часу прострочено (для тривоги)


def status(conn: sqlite3.Connection, now: datetime, data_dir: Path | None = None) -> Status:
    """Рядок панелі Клубку: наступне / оновлено сьогодні / прострочено (жовте — > 12 год чи збій)."""
    data = load(data_dir)
    today = now.date().isoformat()
    first = due(conn)
    failed = data.get("day") == today and data.get("ok") is False
    if data.get("day") == today and data.get("ok") is True and (first is None or first > now):
        at = parse_date(data.get("at"))
        when = f" о {_local(at, now):%H:%M}" if at else ""
        count = f" ({data['count']})" if isinstance(data.get("count"), int) else ""
        return Status(f"Обновить все: оновлено сьогодні{when}{count}")
    if first is None:
        return Status()
    if first > now:
        return Status(f"Обновить все: наступне {_local(first, now):%d.%m о %H:%M}")
    if failed:
        why = str(data.get("text") or "оновлення не підтвердилось")
    elif data.get("day") == today:
        why = "сьогодні вже оновлювали — решта завтра"
    else:
        try:
            at = at_setting()
        except SetupError as error:
            at, why = None, str(error)
        else:
            why = ""
        if not why:
            if not queue.enabled():
                why = "Клубок вимкнено в Налаштуваннях"
            elif at is None:
                why = f"автооновлення вимкнено ({AT_SETTING})"
            elif now.time() < at:
                why = f"робот оновить сьогодні о {at:%H:%M}"
            else:
                why = "робот оновить, щойно буде вільний і програма відкрита"
    warn = failed or now - first > ALERT_AFTER
    return Status(f"Обновить все: прострочено з {_local(first, now):%d.%m %H:%M} — {why}",
                  warn, first)  # fmt: skip


def wanted(conn: sqlite3.Connection, now: datetime) -> bool:
    """Щоденний запуск має сенс: є термін ≤ зараз + 1 год або знімок старший за добу (чи немає)."""
    read = read_at(conn)
    if read is None or now - read > STALE:
        return True
    first = due(conn)
    return first is not None and first <= now + AHEAD
