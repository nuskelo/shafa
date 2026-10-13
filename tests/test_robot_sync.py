import json

import pytest

from shafa.robot import sync


def node(name, children=(), id_=0):
    return {"name": name, "slug": name, "id": id_, "subCatalogs": list(children)}


TREE = node(
    "Каталог",
    [
        node("Жіночий одяг", [node("Взуття", [node("Кросівки", id_=1533), node("Кеди")])]),
        node("Аксесуари", [node("Ремені та пояси", [node("Ремені", id_=2486)])]),
    ],
)
TREE_RESPONSE = {
    "data": {"catalogTree": {"catalogTree": json.dumps({"structure": TREE}), "version": 4611}}
}
COLORS = {"data": {"filterColors": [{"name": "Білий"}, {"name": "Чорний"}]}}
CONDITIONS = {"data": {"filterCondition": [{"name": "Новий"}, {"name": "Ідеальний"}]}}


def catalog(id_, sizes, characteristics, brands):
    return {
        "data": {
            "catalog": {
                "id": id_,
                "isColorRequired": True,
                "sizeGroups": [
                    {"name": name, "sizes": [{"name": s} for s in values]}
                    for name, values in sizes.items()
                ],
                "characteristics": [
                    {
                        "title": t,
                        "maxCountForSelect": m,
                        "isRequired": r,
                        "choices": [{"title": c} for c in choices],
                    }
                    for t, m, r, choices in characteristics
                ],
            },
            "filterTopBrands": {
                "topBrands": [{"name": "Топ"}],  # лише в «топі», не в загальному списку
                "brands": [{"name": b} for b in brands],
            },
        }
    }


def test_leaves_follow_groups_in_tree_order():
    root, version = sync.parse_tree(TREE_RESPONSE)
    groups = (("Жіночий одяг", "Взуття"), ("Аксесуари", "Ремені та пояси"))

    assert version == 4611
    assert sync.leaves(root, groups) == [
        ("Жіночий одяг", "Взуття", "Кросівки"),
        ("Жіночий одяг", "Взуття", "Кеди"),
        ("Аксесуари", "Ремені та пояси", "Ремені"),
    ]


def test_missing_branch_is_an_error():
    root, _ = sync.parse_tree(TREE_RESPONSE)
    with pytest.raises(ValueError, match="Чоловічий одяг / Взуття"):
        sync.leaves(root, (("Чоловічий одяг", "Взуття"),))


def test_build_snapshot():
    sneakers = catalog(
        1533,
        {"Європейський": ["34", "34.5", "Другой"], "🇺🇦 Український": ["15"]},
        [("Сезон", 4, False, ["Весна", "Зима"]), ("Стать", 1, True, ["Унісекс"])],
        ["Nike", "Ecco"],
    )
    belts = catalog(2486, {}, [("Матеріал", 3, False, ["Шкіра"])], ["Nike", "Ecco"])
    kedy = catalog(1534, {}, [], ["Vans"])

    snapshot = sync.build_snapshot(
        TREE_RESPONSE,
        COLORS,
        CONDITIONS,
        {
            ("Жіночий одяг", "Взуття", "Кросівки"): (sneakers, "zhenskaya-obuv/krossovki"),
            ("Жіночий одяг", "Взуття", "Кеди"): (kedy, "zhenskaya-obuv/kedy"),
            ("Аксесуари", "Ремені та пояси", "Ремені"): (belts, "remeni-poyasy/remeni"),
        },
        "2026-10-13",
    )

    assert snapshot["checked"] == "2026-10-13"
    assert snapshot["colors"] == ["Білий", "Чорний"]
    assert snapshot["conditions"] == ["Новий", "Ідеальний"]
    entry = snapshot["sections"]["Жіночий одяг / Взуття / Кросівки"]
    assert entry["sizes"] == {"Європейський": ["34", "34.5", "Другой"], "Український": ["15"]}
    assert entry["characteristics"][1] == {
        "title": "Стать", "max": 1, "required": True, "choices": ["Унісекс"]
    }  # fmt: skip
    belts_entry = snapshot["sections"]["Аксесуари / Ремені та пояси / Ремені"]
    assert entry["brands"] == belts_entry["brands"] == "b1"  # однаковий список — один раз
    assert snapshot["brand_lists"] == {"b1": ["Ecco", "Nike", "Топ"], "b2": ["Vans", "Топ"]}
    assert belts_entry["sizes"] == {} and belts_entry["slug"] == "remeni-poyasy/remeni"


def test_save_is_utf8_json(tmp_path):
    path = tmp_path / "знімок.json"
    sync.save({"checked": "2026-10-13", "colors": ["Білий"]}, path)
    assert "Білий" in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["colors"] == ["Білий"]
