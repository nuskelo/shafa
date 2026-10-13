"""Знімок довідників форми Шафи (рішення D2): дерево, розміри, кольори, характеристики, бренди.

Робот відкриває форму і клікає розділи, як людина; знімок збирається з відповідей самої форми
(лише читання, мутації заблоковані). Складання знімка — чисті функції, перевіряються без мережі.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from shafa import config
from shafa.robot import browser

SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "shafa-catalog.json"
CACHE = config.data_dir() / "sync-cache"  # проміжні відповіді обходу (поза репозиторієм)
SEP = " / "

# Гілки дерева, потрібні Продавчині (взуття й аксесуари).
GROUPS = (
    ("Жіночий одяг", "Взуття"),
    ("Чоловічий одяг", "Взуття"),
    ("Дитячий одяг", "Для дівчаток", "Взуття"),
    ("Дитячий одяг", "Для хлопчиків", "Взуття"),
    ("Дитячий одяг", "Для малюків", "Взуття"),
    ("Аксесуари", "Сумки та рюкзаки"),
    ("Аксесуари", "Гаманці та портмоне"),
    ("Аксесуари", "Ремені та пояси"),
)

TREE_OP = "WEB_ProductFormCategorySelect"
CATALOG_OP = "WEB_ProductFormCatalog"
COLORS_OP = "WEB_ProductFormColorsSelect"
CONDITIONS_OP = "WEB_ProductFormConditionSelect"


def parse_tree(tree_response: dict) -> tuple[dict, int]:
    """Відповідь `WEB_ProductFormCategorySelect` → (корінь дерева, версія)."""
    data = tree_response["data"]["catalogTree"]
    return json.loads(data["catalogTree"])["structure"], data["version"]


def leaves(root: dict, groups: tuple[tuple[str, ...], ...] = GROUPS) -> list[tuple[str, ...]]:
    """Шляхи до типів у потрібних гілках, у порядку дерева."""
    result = []
    for group in groups:
        node = root
        for name in group:
            node = next((c for c in node["subCatalogs"] if c["name"] == name), None)
            if node is None:
                raise ValueError(f"У дереві Шафи немає гілки {SEP.join(group)}")
        result += [(*group, c["name"]) for c in node["subCatalogs"]]
    return result


def section_entry(catalog_response: dict, slug: str) -> tuple[dict, list[str]]:
    """Відповідь `WEB_ProductFormCatalog` → (опис розділу, бренди)."""
    data = catalog_response["data"]
    catalog = data["catalog"]
    sizes = {
        g["name"].replace("🇺🇦", "").strip(): [s["name"] for s in g["sizes"]]
        for g in catalog.get("sizeGroups") or []
    }
    entry = {
        "id": catalog["id"],
        "slug": slug,
        "color_required": bool(catalog.get("isColorRequired")),
        "sizes": sizes,
        "characteristics": [
            {
                "title": c["title"],
                "max": c["maxCountForSelect"],
                "required": bool(c["isRequired"]),
                "choices": [x["title"] for x in c["choices"]],
            }
            for c in catalog.get("characteristics") or []
        ],
    }
    brands = data.get("filterTopBrands") or {}
    names = [b["name"] for b in (brands.get("topBrands") or []) + (brands.get("brands") or [])]
    return entry, list(dict.fromkeys(names))  # «топ» (Nike, Skechers…) + решта, без повторів


def build_snapshot(
    tree_response: dict,
    colors_response: dict,
    conditions_response: dict,
    catalogs: dict[tuple[str, ...], tuple[dict, str]],
    checked: str,
) -> dict:
    """Знімок: розділи за повним шляхом; однакові списки брендів зберігаються один раз."""
    _, version = parse_tree(tree_response)
    sections, brand_lists, keys = {}, {}, {}
    for path, (response, slug) in catalogs.items():
        entry, brands = section_entry(response, slug)
        brands = sorted(set(brands), key=str.casefold)  # порядок нам не важливий
        key = keys.setdefault(tuple(brands), f"b{len(keys) + 1}")
        brand_lists[key] = brands
        sections[SEP.join(path)] = {**entry, "brands": key}
    return {
        "checked": checked,
        "source": "форма https://shafa.ua/uk/new, відповіді WEB_ProductFormCatalog (лише читання)",
        "tree_version": version,
        "colors": [c["name"] for c in colors_response["data"]["filterColors"]],
        "conditions": [c["name"] for c in conditions_response["data"]["filterCondition"]],
        "sections": sections,
        "brand_lists": brand_lists,
    }


def save(snapshot: dict, path: Path = SNAPSHOT) -> None:
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# --- Живий обхід (у тестах не викликається) ---


def _batch_op(page: Any, op: str) -> Any:
    """Предикат для відповіді, де op може бути в пакеті (graphiql-batch)."""

    def predicate(response: Any) -> bool:
        try:
            data = json.loads(response.request.post_data or "")
        except (ValueError, TypeError):
            return False
        items = data if isinstance(data, list) else [data]
        return any(isinstance(i, dict) and i.get("operationName") == op for i in items)

    return predicate


def _from_batch(response: Any, op: str) -> dict:
    data = json.loads(response.request.post_data)
    items = data if isinstance(data, list) else [data]
    bodies = response.json()
    bodies = bodies if isinstance(bodies, list) else [bodies]
    return next(b for i, b in zip(items, bodies, strict=False) if i.get("operationName") == op)


def crawl(session: browser.Session, cache: Path, log=print) -> dict:
    """Обійти потрібні типи у формі й зібрати знімок. Кожен тип — нове відкриття форми.

    Відповідь кожного типу пишеться в `cache` (тека даних): після збою обхід продовжується.
    """
    cache.mkdir(parents=True, exist_ok=True)
    page = session.page
    with (
        page.expect_response(_batch_op(page, TREE_OP), timeout=30000) as tree_info,
        page.expect_response(_batch_op(page, CONDITIONS_OP), timeout=30000) as cond_info,
    ):
        if not session.open_form():
            raise RuntimeError(
                "Немає входу на Шафу в профілі робота: python -m shafa robot --login"
            )
    tree = _from_batch(tree_info.value, TREE_OP)
    conditions = _from_batch(cond_info.value, CONDITIONS_OP)
    root, _ = parse_tree(tree)
    paths = leaves(root)
    log(f"Типів для обходу: {len(paths)}")
    catalogs: dict[tuple[str, ...], tuple[dict, str]] = {}
    colors = None
    for n, path in enumerate(paths, start=1):
        saved = cache / (SEP.join(path).replace("/", "|") + ".json")
        if saved.is_file():
            stored = json.loads(saved.read_text(encoding="utf-8"))
            catalogs[path] = (stored["response"], stored["slug"])
            colors = colors or stored.get("colors")
            continue
        session.open_form()
        for name in path[:-1]:
            session.click(_button(page, name), f"розділ {name}")
        last = _button(page, path[-1])
        with page.expect_response(_batch_op(page, CATALOG_OP), timeout=30000) as cat_info:
            if colors is None:
                with page.expect_response(_batch_op(page, COLORS_OP), timeout=30000) as col_info:
                    session.click(last, f"розділ {path[-1]}")
                colors = _from_batch(col_info.value, COLORS_OP)
            else:
                session.click(last, f"розділ {path[-1]}")
        request = cat_info.value.request
        slug = json.loads(request.post_data)
        slug = (slug[0] if isinstance(slug, list) else slug)["variables"]["catalogSlug"]
        catalogs[path] = (_from_batch(cat_info.value, CATALOG_OP), slug)
        saved.write_text(
            json.dumps({"response": catalogs[path][0], "slug": slug, "colors": colors},
                       ensure_ascii=False),
            encoding="utf-8",
        )  # fmt: skip
        log(f"{n}/{len(paths)} {SEP.join(path)}")
        browser.pause(4.0, 8.0)
    if session.publish_attempts():
        raise RuntimeError(f"Заблоковано спроби змін: {session.publish_attempts()}")
    return build_snapshot(
        tree, colors, conditions, catalogs, datetime.now().astimezone().date().isoformat()
    )


def _button(page: Any, name: str) -> Any:
    return page.get_by_role("button", name=name, exact=True).locator("visible=true").first


def main() -> int:  # pragma: no cover — живий обхід
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        session = browser.Session(p)
        try:
            snapshot = crawl(session, CACHE, log=lambda s: print(s, flush=True))
        finally:
            session.close()
    save(snapshot)
    print(f"Збережено {SNAPSHOT}: розділів {len(snapshot['sections'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
