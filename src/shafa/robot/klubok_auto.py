"""KLB-014: Клубок виставляє сам щодня — без кнопки «Старт».

`Auto` (потік `app`, за зразком `klubok_renew.Daily`): раз на хвилину дивиться, чи пора запускати
той самий процес, що кнопка «Старт» (`klubok_control.start_args`). Умови — у `tick`; налаштування й
позначка доби — `klubok/auto.py`. Один автостарт на місцеву добу; ручний «Старт» його не блокує і
навпаки — їх обмежує лише ліміт доби (`klubok_control.state().left`).
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shafa.klubok import auto, login, queue
from shafa.robot import klubok_control

TICK = 60.0  # с між перевірками годинника
NO_ANSWER = timedelta(minutes=3)  # KLB-018: перевірка входу мовчить стільки — стартуємо без неї


class Auto:
    def __init__(
        self,
        command: Callable[[int], list[str]],
        spawn: Callable[[list[str]], object],
        busy: Callable[[], object],
        log: Callable[[str], None],
        connect: Callable[[], sqlite3.Connection],
        state: Callable[[sqlite3.Connection, datetime], klubok_control.State] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        folder: Path | None = None,
        check: Callable[[], object] | None = None,
    ) -> None:
        """`check` — KLB-018: запускає `--klubok-login-check`; без нього автостарт про вхід не
        питає (злетілий вхід усе одно зупинить сесію сама)."""
        self.check = check
        self.command, self.spawn, self.busy, self.log = command, spawn, busy, log
        self.connect, self.clock, self.folder = connect, clock, folder
        self.state = state or (lambda conn, now: klubok_control.state(conn, None, now))
        self.said: set[str] = set()  # що вже писали в журнал сьогодні (не спамити щохвилини)
        self.checked: datetime | None = None  # коли просили перевірку входу (KLB-018)

    def _say(self, key: str, text: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(text)

    def tick(self) -> str | None:
        """started / busy — що зроблено цього разу; None — нічого."""
        now = self.clock()
        today = f"{now:%Y-%m-%d}"
        if not queue.enabled() or not auto.enabled():
            return None
        conn = self.connect()
        try:
            auto.attach_session(conn, now, self.folder)
            # KLB-018: процес запустили, а сесії немає й робот вільний — день не закрито
            if auto.release_dead(now, bool(self.busy()), self.folder):
                self._say(
                    f"dead:{today}:{now:%H%M}", "Клубок: автостарт не відкрив сесії — повторю"
                )
            if auto.marked_today(now, self.folder):
                return None
            shafa_done = auto.shafa_calm_today(conn, now)
            if not shafa_done and not self._at_reached(now):
                return None
            if (code := auto.loud_today(conn, now)) is not None:
                self._say(
                    f"loud:{today}:{code}",
                    f"Клубок: автостарту сьогодні не буде — сесія зупинилась ({code})",
                )
                return None
            if self.busy():
                self._say(f"busy:{today}", "Клубок: автостарт — робот зайнятий, спробую пізніше")
                return "busy"
            if (wait := auto.retry_wait(now, self.folder)) is not None:
                self._say(f"retry:{today}:{wait}", f"Клубок: автостарт — {wait}")
                return None
            state = self.state(conn, now)
        finally:
            conn.close()
        if not state.can_start or state.left < 1:
            self._say(
                f"no:{today}:{state.why_not or state.code}",
                f"Клубок: автостарт не потрібен — {state.why_not or state.text}",
            )
            return None
        if (answer := self._login(now, today)) is not None:
            return answer
        self.spawn(self.command(state.left))
        auto.write_mark(now, self.folder)
        why = "після сесії Шафи" if shafa_done else "за часом"
        self.log(f"Клубок: автостарт {why} ({now:%H:%M}), до {state.left} пар")
        return "started"

    def _login(self, now: datetime, today: str) -> str | None:
        """KLB-018: перед автостартом — чи не злетів вхід. None — можна стартувати."""
        from shafa.robot import klubok_control

        found = login.verdict(now, klubok_control._login_at(), self.folder)
        if found is False:
            self._say(
                f"login:{today}",
                "Клубок: автостарту немає — вхід злетів, потрібно натиснути «Увійти в Клубок»",
            )
            return "login"
        if found is True or self.check is None:
            return None
        if self.checked is None or now - self.checked >= auto.RETRY_AFTER:
            self.checked = now
            self.check()
            self._say(f"check:{today}:{now:%H%M}", "Клубок: перед автостартом перевіряю вхід")
            return "checking"
        if now - self.checked < NO_ANSWER:
            return "checking"
        return None  # перевірка мовчить (немає відповіді сайту) — сесія перевірить вхід сама

    def _at_reached(self, now: datetime) -> bool:
        try:
            at = auto.at_setting()
        except auto.SetupError as error:
            self._say(f"bad:{error}", f"{error} — запуск за часом вимкнено")
            return False
        return at is not None and now.time() >= at

    def run(self, stop: Any, interval: float = TICK) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — фоновий потік програми не падає
                self._say(f"err:{error}", f"Клубок: автостарт: {type(error).__name__}: {error}")
            stop.wait(interval)
