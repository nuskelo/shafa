"""ШІ-бланк: один запит до Claude на Картку (фото + текст поста) → поля Шафи, перевірені кодом.

Порушення правил не валить програму: поле стає «жовтим» з причиною для Продавчині.
"""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from shafa import catalog, rules
from shafa.catalog import Kind
from shafa.photos import PreparedPhoto

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4000
# Ціна Claude Sonnet 5.5, мікродолари за токен ($2 і $10 за мільйон; docs: about-claude/pricing).
INPUT_MICRO_USD = 2
OUTPUT_MICRO_USD = 10
# Для оцінки без запиту (--dry-run): символів тексту на токен і токенів відповіді — грубо, точне
# число — у `usage` відповіді.
CHARS_PER_TOKEN = 2.5
OUTPUT_TOKENS_ESTIMATE = 1000
# Орієнтир; ліміт довжини назви у формі Шафи не звірено (TD).
TITLE_MAX = 100

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "blank.md"
CATALOG_MARK = "{{ДОВІДНИКИ}}"

FIELDS = (
    "title",
    "section",
    "sizes",
    "colors",
    "brand",
    "material_upper",
    "material_lining",
    "season",
    "type",
    "style",
    "teen",
    "gender",
    "keywords",
    "photo_order",
)

_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_NULLABLE = {"anyOf": [_STRING, {"type": "null"}]}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": _STRING,
        "section": _STRING,
        "sizes": _STRINGS,
        "colors": {"type": "array", "items": {"type": "string", "enum": list(catalog.COLORS)}},
        "brand": _NULLABLE,
        "material_upper": _STRINGS,
        "material_lining": _NULLABLE,
        "season": _NULLABLE,
        "type": _NULLABLE,
        "style": _NULLABLE,
        "teen": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "gender": _NULLABLE,
        "keywords": _STRINGS,
        "photo_order": _STRINGS,
        "doubts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(FIELDS)},
                    "reason": _STRING,
                },
                "required": ["field", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": [*FIELDS, "doubts"],
    "additionalProperties": False,
}


class AiError(Exception):
    """Запит не дав бланка. Текст — для Продавчині, без технічних подробиць і секретів.

    `usage` — токени, якщо відповідь прийшла (за неї заплачено), інакше None.
    """

    def __init__(self, text: str, usage: "Usage | None" = None) -> None:
        super().__init__(text)
        self.usage = usage


@dataclass(frozen=True)
class Field:
    name: str
    value: Any
    yellow: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def cost_micro_usd(self) -> int:
        return self.input_tokens * INPUT_MICRO_USD + self.output_tokens * OUTPUT_MICRO_USD


def system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8").replace(CATALOG_MARK, catalog.render())


def post_for_ai(text: str) -> str:
    """Текст поста без підпису, нікнеймів, посилань і телефонів — їх ШІ знати не треба."""
    for pattern in (rules.SIGNATURE, rules.LINK, rules.MENTION, rules.PHONE):
        text = pattern.sub(" ", text)
    return text.strip()


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def user_text(post_text: str) -> str:
    """Текст поста і поля, які вже заповнив код (ціна, стан, устілка, мембрана)."""
    price = rules.price_from_text(post_text)
    insole = rules.insole_lengths(post_text)
    code_fields = [
        f"Ціна на Шафі: {rules.shafa_price(price)} грн" if price else "Ціна: немає",
        f"Стан: {rules.condition(post_text)}",
        "Довжина устілки: " + (", ".join(_number(x) for x in insole) if insole else "—"),
        f"Мембрана: {rules.membrane(post_text) or '—'}",
    ]
    return (
        f"Текст поста:\n{post_for_ai(post_text)}\n\n"
        "Поля, які вже заповнив код (не повертай їх):\n" + "\n".join(code_fields)
    )


def build_request(post_text: str, photos: list[PreparedPhoto]) -> dict[str, Any]:
    """Параметри `client.messages.create`: фото перед текстом, кожне з іменем файлу."""
    content: list[dict[str, Any]] = []
    for i, photo in enumerate(photos, start=1):
        content.append({"type": "text", "text": f"Фото {i}: {photo.file_name}"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(photo.data).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": user_text(post_text)})
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(),
        "thinking": {"type": "between_tools"},  # мислення вимкнене (рішення D4)
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": content}],
    }


def estimate(request: dict[str, Any], photos: list[PreparedPhoto]) -> Usage:
    """Орієнтовні токени запиту без надсилання: фото — точно за формулою, текст — грубо."""
    texts = [request["system"], json.dumps(SCHEMA, ensure_ascii=False)]
    texts += [b["text"] for b in request["messages"][0]["content"] if b["type"] == "text"]
    text_tokens = round(sum(len(t) for t in texts) / CHARS_PER_TOKEN)
    return Usage(text_tokens + sum(p.tokens for p in photos), OUTPUT_TOKENS_ESTIMATE)


def call(client: Any, request: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
    """Надіслати запит. Відповідь — словник JSON і використані токени, або AiError."""
    try:
        message = client.messages.create(**request)
    except anthropic.AuthenticationError as error:
        raise AiError("Ключ Claude API не прийнято (401)") from error
    except anthropic.RateLimitError as error:
        raise AiError("Забагато запитів до Claude API (429), спробуйте пізніше") from error
    except anthropic.APIStatusError as error:
        raise AiError(f"Помилка Claude API ({error.status_code})") from error
    except anthropic.APIConnectionError as error:
        raise AiError("Немає з'єднання з Claude API") from error

    usage = Usage(message.usage.input_tokens, message.usage.output_tokens)
    if message.stop_reason == "refusal":
        raise AiError("ШІ відмовилось обробити цю Картку", usage)
    if message.stop_reason == "max_tokens":
        raise AiError("Відповідь ШІ обрізана", usage)
    text = "".join(block.text for block in message.content if block.type == "text")
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as error:
        raise AiError("Відповідь ШІ не читається", usage) from error
    if not isinstance(answer, dict):
        raise AiError("Відповідь ШІ не читається", usage)
    return answer, usage


# --- Перевірка відповіді кодом ---


def _strings(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    return None


def _text(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _has_contacts(text: str) -> bool:
    return any(p.search(text) for p in (rules.MENTION, rules.LINK, rules.PHONE))


def check_title(value: Any) -> Field:
    title = _text(value)
    if title is None:
        return Field("title", None, True, "немає назви")
    if _has_contacts(title):
        return Field("title", title, True, "у назві контакти або посилання")
    if len(title) > TITLE_MAX:
        return Field("title", title, True, f"назва довша за {TITLE_MAX} символів")
    return Field("title", title)


def check_section(value: Any) -> tuple[Field, Kind | None]:
    path = _text(value)
    if path is None:
        return Field("section", None, True, "немає розділу"), None
    found, verified = catalog.find_section(path)
    if found is None:
        path = catalog.normalize_section(path)
        return Field("section", path, True, "розділу немає в довіднику Шафи"), None
    kind = catalog.section_kind(found)
    if not verified:
        return Field("section", found, True, "розділ не звірено з довідником"), kind
    return Field("section", found), kind


def _parse_size(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def expected_kinds(size: float) -> set[Kind]:
    """Підказка `docs/rules.md` §6: які розділи пасують до розміру."""
    if size <= 22:
        return {Kind.BABIES}
    if size < 35:
        return {Kind.KIDS}
    if size < 37:
        return {Kind.KIDS, Kind.WOMEN}
    if size < 41:
        return {Kind.WOMEN}
    if size < 43:
        return {Kind.WOMEN, Kind.MEN}
    return {Kind.MEN}


def check_sizes(value: Any, kind: Kind | None) -> tuple[Field, float | None]:
    sizes = _strings(value)
    if sizes is None:
        return Field("sizes", [], True, "немає у відповіді ШІ"), None
    if kind is Kind.ACCESSORY:
        if sizes:
            return Field("sizes", [], True, "у аксесуарів розміру немає"), None
        return Field("sizes", []), None
    if not sizes:
        return Field("sizes", [], True, "немає розміру"), None
    numbers = [_parse_size(s) for s in sizes]
    if None in numbers:
        return Field("sizes", sizes, True, "розмір не число"), None
    shown = [_number(n) for n in numbers]
    if len(sizes) > 2:
        return Field("sizes", shown, True, "більше двох розмірів"), numbers[0]
    if kind is not None and not all(catalog.size_in_range(n, kind) for n in numbers):
        return Field("sizes", shown, True, "розміру немає у списку розділу"), numbers[0]
    return Field("sizes", shown), numbers[0]


def check_size_hint(section: Field, kind: Kind | None, size: float | None) -> Field:
    """Розділ жовтіє, якщо розмір підказує іншу групу (малюки / дитяче / жіноче / чоловіче)."""
    if kind is None or size is None or kind in expected_kinds(size):
        return section
    if kind is Kind.BABIES:
        reason = "Для малюків лише до 22 розміру"
    else:
        reason = f"розмір {_number(size)} зазвичай з іншого розділу"
    return Field("section", section.value, True, _join(section.reason, reason))


def _join(*reasons: str | None) -> str:
    return "; ".join(r for r in reasons if r)


def in_text(word: str, text: str) -> bool:
    """Слово є в тексті з будь-яким закінченням: «Шкіра» → «шкіряні», «Хутро» → «хутром»."""
    stem = word.strip().casefold()
    if len(stem) > 4:
        stem = stem[:-1]
    return re.search(r"(?<!\w)" + re.escape(stem), text.casefold()) is not None


def check_lining(value: Any, post_text: str) -> Field:
    lining = _text(value)
    if lining is not None and not in_text(lining, post_text):
        return Field("material_lining", lining, True, "немає в тексті")
    return Field("material_lining", lining)


def check_colors(value: Any) -> Field:
    colors = _strings(value)
    if not colors:
        return Field("colors", [], True, "немає кольору")
    found = [catalog.canonical(c, catalog.COLORS) for c in colors]
    unknown = [c for c, f in zip(colors, found, strict=True) if f is None]
    known = [f for f in found if f]
    if unknown:
        return Field("colors", known, True, "кольору немає в довіднику: " + ", ".join(unknown))
    if len(known) > 2:
        return Field("colors", known, True, "більше двох кольорів")
    return Field("colors", known)


def check_materials(value: Any, kind: Kind | None) -> Field:
    materials = _strings(value)
    if materials is None:
        return Field("material_upper", [], True, "немає у відповіді ШІ")
    options = catalog.BAG_MATERIALS if kind is Kind.ACCESSORY else catalog.SHOE_MATERIALS
    found = [catalog.canonical(m, options) for m in materials]
    unknown = [m for m, f in zip(materials, found, strict=True) if f is None]
    known = [f for f in found if f]
    if unknown:
        reason = "матеріалу немає в довіднику: " + ", ".join(unknown)
        return Field("material_upper", known, True, reason)
    return Field("material_upper", known)


def check_choice(name: str, value: Any, options: tuple[str, ...]) -> Field:
    text = _text(value)
    if text is None:
        return Field(name, None)
    found = catalog.canonical(text, options)
    if found is None:
        return Field(name, text, True, "значення немає в довіднику")
    return Field(name, found)


def check_keywords(value: Any) -> Field:
    keywords = _strings(value)
    if not keywords:
        return Field("keywords", [], True, "немає ключових слів")
    if any(_has_contacts(k) for k in keywords):
        return Field("keywords", keywords, True, "у ключових словах контакти")
    if not 4 <= len(keywords) <= 6:
        return Field("keywords", keywords, True, "ключових слів не 4–6")
    return Field("keywords", keywords)


def check_photo_order(value: Any, names: list[str]) -> Field:
    order = _strings(value)
    if order is None or sorted(order) != sorted(names):
        return Field("photo_order", names, True, "порядок фото від ШІ не збігається з папкою")
    if order != names:
        return Field("photo_order", order, True, "ШІ переставило фото")
    return Field("photo_order", names)


def _with_doubts(fields: list[Field], doubts: Any) -> list[Field]:
    """Сумніви ШІ: поле жовтіє, причина додається до причини коду."""
    reasons: dict[str, list[str]] = {}
    for doubt in doubts if isinstance(doubts, list) else []:
        if isinstance(doubt, dict) and isinstance(doubt.get("field"), str):
            reason = _text(doubt.get("reason")) or "без причини"
            reasons.setdefault(doubt["field"], []).append(reason)
    result = []
    for field in fields:
        if field.name not in reasons:
            result.append(field)
            continue
        doubt = _join(*("ШІ не впевнене: " + r for r in reasons[field.name]))
        result.append(Field(field.name, field.value, True, _join(field.reason, doubt)))
    return result


def check_answer(answer: dict[str, Any], photo_names: list[str], post_text: str) -> list[Field]:
    """Відповідь ШІ → поля бланка в порядку FIELDS, з жовтими позначками й причинами."""
    section, kind = check_section(answer.get("section"))
    sizes, first_size = check_sizes(answer.get("sizes"), kind)
    section = check_size_hint(section, kind, first_size)
    teen = answer.get("teen")
    fields = [
        check_title(answer.get("title")),
        section,
        sizes,
        check_colors(answer.get("colors")),
        Field("brand", _text(answer.get("brand"))),
        check_materials(answer.get("material_upper"), kind),
        check_lining(answer.get("material_lining"), post_text),
        check_choice("season", answer.get("season"), catalog.SEASONS),
        Field("type", _text(answer.get("type"))),
        Field("style", _text(answer.get("style"))),
        Field("teen", teen if kind is Kind.KIDS and isinstance(teen, bool) else None),
        (
            check_choice("gender", answer.get("gender"), catalog.GENDERS)
            if kind is Kind.ACCESSORY
            else Field("gender", None)
        ),
        check_keywords(answer.get("keywords")),
        check_photo_order(answer.get("photo_order"), photo_names),
    ]
    return _with_doubts(fields, answer.get("doubts"))


def make_client(api_key: str) -> anthropic.Anthropic:
    """Клієнт з ключем, переданим явно: SDK не шукатиме інших облікових даних."""
    return anthropic.Anthropic(api_key=api_key)
