"""Чи пост ще є в каналі (SITE-002). Вона видаляє пост лише коли пару продано.

Bot API не дає ні подій видалення, ні історії каналу. Тому пост пересилаємо тихо
(`disable_notification`) у службовий чат — приватний чат бота з Власником (немає Власника — з нею) —
і одразу видаляємо копію. Telegram каже «повідомлення немає» → пост видалено. Будь-яка інша помилка
(мережа, 429, 5xx, захищений вміст) — «невідомо»: нічого не вирішуємо, спробуємо пізніше.
«Немає зв'язку» ніколи не означає «продано». У каналі нічого не редагується й не видаляється.
"""

from collections.abc import Callable

from shafa.bot.telegram import Telegram, TelegramError

PRESENT, GONE, UNKNOWN = "present", "gone", "unknown"
# Відповідь Telegram на пересилання поста, якого вже немає (400). Інше 400 — «невідомо».
GONE_MARKS = ("message to forward not found", "message_id_invalid")


class Presence:
    def __init__(
        self,
        tg: Telegram,
        channel_id: int,
        service_chat: int,
        log: Callable[[str], None] = print,
    ) -> None:
        self.tg, self.channel_id, self.service_chat, self.log = tg, channel_id, service_chat, log
        self.retry_after: int | None = None  # 429 останньої перевірки: скільки чекати
        self._leftovers: list[int] = []  # копії, які не вдалося видалити, — ще раз наступного разу

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
            self._leftovers.append(copy)
            self._clean()
        return PRESENT

    def _clean(self) -> None:
        for copy in list(self._leftovers):
            try:
                self.tg.delete_message(self.service_chat, copy)
            except TelegramError as error:
                if error.code != 400:  # 400 — копії вже немає; інше — спробуємо ще
                    self.log(f"Канал: копію перевірки не видалено ({error}) — спробую пізніше")
                    return
            self._leftovers.remove(copy)


def service_chat(owner_chat: int | None, seller_chat: int | None) -> int | None:
    """Куди пересилати для перевірки: чат Власника, без нього — її."""
    return owner_chat if owner_chat is not None else seller_chat
