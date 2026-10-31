"""Довідники форми «Додати товар» Шафи — зі знімка `data/shafa-catalog.json` (ROBOT-001, D2).

Знімок збирає `python -m shafa robot --sync-catalog` (робот у формі Шафи, лише читання); дата
звірки — у полі `checked`. Розділи, розміри й поля — точно як на сайті: розміри — явні списки
(D6), поля залежать від типу розділу (D7).
"""

import json
import re
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from shafa.robot import lang

SNAPSHOT = Path(__file__).resolve().parent / "data" / "shafa-catalog.json"
SEP = " / "

WOMEN = "Жіночий одяг / Взуття"
MEN = "Чоловічий одяг / Взуття"
GIRLS = "Дитячий одяг / Для дівчаток / Взуття"
BOYS = "Дитячий одяг / Для хлопчиків / Взуття"
BABIES = "Дитячий одяг / Для малюків / Взуття"
ACCESSORIES = "Аксесуари"

# 32 кольори Шафи. Тут — для схеми відповіді ШІ; тест звіряє зі знімком.
COLORS = (
    "Білий", "Сріблястий", "Бежевий", "Сірий", "Жовтий", "Золотистий", "Помаранчевий", "Рожевий",
    "Червоний", "Бірюзовий", "Синій", "Хакі", "Зелений", "Фіолетовий", "Коричневий", "Чорний",
    "Різнокольоровий", "Блакитний", "Бордовий", "Салатовий", "Персиковий", "Бузковий",
    "Кораловий", "Гірчичний", "Фуксія", "М'ятний", "Малиновий", "Молочний", "Графітовий",
    "Оливковий", "Нюдовий", "Прозорий",
)  # fmt: skip

# Наші поля → назви характеристик у формі Шафи (у різних типів назва різна).
FIELD_TITLES = {
    "material_upper": ("Матеріал верху", "Матеріал"),
    "material_lining": ("Матеріал підкладки",),
    "season": ("Сезон",),
    "type": ("Тип",),
    "style": ("Стиль", "Стиль взуття"),
    "gender": ("Стать",),
    "teen": ("Категорія",),
    "membrane": ("Мембрана",),
    "insole": ("Довжина устілки",),
}
TEEN = {True: "Для підлітків", False: "Для дітей"}
LETTER_SIZE = re.compile(r"(?:\d?X{0,3}S|M|L|\d?X{0,3}L)", re.IGNORECASE)  # S, M, L, XL, XXS, 2XL…
SIZE_SYSTEMS = ("Європейський", "Український")  # перша наявна — наша (малюки — лише українська)


class Kind(StrEnum):
    """Група розділу для правил розміру й ключових слів."""

    WOMEN = "women"
    MEN = "men"
    KIDS = "kids"
    BABIES = "babies"
    ACCESSORY = "accessory"


def _unify(data: dict[str, Any]) -> dict[str, Any]:
    """Підписи й значення полів і назви систем розмірів — українською, де таблиця знає пару (CORE-011).

    Шафа віддає довідники мішаниною: у знімку 19.10 в ~100 полях «Материал», «Пол», «Полиэстер»
    поруч з «Матеріал», «Стать». Без цього `characteristic()` не знаходить поле й робот мовчки
    його не заповнює. Форма, яка російською, порівнюється через `robot.lang` (ROBOT-005).
    """
    for entry in data["sections"].values():
        entry["sizes"] = lang.size_groups(
            entry["sizes"]
        )  # CORE-013: «Европейский» → «Європейський»
        for item in entry["characteristics"]:
            item["title"] = lang.ukrainian(item["title"])
            item["choices"] = list(dict.fromkeys(lang.ukrainian(v) for v in item["choices"]))
    return data


@cache
def snapshot() -> dict[str, Any]:
    return _unify(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


def _key(value: str) -> str:
    return value.strip().casefold().replace("’", "'").replace("ʼ", "'")


def canonical(value: str, options: tuple[str, ...] | list[str]) -> str | None:
    """Значення з довідника у написанні Шафи (без урахування регістру й виду апострофа)."""
    wanted = _key(value)
    return next((o for o in options if _key(o) == wanted), None)


def normalize_section(path: str) -> str:
    """`Жіночий одяг/Взуття/ Кросівки` → `Жіночий одяг / Взуття / Кросівки`."""
    return SEP.join(part.strip() for part in path.split("/") if part.strip())


def sections() -> tuple[str, ...]:
    return tuple(snapshot()["sections"])


def find_section(path: str) -> str | None:
    """Розділ у написанні Шафи або None, якщо його немає у знімку."""
    return canonical(normalize_section(path), sections())


def has_group(group: str) -> bool:
    """У знімку є хоч один тип цієї групи («Аксесуари / Шарфи і хустки») — гілку вже збирали."""
    prefix = _key(normalize_section(group)) + " /"
    return any(_key(path).startswith(prefix) for path in sections())


# CORE-010: рукавички, шарфи, шапки — свої групи розділів (дубль шукається лише в них, CORE-010).
# CORE-011: у них «Матеріал» — широкий список, не лише шкіра (живий знімок 19.10).
OWN_GROUPS = (
    "Аксесуари / Рукавички та рукавиці",
    "Аксесуари / Шарфи і хустки",
    "Аксесуари / Головні убори",
)


def wide_material(path: str | None) -> bool:
    """Розділ рукавичок, шарфів чи шапок — матеріал ставимо будь-який зі списку розділу."""
    key = _key(normalize_section(path)) if path else ""
    return any(key.startswith(_key(group) + " /") for group in OWN_GROUPS)


def section(path: str | None) -> dict[str, Any] | None:
    found = find_section(path) if path else None
    return snapshot()["sections"][found] if found else None


def moved(path: str, group: str | None = None, type_name: str | None = None) -> str | None:
    """Той самий тип в іншій групі або інший тип у тій самій групі; None — такого розділу немає."""
    parent, name = normalize_section(path).rsplit(SEP, 1)
    return find_section(f"{group or parent}{SEP}{type_name or name}")


def swap_group(path: str, old: str, new: str) -> str | None:
    """Той самий розділ в іншій групі (`Жіночий одяг / Взуття / Кросівки` → `Чоловічий одяг / …`).

    None — шлях не з групи `old` або такого розділу в знімку немає (розділів не вигадуємо)."""
    key = _key(normalize_section(path))
    prefix = normalize_section(old)
    if not key.startswith(_key(prefix) + " /"):
        return None
    return find_section(normalize_section(new) + normalize_section(path)[len(prefix) :])


def section_kind(path: str) -> Kind | None:
    """Група розділу за початком шляху або None, якщо шлях не з нашої частини дерева."""
    key = _key(normalize_section(path))
    for prefix, kind in (
        (WOMEN, Kind.WOMEN),
        (MEN, Kind.MEN),
        (GIRLS, Kind.KIDS),
        (BOYS, Kind.KIDS),
        (BABIES, Kind.BABIES),
        (ACCESSORIES, Kind.ACCESSORY),
    ):
        if key.startswith(_key(prefix) + " /"):
            return kind
    return None


def _usable(name: str) -> bool:
    """Розмір, який ми ставимо: число («38,5») чи літерний («S», «XL» — рукавички, шарфи)."""
    return name.replace(".", "").isdigit() or bool(LETTER_SIZE.fullmatch(name.strip()))


def size_system(path: str) -> str | None:
    """Система розмірів розділу («Європейський»; у малюків — «Український») або None.

    CORE-010: у аксесуарів без звичних систем — перша група, де є число чи літерний розмір.
    """
    entry = section(path)
    if entry is None:
        return None
    known = next((s for s in SIZE_SYSTEMS if s in entry["sizes"]), None)
    if known or section_kind(path) is not Kind.ACCESSORY:
        return known
    return next((g for g, names in entry["sizes"].items() if any(_usable(n) for n in names)), None)


def shafa_size(size: str) -> str:
    """Наше «38,5» → як на Шафі «38.5»; літерний («XL») — без змін."""
    return size.replace(",", ".")


def sizes(path: str) -> list[str]:
    """Розміри розділу в нашому написанні («38,5», «XL»); без розміру — порожньо."""
    system = size_system(path)
    if system is None:
        return []
    names = section(path)["sizes"][system]
    return [n.replace(".", ",") if n[:1].isdigit() else n for n in names if _usable(n)]


def size_block(path: str | None) -> dict[str, Any] | None:
    """Окремий блок розміру аксесуара («Виберіть размер *»: кнопки без систем EU/UA, CORE-012).

    `{"title", "required", "choices"}` або None — у знімку розділу блоку немає.
    """
    entry = section(path)
    return entry.get("size_block") if entry else None


def is_letter_size(size: str) -> bool:
    return bool(LETTER_SIZE.fullmatch(size.strip()))


def characteristic(path: str | None, field: str) -> dict[str, Any] | None:
    """Характеристика форми для нашого поля в цьому розділі або None — поля в розділі немає."""
    entry = section(path)
    if entry is None:
        return None
    titles = FIELD_TITLES[field]
    return next((c for c in entry["characteristics"] if c["title"] in titles), None)


def brands() -> list[str]:
    """Бренди Шафи (об'єднання списків усіх розділів) — для написання назви бренду."""
    return snapshot()["brands"]


# --- Текст для інструкції ШІ ---

PROMPT_FIELDS = ("material_upper", "material_lining", "season", "type", "style", "gender", "teen")


def _parent(path: str) -> str:
    return path.rsplit(SEP, 1)[0]


def _signature(path: str) -> tuple:
    parts = []
    for field in PROMPT_FIELDS:
        c = characteristic(path, field)
        if c:
            parts.append((field, c["title"], c["max"], c["required"], tuple(c["choices"])))
    return tuple(parts)


LONG_LIST = 8  # довші списки варіантів пишемо один раз під позначкою — коротша інструкція


def _choices(choices: tuple[str, ...], long_lists: dict[tuple[str, ...], str]) -> str:
    if len(choices) <= LONG_LIST:
        return ", ".join(choices)
    return "список " + long_lists.setdefault(choices, f"L{len(long_lists) + 1}")


def render() -> str:
    """Довідники текстом для інструкції ШІ: розділи, розміри, поля кожного типу (стисло)."""
    lines = ["Розділи (повний шлях = група / тип):"]
    groups: dict[str, list[str]] = {}
    for path in sections():
        groups.setdefault(_parent(path), []).append(path.rsplit(SEP, 1)[1])
    lines += [f"- {group} / … : " + ", ".join(types) for group, types in groups.items()]

    lines += ["", "Розміри (лише з цих списків):"]
    for group in groups:
        first = f"{group}{SEP}{groups[group][0]}"
        if sizes(first):
            lines.append(f"- {group}: система «{size_system(first)}»: " + ", ".join(sizes(first)))
        else:
            lines.append(f"- {group}: розміру немає (порожній список)")

    lines += ["", "Поля типу (лише ці варіанти; «до N» — скільки можна вибрати; * — обов'язкове):"]
    long_lists: dict[tuple[str, ...], str] = {}
    by_signature: dict[tuple, list[str]] = {}
    for path in sections():
        by_signature.setdefault(_signature(path), []).append(path)
    for signature, paths in by_signature.items():
        names = "; ".join(  # група один раз, далі короткі назви типів
            f"{group} / " + ", ".join(p.rsplit(SEP, 1)[1] for p in paths if _parent(p) == group)
            for group in dict.fromkeys(_parent(p) for p in paths)
        )
        if not signature:
            lines.append(f"- {names}: полів немає")
            continue
        fields = [
            f"{field} («{title}»{' *' if req else ''}, до {mx}): " + _choices(choices, long_lists)
            for field, title, mx, req, choices in signature
        ]
        lines.append(f"- {names}:\n    " + "\n    ".join(fields))
    lines += ["", "Довгі списки варіантів (на них посилаються поля вище):"]
    lines += [f"{mark}: " + ", ".join(choices) for choices, mark in long_lists.items()]
    lines += ["", "Кольори: " + ", ".join(COLORS) + "."]
    return "\n".join(lines)
