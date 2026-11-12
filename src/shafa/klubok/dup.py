"""KLB-002 (D6): дубль Клубку — чи пара вже виставлена на Клубку (нею або програмою).

Правило — на основі `robot/listings.py` (`similar`, `sizes_close`, `prices_close`), але під дані
Клубку: `get-items` не дає бренду, розмірів і, можливо, категорії. Тому розміри оголошення — з
назви («38,5 розміру»), бренд — `brand` рядка, а якщо його немає — бренд Картки **міститься в
назві**; категорія — лише коли `category_id` відомий (Женское / Мужское / Детское). Сумнів — краще
«Проблема»: невідоме не перешкоджає збігу. Кириличних назв брендів у знімку Шафи немає — лише латиниця.
"""

import json
import re
import sqlite3
from dataclasses import dataclass

from shafa import rules
from shafa.klubok import catalog, fields
from shafa.review import Card, load_card
from shafa.robot import listings

# «38 розміру», «38,5 розмір», «27-28 розміру», «40,5 размера»; розмір — 1–2 цифри й «,5»
_SIZE = r"\d{1,2}(?:[,.]5)?"
_TITLE_SIZE = re.compile(
    rf"(?<![\d,.])({_SIZE})(?:\s*[-–/]\s*({_SIZE}))?\s*(?:розм[іi]р|размер)", re.IGNORECASE
)
RANGE_MAX = 3  # «36-38» → 36, 37, 38; ширший проміжок — лише кінці


def title_sizes(name: str) -> list[str]:
    """Розміри з назви оголошення («38,5 розміру» → ["38,5"]), без повторів, у порядку назви."""
    found: list[str] = []
    for match in _TITLE_SIZE.finditer(name):
        low, high = (match.group(1), match.group(2))
        values = [low.replace(".", ",")]
        if high:
            high = high.replace(".", ",")
            a, b = float(low.replace(",", ".")), float(high.replace(",", "."))
            if a.is_integer() and b.is_integer() and 0 < b - a <= RANGE_MAX:
                values = [str(n) for n in range(int(a), int(b) + 1)]
            else:
                values.append(high)
        found += [v for v in values if v not in found]
    return found


def brand_key(brand: str) -> str:
    """Бренд для пошуку в назві: без уточнення в дужках, лише літери й цифри, без регістру."""
    brand = re.sub(r"\([^)]*\)", " ", brand)
    return re.sub(r"[\W_]+", "", brand.casefold())


def _group(category_id: int | None) -> int | None:
    """Перший рівень Клубку (Женское / Мужское / Детское) для id будь-якого рівня або None."""
    if category_id is None:
        return None
    for entry in catalog.snapshot()["leaves"].values():
        if category_id in entry["ids"]:
            return entry["ids"][0]
    return None


@dataclass(frozen=True)
class Facts:
    """Що порівнюємо: назва, розміри, бренд (None — невідомо), ціна, перший рівень категорії."""

    name: str
    sizes: tuple[str, ...]
    brand: str | None
    price: int | None
    group: int | None


@dataclass(frozen=True)
class Dup:
    klubok_id: int | None  # оголошення програми ще публікується — id немає
    name: str
    url: str | None
    whose: str  # "her" — її оголошення, "ours" — виставлене програмою


def card_facts(card: Card) -> Facts:
    f = {name: view.value for name, view in card.fields.items()}
    leaf = fields.leaf_of(card)
    return Facts(
        name=(f["title"] or "").strip(),
        sizes=tuple(f["sizes"] or ()),
        brand=(f["brand"] or "").strip() or None,
        price=rules.price_from_text(card.post_text or ""),
        group=leaf.ids[0] if leaf else None,
    )


# KLB-010: підтип речі для Картки без бренду — взуття (`listings.SHOE_TYPES`) і сумки/гаманці.
BAG_TYPES = (
    ("рюкзаки", re.compile(r"^(?:рюкзак|ранец|ранц)")),
    ("гаманці", re.compile(r"^(?:гаман|портмоне|кошел)")),
    ("клатчі", re.compile(r"^клатч")),
    ("барсетки", re.compile(r"^барсет")),
    ("косметички", re.compile(r"^косметич")),
    ("ремені", re.compile(r"^(?:ремін|ремен|пояс)")),
    ("сумки", re.compile(r"^(?:сумк|сумоч|шопер|тоут)")),
)
NO_BRAND_PRICE_GAP = 0.25  # без бренду — ціни ±25 % (з брендом — `listings.PRICE_GAP`, 40 %)
_NOT_MODEL = re.compile(r"(?:см|cm|мм|mm|грн|uah|р|г)$")  # устілка, ціна, «38р»


def subtype(title: str) -> str | None:
    """Підтип за першим упізнаним словом назви: рюкзаки / сумки / … / черевики / кросівки…"""
    for word in re.findall(r"\w+", title.casefold().replace("ʼ", "'").replace("’", "'")):
        for name, pattern in (*BAG_TYPES, *listings.SHOE_TYPES):
            if pattern.match(word):
                return name
    return None


def model_numbers(title: str) -> set[str]:
    """KLB-010: номери моделі в назві («237», «530», «gel-1130» → «1130») — цифри токенів, крім
    розміру («38,5 розміру», «38-39»), року (19xx/20xx), устілки й ціни («24 см», «900грн») і
    одно-двоцифрових чисел без літер."""
    text = _TITLE_SIZE.sub(" ", title.casefold())
    found = set()
    for token in re.findall(r"[\w-]*\d[\w-]*", text):
        token = token.strip("-")
        if _NOT_MODEL.search(token) or re.fullmatch(r"(?:19|20)\d\d", token):
            continue
        if token.isdigit() and len(token) < 3:  # «38», «5» — розмір чи кількість, не модель
            continue
        found.add(re.sub(r"\D", "", token))  # «gel-1130» і «1130» — та сама модель
    return found


def _prices_near(ours: int | None, theirs: int | None) -> bool:
    if not ours or not theirs:
        return True
    return abs(ours - theirs) <= NO_BRAND_PRICE_GAP * max(ours, theirs)


def same(ours: Facts, theirs: Facts, *, brand_in_name: bool) -> bool:
    """Чи схоже оголошення `theirs` на Картку `ours`. `brand_in_name` — бренду в оголошенні немає,
    шукаємо бренд Картки в його назві. KLB-010: різні номери моделі — не дубль; Картка без бренду —
    лише той самий підтип і ціна ±25 %."""
    if ours.group is not None and theirs.group is not None and ours.group != theirs.group:
        return False
    a, b = model_numbers(ours.name), model_numbers(theirs.name)
    if a and b and not a & b:
        return False
    if not ours.brand:
        kind = subtype(ours.name)
        if kind is None or kind != subtype(theirs.name):
            return False
        if not _prices_near(ours.price, theirs.price):
            return False
    if ours.brand:
        if brand_in_name:
            if brand_key(ours.brand) not in brand_key(theirs.name):
                return False
        elif theirs.brand and theirs.brand.casefold() != ours.brand.casefold():
            return False
    if ours.sizes and theirs.sizes and not listings.sizes_close(list(ours.sizes), theirs.sizes):
        return False
    if not listings.similar(ours.name, theirs.name, ours.brand):
        return False
    return listings.prices_close(ours.price, theirs.price)


def _post_ads(conn: sqlite3.Connection, post_id: int | None) -> set[int]:
    """id оголошень Клубку Карток того самого поста (сестри-розміри — окремі пари, не дублі)."""
    if post_id is None:
        return set()
    return {
        r[0]
        for r in conn.execute(
            "SELECT k.klubok_id FROM klubok_ads k JOIN items i ON i.id = k.item_id "
            "WHERE i.post_id = ? AND k.klubok_id IS NOT NULL",
            (post_id,),
        )
    }


def find(conn: sqlite3.Connection, card: Card) -> Dup | None:
    """Схоже оголошення на Клубку: спершу її активні (`klubok_listings`, `sold = 0`), потім
    виставлені програмою (`klubok_ads` `publishing`/`published`). Нічого не пише."""
    ours = card_facts(card)
    own_post = _post_ads(conn, card.post_id)
    for row in conn.execute(
        "SELECT id, name, price, sizes, brand, category_id, url FROM klubok_listings "
        "WHERE sold = 0 ORDER BY id"
    ):
        if row["id"] in own_post:
            continue
        sizes = tuple(json.loads(row["sizes"] or "[]")) or tuple(title_sizes(row["name"]))
        theirs = Facts(row["name"], sizes, row["brand"], row["price"], _group(row["category_id"]))
        if same(ours, theirs, brand_in_name=row["brand"] is None):
            return Dup(row["id"], row["name"], row["url"], "her")
    for row in conn.execute(
        "SELECT k.item_id, k.klubok_id, k.url, i.post_id FROM klubok_ads k "
        "JOIN items i ON i.id = k.item_id WHERE k.state IN ('publishing', 'published') "
        "AND k.item_id != ? ORDER BY k.item_id",
        (card.id,),
    ):
        if card.post_id is not None and row["post_id"] == card.post_id:
            continue  # той самий пост: сестра-розмір (інша — `klubok_post_done` раніше)
        other = load_card(conn, row["item_id"])
        if other is None:
            continue
        theirs = card_facts(other)
        if same(ours, theirs, brand_in_name=False):
            return Dup(row["klubok_id"], theirs.name, row["url"], "ours")
    return None
