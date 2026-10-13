"""Читання Експорту Telegram Desktop (`result.json`)."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Post:
    post_id: int
    date: str
    text: str


def plain_text(text: str | list) -> str:
    """`text` з Експорту → звичайний рядок, без форматування."""
    if isinstance(text, str):
        return text
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)


def read_export(path: Path) -> dict[int, Post]:
    """Пости каналу: номер → Пост. Службові повідомлення пропускаються."""
    data = json.loads(path.read_text(encoding="utf-8"))
    posts: dict[int, Post] = {}
    for message in data["messages"]:
        if message.get("type") != "message":
            continue
        post_id = message["id"]
        posts[post_id] = Post(post_id, message["date"], plain_text(message.get("text", "")))
    return posts
