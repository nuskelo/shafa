"""Пости каналу, які бот не публікував (BOT-015): пам'ять про них і пошук за описом.

Вікторія буває викладає пост просто в канал, коли бот не працює. Тоді поста немає ні в `posts`, ні в
папці, ні в черзі сайту. Кнопка «📦 Лише в чергу Шафи» ставить його в чергу, не пишучи в канал, —
для цього треба знати номер і дату поста. Bot API історії каналу не дає, тож:

- `channel_post` (оновлення про новий пост каналу) бот запам'ятовує в `channel_posts`;
- якщо там поста немає, `Finder` тихо переглядає канал: пересилає повідомлення у службовий чат і
  одразу видаляє копію — це робить `Presence.look` (SITE-002/BOT-014), тут лише порядок номерів,
  ліміти й звірка тексту.

Бот **ніколи не вгадує**: збіг — це рівно один пост; нуль, два й більше або перегляд, що не
завершився (ліміт, збій зв'язку), — «не знайшов». У каналі нічого не редагується й не видаляється.
"""

import re
import sqlite3
from collections.abc import Callable
from difflib import SequenceMatcher

from shafa import rules
from shafa.bot import presence
from shafa.db import now

LIMIT = 400  # запитів до Telegram на один пошук (разом з повторами після 429)
BACK = 300  # скільки номерів униз від найбільшого відомого
AHEAD_GONE = 50  # скільки номерів поспіль «немає» вгорі — далі постів уже не буде
PAUSE = 1.0  # секунд між запитами
RETRIES = 5  # скільки разів чекаємо 429 на одному номері
UNKNOWN_RUN = 5  # стільки збоїв поспіль — перегляд не завершено
SIMILAR = 0.9  # схожість нормалізованих текстів
MIN_LENGTH = 10  # коротший опис нічого не доводить

_NOISE = re.compile(r"[\W_]+")


def normalize(text: str) -> str:
    """Нижній регістр, без службових рядків підпису, посилань, @ніків, хештегів, емодзі й
    пунктуації; пробіли стиснуто."""
    text = rules.without_service_lines(text).lower()
    for pattern in (rules.LINK, rules.MENTION, rules.HASHTAG):
        text = pattern.sub(" ", text)
    return " ".join(_NOISE.sub(" ", text).split())


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def same_post(own: str, post: str) -> bool:
    """Чи `post` — той самий пост, що й опис `own`. Обидва — після `normalize`. Ціна й розмір
    (усі числа) мають збігтися точно; далі — опис повністю в тексті поста або схожість ≥ 0,9."""
    if len(own) < MIN_LENGTH or numbers(own) != numbers(post):
        return False
    return own in post or SequenceMatcher(None, own, post).ratio() >= SIMILAR


def remember(
    conn: sqlite3.Connection, post_id: int, date: int, text: str, group: str | None = None
) -> None:
    """Пост каналу в пам'ять (текст змінили — оновлюється)."""
    with conn:
        conn.execute(
            "INSERT INTO channel_posts (post_id, date, text, media_group_id, seen_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (post_id) DO UPDATE SET "
            "text = excluded.text, media_group_id = coalesce(excluded.media_group_id, "
            "channel_posts.media_group_id)",
            (post_id, date, text, group, now()),
        )


def edited(conn: sqlite3.Connection, post_id: int, text: str) -> None:
    """Пост змінено в каналі: новий текст у пам'яті, якщо пост там є."""
    with conn:
        conn.execute("UPDATE channel_posts SET text = ? WHERE post_id = ?", (text, post_id))


def text_of(message: dict) -> str | None:
    return message.get("caption") or message.get("text") or None


class Finder:
    """Шукає в каналі пост за описом: пам'ять → тихий перегляд."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        looker: presence.Presence | None,
        sleep: Callable[[float], None],
        log: Callable[[str], None] = print,
        limit: int = LIMIT,
    ) -> None:
        self.conn, self.looker, self.sleep, self.log, self.limit = conn, looker, sleep, log, limit
        self.requests = 0

    def find(self, own: str) -> list[int] | None:
        """Номери постів, що збіглися з описом `own`. `None` — перегляд не завершився: нічого
        не можна стверджувати."""
        target = normalize(own)
        if len(target) < MIN_LENGTH:
            return []
        rows = self.conn.execute("SELECT post_id, text FROM channel_posts").fetchall()
        local = sorted(r[0] for r in rows if same_post(target, normalize(r[1])))
        if local:
            return local
        known = {r[0] for r in rows} | {
            r[0] for r in self.conn.execute("SELECT post_id FROM posts").fetchall()
        }
        if not known or self.looker is None:  # нема від чого відштовхнутись чи кого просити
            return []
        return self._browse(target, known)

    def _browse(self, target: str, known: set[int]) -> list[int] | None:
        looker = self.looker
        assert looker is not None
        top = max(known)
        self.requests = 0
        found: list[int] = []
        state = {"unknown": 0}

        def probe(number: int) -> str | None:
            """`present` / `gone`; `None` — перегляд треба кинути (ліміт, збої)."""
            for _ in range(RETRIES + 1):
                if self.requests >= self.limit:
                    self.log(f"Пошук у каналі: ліміт {self.limit} запитів — перегляд кинуто")
                    return None
                self.requests += 1
                status, message = looker.look(number)
                if status == presence.UNKNOWN:
                    wait = looker.retry_after
                    if wait:  # 429: чекаємо й пробуємо той самий номер
                        self.sleep(float(wait))
                        continue
                    state["unknown"] += 1
                    if state["unknown"] >= UNKNOWN_RUN:
                        self.log("Пошук у каналі: Telegram не відповідає — перегляд кинуто")
                        return None
                    self.sleep(PAUSE)
                    return "skip"
                state["unknown"] = 0
                self.sleep(PAUSE)
                if status == presence.PRESENT:
                    self._take(number, message, target, found)
                return "present" if status == presence.PRESENT else "gone"
            self.log(f"Пост {number}: Telegram усе просить зачекати — перегляд кинуто")
            return None

        gone, number = 0, top + 1
        while gone < AHEAD_GONE:  # вгору: нові пости, яких бот не бачив
            result = probe(number)
            if result is None:
                return None
            gone = gone + 1 if result == "gone" else (0 if result == "present" else gone)
            number += 1
        for number in range(top - 1, top - BACK - 1, -1):  # вниз: пропущені між відомими
            if number < 1:
                break
            if number in known:
                continue
            if probe(number) is None:
                return None
        return sorted(found)

    def _take(self, number: int, message: dict | None, target: str, found: list[int]) -> None:
        """Пост з перегляду — в пам'ять; збіг — у `found`. Без дати оригіналу (її віддає лише
        `forward_origin`) пост не беремо: дату вигадувати не можна."""
        if not isinstance(message, dict):
            return
        origin = message.get("forward_origin") or {}
        text, date = text_of(message), origin.get("date")
        if not text or not isinstance(date, int) or origin.get("type") != "channel":
            return
        if isinstance(origin.get("message_id"), int) and origin["message_id"] != number:
            return
        remember(self.conn, number, date, text, message.get("media_group_id"))
        if same_post(target, normalize(text)):
            found.append(number)
