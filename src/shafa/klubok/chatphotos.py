"""KLB-012 (проєкт — `docs/klubok.md`, «Фото пар бота»): фото постів бота — з Експорту групи
обговорення. Бот публікує пост у Канал, а додаткові фото кладе коментарем; Telegram автоматично
пересилає пост каналу в групу обговорення, тож фото пари = пересланий пост + її відповіді з фото.
Після публікації на Шафі тека з фото стирається (`SHAFA_DELETE_PUBLISHED`), Клубок лишається
`skipped klubok_no_photos` (TD-070) — звідси беремо фото й кладемо в `<дані>/klubok-old/<N>/1.jpg…`
(те саме місце, що запасне фото KLB-008; `fields.spare_photos`).

Лише читання Експорту; мережі, ШІ й живого сайту немає. Купців у групі не бере: фото — тільки від
каналу, самої групи й того акаунта, що відповідав на більшість пересланих постів («її»).
"""

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shafa import events
from shafa.borh import copy_photo
from shafa.export import ExportError, Post, photo_path, plain_text
from shafa.folders import klubok_spare, list_photos
from shafa.klubok import fields, queue

NO_PHOTOS = "klubok_no_photos"  # код `fields.ad`: фото Картки немає на диску й запасних теж
GROUP_TYPE = "private_supergroup"
TEXT_LEN = 120  # скільки перших символів тексту звіряємо
WINDOW = 600  # пересланий пост — не пізніше ніж за 10 хв після поста, секунд
PHOTOS_MAX = fields.PHOTOS_MAX
FAIL_IDS = 8  # скільки номерів постів друкуємо на причину
PER_LINE = 9  # пар «пост:фото» в одному рядку
PAIR_LINES = 24  # скільки таких рядків друкуємо

NOT_IN_EXPORT = "поста немає в Експорті каналу"
NO_TEXT = "у поста немає тексту"
NO_FORWARD = "в обговоренні не знайдено пересланого поста"
NO_PICTURES = "у пересланому пості й відповідях немає фото"
NO_FILES = "файли фото відсутні в Експорті обговорення"
UNREADABLE = "жодне фото не відкривається"
HAS_PHOTOS = "тека з фото вже є"


class WrongDiscussion(ExportError):
    """Експорт — не група обговорення нашого Каналу."""


@dataclass(frozen=True)
class Message:
    id: int
    at: datetime
    from_id: str
    name: str
    text: str
    photo: str | None
    reply_to: int | None
    auto: bool  # автопересланий пост каналу


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()[:TEXT_LEN]


@dataclass
class Discussion:
    name: str
    messages: list[Message]
    forwards: list[Message]
    allowed: set[str]
    author: str  # «її» акаунт (відповідає на більшість пересланих постів), "" — немає
    author_name: str
    author_photos: int  # відповідей з фото від неї на пересланi пости

    def replies(self) -> dict[int, list[Message]]:
        out: dict[int, list[Message]] = {}
        for m in self.messages:
            if m.reply_to is not None and m.photo and m.from_id in self.allowed:
                out.setdefault(m.reply_to, []).append(m)
        return out


def _message(raw: dict, channel: str) -> Message | None:
    if raw.get("type") != "message" or not isinstance(raw.get("id"), int):
        return None
    try:
        at = datetime.fromisoformat(str(raw.get("date")))
    except ValueError:
        return None
    from_id = str(raw.get("from_id") or "")
    reply = raw.get("reply_to_message_id")
    return Message(
        id=raw["id"],
        at=at,
        from_id=from_id,
        name=str(raw.get("from") or ""),
        text=plain_text(raw.get("text", "")),
        photo=photo_path(raw.get("photo")),
        reply_to=reply if isinstance(reply, int) else None,
        auto=bool(from_id) and from_id == str(raw.get("forwarded_from_id") or "") == channel,
    )


def read_discussion(raw: bytes, channel_id: int | None) -> Discussion:
    """`result.json` групи обговорення → `Discussion`. Інший чат, не група чи нема автопересилань
    з нашого Каналу — `WrongDiscussion` з поясненням (нічого не записано)."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError("Експорт обговорення — не JSON") from error
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ExportError("Це не Експорт чату Telegram")
    name = str(data.get("name") or "без назви")
    if data.get("type") != GROUP_TYPE:
        raise WrongDiscussion(
            f"Це не група обговорення (тип «{data.get('type')}», очікую «{GROUP_TYPE}»): «{name}»"
        )
    if channel_id is None:
        raise WrongDiscussion("Не знаю id Каналу: Експорт каналу без id")
    channel = f"channel{channel_id}"
    messages = [m for r in data["messages"] if isinstance(r, dict) and (m := _message(r, channel))]
    forwards = [m for m in messages if m.auto]
    if not forwards:
        raise WrongDiscussion(
            f"У «{name}» немає жодного поста, автопереслано з нашого Каналу ({channel}): "
            "це не його група обговорення"
        )
    group = f"channel{data.get('id')}"
    ids = {m.id for m in forwards}
    answers = Counter(
        m.from_id for m in messages if m.reply_to in ids and not m.from_id.startswith("channel")
    )
    author = answers.most_common(1)[0][0] if answers else ""
    allowed = {channel, group} | ({author} if author else set())
    names = {m.from_id: m.name for m in messages if m.name}
    return Discussion(
        name=name,
        messages=messages,
        forwards=forwards,
        allowed=allowed,
        author=author,
        author_name=names.get(author, ""),
        author_photos=sum(
            1 for m in messages if m.from_id == author and m.reply_to in ids and m.photo
        ),
    )


# --- зіставлення ---


@dataclass
class Result:
    post_id: int
    photos: int = 0  # знайдено фото (dry-run) / скопійовано
    reason: str | None = None  # чому пропущено


def find_forward(chat: Discussion, post: Post) -> Message | None:
    """Пересланий пост: текст (перші `TEXT_LEN` символів, нормалізований) збігається, переслано
    за 0…`WINDOW` с після поста; кілька — найближчий за часом."""
    want = normalize(post.text)
    try:
        posted = datetime.fromisoformat(post.date)
    except ValueError:
        return None
    best: tuple[float, Message] | None = None
    for m in chat.forwards:
        delta = (m.at - posted).total_seconds()
        if 0 <= delta <= WINDOW and normalize(m.text) == want and (best is None or delta < best[0]):
            best = (delta, m)
    return best[1] if best else None


def pair_photos(chat: Discussion, forward: Message, replies: dict[int, list[Message]]) -> list[str]:
    """Фото пересланого поста й її відповідей, за часом, без повторів, ≤ `PHOTOS_MAX`."""
    found = ([forward] if forward.photo else []) + sorted(
        replies.get(forward.id, []), key=lambda m: (m.at, m.id)
    )
    out: list[str] = []
    for m in found:
        if m.photo and m.photo not in out:
            out.append(m.photo)
    return out[:PHOTOS_MAX]


def candidates(conn: sqlite3.Connection, root: Path) -> list[int]:
    """Пости, яким Клубок потрібен, а фото немає: вердикт черги `klubok_no_photos`."""
    verdicts = queue.plan(conn, root)
    return sorted({v.post_id for v in verdicts if v.code == NO_PHOTOS and v.post_id is not None})


@dataclass
class Report:
    applied: bool
    chat: Discussion | None = None
    results: list[Result] = field(default_factory=list)
    refreshed: int | None = None  # черга перерахована: пар

    def done(self) -> list[Result]:
        return [r for r in self.results if r.reason is None]

    def failed(self) -> list[Result]:
        return [r for r in self.results if r.reason is not None]

    def lines(self) -> list[str]:
        """≤ 40 рядків."""
        chat = self.chat
        out = [] if self.applied else ["Пробний прогін: нічого не записано."]
        if chat is not None:
            who = f"{chat.author_name or chat.author} ({chat.author_photos} відповідей з фото)"
            out.append(
                f"Обговорення «{chat.name}»: повідомлень {len(chat.messages)}, "
                f"автопересланих постів каналу {len(chat.forwards)}"
            )
            out.append(f"Фото беру від: канал, група, {who if chat.author else 'акаунта немає'}")
        done, failed = self.done(), self.failed()
        total = sum(r.photos for r in done)
        counts = [r.photos for r in done]
        span = f" ({min(counts)}–{max(counts)} на пару)" if counts else ""
        verb = "скопійовано" if self.applied else "буде скопійовано"
        out.append(
            f"Пар без фото: {len(self.results)} · знайдено {len(done)} · "
            f"не знайдено {len(failed)} · {verb} фото: {total}{span}"
        )
        pairs = [f"{r.post_id}:{r.photos}" for r in done]
        rows = [pairs[i : i + PER_LINE] for i in range(0, len(pairs), PER_LINE)]
        out += ["  " + " ".join(row) for row in rows[:PAIR_LINES]]
        if len(rows) > PAIR_LINES:
            out.append(f"  …і ще {len(pairs) - PAIR_LINES * PER_LINE} пар")
        for reason in dict.fromkeys(r.reason for r in failed):
            ids = [r.post_id for r in failed if r.reason == reason]
            more = f" …і ще {len(ids) - FAIL_IDS}" if len(ids) > FAIL_IDS else ""
            out.append(
                f"  пропущено {len(ids)} — {reason}: {' '.join(map(str, ids[:FAIL_IDS]))}{more}"
            )
        if self.refreshed is not None:
            out.append(f"Черга Клубку перерахована: {self.refreshed} пар")
        return out[:40]


def _copy(chat_dir: Path, names: list[str], post_id: int) -> int:
    """Фото → `<дані>/klubok-old/<N>/1.jpg…`; номери підряд; наявного не перезаписуємо."""
    target = klubok_spare(post_id)
    count = 0
    for name in names:
        if copy_photo(chat_dir / name, target / f"{count + 1}.jpg"):
            count += 1
    return count


def run(
    conn: sqlite3.Connection,
    posts: dict[int, Post],
    chat: Discussion,
    chat_dir: Path,
    root: Path,
    *,
    write: bool,
) -> Report:
    report = Report(applied=write, chat=chat)
    replies = chat.replies()
    for post_id in candidates(conn, root):
        result = Result(post_id)
        report.results.append(result)
        post = posts.get(post_id)
        if post is None:
            result.reason = NOT_IN_EXPORT
            continue
        if not normalize(post.text):
            result.reason = NO_TEXT
            continue
        forward = find_forward(chat, post)
        if forward is None:
            result.reason = NO_FORWARD
            continue
        names = pair_photos(chat, forward, replies)
        if not names:
            result.reason = NO_PICTURES
            continue
        present = [n for n in names if (chat_dir / n).is_file()]
        if not present:
            result.reason = NO_FILES
            continue
        folder = klubok_spare(post_id)
        if folder.is_dir() and list_photos(folder):
            result.reason = HAS_PHOTOS
            continue
        if not write:
            result.photos = len(present)
            continue
        result.photos = _copy(chat_dir, present, post_id)
        if not result.photos:
            result.reason = UNREADABLE
    done = report.done()
    if write and done:
        events.emit(
            "klubok.photos",
            f"Клубок: фото постів бота з обговорення — {len(done)} пар, "
            f"{sum(r.photos for r in done)} фото",
            data={"pairs": len(done), "photos": sum(r.photos for r in done),
                  "skipped": len(report.failed())},
        )  # fmt: skip
    return report
