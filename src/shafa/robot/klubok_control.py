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
LOGIN_NAME = (
    "klubok-login-at"  # KLB-010: коли вікно входу в Клубок закрито (як `control.LOGIN_NAME`)
)
MINUTES_PER_PAIR = 3  # форма ≈ 1 хв + пауза 1–3 хв між парами — для «≈ N хв» на панелі
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
    "crashed": "Робот Клубку зупинився посеред роботи. Відкрийте «Проблеми», потім «Старт». "
    "Повториться — скажіть Власнику.",
    "login_needed": "Робот не увійшов у Клубок — потрібно увійти",
    "failed": "Робот Клубку зупинився з помилкою — подробиці в «Детальніше». "
    "Повториться — скажіть Власнику.",
}

# Коротко — для верху Картки (KLB-010: як «Робот чекає · ШІ · Бот» у Шафи)
SHORT = {  # на сторінках Клубку «Клубку» зайве — повний текст у підказці (title)
    "stopped": "Робот чекає",
    "off": "Клубок вимкнено",
    "starting": "Робот запускається",
    "running": "Робот працює",
    "pausing": "Робот: пауза",
    "paused": "Робот на паузі",
    "stopping": "Робот: стоп",
    "login_open": "Відкрито вхід",
    "login_needed": "Робот: треба увійти",
    "snapshot": "Перечитує оголошення",
    "busy_shafa": "Працює робот Шафи",
    "busy": "Робот зайнятий",
    "crashed": "Робот зупинився",
    "failed": "Робот: помилка",
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
IDLE = ("stopped", "off", "crashed", "failed", "login_needed")
CRASHED_LIMIT = (  # N9: ліміт доби вичерпано — «Старт» сьогодні не допоможе
    "Робот Клубку зупинився посеред роботи — відкрийте «Проблеми». На сьогодні ліміт вичерпано: "
    "завтра після 00:00 натисніть «Старт». Повториться — скажіть Власнику."
)
LOGIN_HELP = (
    "Робот вийшов із Клубку. Натисніть «Увійти в Клубок»: відкриється вікно Chrome — увійдіть, як "
    "завжди, і закрийте це вікно. Потім натисніть «Старт»."
)


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
    queued: int = 0  # рядків «у черзі» (Схвалені Клубку)
    unsynced: int = 0  # схвалено, але ще не перераховано (KLB-010: на екрані не показується)
    waves: dict[int, int] = field(default_factory=dict)  # у черзі за хвилями: {1: 120, 2: 25}
    shown_started: int = 0  # «N з M» — ніколи не більше ліміту (зайве — у «Детальніше»)
    today_plan: int = 0  # «Сьогодні виставить N пар»
    eta: str = ""  # «≈ 30 хв»
    progress: str = ""  # «Виставлено 2 з 8 у цій сесії»
    progress_done: int = 0  # виставлено в цій сесії (плитка «Сьогодні виставить»)
    doing: str = ""  # «Зараз: пост 7233 «…» — заповнює форму» / «Пауза між парами — …»
    previous: str = ""  # «Останній: пост 7233 — виставлено ✓»
    last: str = ""  # підсумок останньої сесії
    last_session: int | None = None
    listings: str = ""  # «Ваших оголошень на Клубку: 1787 · перечитано сьогодні о 10:00»
    mini: str = ""
    details: list[str] = field(default_factory=list)


def log_path() -> Path:
    return config.data_dir() / LOG_NAME


def mark_login(now: datetime | None = None) -> None:
    """Вікно входу в Клубок закрито: «треба увійти» знімається (KLB-010, як `control.mark_login`)."""
    path = config.data_dir() / LOGIN_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((now or datetime.now().astimezone()).isoformat(), encoding="utf-8")


def _login_at() -> datetime | None:
    try:
        text = (config.data_dir() / LOGIN_NAME).read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(text)
    except (OSError, ValueError):
        return None


def eta_text(pairs: int) -> str:
    minutes = pairs * MINUTES_PER_PAIR
    if minutes < 60:
        return f"≈ {minutes} хв"
    hours, rest = divmod(minutes, 60)
    return f"≈ {hours} год" + (f" {rest} хв" if rest else "")


def reason_of(code: str | None, reason: str | None) -> str:
    return (
        LAST.get(code or "")
        or klubok_publish.STOPS.get(code or "")
        or queue.REASONS.get(code or "")
        or reason
        or ""
    )


def last_run(row, now: datetime) -> str:
    """«сьогодні о 12:07 — виставлено 4, у «Проблемах» 1 — черга закінчилась» (як у Шафи)."""
    started = control._stamp(row["started_at"], now)
    when = (
        f"о {started:%H:%M}"
        if started is not None and started.date() == now.date()
        else control._when(started, now)
        if started is not None
        else ""
    )
    if when.startswith("о "):
        when = "сьогодні " + when
    text = (f"{when} — " if when else "") + f"виставлено {row['published'] or 0}"
    if row["problems"]:
        text += f", у «Проблемах» {row['problems']}"
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
            "виставлено ✓" if last.get("ok") else "у «Проблемах»"
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
    elif (
        last is not None
        and last["stop_code"] == klubok_publish.LOGIN
        and not (
            (at := _login_at()) is not None and at >= (control._stamp(last["ended_at"], now) or at)
        )
    ):
        code = "login_needed"  # KLB-010 (F4): жовте «треба увійти», як у Шафи
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
    elif code == "login_needed":
        why_not = "Спершу увійдіть у Клубок: натисніть «Увійти в Клубок»"
    elif idle and left <= 0:
        why_not = (
            f"На сьогодні все: почато {min(started, day_limit)} з {day_limit} (ліміт Клубку на "
            "добу). Завтра після 00:00 натисніть «Старт»."
        )
    elif idle and not (queued or unsynced):
        why_not = "Схвалених для Клубку немає — схваліть пари у «На перевірку»."
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
    done = 0
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
    if code == "crashed" and left <= 0:
        text = CRASHED_LIMIT
    details = []
    if started > day_limit:
        details.append(f"Почато сьогодні {started} спроб — на {started - day_limit} більше за ліміт "
                       f"{day_limit} (ліміт змінили під час дня або спроба не дійшла).")  # fmt: skip
    if code == "login_needed":
        details.append(LOGIN_HELP)
    by_wave = dict(
        conn.execute(
            f"SELECT {queue._WAVE} AS wave, count(*) FROM klubok_ads k JOIN items i ON i.id = "
            "k.item_id LEFT JOIN klubok_waves w ON w.post_id = i.post_id WHERE k.state = ? "
            "GROUP BY wave ORDER BY wave",
            (queue.QUEUED,),
        ).fetchall()
    )
    plan_today = min(left, queued) if enabled else 0
    if code == "busy_shafa" and mode == "rehearse":
        text = "Зараз йде пробна перевірка Шафи — «Старт» Клубку стане доступний після неї"
    return State(
        code=code, text=text, can_start=can_start, why_not=why_not, buttons=buttons,
        enabled=enabled, day_limit=day_limit, started=started, left=left, queued=queued,
        unsynced=unsynced, progress=progress, progress_done=done, doing=doing, previous=previous,
        last=last_run(last, now) if last is not None else "",
        last_session=last["id"] if last is not None else None,
        listings=listings_line(conn, now), mini=mini or SHORT[code], details=details,
        waves=by_wave, shown_started=min(started, day_limit) if day_limit else started,
        today_plan=plan_today, eta=eta_text(plan_today) if plan_today else "",
    )  # fmt: skip
