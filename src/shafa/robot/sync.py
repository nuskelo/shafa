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
from shafa.robot import browser, lang

SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "shafa-catalog.json"
CACHE = config.data_dir() / "sync-cache"  # проміжні відповіді обходу (поза репозиторієм)
SEP = " / "

# Гілки дерева, потрібні Продавчині: взуття й аксесуари (сумки, гаманці, ремені, рукавички, шарфи,
# головні убори).
GROUPS = (
    ("Жіночий одяг", "Взуття"),
    ("Чоловічий одяг", "Взуття"),
    ("Дитячий одяг", "Для дівчаток", "Взуття"),
    ("Дитячий одяг", "Для хлопчиків", "Взуття"),
    ("Дитячий одяг", "Для малюків", "Взуття"),
    ("Аксесуари", "Сумки та рюкзаки"),
    ("Аксесуари", "Гаманці та портмоне"),
    ("Аксесуари", "Ремені та пояси"),
    ("Аксесуари", "Рукавички та рукавиці"),  # CORE-010 (TD-033)
    ("Аксесуари", "Шарфи і хустки"),
    ("Аксесуари", "Головні убори"),
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
    return list(leaf_slugs(root, groups))


def leaf_slugs(
    root: dict, groups: tuple[tuple[str, ...], ...] = GROUPS
) -> dict[tuple[str, ...], str]:
    """Шлях типу → його `slug` у дереві Шафи (ним звіряємо, що відкрився саме цей розділ)."""
    result = {}
    for group in groups:
        node = root
        for name in group:
            node = next((c for c in node["subCatalogs"] if c["name"] == name), None)
            if node is None:
                raise ValueError(f"У дереві Шафи немає гілки {SEP.join(group)}")
        result |= {(*group, c["name"]): c["slug"] for c in node["subCatalogs"]}
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


SIZE_BLOCK_LABEL = "Виберіть розмір"  # підпис блоку розміру аксесуарів (на формі — «размер»)
MAX_BUTTON = 12  # довший текст — не кнопка розміру (підказка, значок «?»)


def size_block_entry(heading: str, buttons: list[str]) -> dict | None:
    """Блок розміру аксесуара за розміткою форми (CORE-012): підпис «Виберіть размер *» і кнопки.

    Відповідь `WEB_ProductFormCatalog` у аксесуарів `sizeGroups` не віддає (порожньо), тому блок
    читається з форми. Підпис і кнопки — українською через `lang`; `*` у підписі — обов'язковість.
    Немає кнопок — None.
    """
    choices = [lang.ukrainian(b.strip()) for b in buttons if 0 < len(b.strip()) <= MAX_BUTTON]
    if not choices:
        return None
    title = lang.ukrainian(heading.replace("*", "").strip())
    return {"title": title, "required": "*" in heading, "choices": list(dict.fromkeys(choices))}


def build_snapshot(
    tree_response: dict,
    colors_response: dict,
    conditions_response: dict,
    catalogs: dict[tuple[str, ...], tuple[dict, str]],
    checked: str,
    size_blocks: dict[tuple[str, ...], dict] | None = None,
) -> dict:
    """Знімок: розділи за повним шляхом і спільний список брендів."""
    _, version = parse_tree(tree_response)
    sections, brands = {}, set()
    for path, (response, slug) in catalogs.items():
        entry, names = section_entry(response, slug)
        if block := (size_blocks or {}).get(path):
            entry["size_block"] = block
        sections[SEP.join(path)] = entry
        brands.update(names)
    return {
        "checked": checked,
        "source": "форма https://shafa.ua/uk/new, відповіді WEB_ProductFormCatalog (лише читання)",
        "tree_version": version,
        "colors": [c["name"] for c in colors_response["data"]["filterColors"]],
        "conditions": [c["name"] for c in conditions_response["data"]["filterCondition"]],
        "sections": sections,
        # Один спільний список: у кожного розділу Шафа дає лише ~500 брендів, разом — повніше.
        # Потрібен лише для написання; у формі бренд шукається наживо.
        "brands": sorted(brands, key=str.casefold),
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


def read_size_block(page: Any) -> dict | None:  # pragma: no cover — живе читання форми
    """Блок «Виберіть размер *» відкритої форми: підпис і кнопки (лише читання, нічого не тиснемо)."""
    from shafa.robot import form

    labels = lang.variants(SIZE_BLOCK_LABEL)
    if not page.evaluate(form.FIND_BLOCK, [labels, "size-block"]):
        return None
    got = page.evaluate(
        """(mark) => {
          const block = document.querySelector(`[data-robot-block='${mark}']`);
          const label = document.querySelector(`[data-robot-label='${mark}']`);
          return {
            heading: label ? label.textContent : '',
            buttons: [...block.querySelectorAll('button')].map(b => (b.innerText || '').trim()),
          };
        }""",
        "size-block",
    )
    return size_block_entry(got["heading"], got["buttons"])


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
        opened = session.open_form()
        if opened.state == browser.NO_LOGIN:
            raise RuntimeError(
                "Немає входу на Шафу в профілі робота: python -m shafa robot --login"
            )
        if not opened.ok:
            raise RuntimeError(f"Шафа не показала форму: {opened.detail()}")
    tree = _from_batch(tree_info.value, TREE_OP)
    conditions = _from_batch(cond_info.value, CONDITIONS_OP)
    root, _ = parse_tree(tree)
    slugs = leaf_slugs(root)
    log(f"Типів для обходу: {len(slugs)}")
    catalogs: dict[tuple[str, ...], tuple[dict, str]] = {}
    size_blocks: dict[tuple[str, ...], dict] = {}
    colors = None
    for n, (path, slug) in enumerate(slugs.items(), start=1):
        saved = cache / (SEP.join(path).replace("/", "|") + ".json")
        if saved.is_file():
            stored = json.loads(saved.read_text(encoding="utf-8"))
            catalogs[path] = (stored["response"], stored["slug"])
            if stored.get("size_block"):
                size_blocks[path] = stored["size_block"]
            colors = colors or stored.get("colors")
            continue
        session.open_form()
        if colors is None:
            with page.expect_response(_batch_op(page, COLORS_OP), timeout=30000) as col_info:
                response = session.choose_category(path, slug)
            colors = _from_batch(col_info.value, COLORS_OP)
        else:
            response = session.choose_category(path, slug)
        catalogs[path] = (response, slug)
        block = None
        if path[0] == "Аксесуари" and not response["data"]["catalog"].get("sizeGroups"):
            browser.pause(1.0, 2.0)  # блок розміру малюється після відповіді
            block = read_size_block(page)
            if block:
                size_blocks[path] = block
        saved.write_text(
            json.dumps(
                {"response": response, "slug": slug, "colors": colors, "size_block": block},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log(f"{n}/{len(slugs)} {SEP.join(path)}" + (" · розмір: кнопки" if block else ""))
        browser.pause(4.0, 8.0)
    if session.publish_attempts():
        raise RuntimeError(f"Заблоковано спроби змін: {session.publish_attempts()}")
    return build_snapshot(
        tree,
        colors,
        conditions,
        catalogs,
        datetime.now().astimezone().date().isoformat(),
        size_blocks,
    )


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
