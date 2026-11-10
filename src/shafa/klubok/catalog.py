"""KLB-001: знімок довідника Клубку (публічні дані: категорії й значення фільтрів).

`build(тека)` робить знімок із `categors.json` і `filters.json` (відповіді API сайту, збережені
вручну) лише для листків таблиці `category.EXPECTED`; перевіряє, що кожен id є, є листком і що
назви шляху збігаються з таблицею. Мережі тут немає. Токенів і cookies у знімку немає.
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from shafa.klubok import category

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "klubok-catalog.json"
FILTER_SEX, FILTER_CONDITION, FILTER_SIZE_SHOE = "1", "3", "4"
KEPT_FILTERS = (FILTER_SEX, FILTER_CONDITION, FILTER_SIZE_SHOE)


class CatalogError(Exception):
    """Знімок не збирається: дерево Клубку не схоже на таблицю."""


@dataclass(frozen=True)
class Leaf:
    id: int
    ids: tuple[int, ...]  # ланцюжок cat_id від першого рівня до листка
    path: tuple[str, ...]  # назви тим самим ланцюжком (як на сайті, російською)
    filters: tuple[str, ...]


def _index(categories: list[dict[str, Any]]) -> dict[str, tuple[dict, list[dict]]]:
    found: dict[str, tuple[dict, list[dict]]] = {}

    def walk(node: dict, chain: list[dict]) -> None:
        chain = [*chain, node]
        found[str(node["cat_id"])] = (node, chain)
        for child in node.get("sub_cat") or []:
            walk(child, chain)

    for top in categories:
        walk(top, [])
    return found


def build(source: Path) -> dict[str, Any]:
    """Знімок із теки з `categors.json` і `filters.json`."""
    try:
        categories = json.loads((source / "categors.json").read_text(encoding="utf-8"))
        filters = json.loads((source / "filters.json").read_text(encoding="utf-8"))
        tree = categories["categories"]
        values = filters["filters"]
    except (OSError, ValueError, KeyError) as error:
        raise CatalogError(
            f"Не прочитав categors.json / filters.json у {source}: {error}"
        ) from None
    index = _index(tree)
    leaves: dict[str, Any] = {}
    for leaf_id in category.all_leaf_ids():
        if str(leaf_id) not in index:
            raise CatalogError(f"Листка {leaf_id} немає в categors.json")
        node, chain = index[str(leaf_id)]
        path = tuple(n["name"] for n in chain)
        if path != category.EXPECTED[leaf_id]:
            raise CatalogError(
                f"Лист {leaf_id}: на сайті {' / '.join(path)}, у таблиці "
                f"{' / '.join(category.EXPECTED[leaf_id])}"
            )
        if node.get("sub_cat"):
            raise CatalogError(f"Лист {leaf_id} ({' / '.join(path)}) уже не листок")
        leaves[str(leaf_id)] = {
            "id": leaf_id,
            "ids": [int(n["cat_id"]) for n in chain],
            "path": list(path),
            "filters": [str(f) for f in node["filters"]],
        }
    kept = {}
    for name in KEPT_FILTERS:
        if name not in values:
            raise CatalogError(f"Фільтра {name} немає в filters.json")
        kept[name] = {
            "name": values[name]["name"],
            "values": [{"key": v["key"], "value": v["value"]} for v in values[name]["values"]],
        }
    return {"version": 1, "leaves": leaves, "filters": kept}


def save(snapshot: dict[str, Any], path: Path = SNAPSHOT) -> None:
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


@cache
def snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def leaf(leaf_id: int) -> Leaf | None:
    """Лист за id; None — його немає у знімку чи назви шляху не збігаються з таблицею."""
    entry = snapshot()["leaves"].get(str(leaf_id))
    if entry is None:
        return None
    path = tuple(entry["path"])
    if category.EXPECTED.get(leaf_id) != path:
        return None
    return Leaf(leaf_id, tuple(entry["ids"]), path, tuple(entry["filters"]))


def has_sex(leaf_: Leaf) -> bool:
    """Поле «Для кого» (фільтр 1) є лише в дитячих листках."""
    return FILTER_SEX in leaf_.filters


def needs_size(leaf_: Leaf) -> bool:
    return FILTER_SIZE_SHOE in leaf_.filters


def sizes(leaf_: Leaf) -> list[int]:
    """Розміри листка (цілі) або порожньо — розміру в листка немає."""
    if not needs_size(leaf_):
        return []
    return [int(v["key"]) for v in snapshot()["filters"][FILTER_SIZE_SHOE]["values"]]
