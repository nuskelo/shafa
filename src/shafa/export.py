"""Читання Експорту Telegram Desktop (`result.json`)."""

import json
from dataclasses import dataclass
from pathlib import Path

from shafa import config

CHANNEL_SETTING = "TELEGRAM_CHANNEL_ID"
CHAT_FILE = "export-chat.json"  # KLB-010: id чату першого Експорту (коли канал не задано)


@dataclass(frozen=True)
class Post:
    post_id: int
    date: str
    text: str
    photo: str | None = None  # шлях фото відносно теки Експорту (`photos/…jpg`) або None


def photo_path(value: object) -> str | None:
    """Поле `photo` повідомлення → відносний шлях; Експорт без медіа пише замість шляху пояснення
    «(File not included. …)» — це не фото."""
    if not isinstance(value, str) or not value.strip() or value.startswith("("):
        return None
    return value


def plain_text(text: str | list) -> str:
    """`text` з Експорту → звичайний рядок, без форматування."""
    if isinstance(text, str):
        return text
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)


def read_export(path: Path) -> dict[int, Post]:
    """Пости каналу: номер → Пост. Службові повідомлення пропускаються."""
    return parse_export(path.read_bytes())


class ExportError(ValueError):
    """Файл — не експорт каналу Telegram."""


class WrongChat(ExportError):
    """KLB-010 (N1): Експорт іншого чату, не Каналу."""


def _norm(value: object) -> int | None:
    """id чату → число без мінуса й без префікса Bot API «-100» (`-1002394827824` → 2394827824)."""
    text = str(value or "").strip().lstrip("-")
    if not text.isdigit():
        return None
    if len(text) > 10 and text.startswith("100"):
        text = text[3:]
    return int(text)


def check_chat(raw: bytes, data_dir: Path | None = None) -> None:
    """KLB-010 (N1): Експорт має бути з Каналу. Очікуваний id — `TELEGRAM_CHANNEL_ID`, а якщо його
    немає — id першого прийнятого Експорту (`export-chat.json` у теці даних; у базі id чату не
    зберігається). Інший — `WrongChat` з поясненням; нічого не записано."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError("не JSON") from error
    if not isinstance(data, dict):
        raise ExportError("не експорт каналу")
    got = _norm(data.get("id"))
    folder = data_dir or config.data_dir()
    saved = folder / CHAT_FILE
    expected = _norm(config.setting(CHANNEL_SETTING))
    if expected is None and saved.is_file():
        try:
            expected = _norm(json.loads(saved.read_text(encoding="utf-8")).get("id"))
        except (OSError, ValueError, AttributeError):
            expected = None
    if expected is None:
        if got is not None:
            folder.mkdir(parents=True, exist_ok=True)
            saved.write_text(json.dumps({"id": got}), encoding="utf-8")
        return
    if got != expected:
        name = str(data.get("name") or "без назви")
        raise WrongChat(
            f"Це Експорт іншого чату: «{name}» (id {got}), а потрібен канал id {expected}. "
            "Telegram Desktop → канал → «Експорт історії чату» (формат JSON). Нічого не записано."
        )


def read_channel_export(path: Path) -> dict[int, Post]:
    """Пости з `result.json` лише Каналу (`check_chat`) — для імпорту в базу."""
    raw = path.read_bytes()
    check_chat(raw)
    return parse_export(raw)


def parse_export(raw: bytes) -> dict[int, Post]:
    """Вміст `result.json` → пости. Не експорт каналу — ExportError."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError("не JSON") from error
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ExportError("не експорт каналу")
    posts: dict[int, Post] = {}
    for message in data["messages"]:
        if message.get("type") != "message":
            continue
        post_id = message["id"]
        text = plain_text(message.get("text", ""))
        posts[post_id] = Post(post_id, message["date"], text, photo_path(message.get("photo")))
    return posts
