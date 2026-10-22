"""Чи пост ще є в каналі (SITE-002). Вона видаляє пост лише коли пару продано.

Bot API не дає ні подій видалення, ні історії каналу. Тому пост пересилаємо тихо
(`disable_notification`) у службовий чат — приватний чат бота з Власником (немає Власника — з нею) —
і одразу видаляємо копію. Telegram каже «повідомлення немає» → пост видалено. Будь-яка інша помилка
(мережа, 429, 5xx, захищений вміст) — «невідомо»: нічого не вирішуємо, спробуємо пізніше.
«Немає зв'язку» ніколи не означає «продано». У каналі нічого не редагується й не видаляється.
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from shafa import events
from shafa.bot.telegram import Telegram, TelegramError

PRESENT, GONE, UNKNOWN = "present", "gone", "unknown"
# «Видалено» (= продано) — ЛИШЕ 400 від `forwardMessage` з одним із текстів (SITE-003 b):
#   «Bad Request: message to forward not found» — пост у каналі видалено;
#   «Bad Request: MESSAGE_ID_INVALID» — такого номера в каналі немає.
# Будь-що інше — «невідомо», «Продано» не шлемо: 400 «chat not found», «message has protected
# content and can't be forwarded», 403 «bot is not a member…» (немає прав), 429, 5xx, мережа.
# Звірено з документацією й відповідями Telegram у тестах; живий прогін — TD-036.
GONE_MARKS = ("message to forward not found", "message_id_invalid")
LEFTOVERS_FILE = "presence-leftovers.json"  # BOT-014: копії, яких не вдалося видалити
TOO_OLD = 48 * 3600  # бот не може видалити повідомлення старше 48 год
SETTLE_BUDGET = 120.0  # скільки секунд після ночі добираємо залишки
SETTLE_PAUSE = 5.0


class Presence:
    def __init__(
        self,
        tg: Telegram,
        channel_id: int,
        service_chat: int,
        log: Callable[[str], None] = print,
        data_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tg, self.channel_id, self.service_chat, self.log = tg, channel_id, service_chat, log
        self.retry_after: int | None = None  # 429 останньої перевірки: скільки чекати
        self.clock = clock
        # Залишки — у файлі (бот і робот — різні процеси й екземпляри); без теки — лише в пам'яті.
        self._file = None if data_dir is None else Path(data_dir) / LEFTOVERS_FILE
        self._memory: list[dict] = []

    def check(self, post_id: int) -> str:
        """`PRESENT`, `GONE` або `UNKNOWN`. Копію в службовому чаті одразу видаляє."""
        self.retry_after = None
        self._clean()
        try:
            sent = self.tg.forward_message(self.service_chat, self.channel_id, post_id)
        except TelegramError as error:
            if error.code == 400 and any(m in error.description.lower() for m in GONE_MARKS):
                return GONE
            self.retry_after = error.retry_after
            self.log(f"Канал: пост {post_id} — перевірити не вдалося ({error})")
            return UNKNOWN
        copy = sent.get("message_id") if isinstance(sent, dict) else None
        if isinstance(copy, int):
            entry = {"chat": self.service_chat, "message_id": copy, "at": self.clock()}
            self._write(self._read() + [entry])
            self._clean()
        else:
            self.log("Канал: Telegram не повернув номер копії перевірки — прибрати її нічим")
        return PRESENT

    def settle(
        self,
        budget: float = SETTLE_BUDGET,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """Після нічної перевірки (і при старті): добрати залишки з паузами, разом ≤ `budget` с.
        Лишилось — одна тиха подія. Повертає, скільки копій лишилось."""
        spent = 0.0
        while self._clean() and spent < budget:
            pause = min(float(self.retry_after or SETTLE_PAUSE), budget - spent)
            if pause <= 0:
                break
            sleep(pause)
            spent += pause
        left = [e for e in self._read() if not e.get("told")]
        if left:
            events.emit(
                "presence.leftover",
                "Нічна перевірка: копію поста не вдалося прибрати — видали в чаті з ботом вручну",
            )
            self._write([{**e, "told": True} if e in left else e for e in self._read()])
        return len(self._read())

    def _clean(self) -> bool:
        """Видаляє залишки. True — щось ще лишилось (спробуємо пізніше)."""
        entries = self._read()
        fresh = [e for e in entries if self.clock() - e["at"] <= TOO_OLD]
        if len(fresh) != len(entries):
            self.log("Канал: копію перевірки старше 48 год прибрано із запису — Telegram вже "
                     "не дасть її видалити")  # fmt: skip
        for entry in list(fresh):
            try:
                self.tg.delete_message(entry["chat"], entry["message_id"])
            except TelegramError as error:
                if error.code != 400:  # 400 — копії вже немає; інше — спробуємо ще
                    self.retry_after = error.retry_after
                    self.log(f"Канал: копію перевірки не видалено (код {error.code}) — "
                             "спробую пізніше")  # fmt: skip
                    self._write(fresh)
                    return True
            fresh.remove(entry)
        self._write(fresh)
        return False

    def _read(self) -> list[dict]:
        if self._file is None:
            return list(self._memory)
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [
            e for e in data if isinstance(e, dict)
            and isinstance(e.get("chat"), int) and isinstance(e.get("message_id"), int)
            and isinstance(e.get("at"), int | float)
        ] if isinstance(data, list) else []  # fmt: skip

    def _write(self, entries: list[dict]) -> None:
        if self._file is None:
            self._memory = entries
            return
        try:
            if not entries:
                self._file.unlink(missing_ok=True)
                return
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(entries), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError as error:
            self.log(f"Канал: не вдалося записати залишки перевірки ({type(error).__name__})")


def service_chat(owner_chat: int | None, seller_chat: int | None) -> int | None:
    """Куди пересилати для перевірки: чат Власника, без нього — її."""
    return owner_chat if owner_chat is not None else seller_chat
