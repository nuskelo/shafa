"""Картка → кроки заповнення форми Шафи (чиста функція, без браузера).

Значення поза знімком Шафи не заповнюються — потрапляють у нотатки звіту. Бренд шукається у формі
наживо; не знайшовся — не заповнюється: «Додати бренд» робот не натискає (OQ-004). Бренд є у
знімку, а наживо не знайшовся (`Step.known`) — збій робота (ROBOT-006).
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shafa import ai, catalog, rules
from shafa.db import Status
from shafa.folders import item_folder
from shafa.review import Card
from shafa.robot import lang

OTHER_SIZE = "Інший"  # кнопка блоку розміру аксесуарів, коли розміру в тексті немає (CORE-012)
ORDER = ("material_upper", "material_lining", "season", "type", "style", "gender")


class PlanError(Exception):
    """Картку не можна передати роботу. Текст — для Продавчині."""


@dataclass(frozen=True)
class Step:
    kind: str  # photos, own_photos, text, category, condition, brand, keywords, size_system,
    #           sizes, size_block, colors, choice, untouched
    label: str  # як поле називається на Шафі — для звіту й помилок
    value: Any = None
    target: str | None = None  # name поля (kind="text") або slug розділу (kind="category")
    known: bool = False  # kind="brand": бренд є у знімку довідників Шафи
    required: bool = True  # kind="choice": поле з * на Шафі; без * — не знайшли, то пропускаємо


def _number(text: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(match.group().replace(",", ".")) if match else None


def insole_choices(lengths: list[float], choices: list[str], limit: int) -> list[str]:
    """Заміри устілки → варіанти Шафи («25,5» чи «25,5 см» — як у розділі)."""
    by_number = {_number(c): c for c in choices}
    return [by_number[x] for x in lengths if x in by_number][:limit]


def block_sizes(card: Card, path: str, block: dict, notes: list[str]) -> list[str]:
    """Кнопки блоку розміру аксесуара (CORE-012): літерний з тексту (поправлений Продавчинею — як є),
    розміру немає чи не вгадується — «Інший». Причини — у нотатки."""
    field = card.fields["sizes"]
    if field.edited:
        picked = [c for s in field.value or [] if (c := catalog.canonical(s, block["choices"]))]
    else:
        found = ai.accessory_sizes(
            card.post_text or "", card.fields["condition"].value == rules.NEW, path
        )
        picked = found.value
        if found.reason:
            notes.append(f"Розмір: {found.reason}")
    if picked:
        return picked
    other = next((c for c in block["choices"] if lang.equivalent(c, OTHER_SIZE)), None)
    if other is None:
        raise PlanError(f"У полі «{block['title']}» немає кнопки «{OTHER_SIZE}»")
    notes.append(f"Розмір: у тексті немає — поставлено «{other}»")
    return [other]


def required_gaps(path: str, steps: list[Step]) -> list[str]:
    """Обов'язкові поля розділу за знімком, яких у кроках немає (підписи для Продавчині)."""
    entry = catalog.section(path)
    filled = {(s.kind, s.label) for s in steps if s.value}
    gaps = [
        c["title"]
        for c in entry["characteristics"]
        if c["required"] and ("choice", c["title"]) not in filled
    ]
    if entry["color_required"] and ("colors", "Колір") not in filled:
        gaps.append("Колір")
    block = entry.get("size_block")
    if (
        block
        and block["required"]
        and not catalog.sizes(path)
        and (("size_block", block["title"]) not in filled)
    ):
        gaps.append(block["title"])
    return gaps


def plan(card: Card, root: Path) -> tuple[list[Step], list[str]]:
    """Кроки для форми і нотатки (що не заповнено й чому)."""
    if card.status != Status.APPROVED:
        raise PlanError("Картка не схвалена")
    if block := card.approve_block():
        raise PlanError(f"Картка не готова: {block}")
    path = card.path
    f = {name: view.value for name, view in card.fields.items()}
    notes: list[str] = []

    photos = [item_folder(root, card.folder_name) / p.file_name for p in card.photos]
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
    if f["brand"]:  # список у знімку неповний — шукаємо наживо, тут лише написання
        canonical = catalog.canonical(f["brand"], catalog.brands())
        steps.append(Step("brand", "Бренд", canonical or f["brand"], known=canonical is not None))
    if f["description"]:
        steps.append(Step("text", "Опис", f["description"], "descriptionUk"))
    if f["keywords"]:
        steps.append(Step("keywords", "Ключові слова", list(f["keywords"])))
    if catalog.sizes(path):
        steps.append(Step("size_system", "Розмір", catalog.size_system(path)))
        steps.append(Step("sizes", "Розмір", [catalog.shafa_size(s) for s in f["sizes"]]))
    elif block := catalog.size_block(path):
        steps.append(Step("size_block", block["title"], block_sizes(card, path, block, notes)))
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
            steps.append(
                Step("choice", spec["title"], good[: spec["max"]], required=spec["required"])
            )

    teen = catalog.characteristic(path, "teen")
    if teen and f["teen"] is not None:
        steps.append(
            Step("choice", teen["title"], [catalog.TEEN[f["teen"]]], required=teen["required"])
        )
    membrane = catalog.characteristic(path, "membrane")
    value = catalog.canonical(f["membrane"] or "", membrane["choices"]) if membrane else None
    if value:
        steps.append(Step("choice", membrane["title"], [value], required=membrane["required"]))
    insole = catalog.characteristic(path, "insole")
    if insole and f["insole"]:
        chosen = insole_choices(f["insole"], insole["choices"], insole["max"])
        if chosen:
            steps.append(Step("choice", insole["title"], chosen, required=insole["required"]))
        else:
            notes.append(f"Довжина устілки {f['insole']}: немає у списку Шафи для розділу")
    filled = {s.label for s in steps if s.kind == "choice"}
    others = [c["title"] for c in catalog.section(path)["characteristics"]]
    if others := [t for t in others if t not in filled]:  # Шафа могла поставити сама — у журнал
        steps.append(Step("untouched", "Поля, яких робот не заповнює", others))
    steps.append(Step("text", "Ціна", str(f["price"]), "price"))
    if gaps := required_gaps(path, steps):  # інакше форма не пустить, а dry-fill пройшов би мовчки
        raise PlanError("Обов'язкові поля Шафи без значення: " + ", ".join(gaps))
    return steps, notes
