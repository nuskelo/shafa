"""KLB-001: таблиця категорій і знімок довідника Клубку (без браузера й мережі)."""

import json

import pytest

from shafa.klubok import catalog, category

W = "Жіночий одяг / Взуття / "
M = "Чоловічий одяг / Взуття / "
G = "Дитячий одяг / Для дівчаток / Взуття / "
B = "Дитячий одяг / Для хлопчиків / Взуття / "
S = "Дитячий одяг / Для малюків / Взуття / "
BAG = "Аксесуари / Сумки та рюкзаки / "
WAL = "Аксесуари / Гаманці та портмоне / "


@pytest.mark.parametrize(
    ("path", "winter", "want"),
    [
        (W + "Кросівки", False, 1000645),
        (W + "Снікерси", True, 1000646),
        (W + "Черевики", True, 1000637),
        (W + "Черевики", False, 1000633),
        (W + "Чоботи", True, 1000636),
        (W + "Ботфорти", False, 1000632),
        (W + "Валянки", False, 1000636),  # валянки — завжди зима
        (W + "Ботильйони", True, 1000637),
        (W + "Ботильйони", False, 1000634),
        (W + "Гумові чоботи", True, 1000635),
        (W + "Сліпони", False, 1000652),
        (W + "Мюлі", False, 1000641),
        (W + "Аквашузи", False, 1000643),
        (W + "Чешки", False, 1000648),
        (M + "Бутси", False, 1000981),
        (M + "Черевики", True, 1000973),
        (M + "Черевики", False, 1000968),
        (M + "Сліпони і Еспадрільї", False, 1000977),
        (M + "Сандалії", False, 1000985),
        (G + "Кросівки", False, 1000397),
        (G + "Черевики", True, 1000041),
        (B + "Чоботи", False, 1000040),
        (B + "Угги", False, 1000041),
        (G + "Босоніжки та сандалі", False, 1000391),
        (S + "Босоніжки", False, 1000391),  # D3
        (G + "Чешки і балетки", False, 1000047),
        (B + "Балетки та чешки", False, 1000047),
    ],
)
def test_shoes(path, winter, want):
    assert category.leaf_id(path, winter) == want


@pytest.mark.parametrize(
    "path",
    [
        W + "Аксесуари для взуття",
        M + "Аксесуари",
        G + "Аксесуари для взуття",
        BAG + "Авоськи",
        BAG + "Органайзери",
        BAG + "Кейси для косметики",
        BAG + "Косметички",
        BAG + "Аксесуари для сумок",
        WAL + "Картхолдери",
        WAL + "Ключниці",
        WAL + "Затискачі",
        WAL + "Візитниці",
        "Аксесуари / Ремені та пояси / Ремені",
        "Жіночий одяг / Куртки / Пуховики",
    ],
)
def test_not_in_table_is_none(path):
    assert category.leaf_id(path, False) is None


def test_bags_and_wallets():
    leaf = category.leaf_id
    assert leaf(BAG + "Клатчі", False) == 1000290
    assert leaf(BAG + "Бананки", False) == 1000290
    assert leaf(BAG + "Спортивні рюкзаки", False) == 1000293
    assert leaf(BAG + "Валізи", False) == 1000291
    assert leaf(BAG + "Пляжні сумки", False) == 1000292
    assert leaf(BAG + "Шопери", False) == 1000287
    assert leaf(BAG + "Портфелі", False) == 1000286
    assert leaf(BAG + "Сумки", False) == 1000288
    assert leaf(BAG + "Сумки", False, title="Маленька сумка Zara") == 1000290  # «маленьк» у назві
    assert leaf(BAG + "Рюкзаки", False, title="Маленький рюкзак") == 1000293  # лише тип «Сумки»
    assert leaf(WAL + "Гаманці", False) == 1000295
    assert leaf(WAL + "Портмоне", False) == 1000295


def test_men_bags_by_gender():
    leaf = category.leaf_id
    assert leaf(BAG + "Сумки", False, "Для чоловіків") == 1000990
    assert leaf(BAG + "Клатчі", False, "для чоловіків") == 1000990
    assert leaf(WAL + "Гаманці", False, "Для чоловіків") == 1000998
    assert leaf(BAG + "Сумки", False, "Для жінок") == 1000288
    assert leaf(BAG + "Сумки", False, None) == 1000288
    assert leaf(BAG + "Сумки", False, "Унісекс") == 1000288


def test_barsetka_is_always_men_bag():
    assert category.leaf_id(BAG + "Барсетки", False) == 1000990
    assert category.leaf_id(BAG + "Барсетки", False, "Для жінок") == 1000990


def test_every_table_id_has_expected_path():
    ids = {
        *(t.leaf for t in category._WOMEN.values()),
        *(t.winter for t in category._WOMEN.values() if t.winter),
        *(t.leaf for t in category._MEN.values()),
        *(t.winter for t in category._MEN.values() if t.winter),
        *(t.leaf for t in category._KIDS.values()),
        *(t.winter for t in category._KIDS.values() if t.winter),
        *(t.leaf for t in category._BAGS.values()),
        *(t.leaf for t in category._WALLETS.values()),
        category.MEN_BAG,
        category.MEN_WALLET,
    }
    assert ids == set(category.EXPECTED)


def test_real_snapshot_matches_table(monkeypatch):
    """Справжній знімок у репо: усі листки таблиці є, шлях збігається, це листки."""
    monkeypatch.setattr(
        catalog,
        "SNAPSHOT",
        catalog.Path(catalog.__file__).parent.parent / "data" / "klubok-catalog.json",
    )
    catalog.snapshot.cache_clear()
    for leaf_id in category.all_leaf_ids():
        leaf = catalog.leaf(leaf_id)
        assert leaf is not None, leaf_id
        assert leaf.ids[-1] == leaf_id
        assert len(leaf.ids) == len(leaf.path)
    assert catalog.sizes(catalog.leaf(1000637))[0] == 15
    assert catalog.sizes(catalog.leaf(1000637))[-1] == 52
    catalog.snapshot.cache_clear()


def test_sex_only_in_kids_and_size_only_in_shoes():
    kids, adult, bag = catalog.leaf(1000041), catalog.leaf(1000637), catalog.leaf(1000288)
    assert catalog.has_sex(kids) and not catalog.has_sex(adult) and not catalog.has_sex(bag)
    assert catalog.needs_size(kids) and catalog.needs_size(adult) and not catalog.needs_size(bag)
    assert catalog.sizes(bag) == []


def test_leaf_with_changed_path_is_not_trusted(tmp_path, monkeypatch):
    data = json.loads(catalog.SNAPSHOT.read_text(encoding="utf-8"))
    data["leaves"]["1000637"]["path"][-1] = "Что-то другое"
    changed = tmp_path / "k.json"
    changed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "SNAPSHOT", changed)
    catalog.snapshot.cache_clear()
    assert catalog.leaf(1000637) is None  # дерево змінилось — лист не віддаємо
    assert catalog.leaf(1000633) is not None


def _source(tmp_path, mutate=None):
    real = json.loads(
        (catalog.Path(catalog.__file__).parent.parent / "data" / "klubok-catalog.json").read_text(
            encoding="utf-8"
        )
    )
    nodes: dict = {}
    for leaf in real["leaves"].values():
        node = None
        level = nodes
        for cat_id, name in zip(leaf["ids"], leaf["path"], strict=True):
            node = level.setdefault(
                cat_id,
                {"cat_id": str(cat_id), "name": name, "filters": leaf["filters"], "kids": {}},
            )
            level = node["kids"]

    def out(level):
        return [
            {**{k: v for k, v in n.items() if k != "kids"}, "level": "1", "sub_cat": out(n["kids"])}
            for n in level.values()
        ]

    tree = out(nodes)
    if mutate:
        mutate(tree)
    (tmp_path / "categors.json").write_text(
        json.dumps({"status": "ok", "categories": tree}, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "filters.json").write_text(
        json.dumps({"status": "ok", "filters": real["filters"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return tmp_path


def test_build_roundtrip(tmp_path):
    snap = catalog.build(_source(tmp_path))
    assert set(snap["leaves"]) == {str(i) for i in category.all_leaf_ids()}
    assert snap["leaves"]["1000637"]["path"] == list(category.EXPECTED[1000637])
    assert set(snap["filters"]) == {"1", "3", "4"}


def test_build_refuses_renamed_leaf(tmp_path):
    def rename(tree):
        def walk(nodes):
            for n in nodes:
                if n["cat_id"] == "1000637":
                    n["name"] = "Переименовано"
                walk(n["sub_cat"])

        walk(tree)

    with pytest.raises(catalog.CatalogError, match="1000637"):
        catalog.build(_source(tmp_path, rename))


def test_build_refuses_missing_leaf(tmp_path):
    def drop(tree):
        def walk(nodes):
            nodes[:] = [n for n in nodes if n["cat_id"] != "1000637"]
            for n in nodes:
                walk(n["sub_cat"])

        walk(tree)

    with pytest.raises(catalog.CatalogError, match="1000637"):
        catalog.build(_source(tmp_path, drop))
