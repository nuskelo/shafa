"""KLB-003 D3: знімок її активних оголошень Клубку → `klubok_listings` (сайт лише читається).

`read` — спершу **один** запит `GET /api/get-items?user_id=198135&type=0&sort=date&limit=3000&offset=0`
через `fetch` (у робота — `fetch` усередині сторінки: cookies сесії, крізь guard). KLB-006: посторінково
(`limit=60`) список нестабільний — `sort=date` = за `renew_date`, а після «Обновить все» у сотень
оголошень вона однакова до секунди, і порядок між запитами з різним `offset` не гарантований (живий
знімок 11.11: «1782 різних з 1787» двічі). Один запит віддає все (Фаза 0: 1787 з 1787). Запасний
шлях — сайт віддав менше, ніж `items_count`: до `PASSES` посторінкових проходів (пауза між
сторінками, `RETRY_WAIT` між проходами), записи **об'єднуються за id** (свіжіший перемагає), разом не
більше `PAGE_BUDGET` сторінок. Повний — лише коли різних id рівно `items_count`; не зійшлось —
`Incomplete` (`klubok_snapshot_failed`), нічого не пишемо.

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
PAGE_ALL = 3000  # один запит на весь список (KLB-006); сторінками — лише якщо сайт віддав менше
PASSES = 3  # посторінкових проходів запасного шляху
PAGE_BUDGET = 3 * 30  # сторінок на один знімок разом
MAX_PAGES = 60  # сторінок в одному проході
DROP = 0.9  # активних менше ніж 90 % від попереднього знімка — не пишемо
RETRY_WAIT = 60.0  # с між посторінковими проходами
LINK = "https://klubok.com/"
PHOTO = "https://img.klubok.com/"

FAILED, DROPPED, TOO_MANY = "klubok_snapshot_failed", "klubok_snapshot_drop", "klubok_429"


class SnapshotError(Exception):
    """Знімок не годиться: у базу нічого не записано. `code` — код зупинки сесії."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


class Incomplete(SnapshotError):
    """Прочитано все, але різних id не стільки, скільки `items_count` (нестабільні сторінки)."""


@dataclass(frozen=True)
class Read:
    items: list[dict[str, Any]]
    count: int  # `items_count` сайту


def items_path(offset: int, limit: int = PAGE) -> str:
    return (
        f"/api/get-items?user_id={USER}&type=0&sort=date&limit={limit}&offset={offset}"
    )  # fmt: skip


def _page(status: int, text: str, where: str) -> tuple[list[dict[str, Any]], int]:
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


def _complete(items: list[dict[str, Any]], count: int) -> bool:
    return len({item["id"] for item in items}) == count == len(items)


def read(
    fetch: Callable[[str], tuple[int, str]],
    pause: Callable[[], None],
    wait: Callable[[float], None] = lambda seconds: None,
    log: Callable[[str], None] = print,
) -> Read:
    """Увесь список одним запитом; не вийшло — посторінкові проходи з об'єднанням за id.
    `fetch(path) -> (HTTP-статус, текст)`; 0 — запит не вдався."""
    items, count = _page(*fetch(items_path(0, PAGE_ALL)), "весь список")
    if _complete(items, count):
        return Read(items, count)
    merged = {item["id"]: item for item in items}
    log(f"Клубок: одним запитом {len(merged)} різних з {count}; читаю сторінками")
    budget = PAGE_BUDGET
    for attempt in range(1, PASSES + 1):
        if budget <= 0:
            break
        if attempt > 1:
            log(
                f"Клубок: зібрано {len(merged)} різних з {count}; ще прохід через {RETRY_WAIT:.0f} с"
            )
            wait(RETRY_WAIT)
        for number in range(1, MAX_PAGES + 1):
            if budget <= 0:
                break
            pause()
            budget -= 1
            page, count = _page(*fetch(items_path((number - 1) * PAGE)), f"сторінка {number}")
            merged.update((item["id"], item) for item in page)  # свіжіший перемагає
            if len(page) < PAGE:
                break
        else:
            raise SnapshotError(
                FAILED, f"Список оголошень довший за {MAX_PAGES} сторінок — не дочитано"
            )
        if len(merged) == count:
            return Read(list(merged.values()), count)
    raise Incomplete(FAILED, f"Список оголошень не зійшовся: {len(merged)} різних з {count}")


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
    """Прочитати (`read`) і записати знімок. `SnapshotError` — нічого не записано (сесія
    зупиняється з його кодом)."""
    snap = read(fetch, pause, wait, log)
    count = save(conn, snap)
    log(f"Клубок: прочитано її активних оголошень — {count}")
    return count


def missing(conn: sqlite3.Connection, klubok_ids: list[int]) -> list[int]:
    """Виставлені програмою id, яких немає серед активних у знімку."""
    active = {r[0] for r in conn.execute("SELECT id FROM klubok_listings WHERE sold = 0")}
    return [i for i in klubok_ids if i not in active]
