"""Telegram Bot API напряму через `httpx2` (рішення D1 BOT-001): лише потрібні методи.

Токен стоїть в адресі кожного запиту (`/bot<token>/METHOD`, файли — `/file/bot<token>/…`), а текст
помилок `httpx2` містить адресу. Тому кожна помилка тут замінюється на `TelegramError` з власним
текстом, без адреси й без тексту винятку `httpx2` (`from None` — і без ланцюжка в traceback).
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx2

API = "https://api.telegram.org"
TIMEOUT = 30.0  # секунд на звичайний запит
POLL_EXTRA = 10.0  # до довгого опитування додаємо запас


class TelegramError(Exception):
    """Запит до Telegram не вдався. Текст — без токена й адреси.

    `uncertain` — запит міг дійти (обрив після надсилання, 5xx): дію не повторювати.
    """

    def __init__(
        self,
        text: str,
        uncertain: bool = False,
        code: int | None = None,
        retry_after: int | None = None,
        description: str = "",
    ) -> None:
        super().__init__(text)
        self.uncertain = uncertain
        self.code = code
        self.retry_after = retry_after
        self.description = description  # опис від Telegram (без токена) — розпізнати причину


# Запит точно не пішов: з'єднання не встановлено.
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ProxyError)


@dataclass(frozen=True)
class Upload:
    """Файл для `multipart/form-data`."""

    name: str
    data: bytes
    mime: str = "image/jpeg"


class Telegram:
    def __init__(self, token: str, client: httpx2.Client | None = None) -> None:
        self._token = token
        self._client = client or httpx2.Client(timeout=TIMEOUT)

    def close(self) -> None:
        self._client.close()

    def _transport_error(self, error: Exception, method: str) -> TelegramError:
        if isinstance(error, _NOT_SENT):
            return TelegramError(f"Немає з'єднання з Telegram ({method})")
        if isinstance(error, httpx2.TimeoutException):
            return TelegramError(f"Telegram не відповів вчасно ({method})", uncertain=True)
        return TelegramError(f"Обрив зв'язку з Telegram ({method})", uncertain=True)

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        files: dict[str, Upload] | None = None,
        timeout: float = TIMEOUT,
    ) -> Any:
        """Виклик методу Bot API → поле `result` або TelegramError."""
        url = f"{API}/bot{self._token}/{method}"
        try:
            if files:
                data = {
                    k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for k, v in (params or {}).items()
                }
                upload = {k: (f.name, f.data, f.mime) for k, f in files.items()}
                response = self._client.post(url, data=data, files=upload, timeout=timeout)
            else:
                response = self._client.post(url, json=params or {}, timeout=timeout)
        except httpx2.HTTPError as error:
            raise self._transport_error(error, method) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise TelegramError(
                f"Незрозуміла відповідь Telegram ({method}, {response.status_code})",
                uncertain=response.status_code >= 500,
                code=response.status_code,
            ) from None
        if body.get("ok") is True:
            return body.get("result")
        code = body.get("error_code") if isinstance(body.get("error_code"), int) else None
        code = code or response.status_code
        parameters = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        retry_after = parameters.get("retry_after")
        description = str(body.get("description") or "без опису").replace(self._token, "***")
        raise TelegramError(
            f"Telegram відмовив ({method}, {code}): {description[:200]}",
            uncertain=code >= 500,
            code=code,
            retry_after=retry_after if isinstance(retry_after, int) else None,
            description=description[:200],
        ) from None

    def download(self, file_path: str, limit: int) -> bytes:
        """Файл за `file_path` з `getFile` (не більше `limit` байт)."""
        url = f"{API}/file/bot{self._token}/{file_path}"
        try:
            response = self._client.get(url)
        except httpx2.HTTPError as error:
            raise self._transport_error(error, "file") from None
        if response.status_code != 200:
            raise TelegramError(f"Файл не завантажився ({response.status_code})") from None
        if len(response.content) > limit:
            raise TelegramError("Файл завеликий") from None
        return response.content

    # --- Методи ---

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": timeout,
            # правка поста каналу — новий текст для сайту (SITE-002)
            "allowed_updates": ["message", "callback_query", "edited_channel_post"],
        }
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + POLL_EXTRA)

    def get_chat_member(self, chat_id: int, user_id: int) -> dict:
        return self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    def get_chat(self, chat_id: int) -> dict:
        return self.call("getChat", {"chat_id": chat_id})

    def send_media_group(
        self, chat_id: int, photos: list[Upload], reply_to: int | None
    ) -> list[dict]:
        """Альбом фото (2–10) завантаженням — відповіддю на повідомлення `reply_to` (None — просто
        в чат, BOT-011)."""
        media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(photos))]
        params: dict[str, Any] = {"chat_id": chat_id, "media": media}
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to}
        files = {f"p{i}": photo for i, photo in enumerate(photos)}
        return self.call("sendMediaGroup", params, files=files)

    def get_file(self, file_id: str) -> dict:
        return self.call("getFile", {"file_id": file_id})

    def send_message(
        self, chat_id: int, text: str, markup: dict | None = None, silent: bool = False
    ) -> dict:
        """`silent` — без звуку й вібрації (збірка тихих подій, BOT-012)."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if silent:
            params["disable_notification"] = True
        if markup is not None:
            params["reply_markup"] = markup
        return self.call("sendMessage", params)

    def send_photo(
        self,
        chat_id: int,
        photo: Upload | str,
        caption: str,
        markup: dict | None = None,
        entities: list[dict] | None = None,
    ) -> dict:
        """Фото завантаженням (`Upload`) або за `file_id`. Підпис — простим текстом, посилання в
        ньому — `entities` (без `parse_mode`: її текст не розбирається як розмітка)."""
        params: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
        if markup is not None:
            params["reply_markup"] = markup
        if entities:
            params["caption_entities"] = entities
        if isinstance(photo, Upload):
            return self.call("sendPhoto", params, files={"photo": photo})
        return self.call("sendPhoto", {**params, "photo": photo})

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_text(self, chat_id: int, message_id: int, text: str, markup: dict | None = None):
        """Новий текст повідомлення; без `markup` — і без кнопок."""
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": markup or {"inline_keyboard": []},
        }
        return self.call("editMessageText", params)

    def forward_message(self, chat_id: int, from_chat_id: int, message_id: int) -> dict:
        """Переслати повідомлення тихо (без сповіщення) — перевірка, чи пост ще є (SITE-002)."""
        params = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
            "disable_notification": True,
        }
        return self.call("forwardMessage", params)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def drop_buttons(self, chat_id: int, message_id: int) -> None:
        self.call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    def edit_caption(
        self, chat_id: int, message_id: int, caption: str, entities: list[dict] | None = None
    ) -> None:
        """Новий підпис уже надісланого фото (BOT-006). Той самий — Telegram каже 400 «message
        is not modified»; це розбирає той, хто викликає."""
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "caption": caption}
        if entities:
            params["caption_entities"] = entities
        self.call("editMessageCaption", params)
