"""Читання Експорту Telegram Desktop (`result.json`)."""

import json
from dataclasses import dataclass
from pathlib import Path


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
