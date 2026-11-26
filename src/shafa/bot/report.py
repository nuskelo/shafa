"""Щоденний звіт Власнику в Telegram (BOT-009): що зроблено, що зламалось, що чекає його.

`build` лише читає базу й файли теки даних і нічого не надсилає; коли слати — `due`, позначка
«за яку добу вже надіслано» — файл `report-last.json` у теці даних. Секретів, chat id і шляхів у
тексті немає (`scrub`), довжина — до одного повідомлення Telegram.
"""

import json
import os
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

from shafa import batch, borh, changes, config, hints
from shafa.db import Status
from shafa.robot import rehearse

SETTING = "SHAFA_REPORT_AT"  # «21:00»; порожнє чи «off» — звіту немає; не задано — 21:00
DEFAULT_AT = "21:00"
LAST_NAME = "report-last.json"
HEALTH_NAME = "health-last.json"  # пише `health.run_all`
RETRY = timedelta(minutes=10)  # після помилки Telegram — не частіше
HEALTH_STALE = timedelta(hours=36)  # самоперевірка давніша — «все гаразд» уже нічого не каже
LIMIT = 4000  # з запасом до 4096 символів повідомлення Telegram
TOP = 5
SECRETS = (
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",
    "SITE_INTEGRATION_KEY",
    "TELEGRAM_OWNER_CHAT_ID",
    "TELEGRAM_SELLER_CHAT_ID",
    "TELEGRAM_CHANNEL_ID",
    "TELEGRAM_BOT_CHANNEL_ID",
)

_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\\S*")
_UNIX_PATH = re.compile(r"(?<![\w/])~?(?:/[^\s/]+){2,}")
_LONG_NUMBER = re.compile(r"-?\d{7,}")  # chat id, id каналу


def problem_name(code: str | None) -> str:
    if not code:
        return "Без коду"
    if hint := hints.problem(code) or hints.problem(
        code, "klubok"
    ):  # BOT-019: назви — з довідника, як на екрані
        return hint.title
    return code if re.fullmatch(r"[a-z0-9_]+", code) else "Інше"


# --- Доба ---


def _on(stamp: object, day: date) -> bool:
    """Чи припадає мітка часу на місцеву добу `day` (як ліміт доби робота)."""
    try:
        return datetime.fromisoformat(str(stamp)).astimezone().date() == day
    except ValueError:
        return False


def _since(day: date) -> str:
    """Відсікання в SQL із запасом на пояси: точний відбір — `_on`."""
    return (day - timedelta(days=2)).isoformat()


# --- Рядки звіту ---


def _shafa(conn: sqlite3.Connection, day: date) -> str:
    published = sum(
        1
        for (stamp,) in conn.execute(
            "SELECT published_at FROM items WHERE published_at >= ?", (_since(day),)
        )
        if _on(stamp, day)
    )
    sessions = [
        r
        for r in conn.execute(
            "SELECT * FROM sessions WHERE started_at >= ? AND site = 'shafa' ORDER BY id",
            (_since(day),),
        )
        if _on(r["started_at"], day)
    ]
    line = f"Шафа: опубліковано {published} (сесій {len(sessions)})"
    if not sessions:
        return line
    last = sessions[-1]
    if last["ended_at"] is None:
        return line + "; робот ще працює"
    hint = hints.stop("shafa", last["stop_code"])
    reason = (hint.what if hint else "") or last["stop_reason"] or ""
    return f"{line}; зупинка: {reason}" if reason else line


def _problems(conn: sqlite3.Connection, day: date) -> str:
    codes = Counter(
        code
        for code, stamp in conn.execute(
            "SELECT problem_code, updated_at FROM items WHERE status = ? AND updated_at >= ? "
            "AND only_klubok = 0",
            (Status.PROBLEM, _since(day)),
        )
        if _on(stamp, day)
    )
    if not codes:
        return ""
    names: Counter[str] = Counter()
    for code, count in codes.items():
        names[problem_name(code)] += count
    top = ", ".join(f"{name} ×{count}" for name, count in names.most_common(TOP))
    return f"Проблеми за день: {sum(codes.values())} ({top})"


def _waiting(conn: sqlite3.Connection) -> str:
    def count(status: Status) -> int:
        return conn.execute(
            "SELECT count(*) FROM items WHERE status = ? AND only_klubok = 0", (status,)
        ).fetchone()[0]

    parts = []
    if review := count(Status.REVIEW):
        parts.append(f"На перевірку {review}")
    if approved := count(Status.APPROVED):
        parts.append(f"Схвалені {approved} (у черзі на завтра)")
    return "Чекають тебе: " + " · ".join(parts) if parts else ""


def _changed(data_dir: Path, day: date) -> str:
    """CORE-015: пост змінили вже після публікації на Шафі — там правимо вручну."""
    found = changes.published_on(data_dir, day)
    if not found:
        return ""
    lines = [
        f"Пост {number} змінено після публікації на Шафі: {what}" for number, what in found[:5]
    ]
    if len(found) > 5:
        lines.append(f"…і ще {len(found) - 5}")
    return "\n".join(lines)


def _purged(data_dir: Path, day: date) -> str:
    """PHOTO-001: фото старих постів, прибрані за добу (через 3 дні після публікації)."""
    count = borh.purged_on(data_dir, day)
    if count <= 0:
        return ""
    if count % 10 == 1 and count % 100 != 11:
        word = "пост"
    elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        word = "пости"
    else:
        word = "постів"
    return f"Прибрано фото: {count} {word}"


def _rehearsal(data_dir: Path, day: date) -> str:
    last = rehearse.last_summary(data_dir)
    if last is None or not _on(last.get("at"), day):
        return ""
    checked, problems = last.get("checked") or 0, last.get("problems") or 0
    return f"Пробний прогін: перевірено {checked}, Проблем {problems}"


def _bot_site(conn: sqlite3.Connection, day: date) -> str:
    posts = sum(
        1
        for (stamp,) in conn.execute(
            "SELECT added_at FROM posts WHERE source = 'bot' AND added_at >= ?", (_since(day),)
        )
        if _on(stamp, day)
    )
    sent = failed = 0
    for state, stamp in conn.execute(
        "SELECT state, updated_at FROM site_jobs WHERE state IN ('done', 'failed') "
        "AND updated_at >= ?",
        (_since(day),),
    ):
        if _on(stamp, day):
            sent, failed = sent + (state == "done"), failed + (state == "failed")
    parts = []
    if posts:
        parts.append(f"нових постів {posts}")
    site = ", ".join(
        part for part in (sent and f"надіслано {sent}", failed and f"помилок {failed}") if part
    )
    if site:
        parts.append(f"сайт: {site}")
    return "Бот: " + "; ".join(parts) if parts else ""


def _spent(conn: sqlite3.Connection, day: date) -> int:
    """Мікродолари, фактично витрачені за добу (без оцінки ще не забраних пакетів)."""
    return sum(
        cost
        for stamp, cost in conn.execute(
            "SELECT created_at, cost_micro_usd FROM ai_requests WHERE created_at >= ?",
            (_since(day),),
        )
        if _on(stamp, day)
    )


def _ai(conn: sqlite3.Connection, day: date) -> str:
    spent = _spent(conn, day)
    if not spent:
        return ""
    try:
        limit = f" із ${batch.limits().daily_micro_usd / 1_000_000:.2f}"
    except Exception:  # noqa: BLE001 — неправильне налаштування ліміту звіт не ламає
        limit = ""
    return f"ШІ: ${spent / 1_000_000:.2f}{limit}"


def _health(data_dir: Path) -> str:
    try:
        data = json.loads((data_dir / HEALTH_NAME).read_text(encoding="utf-8"))
        checks = data["checks"]
        at = datetime.fromisoformat(data["at"]).astimezone()
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if datetime.now().astimezone() - at > HEALTH_STALE:
        return "Самоперевірка: давно не виконувалась"
    bad = [c for c in checks if isinstance(c, dict) and c.get("status") in ("fail", "warn")]
    if not bad:
        return "Самоперевірка: все гаразд"
    bad.sort(key=lambda c: c["status"] != "fail")  # спершу помилки, потім уваги
    shown = "; ".join(f"{c.get('name')}: {c.get('text')}" for c in bad[:2])
    return f"Самоперевірка: ⚠️ {shown}"


# --- Текст ---


def scrub(text: str) -> str:
    """Ні значень секретів, ні chat id, ні шляхів: навіть якщо вони потрапили в чужий текст."""
    for name in SECRETS:
        value = os.environ.get(name) or config.read_env_file(config.env_file()).get(name)
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    text = _WINDOWS_PATH.sub("…", text)
    text = _UNIX_PATH.sub("…", text)
    return _LONG_NUMBER.sub("…", text)


def clip(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build(conn: sqlite3.Connection, data_dir: Path, day: date, *, late: bool = False) -> str:
    """Текст звіту за місцеву добу `day` (`late` — це вже не сьогодні: «за вчора»). Порожні рядки
    пропущено."""
    head = f"📊 Звіт за {day:%d.%m}" + (" (за вчора)" if late else "")
    lines = [
        head,
        _shafa(conn, day),
        _problems(conn, day),
        _waiting(conn),
        _changed(data_dir, day),
        _purged(data_dir, day),
        _rehearsal(data_dir, day),
        _bot_site(conn, day),
        _ai(conn, day),
        _health(data_dir),
    ]
    return clip(scrub("\n".join(line for line in lines if line)))


# --- Коли слати ---


def at_time(log=None) -> time | None:
    """Година звіту. Не задано — 21:00; порожнє, «off» чи неправильне — звіту немає."""
    if SETTING in os.environ:
        raw = os.environ[SETTING]
    else:
        raw = config.read_env_file(config.env_file()).get(SETTING, DEFAULT_AT)
    at = rehearse.parse_at(raw)
    if at is None and raw.strip() and raw.strip().lower() != "off" and log is not None:
        log(f"{SETTING}={raw!r}: потрібен час «ГГ:ХХ» або off — звіту немає")
    return at


def due(now: datetime, last: date | None, at: time) -> date | None:
    """За яку добу слати зараз. Пропущена вчорашня — «за вчора» один раз; інакше сьогоднішня, коли
    настав час. Вчорашню наздоганяємо, лише якщо вже щось надсилали (перший запуск не спамить)."""
    local = now.astimezone()
    day = local.date()
    yesterday = day - timedelta(days=1)
    if last is not None and last < yesterday:
        return yesterday
    if local.time() >= at and (last is None or last < day):
        return day
    return None


def last_sent(data_dir: Path) -> date | None:
    try:
        return date.fromisoformat(
            json.loads((data_dir / LAST_NAME).read_text(encoding="utf-8"))["day"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mark_sent(data_dir: Path, day: date, at: datetime) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    temp = data_dir / (LAST_NAME + ".tmp")
    payload = {"day": day.isoformat(), "sent_at": at.isoformat(timespec="seconds")}
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(data_dir / LAST_NAME)
