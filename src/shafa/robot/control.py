"""Керування роботом з екрана (UI-002): стан, запуск окремим процесом, Пауза/Стоп.

Стан — із замка (`lock.busy`: чи живий процес робота і що він робить) і таблиці `sessions`.
Робот запускається окремим процесом `python -m shafa robot …`: закрили вкладку чи навіть сервер
екрана — робот працює далі. Mac: власна сесія процесу (Ctrl+C у вікні сервера його не перерве);
Windows: без вікна консолі й у своїй групі процесів.
"""

import json
import math
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shafa import config
from shafa.robot import listings, lock, login_check, publish, rehearse

ITEM_MINUTES = 4  # орієнтовно: стільки триває один товар (форма + фото + клік)
POST_MINUTES = 3  # орієнтовно: стільки хвилин на пост разом із паузою, поки нема власних вимірів
PACE_POSTS = 3  # UX-009: темп сесії рахуємо від першої Картки й лише після стількох Карток
LOG_NAME = "robot-run.log"
PAUSE_NAME = "robot-pause.json"  # UX-007: скільки сесія простояла на паузі (не рахується в «ще ≈»)
LOGIN_NAME = "login-last"  # UX-002: коли Власник/Продавчиня востаннє закривала вікно входу

TEXTS = {
    "stopped": "Робот чекає",
    "starting": "Робот запускається — відкривається вікно Chrome (до хвилини)",
    "running": "Робот публікує",
    "pausing": "Пауза: чекаю, поки допишеться поточний товар",
    "paused": "Робот на паузі — нічого не робить. «Продовжити» або «Стоп».",
    "stopping": "Стоп: чекаю, поки допишеться поточний товар",
    "login_needed": "Робот не увійшов на Шафу — потрібно увійти",
    "checking": "Перевіряю вхід на Шафу… за кілька секунд робот запуститься сам",
    "reading": "Перечитую ваші оголошення на Шафі, щоб не виставити дубль",  # UX-009 (+ лічильник)
    "waiting_reading": "Читання оголошень ще йде — робот почне публікувати сам, щойно воно закінчиться",
    "login_open": "Відкрито вікно входу на Шафу — увійдіть і закрийте вікно (або кнопка нижче)",
    "rehearsing": "Йде пробна перевірка: форма заповнюється, але нічого не публікується",
    "busy": "Робот зайнятий іншою справою (Власник щось перевіряє)",
    "busy_klubok": "Зараз працює робот Клубку — «Старт» Шафи стане доступний, коли він закінчить",
    "crashed": "Робот зупинився посеред роботи. Натисніть «Старт», щоб продовжити. "
    "Повториться — скажіть Власнику.",
    "failed": "Робот не запустився. Натисніть «Старт» ще раз. Повториться — скажіть Власнику.",
}
CRASH_CLOSED = (
    "Вікно Chrome робота закрилось — натисніть «Старт» ще раз. "
    "Не закривайте це вікно, поки робот працює."
)
CRASH_TEXT = (
    "Робот зупинився посеред роботи{done}. Натисніть «Старт», щоб продовжити. "
    "Повториться — скажіть Власнику."
)
CRASH_LIMIT = (  # KLB-010 (N9): ліміт доби вичерпано — «Старт» сьогодні не допоможе
    "Робот зупинився посеред роботи{done}. На сьогодні ліміт вичерпано — завтра після 00:00 "
    "натисніть «Старт». Повториться — скажіть Власнику."
)

LAST = {  # причина зупинки останньої сесії — простими словами
    "done": "Черга закінчилась",
    "limit": "Денний ліміт вичерпано",
    "stop": "Зупинено кнопкою «Стоп»",
    "login": "Робот вийшов із Шафи",
    "no_form": "Шафа не показала форму «Додати товар» (сайт повільний чи змінився) — "
    "натисніть «Старт» ще раз; повториться — скажіть Власнику",
    "sleep": "Ноутбук засинав — робот зупинився",
    "site": "Шафа, мабуть, змінила форму — Картку в Проблемах, скажіть Власнику",
    "photos": "Не всі фото завантажились на форму — Картку в Проблемах, натисніть «Старт» ще раз; "
    "повториться — скажіть Власнику",
    "slow": "Шафа довго не відповідала — натисніть «Старт» через 15–30 хв",
    "telegram_busy": "Telegram не відповідає — спробуйте «Старт» за годину",
    "streak": "Кілька Карток поспіль не вдались — схоже, загальна причина; скажіть Власнику",
    "error": "Робот зупинився посеред роботи — натисніть «Старт», щоб продовжити; "
    "повториться — скажіть Власнику",
    "interrupted": "Робота перервано",
    "crash": "Робот зупинився посеред роботи — натисніть «Старт», щоб продовжити; "
    "повториться — скажіть Власнику",
}

# Що написати, коли «Старт» не спрацював (код — ключ у `web.MESSAGES`)
WHY_CODES = {
    "busy": "busy", "none": "none", "limit": "limit", "login": "login", "setup": "setup",
    "groups": "groups",
}  # fmt: skip


@dataclass
class State:
    code: str
    text: str
    published_today: int
    day_limit: int
    approved: int
    can_start: bool
    why_not: str = ""
    wait_minutes: int | None = None  # скільки ще дописувати поточний товар
    last: str = ""  # як закінчилась остання сесія (рядок із датою)
    buttons: list[str] = field(default_factory=list)
    # CORE-005: почато сьогодні (старих, нових) і межі на день
    started: publish.Groups = field(default_factory=lambda: publish.Groups(0, 0))
    limits: publish.Groups = field(default_factory=lambda: publish.Groups(0, 0))
    rehearsal: str = ""  # QA-003: підсумок останнього пробного прогону
    rehearsal_problems: int = 0
    session_published: int | None = None  # UI-004: скільки опубліковано в поточній сесії
    # UX-002
    why_code: str = ""  # ключ причини, чому «Старт» не спрацює (`WHY_CODES`)
    last_session: int | None = None  # номер останньої сесії — для посилання «Подробиці сесії»
    last_problems: int = 0  # скільки з неї пішло в Проблеми — для посилання «Відкрити Проблеми»
    idle_note: str = ""  # «Сьогодні робот ще не запускався — можна натискати «Старт»»
    queue_today: int = 0  # скільки Схвалених піде сьогодні (з огляду на ліміти)
    eta_text: str = ""  # «Опублікує до 19 сьогодні (≈ 1 год)»
    progress: str = ""  # «Публікує: 4 з 19 на сьогодні · працює 18 хв · ще ≈ 1 год 20 хв»
    doing: str = ""  # «Зараз: пост 6979 «…» — заповнює форму» / «Пауза між товарами…»
    previous: str = ""  # «Останній: пост 6393 — опубліковано ✓»
    mini: str = ""  # «Робот: 4 з 19 · ≈ 1 год» — для міні-панелі на Картці
    never_logged: bool = False  # профілю робота ще немає — показати «Увійти на Шафу»
    details: list[str] = field(default_factory=list)  # «Детальніше»: пояснення й сирі рядки
    # DES-002: ті самі розрахунки, що в eta_text і progress, але окремими значеннями для цифр панелі
    eta_cards: str = ""  # «5 Карток» — що опублікує сьогодні (як у eta_text)
    eta_span: str = ""  # «15 хв» — оцінка часу на ці Картки
    plan: int | None = None  # скільки Карток у плані поточної сесії
    handled: int = 0  # скільки з них оброблено
    left_text: str = ""  # «1 год 20 хв» — скільки ще; порожньо на паузі й коли менше хвилини
    login_ok: str = ""  # ROBOT-019: «Вхід є — можна «Старт»» після вікна входу й перевірки

    @property
    def groups_text(self) -> str:
        return "Сьогодні на Шафі: " + groups_line(self.started, self.limits)


def _groups_exhausted(rows, started, limits, after: str) -> tuple[str, str]:
    """R-A6: старі (чи нові) на сьогодні вичерпано, а Схвалених з іншої групи нема — так і кажемо."""
    has_old = any(publish.is_old(r["folder_name"]) for r in rows)
    has_new = any(not publish.is_old(r["folder_name"]) for r in rows)
    if has_old and not has_new:
        text = (
            f"Старі пости на сьогодні вичерпано ({started.old} з {limits.old}). "
            "Схвалених нових немає — решту робот опублікує завтра."
        )
        return text, "groups"
    if has_new and not has_old:
        text = (
            f"Нові пости на сьогодні вичерпано ({started.new} з {limits.new}). "
            "Схвалених старих немає — решту робот опублікує завтра."
        )
        return text, "groups"
    return f"На сьогодні все: {groups_line(started, limits)}. {after}", "limit"


def plural_posts(n: int) -> str:
    word = "публікацій"
    if n % 10 == 1 and n % 100 != 11:
        word = "публікацію"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "публікації"
    return f"{n} {word}"


def plural_cards(n: int) -> str:
    return f"{n} " + ("Картки" if n % 10 == 1 and n % 100 != 11 else "Карток")


def groups_line(started: publish.Groups, limits: publish.Groups) -> str:
    """«з минулих постів 0 з 20 · з нових (бот) 0 з 10» (A18)."""
    return f"з минулих постів {started.old} з {limits.old} · з нових (бот) {started.new} з {limits.new}"


def log_path() -> Path:
    return config.data_dir() / LOG_NAME


def login_path() -> Path:
    return config.data_dir() / LOGIN_NAME


def mark_login(now: datetime | None = None) -> None:
    """Вікно входу закрито: після цього «Старт» знову дозволений (`login_needed` знімається)."""
    path = login_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((now or datetime.now().astimezone()).isoformat(), encoding="utf-8")


def _login_at() -> datetime | None:
    try:
        return datetime.fromisoformat(login_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _login_lost(last: sqlite3.Row | None, now: datetime) -> bool:
    """Вхід на Шафу злетів: остання сесія зупинилась кодом `login` (і вікно входу після неї не
    закривали, а свіжіша перевірка не показала «вхід є») або рання перевірка (ROBOT-019) каже, що
    вхід злетів, і вікно входу після неї не закривали."""
    login_at = _login_at()
    if login_check.store.failed(login_at):
        return True
    if last is None or last["stop_code"] != "login":
        return False
    ended = _stamp(last["ended_at"], now)
    if login_at is not None and login_at >= (ended or login_at):
        return False
    found = login_check.store.read()
    return not (found is not None and found[1] and ended is not None and found[0] >= ended)


def login_ok_note(now: datetime) -> str:
    """«Вхід є — можна «Старт»» — коли перевірка після вікна входу щойно підтвердила вхід."""
    found = login_check.store.read()
    login_at = _login_at()
    if found is None or not found[1] or login_at is None or found[0] < login_at:
        return ""
    if now - found[0] > timedelta(hours=2):
        return ""
    return "Вхід є — можна «Старт»."


def pause_path() -> Path:
    return config.data_dir() / PAUSE_NAME


def _pause_record(session_id: int) -> dict:
    try:
        data = json.loads(pause_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("session") == session_id else {}


def mark_pause(session_id: int, paused: bool, now: datetime | None = None) -> None:
    """R-A2: «Пауза» / «Продовжити» / «Стоп» з екрана — запам'ятати, скільки сесія простояла на
    паузі, щоб оцінка «ще ≈» не росла від цього часу. Збій запису оцінку лише спотворить."""
    now = now or datetime.now().astimezone()
    record = _pause_record(session_id)
    total, since = float(record.get("total") or 0), record.get("since")
    if paused and not since:
        since = now.isoformat()
    elif not paused and since:
        try:
            total += max((now - datetime.fromisoformat(since)).total_seconds(), 0)
        except ValueError:
            pass
        since = None
    try:
        pause_path().parent.mkdir(parents=True, exist_ok=True)
        pause_path().write_text(
            json.dumps({"session": session_id, "total": total, "since": since}), encoding="utf-8"
        )
    except OSError:
        pass


def paused_minutes(session_id: int, now: datetime) -> float:
    """Скільки хвилин сесія простояла на паузі (разом з поточною, якщо вона триває)."""
    record = _pause_record(session_id)
    seconds = float(record.get("total") or 0)
    if record.get("since"):
        try:
            seconds += max((now - datetime.fromisoformat(record["since"])).total_seconds(), 0)
        except ValueError:
            pass
    return seconds / 60


def _minutes_left(started: str | None, now: datetime) -> int | None:
    """Скільки хвилин ще дописувати поточний товар; 0 — вже довше звичайного («ще трохи»)."""
    if not started:
        return None
    spent = (now - datetime.fromisoformat(started)).total_seconds() / 60
    return max(0, math.ceil(ITEM_MINUTES - spent))


def span(minutes: float, rough: bool = False) -> str:
    """«менше хвилини», «12 хв», «1 год 20 хв»; `rough` (оцінка «ще ≈») — від 10 хв до 5 хв."""
    if minutes < 1:
        return "менше хвилини"
    whole = int(5 * round(minutes / 5)) if rough and minutes >= 10 else round(minutes)
    hours, rest = divmod(max(whole, 1), 60)
    if not hours:
        return f"{rest} хв"
    return f"{hours} год" + (f" {rest} хв" if rest else "")


def _stamp(value: str | None, now: datetime) -> datetime | None:
    """Момент із бази (UTC-рядок) у місцевому часі `now`."""
    try:
        moment = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(now.tzinfo)


def _when(moment: datetime, now: datetime) -> str:
    """«сьогодні о 12:07», «вчора, 30.10 о 15:28», «28.10 о 15:28»."""
    clock = f"{moment:%H:%M}"
    days = (now.date() - moment.date()).days
    if days == 0:
        return f"сьогодні о {clock}"
    if days == 1:
        return f"вчора, {moment:%d.%m} о {clock}"
    return f"{moment:%d.%m} о {clock}"


def reason_of(code: str | None, reason: str | None) -> str:
    """Причина зупинки сесії простими словами (UX-002: одна на панель і «Сесії»)."""
    if code in ("error", "crash") and "TargetClosed" in (reason or ""):
        return CRASH_CLOSED
    return LAST.get(code or "", "") or (reason or "")


def session_stats(row) -> str:
    """«опубліковано 7, у Проблемах 11» — один підсумок для панелі й «Сесій» (A15)."""
    line = f"опубліковано {row['published'] or 0}"
    if row["problems"]:
        line += f", у Проблемах {row['problems']}"
    return line


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


def last_run(row, now: datetime, with_reason: bool = True) -> str:
    """Рядок про останню сесію. Сьогодні — «Сесія 45 о 12:07 (1 год 12 хв): опубліковано 19, у
    Проблемах 1»; раніше — з датою: «Останній запуск: вчора, 30.10 о 15:28 — опубліковано 13, …»."""
    started = _stamp(row["started_at"], now)
    ended = _stamp(row["ended_at"], now)
    reason = (
        ""
        if row["stop_code"] == "done" or not with_reason
        else _lower(reason_of(row["stop_code"], row["stop_reason"]))
    )
    if started is None:
        return f"Останній запуск: {session_stats(row)}" + (f", {reason}" if reason else "")
    if started.date() == now.date():
        text = f"Сесія {row['id']} о {started:%H:%M}"
        if ended is not None:
            text += f" ({span((ended - started).total_seconds() / 60)})"
        text += f": {session_stats(row)}"
        return text + (f", {reason}" if reason else "")
    if row["stop_code"] == "limit" and with_reason:
        reason = "денний ліміт того дня вичерпано"
    return f"Останній запуск: {_when(started, now)} — {session_stats(row)}" + (
        f", {reason}" if reason else ""
    )


def rehearsal_text(summary: dict | None, now: datetime) -> tuple[str, int]:
    """A11: «Пробна перевірка 28.10 (без публікації): 7 Карток — усе добре»; давніше 2 діб — немає."""
    if not summary:
        return "", 0
    at = _stamp(summary.get("at"), now)
    if at is None or now - at > timedelta(days=2):
        return "", 0
    checked, good = int(summary.get("checked") or 0), int(summary.get("good") or 0)
    problems = int(summary.get("problems") or 0)
    if checked == 0:
        return f"Пробна перевірка {at:%d.%m} (без публікації): Карток для перевірки не було", 0
    count = f"{checked} " + ("Картка" if checked == 1 else "Картки" if checked < 5 else "Карток")
    if good == checked and not problems:
        verdict = "усе добре"
    else:
        verdict = f"добре {good}"
        if problems:
            verdict += f", у Проблемах {problems}"
            if summary.get("codes"):
                verdict += f" ({summary['codes']})"
        if rest := checked - good - problems:
            verdict += f", з помилкою {rest}"
    return f"Пробна перевірка {at:%d.%m} (без публікації): {count} — {verdict}", problems


def reading_text(data: dict, now: datetime) -> str:
    """UX-009: «Перечитую ваші оголошення на Шафі…: 820 з ~1650 · ще ≈ 4 хв. Це раз на 7 днів.»
    `data` — `read`, `expected` (за минулим знімком; 0 — не знаємо), `since` (коли почали)."""
    read, expected = int(data.get("read") or 0), int(data.get("expected") or 0)
    text = TEXTS["reading"]
    if read > 0:
        text += f": {read} з ~{expected}" if expected > read else f": прочитано {read}"
    since = _stamp(data.get("since"), now)
    spent = max((now - since).total_seconds(), 0) if since else 0
    if read >= 30 and expected > read and spent >= 20:
        left = (expected - read) * spent / read / 60
        if left >= 1:
            text += f" · ще ≈ {span(left, rough=True)}"
    try:
        days = listings.refresh_days()
    except listings.SetupError:
        days = listings.DAYS_DEFAULT
    return text + f". Це раз на {days} {'день' if days == 1 else 'днів'}."


def _stage_lines(data: dict, now: datetime) -> tuple[str, str]:
    """Рядки «Зараз: …» і «Останній: …» за записом `robot-now.json`."""
    stage = data.get("stage")
    post = data.get("post")
    title = data.get("title") or ""
    name = f"пост {post}" if post else "товар"
    if title:
        name += f" «{title[:40]}{'…' if len(title) > 40 else ''}»"
    doing = {
        "start": "Зараз: відкриває Шафу і читає ваші товари (щоб не було дублів) — це кілька хвилин",
        "fill": f"Зараз: {name} — заповнює форму",
        "submit": f"Зараз: {name} — натискає «Додати річ»",
        "retry": "Шафа відповідає повільно — коротка пауза й ще одна спроба",
        "between": "Зараз: переходить до наступного поста",
    }.get(stage or "", "")
    if stage == "listings":
        doing = "Зараз: " + reading_text(data, now)
    if stage == "wait_channel":
        until = _stamp(data.get("until"), now)
        left = max(math.ceil((until - now).total_seconds() / 60), 1) if until else 1
        doing = (
            f"Telegram просить зачекати ~{left} хв — перевіряю, чи пост ще в каналі"
            if data.get("asked")
            else "Telegram не відповів — коротка пауза, потім перевірю ще раз, чи пост ще в каналі"
        )
    if stage == "pause":
        until = _stamp(data.get("until"), now)
        left = math.ceil((until - now).total_seconds() / 60) if until else 0
        doing = "Пауза між товарами (як людина) — наступний " + (
            f"за ~{left} хв" if left >= 1 else "зовсім скоро"
        )
    last = data.get("last")
    previous = ""
    if isinstance(last, dict) and last.get("post"):
        previous = f"Останній: пост {last['post']} — " + (
            "опубліковано ✓" if last.get("ok") else "потрапив у Проблеми"
        )
    return doing, previous


def state(
    conn: sqlite3.Connection,
    lock_file: Path | None = None,
    day_limit: int | None = None,
    now: datetime | None = None,
    starting: bool = False,
    failed: bool = False,
    spawned: datetime | None = None,
    never_logged: bool = False,
) -> State:
    """Що зараз з роботом і які кнопки доступні. `spawned` — коли екран запустив процес робота
    (щоб відрізнити «впав посеред роботи» від «не запустився»)."""
    now = now or datetime.now().astimezone()
    day_limit = day_limit or publish.daily_limit()
    busy = lock.busy(lock_file)
    open_session = conn.execute(
        "SELECT * FROM sessions WHERE site = 'shafa' AND ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last = conn.execute(
        "SELECT * FROM sessions WHERE site = 'shafa' AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    rows = publish.queue_rows(conn)
    approved = len(rows)
    today = publish.started_today(conn, now)
    started = publish.started_groups(conn, now)
    try:
        limits: publish.Groups | None = publish.group_limits()
    except publish.SetupError:
        limits = None  # робот на старті зупиниться з поясненням; екран не падає

    # A14: «запускається» — лише поки сесії, почато після запуску (відкритої чи вже закритої), немає
    if starting and spawned is not None:
        newest = conn.execute(
            "SELECT started_at FROM sessions WHERE site = 'shafa' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        opened = _stamp(newest[0], now) if newest else None
        if opened is not None and opened >= spawned.astimezone(now.tzinfo):
            starting = False
    # A19: процес завершився з помилкою після того, як сесія вже почалась, — «впав посеред роботи»
    crashed_after = bool(
        failed
        and last is not None
        and spawned is not None
        and (opened := _stamp(last["started_at"], now)) is not None
        and opened >= spawned.astimezone(now.tzinfo)
    )

    wait = None
    if busy and busy.get("mode") == "publish":
        command = open_session["command"] if open_session else None
        in_work = open_session["item_started_at"] if open_session else None
        if open_session is None:
            code = "starting"  # R-A1: замок є, сесії ще нема — Chrome відкривається
        elif command == "stop":
            code = "stopping"
        elif command == "pause":
            code = "pausing" if in_work else "paused"
        else:
            code = "running"
        if command and in_work:
            wait = _minutes_left(in_work, now)
    elif busy and busy.get("mode") == "login":
        code = "login_open"
    elif busy and busy.get("mode") == listings.LOCK_MODE:
        code = "reading"  # UX-009: фонове перечитування «Мої речі» — «Старт» дочекається його
    elif busy and busy.get("mode") == "rehearse":
        code = "rehearsing"
    elif busy and str(busy.get("mode") or "").startswith("klubok"):
        code = "busy_klubok"  # KLB-009: замок спільний — пояснюємо, хто його тримає
    elif busy:
        code = "busy"
    elif starting:
        code = "starting"
    elif open_session or crashed_after:
        code = "crashed"  # сесія відкрита, а замок вільний — процес помер
    elif failed:
        code = "failed"
    elif _login_lost(last, now):
        code = "login_needed"
    else:
        code = "stopped"

    idle = code in ("stopped", "login_needed", "crashed", "failed", "reading")
    why_not, why_code = "", ""
    after = "Завтра після 00:00 натисніть «Старт» ще раз."
    if idle and code == "login_needed":
        why_not, why_code = "Спершу увійдіть на Шафу: натисніть «Увійти на Шафу»", "login"
    elif idle and approved == 0:
        why_not = "Немає Схвалених — схваліть Картки у «На перевірку», потім «Старт»."
        why_code = "none"
    elif idle and today >= day_limit:
        why_not = (
            f"На сьогодні все: робот уже почав {plural_posts(today)}, а ліміт на добу — "
            f"{day_limit} (Налаштування). Завтра після 00:00 натисніть «Старт»."
        )  # R-A5
        why_code = "limit"
    elif idle and limits is None:
        why_not = "Межі старих і нових на день у налаштуваннях неправильні"
        why_code = "setup"
    elif idle and not any(publish.group_open(r["folder_name"], started, limits) for r in rows):
        why_not, why_code = _groups_exhausted(rows, started, limits, after)
    elif not idle:
        why_code = "busy"
    can_start = idle and not why_not

    # Скільки піде сьогодні (A12/Б3): Схвалені з огляду на ліміти доби й груп
    queue_today = 0
    if idle and approved and limits is not None:
        queue_today = len(publish.take(rows, started, limits, max(day_limit - today, 0)))

    buttons = []
    if idle:
        buttons.append("start")
        if approved and code != "reading":  # пробний прогін потребує замка — він у фонового читання
            buttons.append("rehearse")
    if code == "running":
        buttons += ["pause", "stop"]
    if code in ("pausing", "paused"):
        buttons += ["resume", "stop"]
    if code == "login_needed" or (idle and never_logged):
        buttons.append("login")
    if code == "login_open":
        buttons.append("login_done")

    published_in_session = None
    if code in ("running", "pausing", "paused", "stopping") and open_session:
        published_in_session = conn.execute(
            "SELECT count(*) FROM items WHERE session_id = ? AND status = 'published'",
            (open_session["id"],),
        ).fetchone()[0]

    data = (
        publish.read_now(open_session["id"])
        if open_session and code in ("running", "pausing", "paused", "stopping")
        else {}
    )
    doing, previous = _stage_lines(data, now) if data else ("", "")
    if code == "paused":
        doing = ""  # на паузі робот нічого не робить
    progress, mini = "", ""
    plan_n, handled_n, left_text = None, 0, ""
    if data.get("plan"):
        plan, handled = int(data["plan"]), int(data.get("handled") or 0)
        plan_n, handled_n = plan, handled
        began = _stamp(open_session["started_at"], now)
        elapsed = max((now - began).total_seconds() / 60, 0) if began else 0
        elapsed = max(elapsed - paused_minutes(open_session["id"], now), 0)  # R-A2
        per = POST_MINUTES  # UX-009: до PACE_POSTS Карток — типова оцінка
        first = _stamp(data.get("first_at"), now)
        if handled >= PACE_POSTS and first is not None:
            # темп — від початку першої Картки (етап «Мої речі» не рахується); пауза віднімається
            pace = (now - first).total_seconds() / 60 - paused_minutes(open_session["id"], now)
            if pace > 0:
                per = pace / handled
        left = max(plan - handled, 0) * per
        if code == "paused":  # R-A2: на паузі — без «працює» і «ще ≈»
            progress = f"Опубліковано {handled} з {plan} на сьогодні · на паузі"
            mini = f"Робот: {handled} з {plan} · на паузі"
        else:
            verb = "Готується" if data.get("stage") == "listings" else "Публікує"
            progress = f"{verb}: {handled} з {plan} на сьогодні · працює {span(elapsed)}"
            mini = f"Робот: {handled} з {plan}"
            if left >= 1:
                left_text = span(left, rough=True)
                progress += f" · ще ≈ {left_text}"
                mini += f" · ≈ {left_text}"

    text = TEXTS[code]
    if code == "reading":
        text = reading_text(listings.read_state(listings.BG_NOW), now)
    if code in ("pausing", "stopping"):
        post = data.get("post") if data.get("stage") in ("fill", "submit") else None
        what = f"пост {post}" if post else "поточний товар"
        verb = "Пауза" if code == "pausing" else "Стоп"
        text = f"{verb}: дописую {what}"
        if wait is not None:
            text += f", ще ≈ {wait} хв" if wait else ", ще трохи"
        elif code == "stopping" and not open_session["item_started_at"]:
            text = "Стоп: робот завершує сесію"  # R-A16: між товарами дописувати нічого
    details: list[str] = []
    if code == "crashed":
        done = last["published"] if last is not None and crashed_after else 0
        if open_session:
            done = (
                published_in_session
                or conn.execute(
                    "SELECT count(*) FROM items WHERE session_id = ? AND status = 'published'",
                    (open_session["id"],),
                ).fetchone()[0]
            )
        reason = (last["stop_reason"] or "") if last is not None and crashed_after else ""
        text = (
            CRASH_CLOSED
            if "TargetClosed" in reason
            else (CRASH_LIMIT if today >= day_limit else CRASH_TEXT).format(
                done=f" (опубліковано {done})" if done else ""
            )
        )
        if reason:
            details.append(reason)
    last_text = (  # після збою причина вже в головному рядку — не повторюємо
        last_run(last, now, with_reason=code not in ("crashed", "failed"))
        if last is not None
        else ""
    )
    idle_note = ""
    if can_start and (
        last is None or (_stamp(last["started_at"], now) or now).date() != now.date()
    ):
        idle_note = "Сьогодні робот ще не запускався — можна натискати «Старт»."
    eta, eta_cards, eta_span = "", "", ""
    if can_start and queue_today:
        eta_cards = plural_cards(queue_today)
        eta_span = span(queue_today * POST_MINUTES, rough=True)
        eta = f"Сьогодні опублікує до {eta_cards} (≈ {eta_span})"
    rehearsal, rehearsal_problems = rehearsal_text(rehearse.last_summary(), now)
    return State(
        code, text, today, day_limit, approved, can_start, why_not, wait, last_text, buttons,
        started, limits or publish.Groups(0, 0), rehearsal, rehearsal_problems,
        published_in_session, why_code,
        last["id"] if last is not None else None,
        int(last["problems"] or 0) if last is not None else 0,
        idle_note, queue_today, eta, progress, doing, previous, mini, never_logged and idle, details,
        eta_cards, eta_span, plan_n, handled_n, left_text,
        login_ok_note(now) if can_start else "",
    )  # fmt: skip


PROBE_TIMEOUT = 45  # с: «Старт» чекає перевірку входу найдовше стільки


def probe_login(timeout: float = PROBE_TIMEOUT) -> None:
    """ROBOT-019: запустити `robot --login-check` і дочекатись (результат — у `login-check.json`).
    Замок зайнятий чи Chrome не відкрився — процес сам скаже в журналі; «Старт» вирішує файл."""
    process = spawn(["--login-check"])
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()


def log_line(text: str, log: Path | None = None) -> None:
    """Рядок із часом у журнал робота (нічний пробний прогін: запущено / пропущено)."""
    log = log or log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {text}\n")


def command(args: list[str], program: str = "robot") -> list[str]:
    return [sys.executable, "-m", "shafa", program, *args]


def spawn(args: list[str], log: Path | None = None, program: str = "robot") -> subprocess.Popen:
    """Запустити робота (або бота) окремим процесом; вивід — у файл журналу в теці даних."""
    log = log or log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    options: dict = {}
    if os.name == "nt":  # pragma: no cover — перевіряється в windows-tests
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # кирилиця в журналі й на Windows
    with open(log, "ab") as out:
        return subprocess.Popen(
            command(args, program), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, **options,
        )  # fmt: skip
