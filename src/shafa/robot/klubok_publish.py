"""KLB-003 D2/D4/D5: сесія публікації на Клубку — по одній Картці з черги KLB-002.

Порядок: обірвані сесії Клубку → `crash`, `queue.recover` → рядок `sessions` (`site = 'klubok'`) →
вхід (`/item/add`: є форма і `#user_id` = 198135, інакше стоп `klubok_login`) → знімок її оголошень
(`klubok.snapshot`) → цикл: `queue.take` (щоразу заново: ліміт доби, дублі) → `queue.start` → свіжа
форма → `KlubokDriver.fill` + `verify` → квиток → клік `#btn-submit` (чи людина, `--human-click`) →
відповідь `add-edit-used`:

* `success` + `used_id` → `published` (посилання — з наступного знімка, DRIFT-1);
* `Error` → Проблема `klubok_captcha`, стоп сесії;
* запит пішов, певної відповіді немає → Проблема `klubok_uncertain`, стоп (повтору немає ніколи);
* запит не пішов → `release` (спроба в ліміті), стоп `klubok_not_sent`;
* 429 від Клубку на будь-якому запиті → стоп `klubok_429`.

Помилка форми до кліку — Проблема з кодом KLB-001 і наступна Картка. Між Картками пауза 60–180 с.
Ctrl+C — прапорець: поточна Картка доробляється (відповідь дочікується), потім стоп. Наприкінці,
якщо щось виставлено, — ще один знімок: посилання й звірка «чи видно оголошення в її списку».
"""

import contextlib
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shafa import events
from shafa.db import now
from shafa.klubok import queue, snapshot
from shafa.robot import klubok_form as form
from shafa.robot.klubok_guard import Forbidden

SUBMIT_WAIT = 60.0  # с на відповідь `add-edit-used` після кліку робота
HUMAN_WAIT = 600.0  # с, `--human-click`: людина перевіряє форму й тисне сама
PAUSE = (60.0, 180.0)  # с між Картками
PAGE_PAUSE = (1.0, 2.0)  # с між сторінками знімка
LIMIT_RANGE = (1, 30)

DONE, STOPPED, CRASH, ERROR = "done", "stop", "crash", "klubok_error"
LOGIN, CAPTCHA, NOT_SENT = "klubok_login", "klubok_captcha", "klubok_not_sent"
UNCERTAIN, TOO_MANY, FORBIDDEN = queue.UNCERTAIN, snapshot.TOO_MANY, "klubok_forbidden"
LOUD = {CAPTCHA, UNCERTAIN, LOGIN, TOO_MANY, snapshot.FAILED, snapshot.DROPPED, FORBIDDEN, ERROR}

LOGIN_TEXT = "Увійдіть у Клубок: python -m shafa robot --klubok-login"
STOPS = {
    DONE: "виставлено, скільки просили (--limit)",
    STOPPED: "зупинено (Ctrl+C) після поточної Картки",
    LOGIN: LOGIN_TEXT,
    CAPTCHA: "Клубок не прийняв публікацію: захист від роботів",
    UNCERTAIN: "немає певної відповіді Клубку — перевірте, чи опубліковано",
    NOT_SENT: "«Опубликовать» нічого не надіслало — Картка лишилась у черзі",
    TOO_MANY: "Клубок відповів 429 (забагато запитів)",
    FORBIDDEN: "захист робота заблокував дію",
    ERROR: "несподівана помилка робота",
}
UNCERTAIN_TEXT = "Перевірте на Клубку, чи опубліковано"


class Stop(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(text or code)
        self.code = code
        self.text = text or STOPS.get(code) or queue.REASONS.get(code, code)


@dataclass(frozen=True)
class Options:
    limit: int
    post_id: int | None = None
    human: bool = False
    submit_wait: float = SUBMIT_WAIT
    human_wait: float = HUMAN_WAIT
    pause: tuple[float, float] = PAUSE
    page_pause: tuple[float, float] = PAGE_PAUSE


@dataclass
class Result:
    session_id: int
    code: str = DONE
    reason: str = ""
    published: list[tuple[int, int, str]] = field(default_factory=list)  # item, id Клубку, назва
    problems: list[tuple[int, int | None, str, str]] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
    attempts: int = 0

    def lines(self) -> list[str]:
        """Звіт у термінал, ≤ 15 рядків."""
        head = f"Клубок · сесія {self.session_id}: виставлено {len(self.published)}"
        out = [f"{head}, проблем {len(self.problems)}"]
        shown = self.published[:7]
        out += [f"  ✅ {klubok_id} · {name[:70]}" for _item, klubok_id, name in shown]
        if len(self.published) > len(shown):
            out.append(f"  … ще виставлено {len(self.published) - len(shown)}")
        for _item, post_id, _code, text in self.problems[:3]:
            out.append(f"  ⚠️ пост {post_id}: {text[:90]}")
        if len(self.problems) > 3:
            out.append(f"  … ще проблем {len(self.problems) - 3} — екран, «Проблеми»")
        if self.recovered:
            out.append(f"  Після обриву — у Проблеми «перевірте»: {len(self.recovered)}")
        out.append(f"Зупинка: {self.reason}")
        return out[:15]


def close_crashed(conn: sqlite3.Connection) -> int:
    """Відкриті сесії Клубку (процес помер) → `crash`. Сесій Шафи не чіпає."""
    with conn:
        cursor = conn.execute(
            "UPDATE sessions SET ended_at = ?, stop_code = ?, "
            "stop_reason = 'обрив (робот Клубку не завершив сесію)' "
            "WHERE ended_at IS NULL AND site = 'klubok'",
            (now(), CRASH),
        )
    return cursor.rowcount


def nap(seconds: float, stopping: Callable[[], bool]) -> None:
    """Пауза, яку перериває Ctrl+C (прапорець)."""
    end = time.monotonic() + seconds
    while not stopping() and (left := end - time.monotonic()) > 0:
        time.sleep(min(1.0, left))


class Run:
    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        session: form.Session,
        opts: Options,
        *,
        make_driver: Callable[[form.Session], form.KlubokDriver] = form.KlubokDriver,
        today: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        sleep: Callable[[float], None] | None = None,
        stopping: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = print,
        screens: Path | None = None,
    ) -> None:
        if not LIMIT_RANGE[0] <= opts.limit <= LIMIT_RANGE[1]:
            raise ValueError(f"--limit від {LIMIT_RANGE[0]} до {LIMIT_RANGE[1]}")
        self.conn, self.root, self.session, self.opts = conn, root, session, opts
        self.make_driver, self.today, self.stopping, self.log = make_driver, today, stopping, log
        self.sleep = sleep or (lambda s: nap(s, stopping))
        self.screens = screens

    # --- сесія ---

    def run(self) -> Result:
        conn = self.conn
        if crashed := close_crashed(conn):
            self.log(f"Клубок: закрито обірваних сесій — {crashed}")
        recovered = queue.recover(conn)
        if recovered:
            self.log(f"Клубок: після обриву в Проблеми «перевірте, чи опубліковано» — {recovered}")
        with conn:
            sid = conn.execute(
                "INSERT INTO sessions (started_at, site) VALUES (?, 'klubok')", (now(),)
            ).lastrowid
        self.result = Result(sid, recovered=recovered)
        events.emit("klubok.start", f"Клубок: сесія {sid}, до {self.opts.limit} оголошень")
        try:
            self._loop()
        except Stop as stop:
            self.result.code, self.result.reason = stop.code, stop.text
        except Exception as error:  # noqa: BLE001 — сесію треба закрити, що б не сталось
            self.result.code = ERROR
            self.result.reason = f"{STOPS[ERROR]}: {type(error).__name__}: {str(error)[:200]}"
        finally:
            self._finish()
        return self.result

    def _loop(self) -> None:
        opened = self.session.open_form()
        if not opened.ok or self.session.user_id() != form.KLUBOK_USER:
            raise Stop(LOGIN)
        try:
            snapshot.refresh(
                self.conn, self.session.fetch_text,
                pause=lambda: self.sleep(random.uniform(*self.opts.page_pause)),
                wait=self.sleep, log=self.log,
            )  # fmt: skip
        except snapshot.SnapshotError as error:
            raise Stop(error.code, error.text) from None
        self._check_429()
        while True:
            if self.stopping():
                raise Stop(STOPPED)
            if self.result.attempts >= self.opts.limit:
                raise Stop(DONE)
            taken = queue.take(self.conn, self.root, self.today(), post_id=self.opts.post_id)
            if not taken.cards:
                raise Stop(taken.code or queue.EMPTY)
            self._card(taken.cards[0])
            if self.result.attempts >= self.opts.limit:
                raise Stop(DONE)
            if self.stopping():
                raise Stop(STOPPED)
            pause = random.uniform(*self.opts.pause)
            self.log(f"Пауза {pause:.0f} с")
            self.sleep(pause)

    def _check_429(self) -> None:
        if self.session.too_many:
            raise Stop(TOO_MANY, f"{STOPS[TOO_MANY]}: {self.session.too_many[0]}")

    # --- одна Картка ---

    def _card(self, verdict: queue.Verdict) -> None:
        conn, item_id, ad = self.conn, verdict.item_id, verdict.ad
        if ad is None or not queue.start(conn, item_id, self.result.session_id):
            self.log(f"Пост {verdict.post_id}: Картку вже взято — пропускаю")
            return
        self.result.attempts += 1
        with conn:
            conn.execute(
                "UPDATE sessions SET item_started_at = ? WHERE id = ?",
                (now(), self.result.session_id),
            )
        self.log(f"Пост {verdict.post_id}: заповнюю форму Клубку")
        try:
            outcome = self._submit(verdict)
        except Stop:
            raise
        except Exception as error:  # noqa: BLE001 — до відповіді: що з Карткою — за тим, чи пішов запит
            self._unsettled(item_id)
            code = FORBIDDEN if isinstance(error, Forbidden) else ERROR
            raise Stop(code, f"{STOPS[code]}: {str(error)[:200]}") from None
        finally:
            self.session.drop_ticket()
        if outcome is None:  # Проблема форми — далі наступна
            return
        self._outcome(verdict, outcome)

    def _unsettled(self, item_id: int) -> None:
        """Обрив посеред Картки: запит пішов — «перевірте», ні — назад у чергу."""
        if self.session.sent:
            queue.problem(self.conn, item_id, UNCERTAIN, UNCERTAIN_TEXT)
        else:
            queue.release(self.conn, item_id)

    def _submit(self, verdict: queue.Verdict) -> form.Submitted | None:
        session, item_id, ad = self.session, verdict.item_id, verdict.ad
        assert ad is not None
        opened = session.open_form()
        if not opened.ok or session.user_id() != form.KLUBOK_USER:
            queue.release(self.conn, item_id)
            raise Stop(LOGIN)
        driver = self.make_driver(session)
        try:
            driver.fill(ad)
            driver.verify(ad)
        except form.KlubokError as error:
            if session.too_many:
                queue.release(self.conn, item_id)
                self._check_429()
            queue.problem(self.conn, item_id, error.code, error.text)
            self.result.problems.append((item_id, verdict.post_id, error.code, error.text))
            self.log(f"Пост {verdict.post_id}: {error.code} — {error.text}")
            events.emit("klubok.problem", f"Клубок: пост {verdict.post_id} — {error.text}")
            return None
        if session.too_many:
            queue.release(self.conn, item_id)
            self._check_429()
        for note in driver.notes:
            self.log(f"  · {note}")
        session.issue_ticket(item_id)
        if self.opts.human:
            self.log(
                f"Пост {verdict.post_id}: перевірте форму й натисніть «Опубликовать» — "
                f"чекаю {self.opts.human_wait / 60:.0f} хв"
            )
            return session.wait_submit(self.opts.human_wait)
        session.click_submit()
        return session.wait_submit(self.opts.submit_wait)

    def _outcome(self, verdict: queue.Verdict, sent: form.Submitted) -> None:
        conn, item_id, post_id = self.conn, verdict.item_id, verdict.post_id
        name = verdict.ad.name if verdict.ad else ""
        if sent.outcome == form.SUCCESS and sent.klubok_id is not None:
            queue.published(conn, item_id, sent.klubok_id, None)
            self.result.published.append((item_id, sent.klubok_id, name))
            self.log(f"Пост {post_id}: виставлено на Клубку, id {sent.klubok_id}")
            events.emit("klubok.post", f"Клубок: пост {post_id} виставлено (id {sent.klubok_id})")
            self._check_429()
            return
        if sent.outcome == form.NOT_SENT:
            queue.release(conn, item_id)
            self._blocked_log()
            raise Stop(NOT_SENT, f"{STOPS[NOT_SENT]} (пост {post_id})")
        if sent.outcome == form.CAPTCHA:
            text = (
                f"Клубок не прийняв: захист від роботів (оцінка {sent.score}). "
                "Перевірте на Клубку, що оголошення немає."
            )
            code, stop = CAPTCHA, CAPTCHA
        else:
            text = f"{UNCERTAIN_TEXT} ({sent.why})"
            code = UNCERTAIN
            stop = TOO_MANY if sent.http == 429 or self.session.too_many else UNCERTAIN
        queue.problem(conn, item_id, code, text)
        self.result.problems.append((item_id, post_id, code, text))
        raise Stop(stop, f"{STOPS[stop]} (пост {post_id})")

    def _blocked_log(self) -> None:
        """Що guard заблокував у цій сесії — у файл (HYP-3: хости reCAPTCHA видно лише наживо)."""
        blocked = self.session.unexpected_blocks()
        if not blocked:
            self.log("Заблокованих запитів немає")
            return
        lines = [f"{url} — {why}" for url, why in blocked]
        if self.screens is None:
            for line in lines[:10]:
                self.log(f"  заблоковано: {line}")
            return
        self.screens.mkdir(parents=True, exist_ok=True)
        path = self.screens / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-klubok-blocked.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log(f"Заблоковано запитів: {len(lines)} — список у {path}")

    # --- кінець ---

    def _finish(self) -> None:
        result, conn = self.result, self.conn
        if result.published and result.code not in (TOO_MANY, LOGIN):
            self._final_snapshot()
        if result.code not in (NOT_SENT,):
            with contextlib.suppress(Exception):
                self._blocked_log()
        with conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ?, stop_code = ?, stop_reason = ?, published = ?, "
                "problems = ? WHERE id = ?",
                (now(), result.code, result.reason, len(result.published), len(result.problems),
                 result.session_id),
            )  # fmt: skip
        text = (
            f"Клубок · сесія {result.session_id}: виставлено {len(result.published)}, "
            f"проблем {len(result.problems)}. {result.reason}"
        )
        if result.code in LOUD:
            events.emit("klubok.stop", f"⛔ {text}", loud=True, data={"code": result.code})
        else:
            events.emit("klubok.end", text, data={"session": result.session_id})

    def _final_snapshot(self) -> None:
        """Знімок у кінці: посилання на виставлене й «чи видно його в її списку». Не вдався — лише лог."""
        try:
            # після відповіді сторінка стоїть на обірваному переході (`/payment/…`): `fetch`
            # має йти зі сторінки Клубку
            if not self.session.open_form().ok:
                raise snapshot.SnapshotError(snapshot.FAILED, "форма Клубку не відкрилась")
            snapshot.refresh(
                self.conn, self.session.fetch_text,
                pause=lambda: self.sleep(random.uniform(*self.opts.page_pause)),
                wait=self.sleep, log=self.log,
            )  # fmt: skip
        except Exception as error:  # noqa: BLE001 — не стоп і не Проблема
            self.log(f"Знімок у кінці сесії не вдався: {getattr(error, 'text', error)}")
            return
        ids = [klubok_id for _item, klubok_id, _name in self.result.published]
        for klubok_id in snapshot.missing(self.conn, ids):
            text = f"Оголошення {klubok_id} не видно в її списку на Клубку — перевірте"
            self.log(text)
            events.emit("klubok.missing", text, loud=True, data={"klubok_id": klubok_id})


def run(*args, **kwargs) -> Result:
    return Run(*args, **kwargs).run()
