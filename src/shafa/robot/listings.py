"""Її товари на Шафі — для перевірки дублів перед публікацією (D4, п. 3.13.1 оферти).

Читаються з кабінету «Мої речі» раз на запуск: Активні, На модерації, Відхилені (Деактивовані —
продане чи зняте — не рахуються). Збіг: той самий розділ, спільний розмір (або обидва без
розміру), той самий бренд (або обидва без бренду) і схожа назва. Сумнів — краще «Проблема»,
ніж друге оголошення.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from shafa.robot import browser

FEED_OP = "WEB_MyClothesProductsFeed"
TABS = {
    "ACTIVE": "https://shafa.ua/uk/my/clothes/active",
    "MODERATED": "https://shafa.ua/uk/my/clothes/moderated",
    "DECLINED": "https://shafa.ua/uk/my/clothes/declined",
}
SIMILAR = 0.6  # схожість назв (0…1) за однакових розділу, розміру й бренду
SITE = "https://shafa.ua"
STALL_ROUNDS = 12  # прокруток поспіль без нових товарів — список не довантажується


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


def similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, _words(a), _words(b)).ratio() >= SIMILAR


def find_duplicate(
    title: str, slug: str, sizes: list[str], brand: str | None, listings: list[Listing]
) -> Listing | None:
    """Перший її товар, схожий на Картку, або None."""
    wanted_brand = (brand or "").casefold()
    for listing in listings:
        if listing.slug != slug:
            continue
        if (listing.brand or "").casefold() != wanted_brand:
            continue
        if (sizes or listing.sizes) and not set(sizes) & set(listing.sizes):
            continue
        if similar(title, listing.name):
            return listing
    return None


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
