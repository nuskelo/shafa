"""ROBOT-019: рання перевірка входу на Шафу (`robot --login-check`) і її запуск у програмі.

Вхід Шафи злітає приблизно раз на 10 днів. Без перевірки Продавчиня дізнається про це лише тоді,
коли «Старт» одразу зупиниться. `check` відкриває `/uk/new` у профілі робота (headless, лише
читання, крізь `guard`, без публікації): форма — вхід є; Шафа веде на `/login` — злетів. Результат —
`loginstate` (файл `login-check.json` у теці даних); злетів — одна гучна подія Власнику на добу.
Сайт не відповів ні так ні ні (немає форми) — нічого не записуємо.

`Watch` — потік `app`: перевірка при старті програми й раз на добу, лише коли робот вільний.
Перед «Старт» її просить екран (`web`). Після закриття вікна входу (`robot --login`) перевірка
йде одразу — панель стає зеленою без жодної кнопки.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shafa import events
from shafa.loginstate import Store
from shafa.robot import browser

STATE = "login-check.json"  # {"at", "ok", "why"} — результат останньої перевірки
ALERT = "login-alert.json"  # {"day"} — коли востаннє писали Власнику
store = Store(STATE, ALERT)

TICK = 60.0
EVERY = timedelta(hours=24)  # без перевірки довше — час перевірити
RETRY = timedelta(minutes=10)  # запуск перевірки не частіше

LOST = (
    "Шафа вийшла з акаунта робота. Відкрийте програму → «Увійти на Шафу», увійдіть у вікні, що "
    "відкрилось, і закрийте вікно — інакше «Старт» не спрацює."
)


@dataclass(frozen=True)
class Outcome:
    ok: bool | None  # True — вхід є, False — злетів, None — Шафа не відповіла
    why: str = ""


def check(session: Any) -> Outcome:
    """Лише відкриває форму й дивиться, куди Шафа веде; нічого не натискає."""
    opened = session.open_form()
    if opened.state == browser.NO_LOGIN:
        return Outcome(False, "Шафа веде на сторінку входу")
    if not opened.ok:
        return Outcome(None, "форма не відкрилась")
    return Outcome(True)


def check_once(
    session: Any, now: datetime, folder: Path | None = None,
    emit: Callable[..., object] = events.emit,
) -> Outcome:  # fmt: skip
    """`check` + результат у файл; злетів — Власнику одне повідомлення на добу."""
    outcome = check(session)
    if outcome.ok is None:
        return outcome
    store.write(outcome.ok, now, outcome.why, folder)
    if outcome.ok is False and store.alert_due(now, folder):
        emit("shafa.login", f"{LOST} ({outcome.why})", loud=True)
        store.alert_sent(now, folder)
    return outcome


class Watch:
    """Запускає `--login-check`: при старті програми й раз на добу, коли робот вільний."""

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
        found = store.read(self.folder)
        if found is None or now - found[0] >= EVERY:
            return "раз на добу"
        return None

    def tick(self) -> str | None:
        """started / busy — що зроблено цього разу; None — нічого."""
        now = self.clock()
        why = self._due(now)
        if why is None or (self.last is not None and now - self.last < RETRY):
            return None
        if self.busy():
            self._say(f"busy:{now:%Y-%m-%d %H}", "Шафа: перевірка входу — робот зайнятий")
            return "busy"
        self.started = True
        self.last = now
        self.spawn(self.command())
        self.log(f"Шафа: перевіряю, чи не злетів вхід ({why})")
        return "started"

    def run(self, stop: Any, interval: float = TICK) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — фоновий потік програми не падає
                self._say(f"err:{error}", f"Шафа: перевірка входу: {type(error).__name__}: {error}")
            stop.wait(interval)
