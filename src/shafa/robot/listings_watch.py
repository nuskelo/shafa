"""UX-009: «Мої речі» перечитуються заздалегідь, у фоні, поки робот вільний.

Знімок кабінету (`my_listings`) застаріває раз на `SHAFA_LISTINGS_DAYS` днів, і тоді перше «Старт»
сидить 5–10 хв, поки робот його читає. `Watch` — потік `app`: коли знімок порожній, скинутий
(«Перечитати зараз») чи застаріє протягом доби (`listings.due_soon`) — запускає `robot
--listings-refresh` (окремий процес із замком `listings-refresh`, headless, лише читання), якщо
робот вільний. «Старт» під час такого читання чекає його (`web.App`), панель про це каже.

Як і в `login_check.Watch` (ROBOT-019): Chrome не відкривається, доки профілю робота немає
(чиста установка — `ready=web._logged_in`) і перші 4 хв після старту програми (UX-010: `login_check.Watch`
чекає 2 хв, і разом на одній секунді вони боролися за замок); також — коли вхід
на Шафу злетів (остання перевірка входу `False`): читати нічого. Спроба — не частіше ніж раз на
годину (`RETRY`), успішна чи ні. Невзятий замок (процес вийшов з кодом 2, а запису `listings-bg.json`
після запуску немає — хтось інший тримав робота) — не спроба: повтор на наступному тіку.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from shafa.robot import listings, login_check

TICK = 60.0
RETRY = timedelta(hours=1)  # спроба фонового читання — не частіше
START_DELAY = timedelta(minutes=4)  # UX-010: після `login_check.START_DELAY` (2 хв), не разом із нею


class Watch:
    """Запускає `--listings-refresh`, коли знімок «Мої речі» ось-ось застаріє, а робот вільний."""

    def __init__(
        self,
        command: Callable[[], list[str]],
        spawn: Callable[[list[str]], object],
        busy: Callable[[], object],
        log: Callable[[str], None],
        due: Callable[[], bool],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        folder: Path | None = None,
        ready: Callable[[], bool] = lambda: True,
        delay: timedelta = START_DELAY,
    ) -> None:
        """`due` — знімок час перечитувати (читає базу); `ready` — профіль робота вже є."""
        self.command, self.spawn, self.busy, self.log, self.due = command, spawn, busy, log, due
        self.clock, self.folder, self.ready, self.delay = clock, folder, ready, delay
        self.born = clock()
        self.last: datetime | None = None
        self.process: object | None = None  # UX-010: останній запущений `--listings-refresh`
        self.said: set[str] = set()

    def _say(self, key: str, text: str) -> None:
        if key not in self.said:
            self.said.add(key)
            self.log(text)

    def _forgive_lock(self) -> None:
        """UX-010: процес вийшов з кодом 2, а спроби в `listings-bg.json` після запуску немає —
        він не взяв замок (робот зайнятий). Це не спроба: `RETRY` не чекаємо, спробуємо на тіку."""
        poll = getattr(self.process, "poll", None)
        if self.last is None or poll is None or poll() != 2:
            return
        tried = listings.last_attempt()
        if tried is None or tried < self.last:
            self.last = None
        self.process = None

    def tick(self) -> str | None:
        """started / busy — що зроблено цього разу; None — нічого."""
        now = self.clock()
        if now - self.born < self.delay or not self.ready():
            return None
        self._forgive_lock()
        found = login_check.store.read(self.folder)
        if found is not None and found[1] is False:
            return None  # вхід злетів — читати нічого, спершу «Увійти на Шафу»
        if self.last is not None and now - self.last < RETRY:
            return None
        tried = listings.last_attempt()
        if tried is not None and now - tried < RETRY:
            return None
        if not self.due():
            return None
        if self.busy():
            self._say(f"busy:{now:%Y-%m-%d %H}", "Мої речі: перечитування — робот зайнятий")
            return "busy"
        self.last = now
        self.process = self.spawn(self.command())
        self.log("Мої речі: перечитую заздалегідь, поки робот вільний")
        return "started"

    def run(self, stop: Any, interval: float = TICK) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 — фоновий потік програми не падає
                self._say(
                    f"err:{error}", f"Мої речі: перечитування: {type(error).__name__}: {error}"
                )
            stop.wait(interval)
