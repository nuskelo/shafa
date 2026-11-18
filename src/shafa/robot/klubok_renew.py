"""KLB-004: «Обновить все» на Клубку робить робот (`robot --klubok-renew`, початок сесії публікації,
щоденний запуск у програмі).

`renew` (KLB-013): вхід → знімок «до» (`type=0` ∪ `type=1`; **не вдався — це ще не стоп**, лише
нічого не записано й P невідоме) → сьогодні GET уже був — нічого → сторінка керування (лише
читання): посилання «Обновить все (N)» немає — нічого (термін попереду — «Наступне оновлення: …»,
інакше тиха подія) → позначка доби → **один** GET `/renew/all` за `RenewTicket` (`page.goto`; у
`--human-click` тисне людина) → пауза 5–10 с → знімок «після» (не вдався — стоп) і сторінка
керування → `renew.judge`: успіх — тиха `klubok.renew` і `renewed_at`; інакше гучна
`klubok.renew_failed`. Знімок «до» не вдався, а GET не буде (посилання немає, сьогодні вже було) —
`RenewStop` з його кодом; `klubok_snapshot_drop` при посиланні — текст «Схоже, оголошення прострочені
(N)…» (`drop_text`). 429 / немає входу — `RenewStop` одразу. Повтору немає.

`Daily` — за зразком `rehearse.Nightly`, але без `GRACE`: о `SHAFA_KLUBOK_RENEW_AT` і пізніше того ж
дня, коли `SHAFA_KLUBOK`, робот вільний і `renew.wanted`; позначка доби — лише після запуску.
"""

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shafa import config, events
from shafa.klubok import queue, renew, snapshot
from shafa.robot import klubok_form as form

LOGIN = "klubok_login"  # ті самі коди, що в `klubok_publish`
TOO_MANY = snapshot.TOO_MANY
NOT_DUE, DONE_TODAY, NO_LINK, NO_PAGE = "not_due", "done_today", "no_link", "no_page"
RENEWED, FAILED, NOT_CLICKED = "renewed", "renew_failed", "renew_not_clicked"
QUIET = (NOT_DUE, DONE_TODAY, NO_LINK, RENEWED)  # код виходу 0
SETTLE = (5.0, 10.0)  # с між GET і новим знімком
HUMAN_WAIT = 600.0  # с: `--human-click` — людина тисне «Обновить все»
FAILED_TEXT = "Оновлення на Клубку не підтвердилось — перевірте сторінку"
LOGIN_TEXT = "Увійдіть у Клубок: python -m shafa robot --klubok-login"
DAILY_MARK = "klubok-renew-daily"  # дата, коли щоденний запуск уже був
TICK = 60.0  # с між перевірками годинника


class RenewStop(Exception):
    """Зупинка з кодом сесії (`klubok_login`, `klubok_429`, коди знімка)."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


@dataclass(frozen=True)
class Outcome:
    code: str
    text: str
    link: int | None = None  # N з «Обновить все (N)»
    overdue: int = 0  # |P|: вкладка «Обновить» за знімком до (без нього — N)
    renewed: int = 0
    snapped: bool = (
        False  # KLB-013: у цьому запуску знімок записано (сесії публікації ще один не треба)
    )

    @property
    def ok(self) -> bool:
        return self.code in QUIET


def _fmt(moment: datetime, now: datetime) -> str:
    return f"{moment.astimezone(now.tzinfo):%d.%m %H:%M}"


def drop_text(link: renew.Link | None, text: str) -> str:
    """KLB-013 п. 5: знімок упав, а посилання «Обновить все (N)» є — причина, скоріше, прострочення."""
    if link is None or not link.present:
        return text
    shown = "" if link.count is None else f" ({link.count})"
    return f"Схоже, оголошення прострочені{shown} — потрібне «Обновить все» (знімок не записано)"


def explain(session: form.Session, error: snapshot.SnapshotError) -> str:
    """Текст зупинки знімка: для `klubok_snapshot_drop` — зазирнути на сторінку керування (лише
    читання). Будь-яка помилка читання — текст як був."""
    if error.code != snapshot.DROPPED:
        return error.text
    try:
        status, html = session.fetch_text(form.MANAGE_PATH)
    except Exception:  # noqa: BLE001 — лише пояснення, зупинка все одно буде
        return error.text
    return drop_text(renew.link(html) if status == 200 else None, error.text)


class Renewer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        session: form.Session,
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        human: bool = False,
        human_wait: float = HUMAN_WAIT,
        sleep: Callable[[float], None] = lambda seconds: None,
        log: Callable[[str], None] = print,
        page_pause: tuple[float, float] = (1.0, 2.0),
        settle: tuple[float, float] = SETTLE,
        data_dir: Path | None = None,
    ) -> None:
        self.conn, self.session, self.now, self.human = conn, session, now, human
        self.human_wait, self.sleep, self.log = human_wait, sleep, log
        self.page_pause, self.settle, self.data_dir = page_pause, settle, data_dir

    def _check_429(self) -> None:
        if self.session.too_many:
            raise RenewStop(TOO_MANY, f"Клубок відповів 429 (забагато запитів): "
                                      f"{self.session.too_many[0]}")  # fmt: skip

    def _snapshot(self) -> snapshot.Read:
        """Знімок «після» (і для сесії публікації): не вдався — `RenewStop`."""
        try:
            snap, _count = self._take()
        except snapshot.SnapshotError as error:
            raise RenewStop(error.code, error.text) from None
        self._check_429()
        return snap

    def _take(self) -> tuple[snapshot.Read, int]:
        return snapshot.take(
            self.conn, self.session.fetch_text,
            pause=lambda: self.sleep(random.uniform(*self.page_pause)),
            wait=self.sleep, log=self.log,
        )  # fmt: skip

    def _before(self) -> tuple[snapshot.Read | None, snapshot.SnapshotError | None]:
        """Знімок «до» (KLB-013): помилка — не стоп (крім 429), лише нічого не записано."""
        try:
            snap, _count = self._take()
        except snapshot.SnapshotError as error:
            if error.code == TOO_MANY:
                raise RenewStop(error.code, error.text) from None
            self.log(f"Знімок до «Обновить все» не записано: {error.text}")
            return None, error
        self._check_429()
        return snap, None

    def _manage(self) -> renew.Link | None:
        status, html = self.session.fetch_text(form.MANAGE_PATH)
        self._check_429()
        return renew.link(html) if status == 200 else None

    def run(self, logged_in: bool = False) -> Outcome:
        """`logged_in` — вхід уже перевірено (початок сесії публікації)."""
        if not logged_in:
            opened = self.session.open_form()
            if not opened.ok or self.session.user_id() != form.KLUBOK_USER:
                raise RenewStop(LOGIN, LOGIN_TEXT)
        snap, broken = self._before()
        snapped = snap is not None
        now = self.now()
        if renew.done_on(now.date(), self.data_dir):
            if broken is not None:
                raise RenewStop(broken.code, drop_text(self._manage(), broken.text))
            self.log("Обновить все: сьогодні вже оновлювали — наступне завтра")
            return Outcome(DONE_TODAY, "сьогодні вже оновлювали", snapped=snapped)
        page = self._manage()
        if page is None or not page.present:
            if broken is not None:
                raise RenewStop(broken.code, broken.text)
            return self._nothing(now, page, len(snap.renewable))
        before = sorted(snap.renewable) if snap is not None else None
        active_before = renew.active_count(self.conn) if snap is not None else 0
        total = len(before) if before is not None else (page.count or 0)
        shown = "?" if page.count is None else page.count
        where = (f"у вкладці «Обновить» за знімком: {total}" if before is not None
                 else "знімка до немає")  # fmt: skip
        self.log(f"Обновить все ({shown}); {where}")
        sent = self._send(now)
        if not sent:
            text = "«Обновить все» не натиснуто — нічого не оновлено"
            self.log(f"Обновить все: {text}")
            events.emit("klubok.renew_skip", f"Клубок: {text}")
            return Outcome(NOT_CLICKED, text, page.count, total, snapped=snapped)
        try:
            self._check_429()
            self.sleep(random.uniform(*self.settle))
            after = self._snapshot()
            after_link = self._manage()
        except RenewStop as stop:
            self._failed(now, stop.text, page.count, total)
            raise
        verdict = renew.judge(
            before, page.count, active_before, renew.renew_dates(self.conn),
            len(after.renewable), renew.active_count(self.conn), now, after_link,
        )  # fmt: skip
        if not verdict.ok:
            return self._failed(now, verdict.text, page.count, total, after)
        marked = renew.mark_ok(self.conn, verdict.renewed, now)
        count = renew.active_count(self.conn)
        renew.save(self.data_dir, ok=True, text=verdict.text, count=count, link=page.count,
                   overdue=total, renewed=len(verdict.renewed))  # fmt: skip
        text = (f"Клубок: «Обновить все» — {verdict.text} (посилання: {shown}), активних {count}"
                + (f", виставлених програмою {marked}" if marked else ""))  # fmt: skip
        self.log(text)
        events.emit("klubok.renew", text, data={
            "link": page.count, "overdue": total, "renewed": len(verdict.renewed),
            "tabs": list(after.tabs) if after.tabs else None,
        })  # fmt: skip
        return Outcome(RENEWED, verdict.text, page.count, total, len(verdict.renewed), True)

    def _nothing(self, now: datetime, page: renew.Link | None, renewable: int) -> Outcome:
        """Посилання немає (чи сторінку не прочитано) — GET не робимо; знімок «до» записано."""
        if page is None:
            text = "сторінку керування не прочитано — нічого не оновлював"
            code = NO_PAGE
        else:
            first = renew.due(self.conn)
            if first is not None and first > now:
                text = f"Наступне оновлення: {_fmt(first, now)}"
                self.log(f"Обновить все: {text}")
                return Outcome(NOT_DUE, text, snapped=True)
            text = f"прострочених немає (посилання немає; у вкладці «Обновить» — {renewable})"
            code = NO_LINK
        self.log(f"Обновить все: {text}")
        events.emit("klubok.renew_skip", f"Клубок: «Обновить все» — {text}")
        return Outcome(code, text, overdue=renewable, snapped=True)

    def _send(self, now: datetime) -> bool:
        """Один GET `/renew/all`. Позначка доби: робот — до запиту; `--human-click` — лише коли guard
        пропустив запит «Обновить все» (RENEWED, `Session.on_renew`): людина не натиснула чи
        натиснула не те — позначки немає, того ж дня можна ще раз."""

        def mark() -> None:
            renew.save(self.data_dir, day=now.date().isoformat(), at=now.isoformat(), ok=None)

        if self.human:
            self.session.on_renew = mark
            self.session.open_manage()
            self.log("Натисніть «Обновить все» у вікні Chrome (робот чекає до "
                     f"{self.human_wait / 60:.0f} хв)")  # fmt: skip
            return self.session.wait_renew_click(self.human_wait)
        mark()
        return self.session.renew_all()

    def _failed(
        self, now: datetime, why: str, link: int | None, overdue: int,
        after: snapshot.Read | None = None,
    ) -> Outcome:  # fmt: skip
        renew.save(self.data_dir, ok=False, text=why, link=link, overdue=overdue)
        text = f"{FAILED_TEXT}: {why}"
        self.log(text)
        events.emit("klubok.renew_failed", f"Клубок: {text}", loud=True,
                    data={"link": link, "overdue": overdue})  # fmt: skip
        return Outcome(FAILED, why, link, overdue, snapped=after is not None)


def renew_once(conn: sqlite3.Connection, session: form.Session, **kwargs: Any) -> Outcome:
    logged_in = kwargs.pop("logged_in", False)
    return Renewer(conn, session, **kwargs).run(logged_in=logged_in)


# --- Щоденний запуск у програмі ---


class Daily:
    """Раз на добу о `SHAFA_KLUBOK_RENEW_AT` чи пізніше того ж дня запускає `--klubok-renew`.

    На відміну від `rehearse.Nightly`: без `GRACE`, робот зайнятий чи Mac спав — наступний тик того ж
    дня; позначка доби — лише після запуску, коли термін уже минув (чи знімок старий); термін ще
    попереду (≤ 1 год) — повтор не частіше ніж раз на годину. Налаштування читаються щоразу.
    """

    def __init__(
        self,
        command: Callable[[], list[str]],
        spawn: Callable[[list[str]], object],
        busy: Callable[[], object],
        log: Callable[[str], None],
        connect: Callable[[], sqlite3.Connection],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        folder: Path | None = None,
    ) -> None:
        self.command, self.spawn, self.busy, self.log = command, spawn, busy, log
        self.connect, self.clock, self.folder = connect, clock, folder
        self.said: set[str] = set()  # що вже писали в журнал сьогодні (не спамити кожну хвилину)
        self.last: datetime | None = None  # коли запускали востаннє (термін ще попереду)

    def _marker(self) -> Path:
        return (self.folder or config.data_dir()) / DAILY_MARK

    def _done(self, today: str) -> bool:
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

    def _say(self, key: str, text: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(text)

    def tick(self) -> str | None:
        """started / busy — що зроблено цього разу; None — нічого."""
        now = self.clock()
        today = f"{now:%Y-%m-%d}"
        if not queue.enabled():
            return None
        try:
            at = renew.at_setting()
        except renew.SetupError as error:
            self._say(f"bad:{error}", f"{error} — щоденного «Обновить все» немає")
            return None
        if at is None or now.time() < at or self._done(today):
            return None
        if renew.done_on(now.date(), self.folder):
            return None  # сьогодні вже оновила сесія публікації чи команда
        conn = self.connect()
        try:
            if not renew.wanted(conn, now):
                return None
            first, read = renew.due(conn), renew.read_at(conn)
        finally:
            conn.close()
        # термін ще попереду (≤ 1 год): команда скаже «не настав» — доба лишається відкритою,
        # наступна спроба не раніше ніж за годину
        final = read is None or now - read > renew.STALE or (first is not None and first <= now)
        if not final and self.last is not None and now - self.last < renew.AHEAD:
            return None
        if self.busy():
            self._say(
                f"busy:{today}", f"«Обновить все» о {at:%H:%M}: робот зайнятий — спробую пізніше"
            )
            return "busy"
        self.spawn(self.command())
        self.last = now
        if final:
            self._mark(today)
        self.log(f"«Обновить все» на Клубку запущено ({now:%H:%M})")
        return "started"

    def run(self, stop: Any, interval: float = TICK) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — фоновий потік програми не падає
                self._say(f"err:{error}", f"«Обновить все»: {type(error).__name__}: {error}")
            stop.wait(interval)
