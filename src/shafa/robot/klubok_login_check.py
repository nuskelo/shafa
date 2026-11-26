"""KLB-018: рання перевірка входу в Клубок (`robot --klubok-login-check`) і її запуск у програмі.

Вхід Клубку — лише сесійна cookie, вона злітає через 1–2 дні. `check` відкриває `/item/add` у
профілі робота (лише читання, крізь `klubok_guard`, без публікації): форма й наш `#user_id` — вхід
є; Клубок веде на `/signup`/`/signin` — злетів. Результат — `klubok/login.py`; злетів — одна гучна
подія Власнику на добу. Сайт не відповів ні так ні ні (немає форми) — нічого не записуємо.

`Watch` — потік `app`: перевірка при старті програми й раз на добу о `CHECK_AT` (19:00; KLB-019:
«Обновить все» більше не за часом, перед кожним його запуском вхід перевіряє сама команда), лише
коли робот вільний; автоматику вимикає `SHAFA_KLUBOK_RENEW_AT=off`. Перед автостартом її просить `klubok_auto.Auto`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from shafa import events
from shafa.klubok import login, queue, renew
from shafa.robot import browser
from shafa.robot import klubok_form as form

TICK = 60.0
CHECK_AT = time(19, 0)  # KLB-019: щоденна перевірка входу
RETRY = timedelta(minutes=10)  # запуск перевірки не частіше

LOST = (
    "Робот не увійшов у Клубок: сесія злетіла. Відкрийте програму → Клубок → «Увійти в Клубок», "
    "увійдіть і закрийте вікно — інакше сьогодні нічого не виставиться."
)


@dataclass(frozen=True)
class Outcome:
    ok: bool | None  # True — вхід є, False — злетів, None — сайт не відповів
    why: str = ""


def check(session: form.Session) -> Outcome:
    """Лише відкриває форму й читає, хто увійшов; нічого не натискає."""
    opened = session.open_form()
    if opened.state == browser.NO_LOGIN:
        return Outcome(False, "Клубок веде на сторінку входу")
    if not opened.ok:
        return Outcome(None, "форма не відкрилась")
    if session.user_id() != form.KLUBOK_USER:
        return Outcome(False, "у профілі робота інший акаунт")
    return Outcome(True)


def check_once(
    session: form.Session, now: datetime, folder: Path | None = None,
    emit: Callable[..., object] = events.emit,
) -> Outcome:  # fmt: skip
    """`check` + результат у файл; злетів — Власнику одне повідомлення на добу."""
    outcome = check(session)
    if outcome.ok is None:
        return outcome
    login.write(outcome.ok, now, outcome.why, folder)
    if outcome.ok is False and login.alert_due(now, folder):
        emit("klubok.login", f"{LOST} ({outcome.why})", loud=True)
        login.alert_sent(now, folder)
    return outcome


class Watch:
    """Запускає `--klubok-login-check`: при старті програми й раз на добу перед «Обновить все»."""

    def __init__(
        self,
        command: Callable[[], list[str]],
        spawn: Callable[[list[str]], object],
        busy: Callable[[], object],
        log: Callable[[str], None],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        folder: Path | None = None,
    ) -> None:
        self.command, self.spawn, self.busy, self.log = command, spawn, busy, log
        self.clock, self.folder = clock, folder
        self.started = False  # перевірка «при старті програми» вже була
        self.last: datetime | None = None
        self.said: set[str] = set()

    def _say(self, key: str, text: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(text)

    def _due(self, now: datetime) -> str | None:
        if not self.started:
            return "при старті програми"
        if not renew.automatic():
            return None
        moment = datetime.combine(now.date(), CHECK_AT, tzinfo=now.tzinfo)
        found = login.read(self.folder)
        if now >= moment and (found is None or found[0] < moment):
            return "щоденна, 19:00"
        return None

    def tick(self) -> str | None:
        """started / busy — що зроблено цього разу; None — нічого."""
        now = self.clock()
        if not queue.enabled():
            return None
        why = self._due(now)
        if why is None or (self.last is not None and now - self.last < RETRY):
            return None
        if self.busy():
            self._say(f"busy:{now:%Y-%m-%d %H}", "Клубок: перевірка входу — робот зайнятий")
            return "busy"
        self.started = True
        self.last = now
        self.spawn(self.command())
        self.log(f"Клубок: перевіряю, чи не злетів вхід ({why})")
        return "started"

    def run(self, stop: Any, interval: float = TICK) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — фоновий потік програми не падає
                self._say(
                    f"err:{error}", f"Клубок: перевірка входу: {type(error).__name__}: {error}"
                )
            stop.wait(interval)
