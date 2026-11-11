"""KLB-003 D3: знімок її активних оголошень Клубку → `klubok_listings` (сайт лише читається).

`read` — посторінково `GET /api/get-items?user_id=198135&type=0&sort=date&limit=60&offset=N` через
`fetch` (у робота — `fetch` усередині сторінки: cookies сесії, крізь guard), пауза між сторінками,
не більше 60 сторінок. Повний — лише коли унікальних id стільки, скільки `items_count` (порядок
`sort=date` — за `renew_date`, і «Обновить все» посеред читання його зсуває): інакше `Incomplete`,
`refresh` повторює все читання **один** раз через 60 с.

`save` — лише прочитане повністю, в одній транзакції: upsert за `id` (`post_id`, `asked_at`,
`brand`, `category_id`, `added_at` наявних рядків не чіпаються), чого немає — `sold = 1` (рядок
лишається), `klubok_listings_read`. Активних менше ніж 90 % від попереднього `count` — нічого не
пишемо (`klubok_snapshot_drop`, рішення людини). Там само `klubok_ads.url` виставлених програмою
дописується за `klubok_id` (DRIFT-1: при публікації посилання ще невідоме).
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shafa.klubok import dup

USER = "198135"  # її акаунт на Клубку (`docs/klubok.md`)
PAGE = 60
MAX_PAGES = 60
DROP = 0.9  # активних менше ніж 90 % від попереднього знімка — не пишемо
RETRY_WAIT = 60.0  # с до повторного читання, якщо знімок неповний
LINK = "https://klubok.com/"
PHOTO = "https://img.klubok.com/"

FAILED, DROPPED, TOO_MANY = "klubok_snapshot_failed", "klubok_snapshot_drop", "klubok_429"


class SnapshotError(Exception):
    """Знімок не годиться: у базу нічого не записано. `code` — код зупинки сесії."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


class Incomplete(SnapshotError):
    """Прочитано все, але id не стільки, скільки `items_count` (список зсунувся посеред читання)."""


@dataclass(frozen=True)
class Read:
    items: list[dict[str, Any]]
    count: int  # `items_count` сайту


def items_path(offset: int) -> str:
    return (
        f"/api/get-items?user_id={USER}&type=0&sort=date&limit={PAGE}&offset={offset}"
    )  # fmt: skip


def _page(status: int, text: str, number: int) -> tuple[list[dict[str, Any]], int]:
    where = f"сторінка {number}"
    if status == 429:
        raise SnapshotError(TOO_MANY, f"Клубок відповів 429 (забагато запитів), {where}")
    if status != 200:
        raise SnapshotError(
            FAILED, f"Список оголошень: {where} — HTTP {status or 'немає відповіді'}"
        )
    try:
        data = json.loads(text)
    except ValueError:
        raise SnapshotError(FAILED, f"Список оголошень: {where} — відповідь не JSON") from None
    if not isinstance(data, dict) or data.get("status") != "success":
        raise SnapshotError(FAILED, f"Список оголошень: {where} — не «success»")
    items, count = data.get("items"), data.get("items_count")
    if not isinstance(items, list) or not isinstance(count, int) or isinstance(count, bool):
        raise SnapshotError(FAILED, f"Список оголошень: {where} — немає items / items_count")
    for item in items:
        ok = (
            isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and not isinstance(item.get("id"), bool)
            and item["id"] > 0
            and isinstance(item.get("name"), str)
        )
        if not ok:
            raise SnapshotError(FAILED, f"Список оголошень: {where} — запис без id чи назви")
    return items, count


def read(fetch: Callable[[str], tuple[int, str]], pause: Callable[[], None]) -> Read:
    """Усі сторінки підряд. `fetch(path) -> (HTTP-статус, текст)`; 0 — запит не вдався."""
    items: list[dict[str, Any]] = []
    count = 0
    for number in range(1, MAX_PAGES + 1):
        if number > 1:
            pause()
        page, count = _page(*fetch(items_path((number - 1) * PAGE)), number)
        items.extend(page)
        if len(page) < PAGE:
            break
    else:
        raise SnapshotError(
            FAILED, f"Список оголошень довший за {MAX_PAGES} сторінок — не дочитано"
        )
    unique = {item["id"] for item in items}
    if len(unique) != count or len(unique) != len(items):
        raise Incomplete(
            FAILED,
            f"Список оголошень зсунувся посеред читання: {len(unique)} різних з {count}",
        )
    return Read(items, count)


def _text(value: Any, prefix: str) -> str | None:
    return value if isinstance(value, str) and value.startswith(prefix) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def save(conn: sqlite3.Connection, snap: Read, now: datetime | None = None) -> int:
    """Записати повний знімок (усе або нічого). Повертає кількість активних."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    active = [item for item in snap.items if item.get("sold") is not True]
    row = conn.execute("SELECT count FROM klubok_listings_read WHERE id = 1").fetchone()
    if row is not None and row[0] and len(active) < DROP * row[0]:
        raise SnapshotError(
            DROPPED,
            f"Активних оголошень на Клубку стало набагато менше: було {row[0]}, стало {len(active)}"
            " — перевірте на Клубку й вирішіть (знімок не записано)",
        )
    ids = {item["id"] for item in snap.items}
    with conn:
        for item in snap.items:
            conn.execute(
                "INSERT INTO klubok_listings (id, name, price, sizes, url, photo, sold, moderated, "
                "renew_date, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET name = excluded.name, price = excluded.price, "
                "sizes = excluded.sizes, url = excluded.url, photo = excluded.photo, "
                "sold = excluded.sold, moderated = excluded.moderated, "
                "renew_date = excluded.renew_date",
                (
                    item["id"], item["name"], _int(item.get("price")),
                    json.dumps(dup.title_sizes(item["name"]), ensure_ascii=False),
                    _text(item.get("link"), LINK), _text(item.get("img_link"), PHOTO),
                    int(item.get("sold") is True), _int(item.get("moderated")),
                    item.get("renew_date") if isinstance(item.get("renew_date"), str) else None,
                    stamp,
                ),
            )  # fmt: skip
        gone = [
            (r[0],)
            for r in conn.execute("SELECT id FROM klubok_listings WHERE sold = 0")
            if r[0] not in ids
        ]
        conn.executemany("UPDATE klubok_listings SET sold = 1 WHERE id = ?", gone)
        conn.execute(
            "UPDATE klubok_ads SET url = (SELECT l.url FROM klubok_listings l "
            "WHERE l.id = klubok_ads.klubok_id) WHERE url IS NULL AND klubok_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM klubok_listings l WHERE l.id = klubok_ads.klubok_id "
            "AND l.url IS NOT NULL)"
        )
        conn.execute(
            "INSERT INTO klubok_listings_read (id, read_at, count) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET read_at = excluded.read_at, count = excluded.count",
            (stamp, len(active)),
        )
    return len(active)


def refresh(
    conn: sqlite3.Connection,
    fetch: Callable[[str], tuple[int, str]],
    pause: Callable[[], None],
    wait: Callable[[float], None],
    log: Callable[[str], None] = print,
) -> int:
    """Прочитати й записати знімок; неповний — один повтор усього читання через `RETRY_WAIT`.
    `SnapshotError` — нічого не записано (сесія зупиняється з його кодом)."""
    try:
        snap = read(fetch, pause)
    except Incomplete as error:
        log(f"Клубок: {error.text}; читаю ще раз через {RETRY_WAIT:.0f} с")
        wait(RETRY_WAIT)
        try:
            snap = read(fetch, pause)
        except Incomplete as again:
            raise SnapshotError(FAILED, again.text) from None
    count = save(conn, snap)
    log(f"Клубок: прочитано її активних оголошень — {count}")
    return count


def missing(conn: sqlite3.Connection, klubok_ids: list[int]) -> list[int]:
    """Виставлені програмою id, яких немає серед активних у знімку."""
    active = {r[0] for r in conn.execute("SELECT id FROM klubok_listings WHERE sold = 0")}
    return [i for i in klubok_ids if i not in active]
