"""Публікація схвалених Карток на Шафі (ROBOT-002). Одна Картка — одна публікація, назавжди.

Черга `approved`, найстаріші пости першими. На кожну: чи пост уже публікувався (D3) → кроки форми →
чи нема схожого товару в неї на Шафі (D4) → заповнення → запис `publishing` у базу → один клік
«Додати річ» → `published` лише за id з відповіді `createProduct` (D6) → тека «Опубліковано» →
пауза 1–3 хв. Невпевненість після кліку → «Проблема» `publish_uncertain`, повтору немає ніколи.
Будь-яка поломка сайту чи входу → Картка в «Проблеми», робот зупиняється (не пропускає далі).

Ліміт доби — сумарно за календарну добу місцевого часу ноутбука (D5), з урахуванням попередніх
запусків. Окремо (CORE-005) — її межі «старих» (`borh`) і «нових» (папка/бот) на день, разом ≤ 30:
спершу нові, потім старі, кожна група — до своєї межі; невикористане не переноситься; межі
перечитуються перед кожною Карткою. Рахується кожна спроба, що дійшла до кліку «Додати річ».
Пауза/Стоп — команда в базі (`sessions.command`), робот перевіряє її між товарами.
"""

import random
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shafa import catalog, config, review, rules
from shafa.bot.presence import GONE
from shafa.db import Status, now
from shafa.fill import STALE_RULES
from shafa.folders import PUBLISHED_DIR, is_old
from shafa.robot import listings as listings_mod
from shafa.robot import lock
from shafa.robot.browser import NO_LOGIN
from shafa.robot.form import (
    MARKUP_LIMIT,
    BrandMissing,
    Driver,
    FieldMissing,
    PhotosShort,
    Submitted,
    execute,
    save_markup,
)
from shafa.robot.form import (
    SUBMIT_TIMEOUT as SUBMIT_WAIT,
)
from shafa.robot.plan import PlanError, plan

DAILY_LIMIT = 30
DAILY_LIMIT_SETTING = "SHAFA_DAILY_LIMIT"
OLD_SETTING = "SHAFA_DAILY_OLD"  # старих (з Експорту, одне фото) на день
NEW_SETTING = "SHAFA_DAILY_NEW"  # нових (папка, бот) на день
OLD_DEFAULT, NEW_DEFAULT = 10, 0
GROUPS_MAX = 30  # старих і нових разом на день
DELETE_SETTING = "SHAFA_DELETE_PUBLISHED"  # видаляти папки з «Опубліковано» (типово — ні)
DELETE_AFTER = timedelta(days=3)
BRAND_BROKEN = (
    "Бренд «{}» є у довіднику Шафи, але робот його не знайшов — скажіть Власнику. "
    "«Перевірити ще раз» поверне Картку в Схвалені."
)
SHOT_WAIT = 10  # с: скріншот важкої сторінки (реклама) не має зависати робота
PAUSE = (60.0, 180.0)  # с між товарами
ATTEMPTS = 3  # спроб заповнити Картку до кліку «Додати річ»: перша й 2 повтори (ROBOT-009)
RETRY_PAUSE = (30.0, 60.0)  # с перед повтором
SLOW_TEXT = (
    "Шафа довго не відповідала — робот спробував {n} рази і зупинився. Натисніть «Старт» пізніше."
)
TICK = 5.0  # с: як часто під час паузи перевіряється Стоп
SLEEP_JUMP = 60.0  # с: «тік» став довшим — ноутбук засинав
ITEM_URL = "https://shafa.ua/uk/item/{id}-shafa"  # Шафа переводить на справжню адресу (301)

STALE_TEXT = (
    "Картку схвалено за старими правилами — робот її не бере. Натисніть «Перерахувати правилами» "
    "(вкладка ШІ) або «Перевірити ще раз», перевірте Картку й схваліть знову"
)
UNCERTAIN = (
    "Перевірте на Шафі, чи опубліковано: робот натиснув «Додати річ», але певної відповіді немає"
)


def is_transient(error: BaseException) -> bool:
    """Тимчасовий збій сайту чи мережі (тайм-аут, обрив, сторінка не довантажилась): форму можна
    відкрити заново. Закритий браузер чи помилка в коді — ні (ROBOT-009)."""
    if isinstance(error, TimeoutError | ConnectionError):
        return True
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
    except ImportError:
        return False
    if isinstance(error, PlaywrightTimeout):
        return True
    return isinstance(error, PlaywrightError) and "net::" in str(error)


@dataclass(frozen=True)
class Stop:
    """Чому сесія закінчилась. `code` — для екрана: done, limit, stop, login, no_form, sleep, site,
    photos, slow, error, interrupted (і crash — сесію закрив recover після падіння)."""

    code: str
    text: str


class SetupError(Exception):
    """Налаштування для публікації неправильні. Текст — для людини."""


@dataclass
class Report:
    session_id: int
    published: list[tuple[int | None, str]] = field(default_factory=list)  # (пост, посилання)
    problems: list[tuple[int | None, str, str]] = field(default_factory=list)  # (пост, код, текст)
    notes: list[str] = field(default_factory=list)
    reason: str = ""
    code: str = ""

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


@dataclass(frozen=True)
class Groups:
    """Скільки старих і нових — межа на день або почато сьогодні."""

    old: int
    new: int

    def text(self, limits: "Groups") -> str:
        return f"старих {self.old} з {limits.old} · нових {self.new} з {limits.new}"


def group_limits() -> Groups:
    """Межі «старих» і «нових» на день з налаштувань; кожна 0–30, разом ≤ 30."""
    values = []
    for name, default in ((OLD_SETTING, OLD_DEFAULT), (NEW_SETTING, NEW_DEFAULT)):
        raw = config.setting(name)
        if raw is None:
            values.append(default)
            continue
        if not raw.isdigit() or not 0 <= int(raw) <= GROUPS_MAX:
            raise SetupError(f"{name} — ціле число від 0 до {GROUPS_MAX}")
        values.append(int(raw))
    if sum(values) > GROUPS_MAX:
        raise SetupError(f"{OLD_SETTING} + {NEW_SETTING} — разом не більше {GROUPS_MAX}")
    return Groups(*values)


def delete_enabled() -> bool:
    return (config.setting(DELETE_SETTING) or "").lower() in ("1", "так", "yes", "true")


def _started_rows(conn: sqlite3.Connection, today: datetime) -> list[str]:
    """Назви Карток, чию публікацію почато за місцеву добу `today` (дійшли до кліку)."""
    since = (today - timedelta(days=2)).astimezone(UTC).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT folder_name, publish_started_at FROM items WHERE publish_started_at >= ?", (since,)
    )
    day = today.astimezone().date()
    return [r[0] for r in rows if datetime.fromisoformat(r[1]).astimezone().date() == day]


def started_today(conn: sqlite3.Connection, today: datetime) -> int:
    """Скільки публікацій почато за місцеву добу `today` (і успішних, і невпевнених)."""
    return len(_started_rows(conn, today))


def started_groups(conn: sqlite3.Connection, today: datetime) -> Groups:
    """Скільки старих і нових почато за місцеву добу `today`."""
    names = _started_rows(conn, today)
    old = sum(1 for n in names if is_old(n))
    return Groups(old, len(names) - old)


def group_open(folder_name: str, started: Groups, limits: Groups) -> bool:
    """Чи можна сьогодні ще одну Картку з групи цієї Картки."""
    if is_old(folder_name):
        return started.old < limits.old
    return started.new < limits.new


def queue_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Схвалені Картки: спершу нові (папка, бот) за датою, потім старі за списком (`queue_pos`)."""
    return conn.execute(
        """
        SELECT items.id, items.folder_name FROM items LEFT JOIN posts USING (post_id)
        WHERE items.status = ? AND items.purged_at IS NULL
        ORDER BY items.folder_name LIKE 'borh-%', items.queue_pos IS NULL, items.queue_pos,
                 posts.date IS NULL, posts.date, items.id
        """,
        (Status.APPROVED,),
    ).fetchall()


def queue(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in queue_rows(conn)]


POST_DELETED = "post_deleted"
POST_DELETED_TEXT = "Пост видалено з каналу — продано"


def from_bot(conn: sqlite3.Connection, post_id: int) -> bool:
    """Пост опублікував бот (у канал `TELEGRAM_BOT_CHANNEL_ID`) — лише такі перевіряємо."""
    row = conn.execute("SELECT source FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    return row is not None and row[0] == "bot"


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
        f["colors"] or [], f["price"],
    )  # fmt: skip


def card_listing(card: review.Card, product_id: int, url: str) -> listings_mod.Listing:
    """Щойно опублікована Картка — так, як її покаже кабінет «Мої речі» (для знімка)."""
    f = {name: view.value for name, view in card.fields.items()}
    brand = catalog.canonical(f["brand"], catalog.brands()) if f["brand"] else None
    return listings_mod.Listing(
        id=product_id,
        name=f["title"] or "",
        brand=brand or f["brand"],
        slug=(catalog.section(card.path) or {}).get("slug", ""),
        sizes=tuple(catalog.shafa_size(s) for s in f["sizes"] or []),
        url=url,
        colors=tuple(f["colors"] or []),
        price=f["price"],
    )


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
            "Перевірте на Шафі, чи опубліковано: робот зупинився посеред публікації",
        )  # fmt: skip
    with conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, stop_reason = 'обрив (робот не завершив сесію)', "
            "stop_code = 'crash', command = NULL WHERE ended_at IS NULL",
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
    """Папка опублікованого → `<Головна>/Опубліковано/`. Повертає нотатку, якщо не вийшло (D1).

    Старий пост (CORE-005) папки в Головній не має: його фото лишається в теці даних.
    """
    if is_old(folder_name):
        return None
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
        fetch_photo: Callable[[str], bytes | None] = lambda url: None,
        photos_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rand: Callable[[float, float], float] = random.uniform,
        today: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        presence: Callable[[int], str] | None = None,
    ) -> None:
        self.conn, self.root, self.session = conn, root, session
        self.presence = presence  # SITE-002: чи пост ще є в каналі (`bot.presence`)
        self.make_driver, self.load_listings = make_driver, load_listings
        self.screens, self.held, self.log = screens, held, log
        self.fetch_photo, self.photos_dir = fetch_photo, photos_dir
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
        stop = Stop("error", "невідома помилка")
        try:
            stop = self._loop(report, limit, day_limit)
        except KeyboardInterrupt:
            stop = Stop("interrupted", "перервано (Ctrl+C)")
            raise
        except Exception as error:  # закритий браузер, обрив зв'язку
            stop = Stop("error", f"збій: {type(error).__name__}: {str(error)[:200]}")
            raise
        finally:
            report.reason, report.code = stop.text, stop.code
            self._finish(report)
        return report

    def _finish(self, report: Report) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET ended_at = ?, published = ?, problems = ?, stop_reason = ?, "
                "stop_code = ?, item_started_at = NULL, command = NULL WHERE id = ?",
                (now(), len(report.published), len(report.problems), report.reason, report.code,
                 report.session_id),
            )  # fmt: skip
        self.log(report.text())

    def _loop(self, report: Report, limit: int, day_limit: int) -> Stop:
        today = self.today()
        for name in purge_published(self.conn, self.root, today):
            report.notes.append(f"Видалено з «{PUBLISHED_DIR}»: {name}")
        allowed = min(limit, day_limit - started_today(self.conn, today))
        if allowed <= 0:
            return Stop("limit", f"ліміт доби ({day_limit}) вичерпано")
        rows = queue_rows(self.conn)
        if not rows:
            return Stop("done", "черга порожня")
        limits, started = group_limits(), started_groups(self.conn, today)
        if not any(group_open(r["folder_name"], started, limits) for r in rows):
            return Stop("limit", f"на сьогодні все: {started.text(limits)}")
        if (stop := self._open_form(None)) is not None:
            return stop
        try:
            listings = self._listings()
        except listings_mod.ListingsError as error:
            return Stop("error", f"не прочитано її товари на Шафі ({error}) — дублі не перевірити")
        except listings_mod.SetupError as error:
            return Stop("error", str(error))

        driver = self.make_driver(self.session)
        done, previous, held_back = 0, None, False
        for row in rows:
            if done >= allowed:
                return Stop("limit", "ліміт" + (" доби" if allowed < limit else " запуску"))
            if (stop := self._wait_command(report.session_id)) is not None:
                return stop
            if previous is True and (stop := self._pause(report.session_id)) is not None:
                return stop
            # Межі — щоразу знову: зміна налаштувань діє з наступної Картки, доба може змінитись.
            try:
                limits = group_limits()
            except SetupError as error:
                return Stop("error", str(error))
            started = started_groups(self.conn, self.today())
            if not group_open(row["folder_name"], started, limits):
                held_back = True
                continue
            previous = self._one(report, row["id"], driver, listings)
            if previous is True:
                done += 1
            elif isinstance(previous, Stop):
                return previous
        if held_back:
            started = started_groups(self.conn, self.today())
            return Stop("limit", f"на сьогодні все: {started.text(limits)}")
        return Stop("done", "черга закінчилась")

    def _listings(self) -> list[listings_mod.Listing]:
        """Знімок «Мої речі» з бази, якщо свіжий; старий чи порожній — читати кабінет (5–10 хв)."""
        days = listings_mod.refresh_days()
        listings = listings_mod.stored(self.conn, days)
        if listings is not None:
            self.log(f"Її товарів на Шафі для перевірки дублів: {len(listings)} (знімок)")
            return listings
        listings = self.load_listings(self.session)
        listings_mod.store(self.conn, listings)
        self.log(f"Її товарів на Шафі для перевірки дублів: {len(listings)}")
        return listings

    def _duplicate(self, report: Report, card: review.Card, found: listings_mod.Listing) -> None:
        """Схоже оголошення — у Проблеми з назвою, посиланням і (якщо вдалося) фото для екрана."""
        self._problem(report, card, Status.APPROVED, "possible_duplicate",
                      f"На Шафі вже є схоже оголошення: «{found.name}» — {found.url}")  # fmt: skip
        with self.conn:
            self.conn.execute(
                "UPDATE items SET duplicate_url = ?, duplicate_name = ? WHERE id = ?",
                (found.url, found.name, card.id),
            )
        if self.photos_dir is None:
            return
        target = self.photos_dir / f"{card.id}.jpg"
        target.unlink(missing_ok=True)  # старе фото іншого схожого — геть
        if found.photo and (body := self.fetch_photo(found.photo)):
            self.photos_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

    def _wait_command(self, session_id: int) -> Stop | None:
        """Стоп → причина; Пауза → чекати, доки не скасують."""
        while (what := command(self.conn, session_id)) == "pause":
            self.sleep(TICK)
        return Stop("stop", "Стоп") if what == "stop" else None

    def _pause(self, session_id: int, span: tuple[float, float] = PAUSE) -> Stop | None:
        """Пауза людини між товарами (чи перед повтором); Стоп і сон ноутбука перериває її."""
        left = self.rand(*span)
        while left > 0:
            step = min(TICK, left)
            started = self.clock()
            self.sleep(step)
            if self.clock() - started > SLEEP_JUMP:
                return Stop("sleep", "ноутбук засинав — робот зупинився")
            left -= step
            if (stop := self._wait_command(session_id)) is not None:
                return stop
        return None

    def _post_deleted(self, report: Report, card: review.Card) -> bool:
        """SITE-002: пост від бота видалено з каналу (вона видаляє лише продане) → Архів, не
        публікуємо. «Невідомо» (мережа, 429) — публікуємо як завжди: це не продаж."""
        if self.presence is None or card.post_id is None or not from_bot(self.conn, card.post_id):
            return False
        if self.presence(card.post_id) != GONE:
            return False
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE items SET status = ?, archived_at = ?, problem_code = ?, "
                "problem_text = ?, updated_at = ? WHERE id = ? AND status = ?",
                (Status.ARCHIVED, now(), POST_DELETED, POST_DELETED_TEXT, now(), card.id,
                 Status.APPROVED),
            )  # fmt: skip
        if cursor.rowcount == 1:
            report.notes.append(f"Пост {card.post_id}: {POST_DELETED_TEXT} — у Архіві")
            self.log(f"Пост {card.post_id}: {POST_DELETED_TEXT} — у Архіві, не публікую")
        return True

    def _problem(self, report: Report, card: review.Card, source: str, code: str, text: str):
        if _problem(self.conn, card.id, source, code, text):
            report.problems.append((card.post_id, code, text))
            self.log(f"Пост {card.post_id}: {text}")

    def _url(self) -> str:
        try:
            return self.session.page.url
        except Exception:  # noqa: BLE001
            return "невідома"

    def _shot(self, card: review.Card | None, suffix: str) -> Path:
        self.screens.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        post = f"-{card.post_id}" if card is not None else ""
        shot = self.screens / f"{stamp}{post}-{suffix}.png"
        try:
            self.session.page.screenshot(path=str(shot), full_page=True, timeout=SHOT_WAIT * 1000)
        except Exception:  # noqa: BLE001 — екран лише для звіту; не ховати справжню причину
            return shot
        return shot

    def _brand_miss(self, card: review.Card, driver: Driver) -> None:
        """«Бренд не знайдено»: скриншот, розмітка блоку й тексти варіантів — у `robot-screens/` і в
        журнал (ROBOT-006)."""
        miss = getattr(driver, "brand_miss", None)
        if miss is None:
            return
        shot = self._shot(card, "бренд")
        self.log(f"Пост {card.post_id}: бренд не знайдено; {miss.details()}. Екран: {shot.name}")
        body = f"<!-- {miss.details()} -->\n{miss.markup}"
        path = shot.with_suffix(".html")
        data = body.encode("utf-8")[:MARKUP_LIMIT].decode("utf-8", errors="ignore")
        try:
            path.write_text(data, encoding="utf-8")
        except OSError:
            return
        self.log(f"Пост {card.post_id}: розмітка поля «Бренд» — {path}")

    def _open_form(self, card: review.Card | None) -> Stop | None:
        """Форма «Додати товар» є → None; немає входу чи форми → причина зупинки з деталями."""
        opened = self.session.open_form()
        if opened.ok:
            return None
        if opened.state == NO_LOGIN:
            self.log(f"Шафа веде на вхід: {opened.detail()}")
            return Stop("login", "немає входу на Шафу")
        shot = self._shot(card, "нема-форми")
        self.log(f"Шафа не показала форму «Додати товар»: {opened.detail()}. Екран: {shot}")
        return Stop("no_form", f"Шафа не показала форму ({opened.detail()})")

    def _fill(
        self, report: Report, card: review.Card, driver: Driver, steps: list
    ) -> list[str] | Stop:
        """Відкрити форму й заповнити її. Тимчасовий збій (тайм-аут, обрив) до кліку «Додати річ» —
        форма заново й та сама Картка, до `ATTEMPTS` спроб з паузою (ROBOT-009). Усі спроби невдалі —
        зупинка зі своїм текстом, Картка лишається схваленою, у ліміт доби не йде (його рахує клік).
        Повторів після кліку немає ніколи — сюди клік не доходить."""
        for attempt in range(1, ATTEMPTS + 1):
            if (stop := self._open_form(card)) is not None:
                if stop.code == "login":
                    self._problem(
                        report,
                        card,
                        Status.APPROVED,
                        "robot_logged_out",
                        "Робот не увійшов на Шафу",
                    )
                return stop  # «немає форми» — Картка не винна, лишається у Схвалених
            driver.reset()  # texts/lists/typed/brand_miss — лише цієї Картки (ROBOT-008)
            try:
                return execute(steps, driver, strict=True)
            except Exception as error:
                if not is_transient(error):
                    raise
                self.log(
                    f"Пост {card.post_id}: тимчасовий збій, спроба {attempt} з {ATTEMPTS}: "
                    f"{type(error).__name__}: {str(error)[:300]}. Адреса: {self._url()}"
                )
            if attempt == ATTEMPTS:
                break
            if (stop := self._pause(report.session_id, RETRY_PAUSE)) is not None:
                return stop
        shot = self._shot(card, "збій")
        self.log(f"Пост {card.post_id}: Картка лишилась у Схвалених. Екран: {shot.name}")
        return Stop("slow", SLOW_TEXT.format(n=ATTEMPTS))

    def _one(
        self, report: Report, item_id: int, driver: Driver, listings: list
    ) -> bool | Stop | None:
        """Одна Картка. True — опубліковано; Stop — причина зупинки; None — далі."""
        card = review.load_card(self.conn, item_id)
        if card is None or card.status != Status.APPROVED:
            return None
        ok = Status.APPROVED
        if other := already_published(self.conn, card.id, card.post_id):
            self._problem(report, card, ok, "already_published",
                          f"Цей пост уже опубліковано з іншої папки ({other})")  # fmt: skip
            return None
        if self._post_deleted(report, card):
            return None
        version = self.conn.execute(
            "SELECT rules_version FROM items WHERE id = ?", (card.id,)
        ).fetchone()[0]
        if version is None or version < rules.RULES_VERSION:
            # Схвалена за старими правилами (розміри, «ідуть на»…): робот її не бере (ROBOT-008)
            self._problem(report, card, ok, STALE_RULES, STALE_TEXT)
            return None
        try:
            steps, notes = plan(card, self.root)
        except PlanError as error:
            self._problem(report, card, ok, "robot_not_ready", str(error))
            return None
        checked = self.conn.execute(
            "SELECT duplicate_ok_at FROM items WHERE id = ?", (card.id,)
        ).fetchone()[0]  # «Це інший товар» — Продавчиня вже перевірила
        if not checked and (duplicate := card_duplicate(card, listings)):
            self._duplicate(report, card, duplicate)
            return None

        with self.conn:  # для екрана: «чекаю, поки допишеться поточний товар (~N хв)»
            self.conn.execute(
                "UPDATE sessions SET item_started_at = ? WHERE id = ?", (now(), report.session_id)
            )

        try:
            filled = self._fill(report, card, driver, steps)
        except FieldMissing as error:
            shot = self._shot(card, "нема-поля")
            self._problem(report, card, ok, "robot_field_missing",
                          f"Робот не знайшов на Шафі поле «{error}». Екран: {shot.name}")  # fmt: skip
            if error.details:  # діагностика: що є на формі (не вгадуємо поле, CORE-008 D)
                self.log(
                    f"Пост {card.post_id}: поле «{error}» не знайдено; на формі — {error.details}"
                )
            if dump := save_markup(error, shot):
                self.log(f"Пост {card.post_id}: розмітка поля «{error.field}» — {dump}")
            return Stop("site", "поле не знайдено — мабуть, Шафа змінила форму")
        except BrandMissing as error:
            # Бренд є у довіднику, а наживо не знайшовся — збій робота, не Картки: далі наступна
            self._brand_miss(card, driver)
            self._problem(report, card, ok, "robot_brand_missing", BRAND_BROKEN.format(error))
            return None
        except PhotosShort as error:
            self._problem(report, card, ok, "robot_photos", str(error))
            return Stop("photos", "не всі фото завантажились")
        if isinstance(filled, Stop):
            return filled
        notes += filled
        if getattr(driver, "brand_miss", None):
            self._brand_miss(card, driver)  # бренду немає ні у знімку, ні наживо: публікуємо
        if attempts := self.session.publish_attempts():
            raise RuntimeError(f"Заблоковано спроби публікації до кліку: {attempts}")

        return self._submit(report, card, driver, notes, listings)

    def _submit(
        self, report: Report, card: review.Card, driver: Driver, notes: list[str], listings: list
    ):
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
            return Stop("site", "кнопку «Додати річ» не знайдено")
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
            listing = card_listing(card, result.product_id, url)  # дубль не пройде до перечитування
            listings.append(listing)
            listings_mod.add(self.conn, listing)
            self._shot(card, "опубліковано")
            self.session.leave_result_page()  # реклама/підняття — не чекаємо, нічого не тиснемо
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
            return Stop("error", "захист заблокував запит публікації")
        if not result.sent:
            self._problem(report, card, pub, "robot_not_sent",
                          "Форма не надіслалась — мабуть, Шафа вимагає поле, якого робот не знає. "
                          f"Екран: {shot.name}")  # fmt: skip
            return Stop("site", "форма не надіслалась")
        if result.errors:
            self._problem(report, card, pub, "publish_error",
                          "Шафа не прийняла товар: " + "; ".join(result.errors))  # fmt: skip
            return Stop("error", "Шафа не прийняла товар")
        self.log(f"Пост {card.post_id}: немає певної відповіді Шафи за {SUBMIT_WAIT} с "
                 f"({result.failure or 'немає id у відповіді'}). Адреса: {self._url()}. "
                 f"Екран: {shot}")  # fmt: skip
        self._problem(report, card, pub, "publish_uncertain",
                      f"{UNCERTAIN} ({result.failure or 'немає id у відповіді'})")  # fmt: skip
        return Stop("error", "немає певної відповіді Шафи")


# --- Рішення людини щодо «липких» проблем публікації (UI-002) ---

SHAFA_HOSTS = {"shafa.ua", "www.shafa.ua"}
_ITEM_ID = re.compile(r"(\d{4,12})(?:-[\w-]*)?")
UNSURE = ("publish_uncertain", "publish_error")


def parse_shafa_link(text: str) -> tuple[int, str] | None:
    """Посилання на оголошення Шафи або голий ID → (id, канонічне посилання); інше — None (D6)."""
    text = text.strip()
    if text.isdigit() and 4 <= len(text) <= 12:
        return int(text), ITEM_URL.format(id=int(text))
    parts = urlsplit(text if "://" in text else "https://" + text)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in SHAFA_HOSTS:
        return None
    if parts.port or parts.username or parts.password:
        return None
    segments = [s for s in parts.path.split("/") if s]
    match = _ITEM_ID.fullmatch(segments[-1]) if segments else None
    if not match:
        return None
    return int(match[1]), "https://shafa.ua/" + "/".join(segments)


def _decide(
    conn: sqlite3.Connection, item_id: int, codes: tuple[str, ...], sets: str, values: tuple
) -> bool:
    with conn:
        cursor = conn.execute(
            f"UPDATE items SET {sets}, updated_at = ? WHERE id = ? AND status = ? "
            f"AND problem_code IN ({', '.join('?' * len(codes))})",
            (*values, now(), item_id, Status.PROBLEM, *codes),
        )
    return cursor.rowcount == 1


def on_shafa(conn: sqlite3.Connection, root: Path, item_id: int, link: str) -> str:
    """«На Шафі є → в Опубліковані». Повертає код повідомлення екрана."""
    parsed = parse_shafa_link(link)
    if parsed is None:
        return "bad_link"
    shafa_id, url = parsed
    done = _decide(
        conn, item_id, UNSURE,
        "status = ?, shafa_id = ?, shafa_url = ?, published_at = ?, problem_code = NULL, "
        "problem_text = NULL",
        (Status.PUBLISHED, shafa_id, url, now()),
    )  # fmt: skip
    if not done:
        return "stale"
    folder = conn.execute("SELECT folder_name FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    move_published(root, folder)
    return "on_shafa"


def _same_name(a: str, b: str) -> str:
    return " ".join(a.casefold().split())


def listing_by_name(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """Її товар у знімку «Мої речі» з тією самою назвою, що й Картка (CORE-008 C), або None."""
    card = review.load_card(conn, item_id)
    title = card.fields["title"].value if card else None
    if not title:
        return None
    wanted = _same_name(title, "")
    return next(
        (
            r
            for r in conn.execute("SELECT name, url FROM my_listings ORDER BY id DESC")
            if _same_name(r["name"], "") == wanted
        ),
        None,
    )


def not_on_shafa(conn: sqlite3.Connection, item_id: int) -> str:
    """«На Шафі немає → у Схвалені»: робот спробує ще раз (людина перевірила Шафу).

    Перед повтором — пошук за назвою у знімку «Мої речі» (CORE-008 C): повтор публікації дає
    дубль, тож знайдений товар не відпускаємо — Продавчиня дивиться на нього й каже «На Шафі є».
    """
    found = listing_by_name(conn, item_id)
    if found is not None:
        with conn:
            conn.execute(
                "UPDATE items SET problem_text = ?, updated_at = ? WHERE id = ? AND status = ? "
                "AND problem_code IN (?, ?)",
                (
                    f"{UNCERTAIN}. У «Мої речі» вже є товар із такою назвою: {found['url']}",
                    now(), item_id, Status.PROBLEM, *UNSURE,
                ),
            )  # fmt: skip
        return "listing_found"
    done = _decide(
        conn, item_id, UNSURE, "status = ?, problem_code = NULL, problem_text = NULL",
        (Status.APPROVED,),
    )  # fmt: skip
    return "not_on_shafa" if done else "stale"


def other_item(conn: sqlite3.Connection, item_id: int) -> str:
    """«Це інший товар → у Схвалені»: на схожі оголошення ця Картка більше не перевіряється."""
    done = _decide(
        conn, item_id, ("possible_duplicate",),
        "status = ?, problem_code = NULL, problem_text = NULL, duplicate_ok_at = ?",
        (Status.APPROVED, now()),
    )  # fmt: skip
    return "other_item" if done else "stale"


def to_archive(conn: sqlite3.Connection, item_id: int) -> str:
    """«Справді дубль → в Архів» (і для «пост уже опубліковано»): прибирається через 7 днів."""
    done = _decide(
        conn, item_id, ("possible_duplicate", "already_published"),
        "status = ?, archived_at = ?", (Status.ARCHIVED, now()),
    )  # fmt: skip
    return "duplicate_archived" if done else "stale"
