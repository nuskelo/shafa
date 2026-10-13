"""Її товари на Шафі — для перевірки дублів перед публікацією (D4, п. 3.13.1 оферти).

Читаються з кабінету «Мої речі» раз на запуск: Активні, На модерації, Відхилені (Деактивовані —
продане чи зняте — не рахуються). Збіг: та сама група розділів (усе взуття — жіноче, чоловіче й
дитяче разом; або всі аксесуари: ШІ плутає стать), розмір відрізняється не більше ніж на 0,5 (або
обидва без розміру), той самий бренд (або обидва без бренду) і схожа назва. Сумнів — краще
«Проблема», ніж друге оголошення.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from shafa import catalog
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
            )
        )
    info = products["pageInfo"]
    return listings, bool(info["hasNextPage"]), int(info.get("total") or 0)


def _words(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.casefold()))


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _words(a), _words(b)).ratio()


def similar(a: str, b: str) -> bool:
    return _ratio(a, b) >= SIMILAR


def group_slugs(path: str) -> frozenset[str]:
    """Розділи, серед яких шукати дубль: усе взуття знімка або всі аксесуари."""
    accessory = catalog.section_kind(path) is catalog.Kind.ACCESSORY
    return frozenset(
        entry["slug"]
        for name, entry in catalog.snapshot()["sections"].items()
        if (catalog.section_kind(name) is catalog.Kind.ACCESSORY) == accessory
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
    listings: list[Listing],
) -> Listing | None:  # fmt: skip
    """Її товар, найсхожіший на Картку, або None."""
    wanted_brand = (brand or "").casefold()
    found = [
        listing
        for listing in listings
        if listing.slug in slugs
        and (listing.brand or "").casefold() == wanted_brand
        and sizes_close(sizes, listing.sizes)
        and similar(title, listing.name)
    ]
    return max(found, key=lambda x: _ratio(title, x.name), default=None)


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
