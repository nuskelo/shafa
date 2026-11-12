"""KLB-009: панель робота Клубку на екрані — стан, кнопки, «зараз», підсумок останньої сесії.

Та сама будова, що й `control.state` для Шафи: замок робота (`lock.busy`: живий процес і що він
робить) + `sessions` з `site = 'klubok'` + `klubok-now.json` (`klubok_publish.write_now`). Замок
спільний: поки працює будь-який робот, «Старт» обох сайтів недоступний. `--human-click` з екрана
немає. Лише читання — нічого не пише.
"""

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shafa import config
from shafa.klubok import queue, snapshot
from shafa.robot import control, klubok_publish, lock

LOG_NAME = "klubok-run.log"
SHAFA_MODES = ("publish", "rehearse", "login", "dry-fill", "sync-catalog")  # `__main__.robot`

TEXTS = {
    "stopped": "Робот Клубку чекає",
    "off": "Клубок вимкнено в Налаштуваннях",
    "starting": "Робот Клубку запускається — відкривається вікно Chrome (до хвилини)",
    "running": "Робот виставляє пари на Клубок",
    "pausing": "Пауза: робот дописує поточну пару",
    "paused": "Робот Клубку на паузі — нічого не робить. «Продовжити» або «Стоп».",
    "stopping": "Стоп: робот дописує поточну пару й завершує",
    "login_open": "Відкрито вікно входу в Клубок — увійдіть і закрийте вікно (або кнопка нижче)",
    "snapshot": "Робот перечитує ваші оголошення на Клубку (кілька хвилин)",
    "busy_shafa": "Зараз працює робот Шафи — «Старт» Клубку стане доступний, коли він закінчить",
    "busy": "Робот зайнятий іншою справою (Власник щось перевіряє)",
    "crashed": "Робот Клубку зупинився посеред роботи. Відкрийте «Треба рішення», потім «Старт». "
    "Повториться — скажіть Власнику.",
    "failed": "Робот Клубку зупинився з помилкою — подробиці в «Детальніше». "
    "Повториться — скажіть Власнику.",
}

# Як закінчилась сесія — простими словами (решта — `klubok_publish.STOPS` / `queue.REASONS`)
LAST = {
    klubok_publish.DONE: "виставлено, скільки можна було",
    klubok_publish.STOPPED: "зупинено кнопкою «Стоп»",
    klubok_publish.LOGIN: "робот вийшов із Клубку — натисніть «Увійти в Клубок»",
    klubok_publish.CRASH: "робот зупинився посеред роботи",
    queue.EMPTY: "черга закінчилась",
    queue.LIMIT_REACHED: "денний ліміт Клубку вичерпано",
    snapshot.FAILED: "не вдалось перечитати ваші оголошення на Клубку — «Старт» ще раз пізніше",
    snapshot.TOO_MANY: "Клубок попросив зачекати (429) — «Старт» через годину",
}

WORKING = ("running", "pausing", "paused", "stopping")
IDLE = ("stopped", "off", "crashed", "failed")


@dataclass
class State:
    code: str
    text: str
    can_start: bool
    why_not: str = ""
    buttons: list[str] = field(default_factory=list)
    enabled: bool = False
    day_limit: int = 0
    started: int = 0  # спроб сьогодні (рахуються в ліміт доби)
    left: int = 0  # скільки ще можна сьогодні → `--limit`
    queued: int = 0  # рядків «у черзі»
    unsynced: int = 0  # схвалено, але ще не перераховано
    progress: str = ""  # «Виставлено 2 з 8 у цій сесії»
    doing: str = ""  # «Зараз: пост 7233 «…» — заповнює форму» / «Пауза між парами — …»
    previous: str = ""  # «Останній: пост 7233 — виставлено ✓»
    last: str = ""  # підсумок останньої сесії
    last_session: int | None = None
    listings: str = ""  # «Ваших оголошень на Клубку: 1787 · перечитано сьогодні о 10:00»
    mini: str = ""
    details: list[str] = field(default_factory=list)


def log_path() -> Path:
    return config.data_dir() / LOG_NAME


def reason_of(code: str | None, reason: str | None) -> str:
    return (
        LAST.get(code or "")
        or klubok_publish.STOPS.get(code or "")
        or queue.REASONS.get(code or "")
        or reason
        or ""
    )


def last_run(row, now: datetime) -> str:
    """«Сесія 63 о 12:07: виставлено 4, у «Треба рішення» 1 — черга закінчилась»."""
    started = control._stamp(row["started_at"], now)
    when = (
        f"о {started:%H:%M}"
        if started is not None and started.date() == now.date()
        else control._when(started, now)
        if started is not None
        else ""
    )
    text = f"Сесія {row['id']} {when}".rstrip() + f": виставлено {row['published'] or 0}"
    if row["problems"]:
        text += f", у «Треба рішення» {row['problems']}"
    reason = reason_of(row["stop_code"], row["stop_reason"])
    return text + (f" — {reason}" if reason else "")


def listings_line(conn: sqlite3.Connection, now: datetime) -> str:
    row = conn.execute("SELECT read_at, count FROM klubok_listings_read WHERE id = 1").fetchone()
    if row is None:
        return "Ваші оголошення на Клубку ще не перечитано — натисніть «Перечитати мої оголошення»"
    at = control._stamp(row["read_at"], now)
    when = control._when(at, now) if at is not None else row["read_at"]
    return f"Ваших оголошень на Клубку: {row['count']} · перечитано {when}"


def _stage(data: dict, now: datetime) -> tuple[str, str]:
    stage, post = data.get("stage"), data.get("post")
    title = data.get("title") or ""
    name = f"пост {post}" if post else "пара"
    if title:
        name += f" «{title[:40]}{'…' if len(title) > 40 else ''}»"
    doing = {
        "start": "Зараз: відкриває Клубок і перечитує ваші оголошення (щоб не було дублів)",
        "fill": f"Зараз: {name} — заповнює форму",
        "submit": f"Зараз: {name} — натискає «Опубликовать»",
    }.get(stage or "", "")
    if stage == "pause":
        try:
            left = math.ceil((float(data.get("until") or 0) - now.timestamp()) / 60)
        except (TypeError, ValueError):
            left = 0
        doing = "Пауза між парами (як людина) — наступна " + (
            f"за ~{left} хв" if left >= 1 else "зовсім скоро"
        )
    last, previous = data.get("last"), ""
    if isinstance(last, dict) and last.get("post"):
        previous = f"Останній: пост {last['post']} — " + (
            "виставлено ✓" if last.get("ok") else "у «Треба рішення»"
        )
    return doing, previous


def state(
    conn: sqlite3.Connection,
    lock_file: Path | None = None,
    now: datetime | None = None,
    *,
    starting: bool = False,
    failed: bool = False,
    spawned: datetime | None = None,
    now_file: Path | None = None,
) -> State:
    """Що зараз з роботом Клубку й які кнопки доступні. `spawned` — коли екран запустив процес."""
    now = now or datetime.now().astimezone()
    busy = lock.busy(lock_file)
    mode = (busy or {}).get("mode")
    open_session = conn.execute(
        "SELECT * FROM sessions WHERE site = 'klubok' AND ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last = conn.execute(
        "SELECT * FROM sessions WHERE site = 'klubok' AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    enabled = queue.enabled()
    setup = ""
    try:
        day_limit = queue.daily_limit()
        unsynced = queue.unsynced(conn)
    except queue.SetupError as error:
        day_limit, unsynced, setup = 0, 0, str(error)
    started = queue.started_today(conn, now)
    left = max(day_limit - started, 0)
    queued = conn.execute(
        "SELECT count(*) FROM klubok_ads WHERE state = ?", (queue.QUEUED,)
    ).fetchone()[0]

    newest = conn.execute(
        "SELECT started_at FROM sessions WHERE site = 'klubok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    began = control._stamp(newest[0], now) if newest else None
    after_spawn = (
        spawned is not None and began is not None and began >= spawned.astimezone(now.tzinfo)
    )
    data = klubok_publish.read_now(open_session["id"], now_file) if open_session else {}
    command = open_session["command"] if open_session else None

    if mode == "klubok-publish":
        if open_session is None:
            code = "starting"
        elif command == "stop":
            code = "stopping"
        elif command == "pause":
            code = "paused" if data.get("stage") in ("paused", "pause", None) else "pausing"
        else:
            code = "running"
    elif mode == "klubok-login":
        code = "login_open"
    elif mode == "klubok-snapshot":
        code = "snapshot"
    elif mode in SHAFA_MODES:
        code = "busy_shafa"
    elif busy:
        code = "busy"
    elif starting and not after_spawn:
        code = "starting"
    elif open_session:
        code = "crashed"  # сесія відкрита, а замок вільний — процес помер
    elif failed and not after_spawn:
        code = "failed"  # процес помер до сесії (вхід, знімок, налаштування)
    elif not enabled:
        code = "off"
    else:
        code = "stopped"

    idle = code in IDLE
    why_not = ""
    if code == "off":
        why_not = "Увімкніть Клубок у Налаштуваннях (блок «Клубок»), потім «Старт»."
    elif idle and setup:
        why_not = f"Налаштування Клубку з помилкою: {setup} — Налаштування, блок «Клубок»."
    elif idle and not enabled:
        why_not = "Увімкніть Клубок у Налаштуваннях (блок «Клубок»), потім «Старт»."
    elif idle and left <= 0:
        why_not = (
            f"На сьогодні все: почато {started} з {day_limit} (ліміт Клубку на добу). "
            "Завтра після 00:00 натисніть «Старт»."
        )
    elif idle and not (queued or unsynced):
        why_not = "У черзі Клубку нічого — схваліть Картки у «На перевірку»."
    can_start = idle and not why_not

    buttons: list[str] = []
    if idle:
        buttons += ["start", "login", "snapshot"]
    if code == "running":
        buttons += ["pause", "stop"]
    if code in ("pausing", "paused"):
        buttons += ["resume", "stop"]
    if code == "login_open":
        buttons.append("login_done")

    progress = doing = previous = mini = ""
    if code in WORKING and open_session is not None:
        done = conn.execute(
            "SELECT count(*) FROM klubok_ads WHERE session_id = ? AND state = ?",
            (open_session["id"], queue.PUBLISHED),
        ).fetchone()[0]
        plan = data.get("limit")
        progress = f"Виставлено {done}" + (f" з {plan}" if plan else "") + " у цій сесії"
        mini = f"Клубок: {done}" + (f" з {plan}" if plan else "")
        if code != "paused":
            doing, previous = _stage(data, now)
        else:
            previous = _stage(data, now)[1]
            mini += " · на паузі"

    text = TEXTS[code]
    if code == "busy_shafa" and mode == "rehearse":
        text = "Зараз йде пробна перевірка Шафи — «Старт» Клубку стане доступний після неї"
    return State(
        code=code, text=text, can_start=can_start, why_not=why_not, buttons=buttons,
        enabled=enabled, day_limit=day_limit, started=started, left=left, queued=queued,
        unsynced=unsynced, progress=progress, doing=doing, previous=previous,
        last=last_run(last, now) if last is not None else "",
        last_session=last["id"] if last is not None else None,
        listings=listings_line(conn, now), mini=mini or TEXTS[code],
    )  # fmt: skip
