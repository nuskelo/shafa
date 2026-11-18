"""KLB-003 D3: знімок її активних оголошень Клубку → `klubok_listings` (сайт лише читається).

KLB-013 (живий прогін 18.11): вкладки `/shop/manage` — `get-items` з `type=0` «Активные», `type=1`
«Обновить» (уже можна оновити: за ≈ 2 год до терміну й після), `type=2` — продані/сховані;
`tabs_count` у відповіді — чотири лічильники вкладок `[Активные, Обновить, Проданные, …]`.
**Прострочене зникає з `type=0` і лишається лише в `type=1`**, тож знімок = `type=0` ∪ `type=1` за id
(свіжіший `renew_date` перемагає); прострочене — `sold = 0`, `renew_date` як є (≤ зараз).

`read` — для кожної вкладки спершу **один** запит `GET /api/get-items?user_id=198135&type=N&sort=date
&limit=3000&offset=0` через `fetch` (у робота — `fetch` усередині сторінки: cookies сесії, крізь guard).
KLB-006: посторінково (`limit=60`) список нестабільний — `sort=date` = за `renew_date`, а після
«Обновить все» у сотень оголошень вона однакова до секунди, і порядок між запитами з різним `offset`
не гарантований (живий знімок 11.11: «1782 різних з 1787» двічі). Один запит віддає все (Фаза 0:
1787 з 1787). Запасний шлях — сайт віддав менше, ніж `items_count`: до `PASSES` посторінкових проходів
на вкладку (пауза між сторінками, `RETRY_WAIT` між проходами), записи **об'єднуються за id** (свіжіший
перемагає), не більше `PAGE_BUDGET` сторінок на вкладку. Повнота: «Обновить» — різних id рівно
`items_count`; «Активные» — не менше `items_count` (живий: 1788 при `items_count` 1785 — туди входять
ще не прострочені з «Обновить»), а об'єднання — не менше `tabs_count[0]`. Не зійшлось чи помилка будь-
якої вкладки — `Incomplete` / `SnapshotError` (`klubok_snapshot_failed`), нічого не пишемо.

`save` — лише прочитане повністю, в одній транзакції: upsert за `id` (`post_id`, `asked_at`,
`brand`, `category_id`, `added_at` наявних рядків не чіпаються), чого немає в жодній вкладці —
`sold = 1` (рядок лишається), `klubok_listings_read.count` = активні + прострочені. Їх менше ніж 90 %
від попереднього `count` — нічого не пишемо (`klubok_snapshot_drop`, рішення людини). Там само
`klubok_ads.url` виставлених програмою дописується за `klubok_id` (DRIFT-1: при публікації посилання ще
невідоме).
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
PAGE_BUDGET = 3 * 30  # сторінок на одну вкладку разом
MAX_PAGES = 60  # сторінок в одному проході
DROP = 0.9  # активних + прострочених менше ніж 90 % від попереднього знімка — не пишемо
ACTIVE, RENEWABLE = 0, 1  # `type` у get-items: вкладки «Активные» і «Обновить» (KLB-013)
TAB_NAMES = ("активні", "«Обновить»", "продані", "четверта")
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
    items: list[dict[str, Any]]  # «Активные» ∪ «Обновить» за id
    count: int  # різних id в об'єднанні
    renewable: frozenset[int] = frozenset()  # id з вкладки «Обновить» (`type=1`)
    tabs: tuple[int, ...] | None = None  # `tabs_count` сайту як є (з відповіді «Активные»)

    def tabs_text(self) -> str:
        """«вкладки: активні 1785 · «Обновить» 4 · продані 1 · четверта 0» — для журналу й подій."""
        if not self.tabs:
            return f"вкладка «Обновить» — {len(self.renewable)}"
        named = zip(TAB_NAMES, self.tabs, strict=False)
        return "вкладки: " + " · ".join(f"{name} {n}" for name, n in named)


def items_path(offset: int, limit: int = PAGE, tab: int = ACTIVE) -> str:
    return (
        f"/api/get-items?user_id={USER}&type={tab}&sort=date&limit={limit}&offset={offset}"
    )  # fmt: skip


def _tabs(text: str) -> tuple[int, ...] | None:
    """`tabs_count` з відповіді (лише цілі числа), інакше None."""
    try:
        value = json.loads(text).get("tabs_count")
    except (ValueError, AttributeError):
        return None
    ok = isinstance(value, list) and all(
        isinstance(n, int) and not isinstance(n, bool) for n in value
    )
    return tuple(value) if ok and value else None


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


def _enough(found: int, count: int, tab: int) -> bool:
    """«Обновить» — рівно `items_count`; «Активные» — не менше (KLB-013: 1788 при 1785)."""
    return found == count if tab == RENEWABLE else found >= count


def _read_tab(
    fetch: Callable[[str], tuple[int, str]],
    pause: Callable[[], None],
    wait: Callable[[float], None],
    log: Callable[[str], None],
    tab: int,
) -> tuple[list[dict[str, Any]], tuple[int, ...] | None]:
    """Одна вкладка: одним запитом; не вийшло — посторінкові проходи з об'єднанням за id."""
    label = "" if tab == ACTIVE else " «Обновить»"
    status, text = fetch(items_path(0, PAGE_ALL, tab))
    items, count = _page(status, text, f"весь список{label}")
    tabs = _tabs(text)
    ids = {item["id"] for item in items}
    if len(ids) == len(items) and _enough(len(ids), count, tab):
        return items, tabs
    merged = {item["id"]: item for item in items}
    log(f"Клубок{label}: одним запитом {len(merged)} різних з {count}; читаю сторінками")
    budget = PAGE_BUDGET
    for attempt in range(1, PASSES + 1):
        if budget <= 0:
            break
        if attempt > 1:
            log(
                f"Клубок{label}: зібрано {len(merged)} різних з {count}; "
                f"ще прохід через {RETRY_WAIT:.0f} с"
            )
            wait(RETRY_WAIT)
        for number in range(1, MAX_PAGES + 1):
            if budget <= 0:
                break
            pause()
            budget -= 1
            page, count = _page(
                *fetch(items_path((number - 1) * PAGE, tab=tab)), f"сторінка {number}{label}"
            )
            merged.update((item["id"], item) for item in page)  # свіжіший перемагає
            if len(page) < PAGE:
                break
        else:
            raise SnapshotError(
                FAILED, f"Список оголошень{label} довший за {MAX_PAGES} сторінок — не дочитано"
            )
        if _enough(len(merged), count, tab):
            return list(merged.values()), tabs
    raise Incomplete(FAILED, f"Список оголошень{label} не зійшовся: {len(merged)} різних з {count}")


def _fresher(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Той самий id в обох вкладках: запис із пізнішим `renew_date` (ISO-рядки одного вигляду)."""
    first, second = a.get("renew_date"), b.get("renew_date")
    if isinstance(second, str) and (not isinstance(first, str) or second > first):
        return b
    return a


def read(
    fetch: Callable[[str], tuple[int, str]],
    pause: Callable[[], None],
    wait: Callable[[float], None] = lambda seconds: None,
    log: Callable[[str], None] = print,
) -> Read:
    """«Активные» ∪ «Обновить» (KLB-013). `fetch(path) -> (HTTP-статус, текст)`; 0 — запит не
    вдався. Помилка будь-якої вкладки — `SnapshotError`, нічого не повертається."""
    active, tabs = _read_tab(fetch, pause, wait, log, ACTIVE)
    renewable, _ = _read_tab(fetch, pause, wait, log, RENEWABLE)
    merged = {item["id"]: item for item in active}
    for item in renewable:
        merged[item["id"]] = _fresher(merged[item["id"]], item) if item["id"] in merged else item
    if tabs and len(merged) < tabs[0]:
        raise Incomplete(
            FAILED, f"Список оголошень не зійшовся: {len(merged)} різних, а активних {tabs[0]}"
        )
    return Read(list(merged.values()), len(merged), frozenset(i["id"] for i in renewable), tabs)


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
            f"Активних (з простроченими) оголошень на Клубку стало набагато менше: було {row[0]}, "
            f"стало {len(active)} — перевірте на Клубку й вирішіть (знімок не записано)",
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
    зупиняється з його кодом). Повертає кількість активних (з простроченими)."""
    return take(conn, fetch, pause, wait, log)[1]


def take(
    conn: sqlite3.Connection,
    fetch: Callable[[str], tuple[int, str]],
    pause: Callable[[], None],
    wait: Callable[[float], None],
    log: Callable[[str], None] = print,
) -> tuple[Read, int]:
    """Те саме, що `refresh`, але віддає й прочитане (KLB-013: id вкладки «Обновить»)."""
    snap = read(fetch, pause, wait, log)
    count = save(conn, snap)
    log(f"Клубок: прочитано її активних оголошень — {count} ({snap.tabs_text()})")
    return snap, count


def missing(conn: sqlite3.Connection, klubok_ids: list[int]) -> list[int]:
    """Виставлені програмою id, яких немає серед активних у знімку."""
    active = {r[0] for r in conn.execute("SELECT id FROM klubok_listings WHERE sold = 0")}
    return [i for i in klubok_ids if i not in active]
