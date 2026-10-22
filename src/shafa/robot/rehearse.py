"""Пробний прогін (QA-003): Картки проходять живу форму до кінця, але без кліку «Додати річ».

Помилки форми («поле не знайдено», бренд, фото) інакше видно лише під час справжньої публікації.
Тут вони видно заздалегідь: Схвалена Картка з помилкою — у «Проблемах» з текстом «Пробний прогін: …»
(справжня сесія її не візьме), решта — у звіті. Це не «Сесія»: рядків у `sessions` немає, ліміт доби
не рахується. Публікація неможлива: `Session` без режиму публікації (`publish=False`) не видає
квитків, а будь-яка спроба створити товар → `RuntimeError`.
"""

import contextlib
import json
import random
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from shafa import config, events, review, rules
from shafa.db import Status
from shafa.robot import balance, browser, publish
from shafa.robot.form import (
    PHOTO_RETRY_LOG,
    BrandMissing,
    Driver,
    FieldMissing,
    PhotosShort,
    execute,
    photo_retries,
    save_markup,
    to_problem,
)
from shafa.robot.plan import PlanError, plan, rehearsal_plan

REHEARSE_MAX = 30  # Карток за один прогін
COUNT_DEFAULT = 5
COUNT_SETTING = "SHAFA_REHEARSE_COUNT"
AT_SETTING = "SHAFA_REHEARSE_AT"  # «03:00»; порожнє — нічного прогону немає
PREFIX = "Пробний прогін: "
PAUSE = (5.0, 15.0)  # с між Картками (справжня сесія чекає 1–3 хв — тут нічого не публікується)
SHOT_WAIT = publish.SHOT_WAIT
LAST_NAME = "rehearse-last.json"  # підсумок останнього прогону для панелі
NIGHT_NAME = "rehearse-night"  # дата, коли нічний прогін уже вирішено (запущено чи пропущено)
GRACE = 300.0  # с: стільки після призначеного часу нічний прогін ще запускається
TICK = 30.0  # с між перевірками годинника
LOG_TEXT = 200  # символів помилки в рядку виводу

STALE = "правила застарілі — справжня сесія поверне її в «Проблеми» сама"
ALREADY = "пост уже опубліковано з іншої папки"


class SetupError(Exception):
    """Налаштування пробного прогону неправильні. Текст — для людини."""


# --- Налаштування ---


def count_setting() -> int:
    raw = config.setting(COUNT_SETTING)
    if raw is None:
        return COUNT_DEFAULT
    if not raw.isdigit() or not 1 <= int(raw) <= REHEARSE_MAX:
        raise SetupError(f"{COUNT_SETTING} — ціле число від 1 до {REHEARSE_MAX}")
    return int(raw)


def parse_at(raw: str | None) -> clock_time | None:
    """«03:00» → година; порожнє чи неправильне → None (вимкнено)."""
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (raw or "").strip())
    return clock_time(int(match[1]), int(match[2])) if match else None


# --- Вибір Карток ---


def pick_tomorrow(conn: sqlite3.Connection, today: datetime, count: int) -> list[int]:
    """Перші `count` Карток, які візьме наступна справжня сесія, — у її порядку й з її лімітами."""
    day_limit = publish.daily_limit()
    rows = publish.session_queue(conn, today, day_limit, day_limit)
    return [r["id"] for r in rows[:count]]


def pick_sections(conn: sqlite3.Connection, root: Path, count: int) -> list[int]:
    """По одній Картці з кожного розділу: Схвалені (за чергою робота), потім «На перевірку»."""
    rows = [r["id"] for r in publish.queue_rows(conn)]
    rows += [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE status = ? AND purged_at IS NULL ORDER BY id",
            (Status.REVIEW,),
        )
    ]
    seen, picked = set(), []
    for item_id in rows:
        card = review.load_card(conn, item_id)
        if card is None or card.path is None or card.path in seen:
            continue
        try:
            rehearsal_plan(card, root)
        except PlanError:  # форму з нею не пройти — розділ перевіримо іншою Карткою
            continue
        seen.add(card.path)
        picked.append(item_id)
        if len(picked) >= count:
            break
    return picked


def pick_posts(conn: sqlite3.Connection, post_ids: list[int]) -> tuple[list[int], list[int]]:
    """Картки за номерами постів → (id, номери без Картки)."""
    ids, missing = [], []
    for post_id in post_ids:
        row = conn.execute(
            "SELECT id FROM items WHERE post_id = ? AND purged_at IS NULL ORDER BY id LIMIT 1",
            (post_id,),
        ).fetchone()
        (ids if row else missing).append(row[0] if row else post_id)
    return ids, missing


# --- Звіт ---


@dataclass
class Outcome:
    post_id: int | None
    item_id: int
    result: str  # ok / error / skipped
    code: str = ""
    text: str = ""
    moved: bool = False  # Схвалена Картка → «Проблеми»
    notes: list[str] = field(default_factory=list)


@dataclass
class RehearsalReport:
    outcomes: list[Outcome] = field(default_factory=list)
    reason: str = ""
    code: str = ""

    @property
    def checked(self) -> int:
        return sum(o.result != "skipped" for o in self.outcomes)

    @property
    def good(self) -> int:
        return sum(o.result == "ok" for o in self.outcomes)

    @property
    def moved(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.moved]

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.result == "error"]

    def codes(self) -> str:
        counts = Counter(o.code for o in self.moved)
        return ", ".join(f"{c} ×{n}" if n > 1 else c for c, n in counts.items())

    def summary(self) -> str:
        line = f"Пробний прогін: перевірено {self.checked}, добре {self.good}, "
        line += f"у Проблемах {len(self.moved)}"
        if self.moved:
            line += f" ({self.codes()})"
        if unmoved := len(self.failed) - len(self.moved):
            line += f", з помилкою без схвалення {unmoved}"
        return line

    def text(self) -> str:
        lines = [self.summary()]
        if self.reason:
            lines.append(f"Зупинка: {self.reason}")
        for o in self.outcomes:
            mark = {"ok": "✓", "error": "!", "skipped": "·"}[o.result]
            body = o.text or "добре"
            where = " (Картка в Проблемах)" if o.moved else ""
            lines.append(
                f"  {mark} Пост {o.post_id}: {o.code + ' — ' if o.code else ''}{body}{where}"
            )
            lines += [f"      · {note}" for note in o.notes]
        return "\n".join(lines)


def save(report: RehearsalReport, now: datetime, folder: Path | None = None) -> Path:
    """Звіт дня `rehearse-<дата>.txt` (прогони дня дописуються) і підсумок для панелі."""
    folder = folder or config.data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rehearse-{now:%Y-%m-%d}.txt"
    with open(path, "a", encoding="utf-8") as out:
        out.write(f"=== {now:%Y-%m-%d %H:%M} ===\n{report.text()}\n\n")
    last = {
        "at": now.isoformat(timespec="seconds"),
        "checked": report.checked,
        "good": report.good,
        "problems": len(report.moved),
        "codes": report.codes(),
        "stop": report.code,
        "summary": report.summary(),
    }
    temp = folder / (LAST_NAME + ".tmp")
    temp.write_text(json.dumps(last, ensure_ascii=False), encoding="utf-8")
    temp.replace(folder / LAST_NAME)
    return path


def last_summary(folder: Path | None = None) -> dict | None:
    """Підсумок останнього прогону для панелі; немає чи зіпсований файл — None."""
    path = (folder or config.data_dir()) / LAST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "summary" in data else None


# --- Ядро ---


def _shot(session: Any, screens: Path, post_id: int | None, suffix: str) -> Path:
    screens.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    shot = screens / f"{stamp}-{post_id}-{suffix}.png"
    with contextlib.suppress(Exception):  # екран лише для звіту; не ховати справжню причину
        session.page.screenshot(path=str(shot), full_page=True, timeout=SHOT_WAIT * 1000)
    return shot


def _fill(
    session: Any,
    driver: Driver,
    steps: list,
    log: Callable[[str], None],
    sleep: Callable[[float], None],
    rand: Callable[[float, float], float],
    post_id: int | None,
) -> tuple[list[str] | None, str | None]:
    """Форма з нуля й заповнення; тимчасовий збій — до `ATTEMPTS` спроб, як у публікації.
    Повертає (нотатки, None) або (None, причина зупинки: login / no_form / shafa_balance:<сума>)."""
    for attempt in range(1, publish.ATTEMPTS + 1):
        opened = session.open_form()
        if not opened.ok:
            if opened.state == browser.NO_LOGIN:
                log(
                    "Немає входу на Шафу в профілі робота. Запустіть: python -m shafa robot --login"
                )
                return None, "login"
            log(f"Шафа не показала форму «Додати товар»: {opened.detail()}")
            return None, "no_form"
        driver.reset()
        try:
            notes, amount = balance.guarded(session, driver, steps, execute)
            if amount is not None:  # вікно закрито хрестиком (ROBOT-014)
                log(balance.NOTE)
                balance.announce(amount)
            return notes, None
        except balance.BalanceStop as error:
            log(f"Шафа показала вікно від'ємного балансу{error.amount}: закрити не вдалося")
            return None, f"shafa_balance:{error.amount}"
        except Exception as error:
            if not publish.is_transient(error):
                raise
            log(
                f"Пост {post_id}: тимчасовий збій, спроба {attempt} з {publish.ATTEMPTS}: "
                f"{type(error).__name__}: {str(error)[:300]}"
            )
        finally:
            for _ in range(photo_retries(driver)):  # ROBOT-015
                log(PHOTO_RETRY_LOG.format(post_id))
        if attempt < publish.ATTEMPTS:
            sleep(rand(*publish.RETRY_PAUSE))
    raise TimeoutError(publish.SLOW_TEXT)


def rehearse(
    conn: sqlite3.Connection,
    root: Path,
    cards: list[int],
    session: Any,
    make_driver: Callable[[Any], Driver],
    screens: Path,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[float, float], float] = random.uniform,
    today: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    folder: Path | None = None,
) -> RehearsalReport:
    """Пройти форму для кожної Картки (id) без кліку «Додати річ»; звіт пишеться завжди."""
    if not 1 <= len(cards) <= REHEARSE_MAX:
        raise ValueError(f"За прогін — від 1 до {REHEARSE_MAX} Карток")
    report = RehearsalReport(reason="збій", code="error")
    events.emit(
        "rehearse.start", f"Пробний прогін: перевіряю {len(cards)} Карток (нічого не публікую)"
    )
    try:
        _loop(report, conn, root, cards, session, make_driver, screens, log, sleep, rand)
    except KeyboardInterrupt:
        report.reason, report.code = "перервано (Ctrl+C)", "interrupted"
        raise
    except Exception as error:
        report.reason = f"збій: {type(error).__name__}: {str(error)[:200]}"
        raise
    finally:
        path = save(report, today(), folder)
        events.emit(*_event(report))
        log(report.summary())
        log(f"Звіт: {path}")
    return report


def _event(report: RehearsalReport) -> tuple[str, str]:
    """BOT-012: підсумок для журналу подій — «добре» тихо, будь-що інше гучно. Людською мовою:
    без кодів, а що не так — словами першої невдалої Картки."""
    if report.code == "done" and not report.failed:
        return "rehearse.done", f"Пробний прогін: {report.good} з {report.checked} добре"
    text = f"Пробний прогін: добре {report.good} з {report.checked}"
    if failed := report.failed:
        posts = ", ".join(str(o.post_id) for o in failed[:5]) + ("…" if len(failed) > 5 else "")
        text += f", не пройшли {len(failed)} (пости {posts})"
        if report.moved:
            text += f"; Схвалених серед них — {len(report.moved)}, вони в «Проблемах»"
        if failed[0].text:
            text += f". Перша: {failed[0].text[:150]}"
    if report.code != "done":
        text += f". Зупинка: {report.reason}"
    return "rehearse.bad", text


def _loop(report, conn, root, cards, session, make_driver, screens, log, sleep, rand) -> None:
    driver = make_driver(session)
    streak: list[str] = []  # коди Карток поспіль, чия помилка — з форми (як ROBOT-010)
    previous = False  # чи минулу Картку вже відкривали на формі (пауза між ними)
    for item_id in cards:
        card = review.load_card(conn, item_id)
        if card is None or card.status not in (Status.APPROVED, Status.REVIEW):
            continue
        outcome = Outcome(card.post_id, card.id, "ok")
        skip = _skip_reason(conn, card)
        if skip:
            outcome.result, outcome.text = "skipped", skip
            report.outcomes.append(outcome)
            log(f"Пост {card.post_id}: пропущено — {skip}")
            continue
        if previous:
            sleep(rand(*PAUSE))
        previous = True
        stop = _one(outcome, conn, root, card, session, driver, screens, log, sleep, rand)
        if stop:  # форми не було — Картку не перевірено
            code, _, amount = stop.partition(":")
            report.code = code
            report.reason = {"login": "немає входу на Шафу",
                             "no_form": "Шафа не показала форму",
                             "shafa_balance": balance.STOP_TEXT.format(amount=amount)}[code]  # fmt: skip
            return
        report.outcomes.append(outcome)
        if outcome.result == "ok":
            streak.clear()
            log(f"Пост {card.post_id}: добре" + (f" (пропусків: {len(outcome.notes)})"
                                                  if outcome.notes else ""))  # fmt: skip
        else:
            log(f"Пост {card.post_id}: {outcome.code} — {outcome.text[:LOG_TEXT]}")
            if outcome.code != "robot_not_ready":  # помилка Картки, а не форми
                streak.append(outcome.code)
            if len(streak) >= publish.STREAK_ROW:
                report.code = "streak"
                report.reason = publish.STREAK_TEXT.format(
                    n=len(streak), what="Картки поспіль", codes=", ".join(streak)
                )
                return
    report.code, report.reason = "done", "перевірено все, що було в списку"


def _skip_reason(conn: sqlite3.Connection, card: review.Card) -> str | None:
    if card.status != Status.APPROVED:
        return None
    if publish.already_published(conn, card.id, card.post_id):
        return ALREADY
    version = conn.execute("SELECT rules_version FROM items WHERE id = ?", (card.id,)).fetchone()[0]
    return STALE if version is None or version < rules.RULES_VERSION else None


def _one(outcome, conn, root, card, session, driver, screens, log, sleep, rand) -> str | None:
    """Одна Картка. Повертає причину зупинки (login / no_form) або None."""
    approved = card.status == Status.APPROVED
    try:
        steps, notes = plan(card, root) if approved else rehearsal_plan(card, root)
    except PlanError as error:
        _error(conn, outcome, card, "robot_not_ready", str(error))
        return None
    try:
        filled, stop = _fill(session, driver, steps, log, sleep, rand, card.post_id)
    except FieldMissing as error:
        shot = _shot(session, screens, card.post_id, "нема-поля")
        _error(conn, outcome, card, "robot_field_missing",
               f"Робот не знайшов на Шафі поле «{error}». Екран: {shot.name}")  # fmt: skip
        if error.details:
            log(f"Пост {card.post_id}: поле «{error}» не знайдено; на формі — {error.details}")
        if dump := save_markup(error, shot):
            log(f"Пост {card.post_id}: розмітка поля «{error.field}» — {dump}")
        return None
    except BrandMissing as error:
        _brand_miss(session, driver, screens, card, log)
        _error(conn, outcome, card, "robot_brand_missing", publish.BRAND_BROKEN.format(error))
        return None
    except PhotosShort as error:
        shot = _shot(session, screens, card.post_id, "фото")
        log(f"Пост {card.post_id}: фото — {error}. Екран: {shot.name}")
        _error(conn, outcome, card, "robot_photos", str(error))
        return None
    except TimeoutError:  # усі спроби невдалі (`_fill`); інші збої йдуть вгору
        _shot(session, screens, card.post_id, "збій")
        _error(conn, outcome, card, "robot_slow", publish.SLOW_TEXT)
        return None
    if stop:
        return stop
    outcome.notes = notes + (filled or [])
    if getattr(driver, "brand_miss", None):
        outcome.notes.append("Бренду немає ні у знімку, ні наживо — поле лишиться порожнім")
    if attempts := session.publish_attempts():
        raise RuntimeError(f"Заблоковано спроби публікації: {attempts}")
    return None


def _error(conn, outcome: Outcome, card: review.Card, code: str, text: str) -> None:
    outcome.result, outcome.code, outcome.text = "error", code, text
    if card.status == Status.APPROVED:
        to_problem(conn, card.id, code, PREFIX + text)
        outcome.moved = True


def _brand_miss(session, driver, screens: Path, card: review.Card, log) -> None:
    miss = getattr(driver, "brand_miss", None)
    if miss is None:
        return
    shot = _shot(session, screens, card.post_id, "бренд")
    log(f"Пост {card.post_id}: бренд не знайдено; {miss.details()}. Екран: {shot.name}")
    data = f"<!-- {miss.details()} -->\n{miss.markup}".encode()[: publish.MARKUP_LIMIT]
    try:
        shot.with_suffix(".html").write_text(
            data.decode("utf-8", errors="ignore"), encoding="utf-8"
        )
    except OSError:
        return


# --- Нічний запуск ---


class Nightly:
    """Раз на добу о `SHAFA_REHEARSE_AT` запускає `rehearse`, якщо програма відкрита й робот вільний.

    «Доганяння» немає: ноутбук спав через призначений час — пропуск з рядком у журналі. Дата, на яку
    справу вже вирішено (запущено чи пропущено), лежить у файлі: перезапуск програми вдруге не
    запускає. Налаштування читаються щоразу — зміна діє без перезапуску.
    """

    def __init__(
        self,
        command: Callable[[int], list[str]],
        spawn: Callable[[list[str]], object],
        busy: Callable[[], object],
        log: Callable[[str], None],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        folder: Path | None = None,
        grace: float = GRACE,
    ) -> None:
        self.command, self.spawn, self.busy, self.log = command, spawn, busy, log
        self.clock, self.folder, self.grace = clock, folder, grace
        self.last: datetime | None = None
        self.bad: str | None = None  # неправильне значення, про яке вже написали

    def _marker(self) -> Path:
        return (self.folder or config.data_dir()) / NIGHT_NAME

    def _done_today(self, today: str) -> bool:
        try:
            return self._marker().read_text(encoding="utf-8").strip() == today
        except OSError:
            return False

    def _mark(self, today: str) -> None:
        try:
            self._marker().parent.mkdir(parents=True, exist_ok=True)
            self._marker().write_text(today, encoding="utf-8")
        except OSError:
            pass

    def tick(self) -> str | None:
        """started / busy / slept — що зроблено цього разу; None — нічого."""
        now = self.clock()
        last, self.last = self.last, now
        raw = config.setting(AT_SETTING)
        if not raw:
            return None
        at = parse_at(raw)
        if at is None:
            if self.bad != raw:
                self.bad = raw
                self.log(f"{AT_SETTING}={raw!r}: потрібен час «ГГ:ХХ» — нічного прогону немає")
            return None
        due = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        today = f"{now:%Y-%m-%d}"
        if now < due or self._done_today(today):
            return None
        self._mark(today)
        label = f"{at:%H:%M}"
        if (now - due).total_seconds() <= self.grace:
            if self.busy():
                self.log(f"Пробний прогін о {label} пропущено: робот зайнятий")
                return "busy"
            try:
                count = count_setting()
            except SetupError as error:
                self.log(f"Пробний прогін о {label} не запущено: {error}")
                return None
            self.spawn(self.command(count))
            self.log(f"Пробний прогін о {label} запущено (Карток: {count})")
            return "started"
        if last is not None and last < due:
            self.log(f"Пробний прогін о {label} пропущено: комп'ютер спав")
            return "slept"
        return None  # програму відкрито після цієї години — без доганяння

    def run(self, stop: Any, interval: float = TICK) -> None:
        """Цикл для потоку; `stop` — `threading.Event`. Помилка однієї перевірки потік не вбиває."""
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001
                self.log(f"Нічний пробний прогін: {type(error).__name__}: {str(error)[:200]}")
            stop.wait(interval)
