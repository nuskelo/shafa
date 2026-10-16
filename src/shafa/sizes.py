"""Розмір US / UK → європейський (CORE-005, `docs/rules.md` §7). Таблиця в коді, ШІ не переводить.

ШІ лише каже, в якій системі розмір у тексті, і вибирає розділ (стать, дитяче). Переводить код за
таблицею групи розділу. Немає групи, розміру немає в таблиці або в таблиці діапазон («35–36») —
жовте поле з причиною: людина дивиться на ярлик.

Джерело таблиці: Grivet Outdoors, «Shoe Size Chart — US, UK, EU & Inches Conversion»
(https://www.grivetoutdoors.com/pages/shoe-size-chart, звірено 16.10.2026). Різні джерела й бренди
розходяться на пів розміру — тому діапазони не вгадуємо, а позначаємо жовтим.
Рядок «US 1,5 → UK 14» у дитячій таблиці джерела — помилка (після UK 13 іде UK 1), його не взято.
"""

from shafa.catalog import Kind

EU = "EU"
US = "US"
UK = "UK"
SYSTEMS = (EU, US, UK)

# (US, UK, EU): EU — одне число або (від, до), коли таблиця дає діапазон.
_WOMEN = [
    (4, 2, 35), (4.5, 2.5, 35), (5, 3, (35, 36)), (5.5, 3.5, 36), (6, 4, (36, 37)), (6.5, 4.5, 37),
    (7, 5, (37, 38)), (7.5, 5.5, 38), (8, 6, (38, 39)), (8.5, 6.5, 39), (9, 7, (39, 40)),
    (9.5, 7.5, 40), (10, 8, (40, 41)), (10.5, 8.5, 41), (11, 9, (41, 42)), (11.5, 9.5, 42),
    (12, 10, (42, 43)),
]  # fmt: skip
_MEN = [
    (6, 5.5, 39), (6.5, 6, 39), (7, 6.5, 40), (7.5, 7, (40, 41)), (8, 7.5, 41), (8.5, 8, (41, 42)),
    (9, 8.5, 42), (9.5, 9, (42, 43)), (10, 9.5, 43), (10.5, 10, (43, 44)), (11, 10.5, 44),
    (11.5, 11, (44, 45)), (12, 11.5, 45), (13, 12.5, 46), (14, 13.5, 47), (15, 14.5, 48),
    (16, 15.5, 49),
]  # fmt: skip
# Діти: «little kid» (4–7 років) і «big kid» (7–12 років) — одна шкала.
_KIDS = [
    (10.5, 9.5, 27), (11, 10, 28), (11.5, 10.5, 29), (12, 11, 30), (12.5, 11.5, 30), (13, 12, 31),
    (13.5, 12.5, 31), (1, 13, 32), (1.5, None, 33), (2, 1, 33), (2.5, 1.5, 34), (3, 2, 34),
    (3.5, 2.5, 35), (4, 3, 36), (4.5, 3.5, 36), (5, 4, 37), (5.5, 4.5, 37), (6, 5, 38),
    (6.5, 5.5, 38), (7, 6, 39),
]  # fmt: skip
TABLES = {Kind.WOMEN: _WOMEN, Kind.MEN: _MEN, Kind.KIDS: _KIDS}


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def parse(text: str) -> float | None:
    """«8», «8,5», «8.5», «8½», «8C», «US 8» → 8.0 / 8.5; інакше None."""
    cleaned = text.strip().upper().replace("½", ".5").replace(",", ".")
    for mark in (US, UK, "C", "Y", "K"):
        cleaned = cleaned.replace(mark, "")
    try:
        return float(cleaned.strip())
    except ValueError:
        return None


def to_eu(sizes: list[str], system: str, kind: Kind | None) -> tuple[list[str], str | None]:
    """Розміри з тексту в системі `system` → європейські й причина жовтого (None — усе певно)."""
    if system not in (US, UK) or not sizes:
        return sizes, None
    shown = ", ".join(f"{system} {s}" for s in sizes)
    table = TABLES.get(kind) if kind else None
    if table is None:
        return [], f"у тексті {shown} — переведення в EU лише для жіночого, чоловічого й дитячого"
    column = 0 if system == US else 1
    result: list[str] = []
    for size in sizes:
        value = parse(size)
        row = next((r for r in table if value is not None and r[column] == value), None)
        if row is None:
            return [], f"у тексті {shown} — такого розміру немає в таблиці, глянь ярлик"
        eu = row[2]
        if isinstance(eu, tuple):
            low, high = eu
            return [_number(low)], (
                f"у тексті {shown} — за таблицею EU {_number(low)}–{_number(high)}, глянь ярлик"
            )
        result.append(_number(eu))
    return list(dict.fromkeys(result)), None
