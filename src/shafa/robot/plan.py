"""Картка → кроки заповнення форми Шафи (чиста функція, без браузера).

Значення поза знімком Шафи не заповнюються — потрапляють у нотатки звіту. Бренд шукається у формі
наживо; не знайшовся — не заповнюється: «Додати бренд» робот не натискає (OQ-004).
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shafa import catalog
from shafa.db import Status
from shafa.review import Card

ORDER = ("material_upper", "material_lining", "season", "type", "style", "gender")


class PlanError(Exception):
    """Картку не можна передати роботу. Текст — для Продавчині."""


@dataclass(frozen=True)
class Step:
    kind: str  # photos, own_photos, text, category, condition, brand, keywords, size_system,
    #           sizes, colors, choice
    label: str  # як поле називається на Шафі — для звіту й помилок
    value: Any = None
    target: str | None = None  # name поля (kind="text") або slug розділу (kind="category")


def _number(text: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(match.group().replace(",", ".")) if match else None


def insole_choices(lengths: list[float], choices: list[str], limit: int) -> list[str]:
    """Заміри устілки → варіанти Шафи («25,5» чи «25,5 см» — як у розділі)."""
    by_number = {_number(c): c for c in choices}
    return [by_number[x] for x in lengths if x in by_number][:limit]


def plan(card: Card, root: Path) -> tuple[list[Step], list[str]]:
    """Кроки для форми і нотатки (що не заповнено й чому)."""
    if card.status != Status.APPROVED:
        raise PlanError("Картка не схвалена")
    if block := card.approve_block():
        raise PlanError(f"Картка не готова: {block}")
    path = card.path
    f = {name: view.value for name, view in card.fields.items()}
    notes: list[str] = []

    photos = [root / card.folder_name / p.file_name for p in card.photos]
    missing = [p.name for p in photos if not p.is_file()]
    if not photos or missing:
        raise PlanError("Немає фото: " + (", ".join(missing) or "папка порожня"))
    if not f["title"]:
        raise PlanError("Немає назви")
    if f["price"] is None:
        raise PlanError("Немає ціни")

    steps = [
        Step("photos", "Фото", [str(p) for p in photos[:10]]),
        Step("own_photos", "Фото зроблені самостійно"),
        Step("text", "Назва", f["title"], "titleUk"),
        Step("category", "Розділ", path.split(catalog.SEP), catalog.section(path)["slug"]),
        Step("condition", "Стан", f["condition"]),
    ]
    if f["brand"]:  # список Шафи у знімку обрізаний (~500) — шукаємо наживо, тут лише написання
        brand = catalog.canonical(f["brand"], catalog.brands(path)) or f["brand"]
        steps.append(Step("brand", "Бренд", brand))
    if f["description"]:
        steps.append(Step("text", "Опис", f["description"], "descriptionUk"))
    if f["keywords"]:
        steps.append(Step("keywords", "Ключові слова", list(f["keywords"])))
    if catalog.sizes(path):
        steps.append(Step("size_system", "Розмір", catalog.size_system(path)))
        steps.append(Step("sizes", "Розмір", [catalog.shafa_size(s) for s in f["sizes"]]))
    steps.append(Step("colors", "Колір", list(f["colors"])))

    for name in ORDER:
        spec = catalog.characteristic(path, name)
        value = f[name]
        values = [value] if isinstance(value, str) else list(value or [])
        if not values:
            continue
        if spec is None:
            notes.append(f"{card.fields[name].spec.label}: у розділі Шафи такого поля немає")
            continue
        good = [c for v in values if (c := catalog.canonical(v, spec["choices"]))]
        skipped = [v for v in values if not catalog.canonical(v, spec["choices"])]
        if skipped:
            notes.append(f"{spec['title']}: немає у списку Шафи — {', '.join(skipped)}")
        if good:
            steps.append(Step("choice", spec["title"], good[: spec["max"]]))

    teen = catalog.characteristic(path, "teen")
    if teen and f["teen"] is not None:
        steps.append(Step("choice", teen["title"], [catalog.TEEN[f["teen"]]]))
    membrane = catalog.characteristic(path, "membrane")
    value = catalog.canonical(f["membrane"] or "", membrane["choices"]) if membrane else None
    if value:
        steps.append(Step("choice", membrane["title"], [value]))
    insole = catalog.characteristic(path, "insole")
    if insole and f["insole"]:
        chosen = insole_choices(f["insole"], insole["choices"], insole["max"])
        if chosen:
            steps.append(Step("choice", insole["title"], chosen))
        else:
            notes.append(f"Довжина устілки {f['insole']}: немає у списку Шафи для розділу")
    steps.append(Step("text", "Ціна", str(f["price"]), "price"))
    return steps, notes
