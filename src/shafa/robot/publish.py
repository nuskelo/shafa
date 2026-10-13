"""Публікація схвалених Карток на Шафі (ROBOT-002). Одна Картка — одна публікація, назавжди.

Черга `approved`, найстаріші пости першими. На кожну: чи пост уже публікувався (D3) → кроки форми →
чи нема схожого товару в неї на Шафі (D4) → заповнення → запис `publishing` у базу → один клік
«Додати річ» → `published` лише за id з відповіді `createProduct` (D6) → тека «Опубліковано» →
пауза 1–3 хв. Невпевненість після кліку → «Проблема» `publish_uncertain`, повтору немає ніколи.
Будь-яка поломка сайту чи входу → Картка в «Проблеми», робот зупиняється (не пропускає далі).

Ліміт доби — сумарно за календарну добу місцевого часу ноутбука (D5), з урахуванням попередніх
запусків. Пауза/Стоп — команда в базі (`sessions.command`), робот перевіряє її між товарами.
"""

import random
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shafa import catalog, config, review
from shafa.db import Status, now
from shafa.folders import PUBLISHED_DIR
from shafa.robot import listings as listings_mod
from shafa.robot import lock
from shafa.robot.form import (
    BrandMissing,
    Driver,
    FieldMissing,
    PhotosShort,
    Submitted,
    execute,
)
from shafa.robot.plan import PlanError, plan

DAILY_LIMIT = 30
DAILY_LIMIT_SETTING = "SHAFA_DAILY_LIMIT"
DELETE_SETTING = "SHAFA_DELETE_PUBLISHED"  # видаляти папки з «Опубліковано» (типово — ні)
DELETE_AFTER = timedelta(days=3)
PAUSE = (60.0, 180.0)  # с між товарами
TICK = 5.0  # с: як часто під час паузи перевіряється Стоп
SLEEP_JUMP = 60.0  # с: «тік» став довшим — ноутбук засинав
ITEM_URL = "https://shafa.ua/uk/item/{id}-shafa"  # Шафа переводить на справжню адресу (301)

UNCERTAIN = (
    "Робот натиснув «Додати річ», але певної відповіді немає. Перевір на Шафі, чи товар з'явився"
)


class SetupError(Exception):
    """Налаштування для публікації неправильні. Текст — для людини."""


@dataclass
class Report:
    session_id: int
    published: list[tuple[int | None, str]] = field(default_factory=list)  # (пост, посилання)
    problems: list[tuple[int | None, str, str]] = field(default_factory=list)  # (пост, код, текст)
    notes: list[str] = field(default_factory=list)
    reason: str = ""

    def text(self) -> str:
        lines = [
            (
                f"Сесія {self.session_id}: опубліковано {len(self.published)}, "
                f"у Проблемах {len(self.problems)}. Зупинка: {self.reason}"
            )
        ]
        lines += [f"  ✓ пост {post}: {url}" for post, url in self.published]
        lines += [f"  ! пост {post}: {text} ({code})" for post, code, text in self.problems]
        lines += [f"  · {note}" for note in self.notes]
        return "\n".join(lines)


def daily_limit() -> int:
    raw = config.setting(DAILY_LIMIT_SETTING)
    if raw is None:
        return DAILY_LIMIT
    if not raw.isdigit() or not 1 <= int(raw) <= 100:
        raise SetupError(f"{DAILY_LIMIT_SETTING} — ціле число від 1 до 100")
    return int(raw)


def delete_enabled() -> bool:
    return (config.setting(DELETE_SETTING) or "").lower() in ("1", "так", "yes", "true")


def started_today(conn: sqlite3.Connection, today: datetime) -> int:
    """Скільки публікацій почато за місцеву добу `today` (і успішних, і невпевнених)."""
    since = (today - timedelta(days=2)).astimezone(UTC).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT publish_started_at FROM items WHERE publish_started_at >= ?", (since,)
    )
    day = today.astimezone().date()
    return sum(1 for r in rows if datetime.fromisoformat(r[0]).astimezone().date() == day)


def queue(conn: sqlite3.Connection) -> list[int]:
    """Схвалені Картки, найстаріші пости першими."""
    return [
        r[0]
        for r in conn.execute(
            """
            SELECT items.id FROM items LEFT JOIN posts USING (post_id)
            WHERE items.status = ? AND items.purged_at IS NULL
            ORDER BY posts.date IS NULL, posts.date, items.id
            """,
            (Status.APPROVED,),
        )
    ]


def already_published(conn: sqlite3.Connection, item_id: int, post_id: int | None) -> str | None:
    """Назва іншої папки, з якої цей пост уже публікувався (D3), або None."""
    if post_id is None:
        return None
    row = conn.execute(
        "SELECT folder_name FROM items WHERE post_id = ? AND id != ? AND status IN (?, ?)",
        (post_id, item_id, Status.PUBLISHED, Status.PUBLISHING),
    ).fetchone()
    return row[0] if row else None


def card_duplicate(
    card: review.Card, listings: list[listings_mod.Listing]
) -> listings_mod.Listing | None:
    """Її товар на Шафі, схожий на схвалену Картку (D4), або None."""
    f = {name: view.value for name, view in card.fields.items()}
    brand = catalog.canonical(f["brand"], catalog.brands()) if f["brand"] else None
    return listings_mod.find_duplicate(
        f["title"], listings_mod.group_slugs(card.path),
        [catalog.shafa_size(s) for s in f["sizes"] or []], brand or f["brand"], listings,
    )  # fmt: skip


def _problem(conn: sqlite3.Connection, item_id: int, source: str, code: str, text: str) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (Status.PROBLEM, code, text, now(), item_id, source),
        )
    return cursor.rowcount == 1


def recover(conn: sqlite3.Connection) -> list[int]:
    """Після обриву: Картки, що лишились у `publishing`, — у «Проблеми» (не повторювати)."""
    ids = [
        r[0] for r in conn.execute("SELECT id FROM items WHERE status = ?", (Status.PUBLISHING,))
    ]
    for item_id in ids:
        _problem(
            conn, item_id, Status.PUBLISHING, "publish_uncertain",
            "Робот зупинився посеред публікації. Перевір на Шафі, чи товар з'явився",
        )  # fmt: skip
    with conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, stop_reason = 'обрив (робот не завершив сесію)', "
            "command = NULL WHERE ended_at IS NULL",
            (now(),),
        )
    return ids


def command(conn: sqlite3.Connection, session_id: int) -> str | None:
    row = conn.execute("SELECT command FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row[0] if row else None


def request(conn: sqlite3.Connection, what: str | None) -> bool:
    """Пауза / Стоп / Продовжити (None) для запущеної сесії. False — сесії немає."""
    with conn:
        cursor = conn.execute("UPDATE sessions SET command = ? WHERE ended_at IS NULL", (what,))
    return cursor.rowcount > 0


def move_published(root: Path, folder_name: str) -> str | None:
    """Папка опублікованого → `<Головна>/Опубліковано/`. Повертає нотатку, якщо не вийшло (D1)."""
    source = root / folder_name
    target = root / PUBLISHED_DIR / folder_name
    if target.exists():
        return f"У «{PUBLISHED_DIR}» уже є папка {folder_name} — не перенесено"
    if not source.is_dir():
        return f"Папки {folder_name} немає — не перенесено"
    try:
        target.parent.mkdir(exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as error:  # Windows: файл відкритий у переглядачі
        return f"Папку {folder_name} не перенесено: {error}"
    return None


def purge_published(conn: sqlite3.Connection, root: Path, today: datetime) -> list[str]:
    """Видалити з «Опубліковано» папки Карток, опублікованих понад 3 дні тому.

    Лише коли увімкнено налаштування (до пілоту — вимкнено) і лише папки опублікованих Карток.
    """
    if not delete_enabled():
        return []
    cutoff = (today - DELETE_AFTER).astimezone(UTC).isoformat(timespec="seconds")
    removed = []
    for (name,) in conn.execute(
        "SELECT folder_name FROM items WHERE status = ? AND published_at < ?",
        (Status.PUBLISHED, cutoff),
    ):
        folder = root / PUBLISHED_DIR / name
        if folder.is_dir() and folder.parent == root / PUBLISHED_DIR:
            shutil.rmtree(folder)
            removed.append(name)
    return removed


class Run:
    """Одна сесія робота. Залежності передаються ззовні — тести без браузера й мережі."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        session: Any,
        make_driver: Callable[[Any], Driver],
        load_listings: Callable[[Any], list[listings_mod.Listing]],
        screens: Path,
        held: lock.Held,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rand: Callable[[float, float], float] = random.uniform,
        today: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.conn, self.root, self.session = conn, root, session
        self.make_driver, self.load_listings = make_driver, load_listings
        self.screens, self.held, self.log = screens, held, log
        self.sleep, self.clock, self.rand, self.today = sleep, clock, rand, today

    def start(self, limit: int, day_limit: int) -> Report:
        # recover() закриває відкриту сесію й Картки `publishing` — лише з узятим замком:
        # без нього це могли б бути сесія й Картки іншого живого робота.
        if self.held.released or self.held.mode != "publish":
            raise lock.Busy
        for item_id in recover(self.conn):
            self.log(f"Картка {item_id} лишилась посеред публікації — у Проблемах")
        with self.conn:
            session_id = self.conn.execute(
                "INSERT INTO sessions (started_at) VALUES (?)", (now(),)
            ).lastrowid
        report = Report(session_id)
        try:
            report.reason = self._loop(report, limit, day_limit)
        except KeyboardInterrupt:
            report.reason = "перервано (Ctrl+C)"
            raise
        except Exception as error:  # закритий браузер, обрив зв'язку
            report.reason = f"збій: {type(error).__name__}: {str(error)[:200]}"
            raise
        finally:
            self._finish(report)
        return report

    def _finish(self, report: Report) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET ended_at = ?, published = ?, problems = ?, stop_reason = ?, "
                "command = NULL WHERE id = ?",
                (now(), len(report.published), len(report.problems), report.reason,
                 report.session_id),
            )  # fmt: skip
        self.log(report.text())

    def _loop(self, report: Report, limit: int, day_limit: int) -> str:
        today = self.today()
        for name in purge_published(self.conn, self.root, today):
            report.notes.append(f"Видалено з «{PUBLISHED_DIR}»: {name}")
        allowed = min(limit, day_limit - started_today(self.conn, today))
        if allowed <= 0:
            return f"ліміт доби ({day_limit}) вичерпано"
        ids = queue(self.conn)
        if not ids:
            return "черга порожня"
        if not self.session.open_form():
            return "немає входу на Шафу: python -m shafa robot --login"
        try:
            listings = self.load_listings(self.session)
        except listings_mod.ListingsError as error:
            return f"не прочитано її товари на Шафі ({error}) — дублі не перевірити"
        self.log(f"Її товарів на Шафі для перевірки дублів: {len(listings)}")

        driver = self.make_driver(self.session)
        done, previous = 0, None
        for item_id in ids:
            if done >= allowed:
                return "ліміт" + (" доби" if allowed < limit else " запуску")
            if (stop := self._wait_command(report.session_id)) is not None:
                return stop
            if previous is True and (stop := self._pause(report.session_id)) is not None:
                return stop
            previous = self._one(report, item_id, driver, listings)
            if previous is True:
                done += 1
            elif isinstance(previous, str):
                return previous
        return "черга закінчилась"

    def _wait_command(self, session_id: int) -> str | None:
        """Стоп → причина; Пауза → чекати, доки не скасують."""
        while (what := command(self.conn, session_id)) == "pause":
            self.sleep(TICK)
        return "Стоп" if what == "stop" else None

    def _pause(self, session_id: int) -> str | None:
        """Пауза людини між товарами; Стоп і сон ноутбука перериває її."""
        left = self.rand(*PAUSE)
        while left > 0:
            step = min(TICK, left)
            started = self.clock()
            self.sleep(step)
            if self.clock() - started > SLEEP_JUMP:
                return "ноутбук засинав — робот зупинився"
            left -= step
            if (stop := self._wait_command(session_id)) is not None:
                return stop
        return None

    def _problem(self, report: Report, card: review.Card, source: str, code: str, text: str):
        if _problem(self.conn, card.id, source, code, text):
            report.problems.append((card.post_id, code, text))
            self.log(f"Пост {card.post_id}: {text}")

    def _shot(self, card: review.Card, suffix: str) -> Path:
        self.screens.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        shot = self.screens / f"{stamp}-{card.post_id}-{suffix}.png"
        try:
            self.session.page.screenshot(path=str(shot), full_page=True)
        except Exception:  # noqa: BLE001 — екран лише для звіту; не ховати справжню причину
            return shot
        return shot

    def _one(
        self, report: Report, item_id: int, driver: Driver, listings: list
    ) -> bool | str | None:
        """Одна Картка. True — опубліковано; рядок — причина зупинки; None — далі."""
        card = review.load_card(self.conn, item_id)
        if card is None or card.status != Status.APPROVED:
            return None
        ok = Status.APPROVED
        if other := already_published(self.conn, card.id, card.post_id):
            self._problem(report, card, ok, "possible_duplicate",
                          f"Пост {card.post_id} уже опубліковано (папка {other})")  # fmt: skip
            return None
        try:
            steps, notes = plan(card, self.root)
        except PlanError as error:
            self._problem(report, card, ok, "robot_not_ready", str(error))
            return None
        if duplicate := card_duplicate(card, listings):
            self._problem(report, card, ok, "possible_duplicate",
                          f"На Шафі вже є схоже оголошення: «{duplicate.name}» — {duplicate.url}")  # fmt: skip
            return None

        if not self.session.open_form():
            self._problem(report, card, ok, "robot_logged_out", "Робот не увійшов на Шафу")
            return "немає входу на Шафу"
        try:
            notes += execute(steps, driver, strict=True)
        except FieldMissing as error:
            shot = self._shot(card, "нема-поля")
            self._problem(report, card, ok, "robot_field_missing",
                          f"Робот не знайшов на Шафі поле «{error}». Екран: {shot.name}")  # fmt: skip
            return "поле не знайдено — мабуть, Шафа змінила форму"
        except BrandMissing as error:
            self._problem(report, card, ok, "robot_brand_missing",
                          f"Бренду «{error}» немає в списку Шафи")  # fmt: skip
            return "бренд не знайдено"
        except PhotosShort as error:
            self._problem(report, card, ok, "robot_photos", str(error))
            return "не всі фото завантажились"
        if attempts := self.session.publish_attempts():
            raise RuntimeError(f"Заблоковано спроби публікації до кліку: {attempts}")

        return self._submit(report, card, driver, notes)

    def _submit(self, report: Report, card: review.Card, driver: Driver, notes: list[str]):
        stamp = now()
        with self.conn:  # запис ДО кліку: після обриву Картка не повториться
            cursor = self.conn.execute(
                "UPDATE items SET status = ?, publish_started_at = ?, session_id = ?, "
                "updated_at = ? WHERE id = ? AND status = ?",
                (Status.PUBLISHING, stamp, report.session_id, stamp, card.id, Status.APPROVED),
            )
        if cursor.rowcount != 1:
            return None
        pub = Status.PUBLISHING
        status = self.conn.execute("SELECT status FROM items WHERE id = ?", (card.id,)).fetchone()
        try:
            self.session.arm_publish(card.id, status[0])
            try:
                result: Submitted = driver.submit()
            finally:
                self.session.disarm()
        except FieldMissing as error:  # до кліку: нічого не надіслано
            self._problem(report, card, pub, "robot_field_missing",
                          f"Робот не знайшов на Шафі «{error}»")  # fmt: skip
            return "кнопку «Додати річ» не знайдено"
        except BaseException:
            self._problem(report, card, pub, "publish_uncertain", UNCERTAIN)
            raise

        if result.product_id:
            url = ITEM_URL.format(id=result.product_id)
            with self.conn:
                self.conn.execute(
                    "UPDATE items SET status = ?, shafa_id = ?, shafa_url = ?, published_at = ?, "
                    "problem_code = NULL, problem_text = NULL, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (Status.PUBLISHED, result.product_id, url, now(), now(), card.id, pub),
                )
            report.published.append((card.post_id, url))
            self.log(f"Пост {card.post_id}: опубліковано {url}")
            self._shot(card, "опубліковано")
            report.notes += [f"Пост {card.post_id}: {note}" for note in notes]
            if result.errors:
                report.notes.append(f"Пост {card.post_id}: Шафа додала зауваження: {result.errors}")
            if note := move_published(self.root, card.folder_name):
                report.notes.append(note)
            return True
        shot = self._shot(card, "після-кліку")
        if not result.sent and result.blocked:
            self._problem(report, card, pub, "robot_publish_blocked",
                          "Захист не пропустив запит публікації — товар не створено")  # fmt: skip
            return "захист заблокував запит публікації"
        if not result.sent:
            self._problem(report, card, pub, "robot_not_sent",
                          "Форма не надіслалась — мабуть, Шафа вимагає поле, якого робот не знає. "
                          f"Екран: {shot.name}")  # fmt: skip
            return "форма не надіслалась"
        if result.errors:
            self._problem(report, card, pub, "publish_error",
                          "Шафа не прийняла товар: " + "; ".join(result.errors))  # fmt: skip
            return "Шафа не прийняла товар"
        self._problem(report, card, pub, "publish_uncertain",
                      f"{UNCERTAIN} ({result.failure or 'немає id у відповіді'})")  # fmt: skip
        return "немає певної відповіді Шафи"
