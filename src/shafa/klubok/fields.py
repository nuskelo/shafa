"""KLB-001: поля оголошення Клубку з перевіреної Картки й тексту поста. Без ШІ, мережі й бази.

`ad(card, root)` → `KlubokAd` або `Skip(code, text)`: чого не вистачає, того Картка на Клубок не
йде (Шафи це не зачіпає). Правила — `docs/klubok.md`, таблиця «Що в Картці → що на Клубку».
"""

import math
import re
from dataclasses import dataclass
from pathlib import Path

from shafa import catalog as shafa_catalog
from shafa import rules
from shafa.folders import SPARE_PHOTO, klubok_spare, photo_folder
from shafa.klubok import catalog, category
from shafa.review import Card

TITLE_MAX = 100
TITLE_MIN = 3
PHOTOS_MAX = 15
SEX_GIRL, SEX_BOY, SEX_ANY = 0, 1, 2
CONDITION_USED, CONDITION_NEW = 0, 1
COLOR_MULTI = 99

# Назва кольору Шафи → ключ кольору Клубку (таблиця рішень Архітектора; решта — не ставимо).
COLORS = {
    "Білий": 8, "Сріблястий": 9, "Бежевий": 10, "Сірий": 6, "Графітовий": 6, "Жовтий": 11,
    "Гірчичний": 11, "Золотистий": 12, "Помаранчевий": 13, "Червоний": 14, "Малиновий": 14,
    "Рожевий": 15, "Фуксія": 15, "Синій": 1, "Блакитний": 2, "Бірюзовий": 2, "Зелений": 3,
    "Салатовий": 3, "М'ятний": 3, "Хакі": 4, "Оливковий": 4, "Фіолетовий": 16, "Бузковий": 16,
    "Коричневий": 5, "Чорний": 7, "Бордовий": 17, "Різнокольоровий": COLOR_MULTI,
}  # fmt: skip
MULTICOLOR = "Різнокольоровий"
SUMMER = "Літо"
MULTI_FROM = 3  # три кольори й більше — «Разноцветное»


@dataclass(frozen=True)
class KlubokAd:
    item_id: int
    name: str
    text: str
    price: int
    leaf: catalog.Leaf
    sex: int | None  # лише дитячі листки (фільтр 1)
    condition: int
    sizes: tuple[int, ...]
    color: int | None
    brand: str | None
    photos: tuple[Path, ...]


@dataclass(frozen=True)
class Skip:
    code: str
    text: str


def seasons(card: Card, path: str) -> set[str]:
    """Сезони Картки (її правка діє); порожній бланк — з тексту правилами коду. Без регістру."""
    season = card.fields["season"].value or []
    if not season:
        season = rules.season(card.post_text or "", path)[0] or []
    return {s.casefold() for s in season}


def winter(card: Card, path: str) -> bool:
    return rules.WINTER.casefold() in seasons(card, path)


def summer_only(card: Card, path: str) -> bool:
    """Лише «Літо» (KLB-002: таке йде на Клубок останнім)."""
    return seasons(card, path) == {SUMMER.casefold()}


def card_path(card: Card) -> str:
    return card.path or shafa_catalog.normalize_section(card.fields["section"].value or "")


def leaf_of(card: Card) -> catalog.Leaf | None:
    """Листок Клубку для Картки або None (без фото, ціни й назви — для дублів і черги)."""
    path = card_path(card)
    if not path:
        return None
    gender, title = card.fields["gender"].value, (card.fields["title"].value or "").strip()
    leaf_id = category.leaf_id(path, winter(card, path), gender, title)
    return catalog.leaf(leaf_id) if leaf_id is not None else None


def _sentence(text: str) -> str:
    """Перше речення поста без ціни, підпису, контактів і хештегів."""
    text = rules.without_service_lines((text or "").strip())
    sentence = rules._without_price(text[: rules.first_sentence_end(text)])
    for pattern in (rules.SIGNATURE, rules.LINK, rules.MENTION, rules.PHONE, rules.HASHTAG):
        sentence = pattern.sub(" ", sentence)
    sentence = re.sub(r"[ \t ]+", " ", sentence).strip()
    sentence = re.sub(r" (?=[,.!?])", "", sentence)
    return sentence + "." if sentence and sentence[-1] not in ".!?" else sentence


def description(post_text: str | None, card_description: str | None) -> str:
    """Перше речення поста + порожній рядок + опис Картки (її правки діють)."""
    parts = [_sentence(post_text or ""), (card_description or "").strip()]
    return "\n\n".join(p for p in parts if p)


def sizes_for(values: list[str]) -> list[int] | None:
    """«38,5» → 38 і 39 (обидва, рішення Власника 10.11); не число — None."""
    found: set[int] = set()
    for value in values:
        try:
            number = float(value.replace(",", "."))
        except ValueError:
            return None
        if not math.isfinite(number) or number <= 0:
            return None
        found.update({math.floor(number), math.ceil(number)})
    return sorted(found)


def color_key(colors: list[str]) -> int | None:
    """Ключ кольору Клубку: перший колір; «Різнокольоровий» чи ≥ 3 кольорів — 99; невідомий — None."""
    if not colors:
        return None
    if len(colors) >= MULTI_FROM or colors[0] == MULTICOLOR:
        return COLOR_MULTI
    return COLORS.get(colors[0])


def sex_for(post_text: str | None, path: str | None = None) -> int:
    """Для кого дитяче взуття: дівчинка 0, хлопчик 1, інакше «Любой пол» 2. D8 (KLB-002): спершу
    розділ Картки (вона його перевірила), інакше слово в тексті."""
    if path and path.startswith(shafa_catalog.GIRLS):
        return SEX_GIRL
    if path and path.startswith(shafa_catalog.BOYS):
        return SEX_BOY
    return {"girls": SEX_GIRL, "boys": SEX_BOY}.get(rules.said_gender(post_text or ""), SEX_ANY)


def spare_photo(post_id: int | None) -> Path | None:
    """KLB-008 (R5): запасне фото поста з Експорту (`<дані>/klubok-old/<N>/1.jpg`) або None."""
    if post_id is None:
        return None
    path = klubok_spare(post_id) / SPARE_PHOTO
    return path if path.is_file() else None


def klubok_price(card: Card) -> int | None:
    """KLB-010 (F7): ціна на Клубку — у «лише Клубок» поле Картки (ціна поста, можна правити),
    у Картки Шафи — ціна з поста (без +15 % Шафи)."""
    value = card.fields["price"].value
    if card.only_klubok and isinstance(value, int) and value > 0:
        return value
    return rules.price_from_text(card.post_text or "") or None


def ad(card: Card, root: Path) -> KlubokAd | Skip:
    f = {name: view.value for name, view in card.fields.items()}
    section = f["section"] or ""
    path = card.path or shafa_catalog.normalize_section(section)
    title = (f["title"] or "").strip()

    leaf_id = category.leaf_id(path, winter(card, path), f["gender"], title) if path else None
    if leaf_id is None:
        return Skip("klubok_no_category", f"Для «{path or 'без розділу'}» немає категорії Клубку")
    leaf = catalog.leaf(leaf_id)
    if leaf is None:
        return Skip(
            "klubok_no_category",
            f"Категорії {leaf_id} немає у знімку Клубку або дерево змінилось — оновіть знімок",
        )

    price = klubok_price(card)
    if not price:
        return Skip("klubok_no_price", "У тексті поста немає ціни")

    if len(title) < TITLE_MIN:
        return Skip("klubok_no_title", "У Картки немає назви")
    if len(title) > TITLE_MAX:
        return Skip(
            "klubok_title_long", f"Назва довга: {len(title)} знаків, на Клубку до {TITLE_MAX}"
        )

    chosen: tuple[int, ...] = ()
    if catalog.needs_size(leaf):
        sizes = sizes_for(f["sizes"] or [])
        if not sizes:
            return Skip("klubok_size", "У Картки немає розміру, а Клубок його вимагає")
        allowed = set(catalog.sizes(leaf))
        if not set(sizes) <= allowed:
            bad = ", ".join(str(s) for s in sizes if s not in allowed)
            return Skip("klubok_size", f"Розміру {bad} немає у списку категорії Клубку")
        chosen = tuple(sizes)

    folder = photo_folder(root, card.folder_name)  # D7: опубліковане — з «Опубліковано»
    photos = tuple(folder / p.file_name for p in card.photos)[:PHOTOS_MAX]
    missing = [p.name for p in photos if not p.is_file()]
    if not photos or missing:
        spare = spare_photo(card.post_id)  # KLB-008 (R5): одне фото поста з Експорту
        if spare is None:
            return Skip(
                "klubok_no_photos", "Немає фото: " + (", ".join(missing) or "папка порожня")
            )
        photos = (spare,)

    return KlubokAd(
        item_id=card.id,
        name=title,
        text=description(card.post_text, f["description"]),
        price=price,
        leaf=leaf,
        sex=sex_for(card.post_text, path) if catalog.has_sex(leaf) else None,
        condition=CONDITION_NEW if f["condition"] == rules.NEW else CONDITION_USED,
        sizes=chosen,
        color=color_key(f["colors"] or []),
        brand=(f["brand"] or "").strip() or None,
        photos=photos,
    )
