"""Тривоги Власнику в Telegram одразу (BOT-010): зупинка робота, нові Проблеми, збій самоперевірки,
бюджет ШІ. Звіт о 21:00 (`report`) лишається як є.

Бот сам читає базу й файли теки даних; робот, програма й `health` нічого нового не пишуть. Що вже
надіслано — файл `alerts-last.json` у теці даних. Перший запуск лише запам'ятовує поточний стан і
нічого не шле за минуле. `run` нічого не знає про Telegram: надсилання — функція `send`, яка
каже, чи дійшло; не дійшло — подія лишається до наступного циклу (без дублів).
"""

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from shafa import batch, config
from shafa.bot import actions, report
from shafa.db import Status
from shafa.robot import control

SETTING = "SHAFA_ALERTS"  # не задано — тривоги є; «off» (чи «ні», «no», «0», «false») — вимкнено
OFF = {"off", "ні", "no", "0", "false"}
LAST_NAME = "alerts-last.json"
EVERY = timedelta(seconds=60)  # як часто бот дивиться в базу
SAME = timedelta(hours=1)  # однакове повідомлення (той самий ключ) — не частіше
HEALTH_REPEAT = timedelta(hours=6)  # той самий FAIL самоперевірки — не частіше
PER_HOUR = 20  # повідомлень на годину, включно з «ще N подій»
BUDGET_SHARE = 80  # % добового ліміту ШІ
LIST_MAX = 10  # проблем в одному повідомленні
LINE_MAX = actions.LINE_MAX  # символів на одну проблему
CALM = {"done", "limit", "stop"}  # звичайне завершення (чи її власний «Стоп») — одним рядком


@dataclass
class Event:
    key: str
    text: str
    commit: Callable[[dict], None]  # що запам'ятати, коли повідомлення дійшло
    markup: dict | None = None  # кнопки під повідомленням (BOT-011)
    album_of: int | None = None  # Картка, чиї фото (+ схожого оголошення) йдуть перед повідомленням


def enabled() -> bool:
    if SETTING in os.environ:
        raw = os.environ[SETTING]
    else:
        raw = config.read_env_file(config.env_file()).get(SETTING, "")
    return raw.strip().lower() not in OFF


# --- Стан ---


def load(data_dir: Path) -> dict | None:
    """None — файла немає чи він зіпсований: тоді це «перший запуск»."""
    try:
        state = json.loads((data_dir / LAST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and "session_id" in state else None


def save(data_dir: Path, state: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    temp = data_dir / (LAST_NAME + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.replace(data_dir / LAST_NAME)


def forget(data_dir: Path) -> None:
    """Тривоги вимкнено: стан застаріває, тож після ввімкнення — знову «перший запуск»."""
    try:
        (data_dir / LAST_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def _stamp(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return None


def _problems_now(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["id"]): row["problem_code"] or ""
        for row in conn.execute("SELECT id, problem_code FROM items WHERE status = ?", (Status.PROBLEM,))
    }  # fmt: skip


def _last_session(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM sessions WHERE ended_at IS NOT NULL"
    ).fetchone()[0]


def _health_file(data_dir: Path) -> tuple[datetime, list[dict]] | None:
    try:
        data = json.loads((data_dir / report.HEALTH_NAME).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(data["at"]).astimezone()
        checks = [c for c in data["checks"] if isinstance(c, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at, checks


def _fails(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c.get("status") == "fail" and c.get("name")]


def _budget_high(conn: sqlite3.Connection, day: date) -> tuple[int, int] | None:
    """(витрачено, ліміт) у мікродоларах, коли витрачено ≥ 80 % добового ліміту."""
    try:
        limit = batch.limits().daily_micro_usd
    except Exception:  # noqa: BLE001 — неправильне налаштування тривог не ламає
        return None
    spent = report._spent(conn, day)
    return (spent, limit) if limit > 0 and spent * 100 >= limit * BUDGET_SHARE else None


def snapshot(conn: sqlite3.Connection, data_dir: Path, moment: datetime) -> dict:
    """Перший запуск: поточний стан як «вже відоме», без жодного повідомлення."""
    state: dict = {
        "session_id": _last_session(conn),
        "problems": _problems_now(conn),
        "health_at": "",
        "health_sent": {},
        "budget_day": "",
        "sent": {},
        "hour": [],
    }
    if found := _health_file(data_dir):
        at, checks = found
        state["health_at"] = _stamp(at)
        state["health_sent"] = {str(c["name"]): _stamp(moment) for c in _fails(checks)}
    if _budget_high(conn, moment.astimezone().date()):
        state["budget_day"] = moment.astimezone().date().isoformat()
    return state


# --- Події ---


def _sentence(text: object) -> str:
    return str(text or "").strip().rstrip(".")


def _session_events(conn: sqlite3.Connection, state: dict) -> list[Event]:
    events = []
    rows = conn.execute(
        "SELECT * FROM sessions WHERE id > ? AND ended_at IS NOT NULL ORDER BY id",
        (state["session_id"],),
    ).fetchall()
    for row in rows:
        code = row["stop_code"] or ""
        reason = control.LAST.get(code, "") or row["stop_reason"] or ""
        counts = f"опубліковано {row['published']}, у Проблемах {row['problems']}"
        if code in CALM or (not code and not reason):
            tail = f" — {_sentence(reason).lower()}" if code in ("limit", "stop") else ""
            text = f"✅ Сесія {row['id']}: {counts}{tail}"
        else:
            text = (
                f"⛔ Робот зупинився: {_sentence(reason) or 'причина невідома — подивись екран'}."
            )
            text += f" Опубліковано {row['published']}, у Проблемах {row['problems']}."
        keyboard = (
            None if code in CALM or (not code and not reason) else actions.session_markup(row["id"])
        )
        events.append(Event(f"session:{row['id']}", text, _set("session_id", row["id"]), keyboard))
    return events


def _set(name: str, value: object) -> Callable[[dict], None]:
    def commit(state: dict) -> None:
        state[name] = value

    return commit


def _problem_events(conn: sqlite3.Connection, state: dict) -> list[Event]:
    seen: dict = state["problems"]
    now = _problems_now(conn)
    fresh = [i for i, code in now.items() if seen.get(i) != code]
    if not fresh:
        if now != seen:  # частина Проблем розв'язана — «новою» вона буде, якщо повернеться
            state["problems"] = now
        return []
    rows = conn.execute(
        "SELECT id, post_id, folder_name, problem_code, problem_text, shafa_id, shafa_url "
        f"FROM items WHERE id IN ({','.join('?' * len(fresh))}) ORDER BY id",
        [int(i) for i in fresh],
    ).fetchall()
    lines = [actions.card_text(row, LINE_MAX) for row in rows[:LIST_MAX]]
    more = len(rows) - len(lines)
    if len(rows) == 1:
        text = "⚠️ " + lines[0]
    else:
        text = f"⚠️ Нові Проблеми ({len(rows)}):\n" + "\n".join("• " + line for line in lines)
        text += f"\n…і ще {more}" if more else ""
        if len(rows) > actions.MULTI_MAX:
            text += f"\nКнопки — для перших {actions.MULTI_MAX}, решту — у програмі."
    album_of = None
    if len(rows) == 1 and rows[0]["problem_code"] == "possible_duplicate":
        album_of = rows[0]["id"]
    event = Event(
        "problems:" + ",".join(fresh), text, _set("problems", now), actions.markup(rows), album_of
    )
    return [event]


def _health_events(data_dir: Path, state: dict, moment: datetime) -> list[Event]:
    found = _health_file(data_dir)
    if found is None:
        return []
    at, checks = found
    known = _parse(state["health_at"])
    if known is not None and at <= known:
        return []
    sent: dict = state["health_sent"]
    due = []
    for check in _fails(checks):
        last = _parse(sent.get(str(check["name"])))
        if last is None or moment - last >= HEALTH_REPEAT:
            due.append(check)
    if not due:
        state["health_at"] = _stamp(at)
        return []
    lines = []
    for check in due:
        line = f"🔴 {check['name']}: {_sentence(check.get('text'))}."
        if fix := _sentence(check.get("fix")):
            line += f" {fix}."
        lines.append(line)

    def commit(state: dict) -> None:
        state["health_at"] = _stamp(at)
        state["health_sent"].update({str(c["name"]): _stamp(moment) for c in due})

    return [
        Event("health:" + ",".join(sorted(str(c["name"]) for c in due)), "\n".join(lines), commit)
    ]


def _budget_events(conn: sqlite3.Connection, state: dict, moment: datetime) -> list[Event]:
    day = moment.astimezone().date()
    if state["budget_day"] == day.isoformat() or not (high := _budget_high(conn, day)):
        return []
    spent, limit = high
    text = (
        f"💸 ШІ: сьогодні витрачено ${spent / 1_000_000:.2f} із ${limit / 1_000_000:.2f} "
        f"({spent * 100 // limit}%). Коли ліміт вичерпається, нові Картки зачекають до завтра."
    )
    return [Event(f"budget:{day}", text, _set("budget_day", day.isoformat()))]


def collect(conn: sqlite3.Connection, data_dir: Path, state: dict, moment: datetime) -> list[Event]:
    """Що нового від останнього разу. Може тихо оновити `state` (розв'язані Проблеми, самоперевірка
    без помилок); усе, про що треба сказати, — подіями."""
    return [
        *_session_events(conn, state),
        *_problem_events(conn, state),
        *_health_events(data_dir, state, moment),
        *_budget_events(conn, state, moment),
    ]


# --- Надсилання ---


def run(
    conn: sqlite3.Connection, data_dir: Path, moment: datetime, send: Callable[..., bool]
) -> int:
    """Один цикл. `send(text[, кнопки, Картка для фото])` каже, чи дійшло. Повертає, скільки повідомлень надіслано."""
    state = load(data_dir)
    if state is None:
        save(data_dir, snapshot(conn, data_dir, moment))
        return 0
    before = json.dumps(state, sort_keys=True)
    hour_ago = moment - SAME
    state["hour"] = [s for s in state["hour"] if (t := _parse(s)) and t > hour_ago]
    state["sent"] = {k: s for k, s in state["sent"].items() if (t := _parse(s)) and t > hour_ago}
    sent_count = overflow = 0
    for event in collect(conn, data_dir, state, moment):
        if event.key in state["sent"]:  # те саме щойно було: лише позначаємо опрацьованим
            event.commit(state)
        elif len(state["hour"]) >= PER_HOUR - 1:
            overflow += 1
            event.commit(state)
        else:
            text = report.clip(report.scrub(event.text))
            extra = (event.markup, event.album_of) if event.markup or event.album_of else ()
            if not send(text, *extra):
                break  # Telegram не відповів: решта — наступним циклом
            event.commit(state)
            state["sent"][event.key] = _stamp(moment)
            state["hour"].append(_stamp(moment))
            sent_count += 1
    if overflow and send(f"ℹ️ Ще {overflow} подій — подивись /zvit."):
        state["hour"].append(_stamp(moment))
        sent_count += 1
    if json.dumps(state, sort_keys=True) != before:
        save(data_dir, state)
    return sent_count
