"""KLB-004: «Обновить все» на Клубку — правила без браузера й мережі.

Факт (Фаза 0): `GET /renew/all` оновлює лише **прострочені** оголошення; у кожного свій термін —
`renew_date` у `get-items` = мить оновлення + 7 діб. Тож оновлення буває й щодня малими групами.

* `due` — найраніший `renew_date` її активних у знімку (KLB-013: прострочені теж там, `sold = 0` зі
  старим `renew_date`, — вкладка «Обновить»); `overdue` — прострочені на мить `at`.
* `link` — посилання «Обновить все (N)» на сторінці керування (`/users/198135/manage`): немає —
  GET не робимо, навіть якщо знімок каже інакше (Ітерація 1, в).
* `judge` (KLB-013): P — id вкладки «Обновить» за знімком до GET, а без нього — N з посилання.
  Успіх — посилання після GET зникло (чи N після ≤ 5 % від N), у «Обновить» лишилось ≤ 5 % від P,
  активних ≥ 90 % від знімка до, і (коли id з P відомі) ≥ 95 % з них мають `renew_date` > зараз +
  6 діб.
* KLB-019: коли робити GET. Не «раз на добу о 20:00», а щойно оголошення можна оновити (посилання
  з'являється ≈ за 2 год до терміну): `wanted` — найраніший `renew_date` ≤ зараз + 1 год 45 хв
  (`AHEAD`) чи знімок старший за добу; `blocked` — від останнього GET ≥ 3 год (`MIN_GAP`) і за
  місцеву добу < 4 GET (`MAX_PER_DAY`). `klubok-renew.json` у теці даних: `gets` — часи GET (пишеться
  **до** GET), `runs` — підтверджені оновлення (час, скільки) для панелі, `day`/`at`/`ok` — останній.
  `SHAFA_KLUBOK_RENEW_AT` більше не час запуску: лише `off` вимикає автоматику.
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shafa import config
from shafa.klubok import queue

STATE_NAME = "klubok-renew.json"
AT_SETTING = "SHAFA_KLUBOK_RENEW_AT"  # KLB-019: лише `off`/порожнє вимикає; інше значення — ігнор
OFF = ("", "off", "ні", "no", "0", "false")
FRESH = timedelta(days=6)  # після оновлення `renew_date` має бути далі, ніж зараз + 6 діб
SHARE = 0.95  # стільки з прострочених мають оновитись
LEFT = 0.05  # KLB-013: у «Обновить» і в «Обновить все (N)» після GET — не більше 5 % від до
KEEP = 0.9  # активних після — не менше 90 % від знімка до
AHEAD = timedelta(hours=1, minutes=45)  # запуск: є термін ≤ зараз + 1 год 45 хв (посилання вже є)
STALE = timedelta(hours=24)  # … або знімок старший за добу
MIN_GAP = timedelta(hours=3)  # від останнього GET `/renew/all`
MAX_PER_DAY = 4  # GET за місцеву добу
GROUP = timedelta(minutes=15)  # панель: «наступне ≈ 23:25 (5)» — стільки оголошень у цьому терміні
KEEP_GETS = timedelta(days=3)  # скільки доб тримати часи GET у стані
ALERT_AFTER = timedelta(hours=12)  # прострочено довше — жовте на панелі й тривога Власнику
LINK = re.compile(
    r"""<a\b[^>]*\bhref\s*=\s*["']/renew/all["'][^>]*>(.*?)</a>""", re.DOTALL | re.IGNORECASE
)
COUNT = re.compile(r"\((\d+)\)")


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
    before: list[int] | None,
    link_before: int | None,
    active_before: int,
    after: dict[int, datetime | None],
    left: int,
    active_after: int,
    now: datetime,
    link_after: Link | None,
) -> Verdict:
    """KLB-013 (б). `before` — id вкладки «Обновить» за знімком до GET (None — знімка до немає),
    `link_before` — N з «Обновить все (N)», `after` — `renew_date` активних після, `left` — скільки
    в «Обновить» після, `link_after` — посилання після (None — сторінку не прочитано)."""
    if active_before and active_after < KEEP * active_before:
        return Verdict(False, f"активних було {active_before}, стало {active_after}")
    edge = now + FRESH
    known = before or []
    renewed = [i for i in known if (moment := after.get(i)) is not None and moment > edge]
    if link_after is None:
        return Verdict(False, "сторінку керування після оновлення не прочитано", renewed)
    if link_after.present and not (
        link_after.count is not None and link_before
        and link_after.count <= LEFT * link_before
    ):  # fmt: skip
        shown = "" if link_after.count is None else f" ({link_after.count})"
        return Verdict(False, f"посилання «Обновить все»{shown} не зникло", renewed)
    total = len(before) if before is not None else link_before
    if total is None:
        if left:
            return Verdict(False, f"у вкладці «Обновить» лишилось {left}", renewed)
        return Verdict(True, "посилання «Обновить все» зникло, вкладка «Обновить» порожня")
    if left > LEFT * total:
        return Verdict(False, f"у вкладці «Обновить» лишилось {left} з {total}", renewed)
    if before and len(renewed) < SHARE * len(before):
        return Verdict(False, f"оновлено лише {len(renewed)} з {len(before)}", renewed)
    return Verdict(True, f"оновлено {total - left} з {total}", renewed)


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


def gets(data_dir: Path | None = None) -> list[datetime]:
    """Часи GET `/renew/all` (останні доби); старий стан без `gets` — один GET з `at`."""
    data = load(data_dir)
    found = [m for v in data.get("gets") or [] if (m := parse_date(v)) is not None]
    if not found and data.get("day") and (legacy := parse_date(data.get("at"))) is not None:
        found = [legacy]
    return sorted(found)


def record_get(data_dir: Path | None, at: datetime) -> None:
    """Позначка **до** GET (KLB-019: список часів, а не одна доба)."""
    keep = [m for m in gets(data_dir) if at - m < KEEP_GETS]
    save(data_dir, gets=[m.isoformat() for m in [*keep, at]], day=at.date().isoformat(),
         at=at.isoformat(), ok=None)  # fmt: skip


def record_done(data_dir: Path | None, at: datetime, got: int, **fields) -> None:
    """Підтверджене оновлення: рядок для панелі «оновлено сьогодні о 20:00 (1772), о 23:00 (5)»."""
    runs = [r for r in load(data_dir).get("runs") or [] if _run_at(r) is not None]
    runs = [r for r in runs if at - _run_at(r) < KEEP_GETS]
    runs.append({"at": at.isoformat(), "count": got})
    save(data_dir, ok=True, runs=runs, **fields)


def _run_at(run: object) -> datetime | None:
    return parse_date(run.get("at")) if isinstance(run, dict) else None


def runs_on(now: datetime, data_dir: Path | None = None) -> list[tuple[datetime, int | None]]:
    """Підтверджені оновлення місцевої доби `now` (старий стан — один запис із `at`/`count`)."""
    data = load(data_dir)
    found = []
    for run in data.get("runs") or []:
        moment = _run_at(run)
        if moment is not None:
            count = run.get("count")
            found.append((moment, count if isinstance(count, int) else None))
    if not found and data.get("ok") is True and (moment := parse_date(data.get("at"))):
        count = data.get("count")
        found = [(moment, count if isinstance(count, int) else None)]
    return sorted(r for r in found if _local(r[0], now).date() == now.date())


def blocked(now: datetime, data_dir: Path | None = None) -> str | None:
    """KLB-019: чому GET зараз не можна (≥ 3 год від минулого, < 4 за місцеву добу); можна — None."""
    past = [m for m in gets(data_dir) if m <= now + timedelta(minutes=1)]
    if past and now - past[-1] < MIN_GAP:
        until = _local(past[-1] + MIN_GAP, now)
        return f"минулий GET о {_local(past[-1], now):%H:%M} — не раніше {until:%H:%M}"
    if sum(1 for m in past if _local(m, now).date() == now.date()) >= MAX_PER_DAY:
        return f"сьогодні вже {MAX_PER_DAY} оновлення — решта завтра"
    return None


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


def _raw() -> str:
    if AT_SETTING in os.environ:
        return os.environ[AT_SETTING].strip()
    return config.read_env_file(config.env_file()).get(AT_SETTING, "on").strip()


def automatic() -> bool:
    """Автоматика ввімкнена. Немає налаштування — так; порожнє чи `off` — ні (KLB-019)."""
    return _raw().lower() not in OFF


def ignored_value() -> str | None:
    """`SHAFA_KLUBOK_RENEW_AT` із часом чи іншим словом: більше не діє — лише для рядка в журнал."""
    raw = _raw()
    return raw if raw and raw.lower() not in OFF and raw.lower() != "on" else None


def _local(moment: datetime, now: datetime) -> datetime:
    return moment.astimezone(now.tzinfo)


@dataclass(frozen=True)
class Status:
    line: str = ""
    warn: bool = False
    since: datetime | None = None  # з якого часу прострочено (для тривоги)


def _group(conn: sqlite3.Connection, first: datetime) -> int:
    return sum(1 for _id, m in _active(conn) if m is not None and first <= m <= first + GROUP)


def _when(moment: datetime, now: datetime) -> str:
    local = _local(moment, now)
    return f"{local:%H:%M}" if local.date() == now.date() else f"{local:%d.%m о %H:%M}"


def status(conn: sqlite3.Connection, now: datetime, data_dir: Path | None = None) -> Status:
    """Рядок панелі Клубку: оновлено сьогодні / наступне ≈ … (N) / прострочено (жовте — > 12 год чи збій)."""
    data = load(data_dir)
    first = due(conn)
    failed = data.get("ok") is False and data.get("day") == now.date().isoformat()
    done = runs_on(now, data_dir)
    parts = []
    if done:
        parts.append("оновлено сьогодні " + ", ".join(
            f"о {_local(at, now):%H:%M}" + ("" if n is None else f" ({n})") for at, n in done))  # fmt: skip
    if first is not None and first > now:
        parts.append(f"наступне ≈ {_when(first, now)} ({_group(conn, first)})")
    if first is None or first > now:
        return Status("Обновить все: " + "; ".join(parts) if parts else "")
    if failed:
        why = str(data.get("text") or "оновлення не підтвердилось")
    elif not queue.enabled():
        why = "Клубок вимкнено в Налаштуваннях"
    elif not automatic():
        why = f"автооновлення вимкнено ({AT_SETTING}=off)"
    elif wait := blocked(now, data_dir):
        why = f"робот оновить пізніше ({wait})"
    else:
        why = "робот оновить, щойно буде вільний і програма відкрита"
    warn = failed or now - first > ALERT_AFTER
    return Status(f"Обновить все: прострочено з {_local(first, now):%d.%m %H:%M} — {why}",
                  warn, first)  # fmt: skip


def wanted(conn: sqlite3.Connection, now: datetime) -> bool:
    """Запуск має сенс: є термін ≤ зараз + 1 год 45 хв або знімок старший за добу (чи немає)."""
    read = read_at(conn)
    if read is None or now - read > STALE:
        return True
    first = due(conn)
    return first is not None and first <= now + AHEAD
