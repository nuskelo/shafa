"""Керування роботом з екрана (UI-002): стан, запуск окремим процесом, Пауза/Стоп.

Стан — із замка (`lock.busy`: чи живий процес робота і що він робить) і таблиці `sessions`.
Робот запускається окремим процесом `python -m shafa robot …`: закрили вкладку чи навіть сервер
екрана — робот працює далі. Mac: власна сесія процесу (Ctrl+C у вікні сервера його не перерве);
Windows: без вікна консолі й у своїй групі процесів.
"""

import math
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shafa import config
from shafa.robot import lock, publish

ITEM_MINUTES = 4  # орієнтовно: стільки триває один товар (форма + фото + клік)
LOG_NAME = "robot-run.log"

TEXTS = {
    "stopped": "Робот зупинений",
    "starting": "Робот запускається…",
    "running": "Робот публікує",
    "pausing": "Пауза: чекаю, поки допишеться поточний товар",
    "paused": "Робот на паузі",
    "stopping": "Стоп: чекаю, поки допишеться поточний товар",
    "login_needed": "Робот не увійшов на Шафу — потрібно увійти",
    "login_open": "Відкрито вікно входу на Шафу — увійдіть і закрийте вікно (або кнопка нижче)",
    "busy": "Робот зайнятий іншою справою (Власник щось перевіряє)",
    "crashed": "Робот зупинився несподівано — натисніть «Старт», щоб продовжити",
    "failed": "Робот не запустився",
}

LAST = {  # причина зупинки останньої сесії — простими словами
    "done": "Усі Схвалені опубліковано",
    "limit": "Ліміт на сьогодні вичерпано",
    "stop": "Зупинено кнопкою «Стоп»",
    "login": "Робот вийшов із Шафи",
    "no_form": "Шафа не показала форму «Додати товар» (сайт повільний чи змінився) — "
    "натисніть «Старт» ще раз; повториться — скажіть Власнику",
    "sleep": "Ноутбук засинав — робот зупинився",
    "site": "Шафа, мабуть, змінила форму — Картку в Проблемах, скажіть Власнику",
    "photos": "Не всі фото завантажились на форму — Картку в Проблемах, натисніть «Старт» ще раз; "
    "повториться — скажіть Власнику",
    "error": "Помилка — Картку в Проблемах, перевірте її",
    "interrupted": "Робота перервано",
    "crash": "Робот зупинився несподівано",
}


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
    last: str = ""  # як закінчилась остання сесія
    buttons: list[str] = field(default_factory=list)
    # CORE-005: почато сьогодні (старих, нових) і межі на день
    started: publish.Groups = field(default_factory=lambda: publish.Groups(0, 0))
    limits: publish.Groups = field(default_factory=lambda: publish.Groups(0, 0))

    @property
    def groups_text(self) -> str:
        return "Сьогодні: " + self.started.text(self.limits)


def log_path() -> Path:
    return config.data_dir() / LOG_NAME


def _minutes_left(started: str | None, now: datetime) -> int | None:
    if not started:
        return None
    spent = (now - datetime.fromisoformat(started)).total_seconds() / 60
    return max(1, math.ceil(ITEM_MINUTES - spent))


def state(
    conn: sqlite3.Connection,
    lock_file: Path | None = None,
    day_limit: int | None = None,
    now: datetime | None = None,
    starting: bool = False,
    failed: bool = False,
) -> State:
    """Що зараз з роботом і які кнопки доступні."""
    now = now or datetime.now().astimezone()
    day_limit = day_limit or publish.daily_limit()
    busy = lock.busy(lock_file)
    open_session = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    rows = publish.queue_rows(conn)
    approved = len(rows)
    today = publish.started_today(conn, now)
    started = publish.started_groups(conn, now)
    try:
        limits: publish.Groups | None = publish.group_limits()
    except publish.SetupError:
        limits = None  # робот на старті зупиниться з поясненням; екран не падає

    wait = None
    if busy and busy.get("mode") == "publish":
        command = open_session["command"] if open_session else None
        in_work = open_session["item_started_at"] if open_session else None
        if command == "stop":
            code = "stopping"
        elif command == "pause":
            code = "pausing" if in_work else "paused"
        else:
            code = "running"
        if command and in_work:
            wait = _minutes_left(in_work, now)
    elif busy and busy.get("mode") == "login":
        code = "login_open"
    elif busy:
        code = "busy"
    elif starting:
        code = "starting"
    elif open_session:
        code = "crashed"  # сесія відкрита, а замок вільний — процес помер
    elif failed:
        code = "failed"
    elif last is not None and last["stop_code"] == "login":
        code = "login_needed"
    else:
        code = "stopped"

    idle = code in ("stopped", "login_needed", "crashed", "failed")
    why_not = ""
    if idle and approved == 0:
        why_not = "Немає Схвалених Карток"
    elif idle and today >= day_limit:
        why_not = f"Сьогодні вже {today} з {day_limit} — ліміт доби вичерпано, продовжимо завтра"
    elif idle and limits is None:
        why_not = "Межі старих і нових на день у налаштуваннях неправильні"
    elif idle and not any(publish.group_open(r["folder_name"], started, limits) for r in rows):
        why_not = f"Сьогодні вже {started.text(limits)} — продовжимо завтра"
    can_start = idle and not why_not

    buttons = []
    if idle:
        buttons.append("start")
    if code == "running":
        buttons += ["pause", "stop"]
    if code in ("pausing", "paused"):
        buttons += ["resume", "stop"]
    if code == "login_needed":
        buttons.append("login")
    if code == "login_open":
        buttons.append("login_done")

    text = TEXTS[code]
    if wait:
        text += f" (~{wait} хв)"
    last_text = ""
    if last is not None:
        last_text = LAST.get(last["stop_code"] or "", "") or (last["stop_reason"] or "")
        if last["stop_code"] == "done" and last["problems"]:  # не всі Схвалені опубліковано
            last_text = f"Опубліковано {last['published']}, у Проблемах {last['problems']}"
    return State(code, text, today, day_limit, approved, can_start, why_not, wait, last_text,
                 buttons, started, limits or publish.Groups(0, 0))  # fmt: skip


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
