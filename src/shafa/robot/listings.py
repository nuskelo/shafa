"""Її товари на Шафі — для перевірки дублів перед публікацією (D4, п. 3.13.1 оферти).

Читаються з кабінету «Мої речі»: Активні, На модерації, Відхилені (Деактивовані — продане чи
зняте — не рахуються). CORE-007: знімок у базі (`my_listings`), перечитується раз на N днів
(«Перечитувати Мої речі кожні N днів», типово 7) або кнопкою «Перечитати зараз»; кожна щойно
опублікована роботом Картка одразу дописується в знімок. Знімок старий чи порожній — читаємо кабінет.

Збіг: та сама група розділів (усе взуття — жіноче, чоловіче й дитяче разом; або всі аксесуари: ШІ
плутає стать), розмір відрізняється не більше ніж на 0,5 (або обидва без розміру), той самий бренд
(або обидва без бренду) і схожа суть назви — без шаблонних слів («оригінал», «N розміру», «в
ідеальному стані», бренд). Не збіг: різний тип у назві (тапочки / уги / чоботи…), різний колір
(коли відомий з обох боків), ціна різниться більше ніж на 40 %. Сумнів — краще «Проблема», ніж
друге оголошення.
"""

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from shafa import catalog, config
from shafa.robot import browser

FEED_OP = "WEB_MyClothesProductsFeed"
TABS = {
    "ACTIVE": "https://shafa.ua/uk/my/clothes/active",
    "MODERATED": "https://shafa.ua/uk/my/clothes/moderated",
    "DECLINED": "https://shafa.ua/uk/my/clothes/declined",
}
SIMILAR = 0.6  # схожість назв (0…1) за однакових групи розділів, розміру й бренду
SIZE_STEP = 0.5  # «вказаний 38, ідуть на 37,5»: сусідній розмір — теж збіг
SITE = "https://shafa.ua"
STALL_ROUNDS = 20  # прокруток поспіль без нових товарів — список не довантажується
PRICE_GAP = 0.4  # ціни різняться більше ніж на 40 % — не той самий товар
DAYS_SETTING = "SHAFA_LISTINGS_DAYS"  # перечитувати «Мої речі» кожні N днів
DAYS_DEFAULT = 7
DAYS_RANGE = (1, 30)
PHOTOS_DIR = "duplicates"  # фото схожих оголошень для екрана — у теці даних поруч із базою


class ListingsError(Exception):
    """Список її товарів не прочитався повністю — публікувати не можна (дублі не перевірити)."""


@dataclass(frozen=True)
class Listing:
    id: int
    name: str
    brand: str | None
    slug: str
    sizes: tuple[str, ...]
    url: str
    colors: tuple[str, ...] = ()
    price: int | None = None
    photo: str | None = None  # адреса фото на Шафі


def _price(value: Any) -> int | None:
    """Ціна з відповіді кабінету: число, рядок «1 300» або {"amount": …}. Назви полів не звірені
    наживо (CORE-007) — нема чого розібрати → None, правило ціни просто не діє."""
    if isinstance(value, dict):
        value = next((value[k] for k in ("amount", "value", "price") if k in value), None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and (digits := re.sub(r"\D", "", value.split(".")[0])):
        return int(digits)
    return None


def _colors(node: dict) -> tuple[str, ...]:
    raw = node.get("colors") or ([node["color"]] if node.get("color") else [])
    names = [(c.get("name") or c.get("title")) if isinstance(c, dict) else c for c in raw]
    return tuple(n for n in names if isinstance(n, str) and n)


def _photo(node: dict) -> str | None:
    """Перше фото товару, якщо кабінет його віддав (лише http(s))."""
    for key in ("thumbnail", "photo", "image", "mainPhoto", "mainImage", "photos", "images"):
        value = node.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = next((value[k] for k in ("url", "src", "thumbnail") if value.get(k)), None)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return None


def parse_feed(response: dict) -> tuple[list[Listing], bool, int]:
    """Відповідь `WEB_MyClothesProductsFeed` → (товари, чи є ще сторінка, скільки всього)."""
    products = response["data"]["viewer"]["products"]
    listings = []
    for edge in products["edges"]:
        node = edge["node"]
        listings.append(
            Listing(
                id=int(node["id"]),
                name=node.get("name") or "",
                brand=(node.get("brand") or {}).get("name"),
                slug=node.get("catalogSlug") or "",
                sizes=tuple(s["name"] for s in node.get("sizes") or []),
                url=SITE + (node.get("url") or f"/uk/item/{node['id']}-shafa"),
                colors=_colors(node),
                price=_price(node.get("price")),
                photo=_photo(node),
            )
        )
    info = products["pageInfo"]
    return listings, bool(info["hasNextPage"]), int(info.get("total") or 0)


# Шаблонні слова її назв: суть лишається («уги замшеві» проти «тапочки»).
TEMPLATE_WORDS = frozenset(
    [
        "оригінал",
        "оригінальні",
        "оригінальний",
        "оригінальна",
        "розміру",
        "розмір",
        "розміри",
        "р",
        "в",
        "у",
        "стані",
        "стан",
        "нових",
        "нового",
        "новий",
        "нові",
        "нова",
        "новому",
        "ідеальному",
        "ідеальний",
        "ідеальні",
        "відмінному",
        "відмінний",
        "чудовому",
        "хорошому",
        "гарному",
        "дуже",
        "та",
        "і",
        "й",
        "з",
    ]
)
# Тип речі в назві: різний — не дубль. Корені, без бренду («UGG» — бренд, не тип).
SHOE_TYPES = (
    ("тапочки", re.compile(r"^(?:тапоч|капц|капці)")),
    ("уги", re.compile(r"^у[гґ]{1,2}(?:и|і|ів|ах)?$")),
    ("дутики", re.compile(r"^дутик")),
    ("чоботи", re.compile(r"^(?:чобот|чобіт|сапож|ботфорт)")),
    ("черевики", re.compile(r"^(?:черев|напівчерев|ботинк|челсі|мартінс)")),
    ("ботильйони", re.compile(r"^ботильйон")),
    ("кросівки", re.compile(r"^(?:кросів|кросов|снікер|сникер)")),
    ("кеди", re.compile(r"^кед")),
    ("туфлі", re.compile(r"^туфл")),
    ("босоніжки", re.compile(r"^(?:босоніж|сандал)")),
    ("мокасини", re.compile(r"^(?:мокасин|лофер|топсайдер)")),
    ("сліпони", re.compile(r"^(?:сліпон|еспадрил)")),
    ("балетки", re.compile(r"^балетк")),
    ("шльопанці", re.compile(r"^(?:шльопан|в['ʼ’]?єтнам|сабо|мюлі|крокс)")),
    ("бутси", re.compile(r"^бутс")),
    ("гумові", re.compile(r"^гумов")),
)


def _words(title: str) -> list[str]:
    return re.findall(r"\w+", title.casefold().replace("ʼ", "'").replace("’", "'"))


def shoe_type(title: str) -> str | None:
    """Тип речі за першим упізнаним словом назви або None."""
    for word in _words(title):
        for name, pattern in SHOE_TYPES:
            if pattern.match(word):
                return name
    return None


def core(title: str, brand: str | None = None) -> str:
    """Суть назви: без шаблонних слів, чисел (розмір) і бренду."""
    skip = TEMPLATE_WORDS | set(_words(brand or ""))
    return " ".join(w for w in _words(title) if w not in skip and not w.isdigit())


def _ratio(a: str, b: str, brand: str | None = None) -> float:
    return SequenceMatcher(None, core(a, brand), core(b, brand)).ratio()


def similar(a: str, b: str, brand: str | None = None) -> bool:
    """Схожа суть назви; різний тип у назві — ні. Одна суть — частина іншої (чи порожня) — так."""
    type_a, type_b = shoe_type(a), shoe_type(b)
    if type_a and type_b and type_a != type_b:
        return False
    words_a, words_b = set(core(a, brand).split()), set(core(b, brand).split())
    if words_a <= words_b or words_b <= words_a:  # одна назва — частина іншої (або порожня)
        return True
    return _ratio(a, b, brand) >= SIMILAR


def prices_close(ours: int | None, theirs: int | None) -> bool:
    if not ours or not theirs:
        return True
    return abs(ours - theirs) <= PRICE_GAP * max(ours, theirs)


# CORE-010: рукавички, шарфи й шапки шукають дубль лише між собою; решта аксесуарів (сумки,
# гаманці, ремені) — разом, як раніше.
OWN_GROUPS = (
    "Аксесуари / Рукавички та рукавиці",
    "Аксесуари / Шарфи і хустки",
    "Аксесуари / Головні убори",
)


def _bucket(name: str) -> str | None:
    """Де шукати дубль: назва власної групи, «accessory» (решта аксесуарів) або None (взуття)."""
    if catalog.section_kind(name) is not catalog.Kind.ACCESSORY:
        return None
    key = catalog.normalize_section(name).casefold()
    return next((g for g in OWN_GROUPS if key.startswith(g.casefold() + " /")), "accessory")


def group_slugs(path: str) -> frozenset[str]:
    """Розділи, серед яких шукати дубль: усе взуття знімка, рукавички / шарфи / шапки окремо
    або решта аксесуарів."""
    bucket = _bucket(path)
    return frozenset(
        entry["slug"]
        for name, entry in catalog.snapshot()["sections"].items()
        if _bucket(name) == bucket
    )


def _number(size: str) -> float | None:
    try:
        return float(size.replace(",", "."))
    except ValueError:
        return None


def sizes_close(ours: list[str], theirs: tuple[str, ...]) -> bool:
    """Будь-який наш розмір не далі 0,5 від будь-якого її; нечислові («One size») — рівні."""
    if not ours and not theirs:
        return True
    for a in ours:
        for b in theirs:
            x, y = _number(a), _number(b)
            if (x is not None and y is not None and abs(x - y) <= SIZE_STEP) or a == b:
                return True
    return False


def find_duplicate(
    title: str, slugs: frozenset[str], sizes: list[str], brand: str | None,
    listings: list[Listing], colors: list[str] | tuple[str, ...] = (), price: int | None = None,
) -> Listing | None:  # fmt: skip
    """Її товар, найсхожіший на Картку, або None.

    Колір не порівнюється (CORE-008 F, TD-034): кабінет «Мої речі» його не віддає — «колір
    невідомий» не рахуємо ні збігом, ні незбігом. Параметр `colors` лишився для викликів.
    """
    wanted_brand = (brand or "").casefold()
    found = [
        listing
        for listing in listings
        if listing.slug in slugs
        and (listing.brand or "").casefold() == wanted_brand
        and sizes_close(sizes, listing.sizes)
        and similar(title, listing.name, brand)
        and prices_close(price, listing.price)
    ]
    return max(found, key=lambda x: _ratio(title, x.name, brand), default=None)


# --- Знімок «Мої речі» в базі (CORE-007) ---


class SetupError(Exception):
    """Налаштування з помилкою — робот не стартує, екран пояснює."""


def refresh_days() -> int:
    raw = config.setting(DAYS_SETTING)
    if raw is None:
        return DAYS_DEFAULT
    low, high = DAYS_RANGE
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise SetupError(f"{DAYS_SETTING} — ціле число від {low} до {high}")
    return int(raw)


def _now() -> datetime:
    return datetime.now(UTC)


def _row(listing: Listing, stamp: str) -> tuple:
    return (
        listing.id, listing.name, listing.brand, listing.slug,
        json.dumps(list(listing.sizes), ensure_ascii=False), listing.url,
        json.dumps(list(listing.colors), ensure_ascii=False), listing.price, listing.photo, stamp,
    )  # fmt: skip


_INSERT = (
    "INSERT OR REPLACE INTO my_listings (id, name, brand, slug, sizes, url, colors, price, photo, "
    "added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def snapshot_info(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Для екрана: скільки її товарів у знімку і коли кабінет прочитано (None — ще ні / скинуто)."""
    count = conn.execute("SELECT count(*) FROM my_listings").fetchone()[0]
    row = conn.execute("SELECT read_at FROM my_listings_read").fetchone()
    return count, row[0] if row else None


def stored(
    conn: sqlite3.Connection, days: int, now: datetime | None = None
) -> list[Listing] | None:
    """Знімок, якщо він свіжий (прочитано не раніше ніж N днів тому) і не порожній; інакше None."""
    count, read_at = snapshot_info(conn)
    if read_at is None or not count:
        return None
    if (now or _now()) - datetime.fromisoformat(read_at) > timedelta(days=days):
        return None
    return [
        Listing(
            r["id"],
            r["name"],
            r["brand"],
            r["slug"],
            tuple(json.loads(r["sizes"])),
            r["url"],
            tuple(json.loads(r["colors"])),
            r["price"],
            r["photo"],
        )
        for r in conn.execute("SELECT * FROM my_listings ORDER BY id")
    ]


def store(conn: sqlite3.Connection, listings: list[Listing], now: datetime | None = None) -> None:
    """Повний знімок кабінету замість старого (зняті з продажу зникають)."""
    stamp = (now or _now()).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM my_listings")
        conn.executemany(_INSERT, [_row(x, stamp) for x in listings])
        conn.execute(
            "INSERT OR REPLACE INTO my_listings_read (id, read_at) VALUES (1, ?)", (stamp,)
        )


def add(conn: sqlite3.Connection, listing: Listing, now: datetime | None = None) -> None:
    """Щойно опублікований роботом товар — у знімок одразу (дубль не пройде між перечитуваннями)."""
    with conn:
        conn.execute(_INSERT, _row(listing, (now or _now()).isoformat(timespec="seconds")))


def reset(conn: sqlite3.Connection) -> None:
    """«Перечитати зараз»: знімок застарів — робот прочитає кабінет на наступному старті."""
    with conn:
        conn.execute("DELETE FROM my_listings_read")


def fetch_photo(session: browser.Session, url: str) -> bytes | None:
    """Фото схожого оголошення для екрана: окрема вкладка крізь той самий захист (лише читання)."""
    page = session.context.new_page()
    try:
        response = page.goto(url, wait_until="load")
        if response is None or not response.ok:
            return None
        if not (response.headers.get("content-type") or "").startswith("image/"):
            return None
        return response.body()
    except Exception:  # noqa: BLE001 — фото лише для екрана; без нього Проблема та сама
        return None
    finally:
        page.close()


# --- Живе читання (у тестах не викликається) ---


def _feeds(response: Any) -> list[dict]:
    """Відповіді `WEB_MyClothesProductsFeed` з одного запиту (окремого чи пакета)."""
    try:
        sent = json.loads(response.request.post_data or "")
    except (ValueError, TypeError):
        return []
    items = sent if isinstance(sent, list) else [sent]
    if not any(isinstance(i, dict) and i.get("operationName") == FEED_OP for i in items):
        return []
    bodies = response.json()
    bodies = bodies if isinstance(bodies, list) else [bodies]
    return [
        b
        for i, b in zip(items, bodies, strict=False)
        if isinstance(i, dict) and i.get("operationName") == FEED_OP
    ]


def load(session: browser.Session, log: Callable[[str], None] = print) -> list[Listing]:
    """Прокрутити вкладки кабінету, як людина, і зібрати всі товари. Лише читання."""
    page = session.page
    found: dict[int, Listing] = {}
    for tab, url in TABS.items():
        pages: list[tuple[list[Listing], bool, int]] = []

        def collect(response: Any, pages: list = pages) -> None:
            try:
                pages.extend(parse_feed(body) for body in _feeds(response))
            except (KeyError, TypeError, ValueError):
                pass  # чужа чи зламана відповідь — нижче звіримо кількість

        page.on("response", collect)
        try:
            page.goto(url, wait_until="load")
            browser.pause(3.0, 4.0)
            seen, stall = 0, 0
            while not pages or pages[-1][1]:
                if stall >= STALL_ROUNDS:
                    break
                if stall:  # унизу сторінки довантаження інколи «засинає» — трохи вгору й знову вниз
                    page.mouse.wheel(0, -2500)
                    browser.pause(0.5, 1.0)
                page.mouse.wheel(0, 20000)
                browser.pause(1.5, 3.0)
                count = sum(len(p[0]) for p in pages)
                stall = stall + 1 if count == seen else 0
                seen = count
        finally:
            page.remove_listener("response", collect)
        if not pages:
            raise ListingsError(f"Шафа не віддала список «{tab}»")
        tab_items = {x.id: x for p in pages for x in p[0]}
        total = pages[-1][2]
        if len(tab_items) < total:
            raise ListingsError(f"Список «{tab}»: прочитано {len(tab_items)} з {total}")
        log(f"Мої речі · {tab}: {len(tab_items)}")
        found |= tab_items
    return list(found.values())
