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
from shafa import sizes as sizes_mod
from shafa.bot.hashtag import accessory_group
from shafa.catalog import Kind
from shafa.photos import PreparedPhoto

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4000
# Ціна Claude Sonnet 5.5 у десятих частках мікродолара за токен (= $ за мільйон × 10; docs:
# about-claude/pricing, звірено 13.10.2026): вхід $2, вихід $10, запис кешу на годину $4,
# читання з кешу $0,10. Пакет (Batch API) — усе вдвічі дешевше, знижки складаються.
INPUT_PRICE = 20
OUTPUT_PRICE = 100
CACHE_WRITE_PRICE = 40
CACHE_READ_PRICE = 1
# Кеш інструкції — лише в пакеті і лише на годину: пакет може йти довше 5 хв (рішення D1).
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}
# Для оцінки без запиту (--dry-run, запобіжник грошей): символів тексту на токен і токенів
# відповіді — грубо, точне число — у `usage` відповіді. Замір CORE-004: інструкція й схема
# (23 771 символ) — 13 991 токен, ≈1,7 символу на токен; беремо з запасом.
CHARS_PER_TOKEN = 1.6
OUTPUT_TOKENS_ESTIMATE = 1000
# Ліміт поля «Назва» у формі Шафи (`maxlength`, звірено 13.10.2026, ROBOT-001).
TITLE_MAX = 150

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
        "size_system": {"type": "string", "enum": list(sizes_mod.SYSTEMS)},
        "colors": {"type": "array", "items": {"type": "string", "enum": list(catalog.COLORS)}},
        "brand": _NULLABLE,
        "material_upper": _STRINGS,
        "material_lining": _STRINGS,
        "season": _STRINGS,
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
    "required": [*FIELDS, "size_system", "doubts"],
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
    """Токени запиту. `input_tokens` — лише некешовані; запис кешу — завжди на годину (інших
    позначок ми не ставимо)."""

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    batch: bool = False

    @property
    def cost_micro_usd(self) -> int:
        tenths = (
            self.input_tokens * INPUT_PRICE
            + self.output_tokens * OUTPUT_PRICE
            + self.cache_write_tokens * CACHE_WRITE_PRICE
            + self.cache_read_tokens * CACHE_READ_PRICE
        )
        divisor = 20 if self.batch else 10
        return (tenths + divisor // 2) // divisor


def usage_from(usage: Any, batch: bool = False) -> Usage:
    """`usage` відповіді API → Usage; поля кешу бувають відсутні або None."""
    return Usage(
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_creation_input_tokens", None) or 0,
        getattr(usage, "cache_read_input_tokens", None) or 0,
        batch,
    )


def system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8").replace(CATALOG_MARK, catalog.render())


def post_for_ai(text: str) -> str:
    """Текст поста без підпису, нікнеймів, посилань і телефонів — їх ШІ знати не треба."""
    for pattern in (rules.SIGNATURE, rules.LINK, rules.MENTION, rules.PHONE):
        text = pattern.sub(" ", text)
    return text.strip()


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def user_text(post_text: str, corrections: list[tuple[str, str]] | None = None) -> str:
    """Текст поста, поля коду (ціна, стан, устілка, мембрана) і її правки (CORE-008 B)."""
    price = rules.price_from_text(post_text)
    insole = rules.insole_lengths(post_text)
    code_fields = [
        f"Ціна на Шафі: {rules.shafa_price(price)} грн" if price else "Ціна: немає",
        f"Стан: {rules.condition(post_text)}",
        "Довжина устілки: " + (", ".join(_number(x) for x in insole) if insole else "—"),
        f"Мембрана: {rules.membrane(post_text) or '—'}",
    ]
    text = (
        f"Текст поста:\n{post_for_ai(post_text)}\n\n"
        "Поля, які вже заповнив код (не повертай їх):\n" + "\n".join(code_fields)
    )
    if corrections:
        lines = "\n".join(
            f"Продавчиня виправила: {label} → {value}" for label, value in corrections
        )
        text += (
            "\n\nПравки Продавчині (вона знає товар краще за пост — прийми їх як є, "
            f"не сперечайся й не ставь на них сумнів):\n{lines}"
        )
    return text


def build_request(
    post_text: str,
    photos: list[PreparedPhoto],
    cache: bool = False,
    corrections: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Параметри `client.messages.create`: фото перед текстом, кожне з іменем файлу.

    `cache` — інструкція з позначкою кешу (для пакета): вона однакова для всіх Карток і стоїть
    першою (`system` перед `messages`), тож кешується весь її текст. Мислення й схема — сталі.
    """
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
    content.append({"type": "text", "text": user_text(post_text, corrections)})
    system: str | list[dict[str, Any]] = system_prompt()
    if cache:
        system = [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}]
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "thinking": {"type": "between_tools"},  # мислення вимкнене (рішення D4)
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": content}],
    }


def _tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def estimate(request: dict[str, Any], photos: list[PreparedPhoto], batch: bool = False) -> Usage:
    """Орієнтовні токени запиту без надсилання: фото — точно за формулою, текст — грубо.

    Інструкція з позначкою кешу рахується як запис у кеш — найгірший випадок (кеш не спрацював).
    """
    system = request["system"]
    cached = 0
    if isinstance(system, list):
        cached = _tokens("".join(b["text"] for b in system))
        system = ""
    texts = [system, json.dumps(SCHEMA, ensure_ascii=False)]
    texts += [b["text"] for b in request["messages"][0]["content"] if b["type"] == "text"]
    text_tokens = sum(_tokens(t) for t in texts)
    return Usage(
        text_tokens + sum(p.tokens for p in photos),
        OUTPUT_TOKENS_ESTIMATE,
        cache_write_tokens=cached,
        batch=batch,
    )


def call(client: Any, request: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
    """Надіслати запит. Відповідь — словник JSON і використані токени, або AiError."""
    try:
        message = client.messages.create(**request)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
        raise AiError(error_text(error)) from error
    return parse(message)


def error_text(error: anthropic.AnthropicError) -> str:
    """Помилка API простими словами, без тексту відповіді (там може бути що завгодно)."""
    if isinstance(error, anthropic.AuthenticationError):
        return "Ключ Claude API не прийнято (401)"
    if isinstance(error, anthropic.RateLimitError):
        return "Забагато запитів до Claude API (429), спробуйте пізніше"
    if isinstance(error, anthropic.APIStatusError):
        return f"Помилка Claude API ({error.status_code})"
    if isinstance(error, anthropic.APIConnectionError):
        return "Немає з'єднання з Claude API"
    return "Помилка Claude API"


def parse(message: Any, batch: bool = False) -> tuple[dict[str, Any], Usage]:
    """Відповідь (звичайна або з пакета) → словник JSON і токени, або AiError з токенами."""
    usage = usage_from(message.usage, batch)
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
    found = catalog.find_section(path)
    if found is None:
        path = catalog.normalize_section(path)
        return Field("section", path, True, "розділу немає в довіднику Шафи"), None
    return Field("section", found), catalog.section_kind(found)


NEW_ONE_SIZE = "нове — лише один розмір"
UNPAIRED = "розпаровка: лівий і правий різні — перевір розмір"


def _parse_size(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _letter_parts(sizes: list[str]) -> list[str]:
    parts = [p.strip() for s in sizes for p in re.split(r"[/,;]|(?<=\w)\s*[-–]\s*(?=\w)", s)]
    return [p for p in parts if p] or ["?"]


def _check_letter_sizes(sizes: list[str], allowed: list[str]) -> Field:
    """Літерні розміри аксесуарів (CORE-010): «S/M» — два розміри; лише зі списку розділу."""
    shown = list(dict.fromkeys(p.upper() for p in _letter_parts(sizes)))
    if len(shown) > 2:
        return Field("sizes", shown, True, "більше двох розмірів")
    by_upper = {a.upper(): a for a in allowed}
    if not all(p in by_upper for p in shown):
        return Field("sizes", shown, True, "розміру немає у списку Шафи для розділу")
    return Field("sizes", [by_upper[p] for p in shown])


def check_sizes(value: Any, path: str | None, post_text: str) -> tuple[Field, float | None]:
    """Розміри за списком Шафи для розділу (D6) і правилом ярлика (rules.md §7, D4)."""
    sizes = _strings(value)
    if sizes is None:
        return Field("sizes", [], True, "немає у відповіді ШІ"), None
    allowed = catalog.sizes(path) if path else None
    if path and not allowed:
        if sizes and catalog.section_kind(path) is not Kind.ACCESSORY:
            return Field("sizes", [], True, "у цього розділу розміру немає"), None
        return Field("sizes", []), None  # CORE-011: в аксесуарів Шафа розміру не має — він в описі
    if not sizes:
        return Field("sizes", [], True, "немає розміру"), None
    if allowed and all(catalog.is_letter_size(p) for p in _letter_parts(sizes)):
        return _check_letter_sizes(sizes, allowed), None
    numbers = [_parse_size(s) for s in sizes]
    if None in numbers:
        return Field("sizes", sizes, True, "розмір не число"), None
    shown = [_number(n) for n in numbers]
    if len(sizes) > 2:
        return Field("sizes", shown, True, "більше двох розмірів"), numbers[0]
    if allowed and not all(s in allowed for s in shown):
        return Field("sizes", shown, True, "розміру немає у списку Шафи для розділу"), numbers[0]
    if len(shown) == 2 and rules.condition(post_text) == rules.NEW:
        return Field("sizes", shown, True, NEW_ONE_SIZE), numbers[0]
    return Field("sizes", shown), numbers[0]


def check_sizes_any(
    answer: dict[str, Any], path: str | None, kind: Kind | None, post_text: str
) -> tuple[Field, float | None]:
    """Розміри: US/UK з тексту — у європейські за таблицею коду (CORE-005), далі як завжди."""
    system = answer.get("size_system")
    raw = _strings(answer.get("sizes"))
    if system not in (sizes_mod.US, sizes_mod.UK) or not raw:
        return check_sizes(answer.get("sizes"), path, post_text)
    if kind is Kind.ACCESSORY:
        return check_sizes([], path, post_text)
    converted, doubt = sizes_mod.to_eu(raw, system, kind)
    if not converted:
        return Field("sizes", [], True, doubt), None
    field, first = check_sizes(converted, path, post_text)
    if doubt:
        return Field(field.name, field.value, True, _join(field.reason, doubt)), first
    return field, first


_GROUPS = {
    rules.WOMEN: catalog.WOMEN,
    rules.MEN: catalog.MEN,
    rules.GIRLS: catalog.GIRLS,
    rules.BOYS: catalog.BOYS,
}
_GROUP_NAMES = {
    catalog.WOMEN: "жіноче",
    catalog.MEN: "чоловіче",
    catalog.GIRLS: "для дівчаток",
    catalog.BOYS: "для хлопчиків",
}
NO_KID_GENDER = "у тексті не сказано, для дівчинки чи хлопчика — перевір"
MAYBE_KID = "можливо дитяча модель — перевір"
# ШІ бачить на фото дитячі ознаки (липучки на дитячому фасоні, дитячий принт) — пише в doubts.
# Лише про ознаки моделі — загальне «дитяче чи доросле» за розміром не рахується (SITE-003).
_KID_DOUBT = re.compile(r"дитяч\w*\s+(?:модел|лінійк|ознак|фасон|принт)", re.IGNORECASE)


def _target(
    kind: Kind, size: float, text: str, ai_kid: bool = False
) -> tuple[str | None, str | None]:
    """Група розділу за текстом і розміром (`docs/rules.md` §6): (група, причина жовтого).

    (None, None) — лишається вибір ШІ (41–42 без слова в тексті; 40, коли ШІ бачить чоловіче).
    Група й причина разом — код переносить у групу, але поле жовте (35–36 «можливо дитяча»).
    """
    said = rules.said_gender(text)
    if size < rules.FOREIGN_LABEL:  # не європейський розмір (UK/US з ярлика) — групу він не каже
        return None, None
    if size <= 22:
        return None, (None if kind is Kind.BABIES else "до 22 розміру включно — «Для малюків»")
    if kind is Kind.BABIES:  # 23 і більше — ніколи не малюки
        if said in (rules.GIRLS, rules.BOYS):
            return _GROUPS[said], None
        return None, "«Для малюків» лише до 22 розміру; " + NO_KID_GENDER
    if said is not None:  # «жіночі», «чоловічі», «для дівчинки» важливіші за розмір
        return _GROUPS[said], None
    if size < 35:
        return None, (NO_KID_GENDER if kind is Kind.KIDS else f"розмір {_number(size)} — дитячий")
    kid = rules.kid_words(text)
    if size < 37 and not rules.adult_signs(text) and (kid or rules.kid_model(text) or ai_kid):
        # CORE-009 A3: дитяча лінійка (Kids, Junior, «J») чи дитячі ознаки — жіноче, але перевірити
        if kind is Kind.KIDS and kid:  # текст каже «дитячі», і ШІ згодне — лишається дитяче
            return None, NO_KID_GENDER
        return catalog.WOMEN, MAYBE_KID
    if size < 37 or (size < 40 and not (kid and kind is Kind.KIDS)):  # SITE-003: 35+ — доросле
        return catalog.WOMEN, None
    if size == 40 and not kid:  # CORE-009 A1: 40 — жіноче; чоловіче — лише якщо ШІ бачить явне
        return (None, None) if kind is Kind.MEN else (catalog.WOMEN, None)
    if kind is Kind.KIDS:
        return None, f"розмір {_number(size)} зазвичай дорослий"
    if size < 43:
        return None, None
    return catalog.MEN, None


def check_group(
    section: Field, kind: Kind | None, size: float | None, text: str, ai_kid: bool = False
) -> tuple[Field, bool]:
    """Розділ у потрібній групі. Друге — групу вирішив код (сумнів ШІ про стать не рахується)."""
    if kind in (None, Kind.ACCESSORY) or size is None or section.yellow:
        return section, False
    group, reason = _target(kind, size, text, ai_kid)
    if group is None:
        return (
            (Field("section", section.value, True, reason), False) if reason else (section, False)
        )
    path = catalog.moved(section.value, group)
    if path is None:
        name = section.value.rsplit(catalog.SEP, 1)[1]
        why = f"має бути {_GROUP_NAMES[group]}, але типу «{name}» там немає — вибери розділ"
        return Field("section", section.value, True, why), False
    return Field("section", path, bool(reason), reason), True


BOOTS = "Чоботи"
UGGS = "Уггі"
_SAPOZHKY = re.compile(r"сапожк|сапог", re.IGNORECASE)
_UGGY = re.compile(r"(?<![\w'’ʼ])у[гґ]{1,2}[иі](?![\w'’ʼ])", re.IGNORECASE)
_DUTYKY = re.compile(r"дутик", re.IGNORECASE)


def check_shoe_type(section: Field, text: str) -> Field:
    """Тип розділу за словом поста: «уги» → Уггі, «сапожки» → Чоботи; «Дутики» — лише якщо так
    у тексті. Тип — лише з першого речення (QA-001: «Висота сапожка 13 см» у замірах угів — не тип).
    """
    if section.yellow or not section.value:
        return section
    name = section.value.rsplit(catalog.SEP, 1)[1]
    first = text[: rules.first_sentence_end(text)]
    for word, kind in ((_UGGY, UGGS), (_SAPOZHKY, BOOTS)):
        if word.search(first):
            if name != kind and (path := catalog.moved(section.value, None, kind)):
                return Field("section", path)
            break
    if name == "Дутики" and not _DUTYKY.search(text):
        return Field(
            "section", section.value, True, "«Дутики» — лише коли так у тексті; перевір тип"
        )
    return section


def _join(*reasons: str | None) -> str:
    return "; ".join(r for r in reasons if r)


def in_text(word: str, text: str) -> bool:
    """Слово є в тексті з будь-яким закінченням: «Шкіра» → «шкіряні», «Хутро» → «хутром»."""
    stem = word.strip().casefold()
    if len(stem) > 4:
        stem = stem[:-1]
    return re.search(r"(?<!\w)" + re.escape(stem), text.casefold()) is not None


def check_colors(value: Any) -> Field:
    colors = _strings(value)
    if not colors:
        return Field("colors", [], True, "немає кольору")
    found = [catalog.canonical(c, catalog.COLORS) for c in colors]
    unknown = [c for c, f in zip(colors, found, strict=True) if f is None]
    known = [f for f in found if f]
    if unknown:
        return Field("colors", known, True, "кольору немає в довіднику: " + ", ".join(unknown))
    return Field("colors", known[:2])  # головний — колір верху, перший; дрібні деталі не рахуємо


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return _strings(value)


def check_values(name: str, value: Any, path: str | None, single: bool = False) -> Field:
    """Значення поля — лише з варіантів цього типу розділу у знімку Шафи (D7)."""
    values = _as_list(value)
    if values is None:
        return Field(name, None if single else [], True, "немає у відповіді ШІ")

    def shown(items: list[str]) -> Any:
        return (items[0] if items else None) if single else items

    if path is None:  # розділ невідомий — він сам жовтий
        return Field(name, shown(values))
    spec = catalog.characteristic(path, name)
    if spec is None:  # QA-001: поля в типу немає — Шафа його не покаже, лишаємо порожнім, не жовте
        return Field(name, shown([]))
    found = [catalog.canonical(v, spec["choices"]) for v in values]
    unknown = [v for v, f in zip(values, found, strict=True) if f is None]
    if unknown:
        return Field(name, shown(values), True, "немає у списку Шафи: " + ", ".join(unknown))
    found = list(dict.fromkeys(found))
    if len(found) > spec["max"] or (single and len(found) > 1):
        return Field(name, shown(found), True, f"більше {1 if single else spec['max']}")
    if not found and spec["required"]:
        return Field(name, shown(found), True, "обов'язкове поле Шафи")
    return Field(name, shown(found))


# CORE-010: стать аксесуара — з тексту, інакше «Для жінок» (`docs/rules.md` §6).
_SAID_GENDER = {
    rules.WOMEN: "Для жінок",
    rules.MEN: "Для чоловіків",
    rules.GIRLS: "Для дівчаток",
    rules.BOYS: "Для хлопчиків",
}
DEFAULT_GENDER = "Для жінок"
GENDER_NOT_SAID = "у тексті стать не сказано, ШІ бачить іншу — перевір"


def check_gender(value: Any, path: str | None, kind: Kind | None, text: str) -> tuple[Field, bool]:
    """Стать: для аксесуарів слово в тексті важливіше за ШІ. Друге — вирішив код (сумнів ШІ не рахується)."""
    field = check_values("gender", value, path, single=True)
    spec = catalog.characteristic(path, "gender") if path else None
    if kind is not Kind.ACCESSORY or spec is None:
        return field, False
    said = rules.said_gender(text)
    if said is not None:
        chosen = catalog.canonical(_SAID_GENDER[said], spec["choices"])
        if chosen:
            return Field("gender", chosen), True
        return Field(
            "gender", field.value, True, f"у тексті «{_SAID_GENDER[said]}», у Шафи так немає"
        ), True
    if field.yellow:
        return field, False
    default = catalog.canonical(DEFAULT_GENDER, spec["choices"])
    if default and field.value in (None, default):
        return Field("gender", default), False
    if field.value in _SAID_GENDER.values():  # конкретна інша людина без слова в тексті
        return Field("gender", field.value, True, GENDER_NOT_SAID), True
    return field, False


# CORE-005, `docs/rules.md` §9: матеріал верху — лише ці три і лише якщо названі в тексті.
MATERIALS = ("Шкіра", "Замша", "Нубук")
_FAKE_LEATHER = re.compile(r"штучн\w*\s+шкір|еко-?\s?шкір|шкірзам|кожзам|екокож", re.IGNORECASE)
# CORE-011: рукавички, шарфи, шапки — «Матеріал» зі списку розділу, не лише шкіра; синоніми тексту.
_MATERIAL_ALIASES = {"Вовна": ("Шерсть",)}


def _material_in_text(choice: str, text: str) -> bool:
    if any(in_text(alias, text) for alias in _MATERIAL_ALIASES.get(choice, ())):
        return True
    return all(in_text(word, text) for word in choice.split())


def check_material(value: Any, path: str | None, post_text: str) -> Field:
    """Взуття та решта аксесуарів — лише Шкіра/Замша/Нубук; рукавички, шарфи, шапки — будь-який з
    «Матеріал» розділу. Лише названі в тексті; інше — поле порожнє, без жовтого."""
    values = _as_list(value)
    if values is None:
        return Field("material_upper", [], True, "немає у відповіді ШІ")
    spec = catalog.characteristic(path, "material_upper") if catalog.wide_material(path) else None
    allowed = spec["choices"] if spec else MATERIALS
    kept = [
        m
        for v in values
        if (m := catalog.canonical(v, allowed)) and _material_in_text(m, post_text)
    ]
    field = check_values("material_upper", list(dict.fromkeys(kept)), path)
    if "Шкіра" in field.value and _FAKE_LEATHER.search(post_text) and not field.yellow:
        return Field(field.name, field.value, True, "у тексті є штучна шкіра — перевір")
    return field


# Синоніми підкладки: у різних типів Шафи різні назви одного матеріалу.
_LINING_ALIASES = {
    "Хутро": ("Натуральне хутро",),
    "Натуральне хутро": ("Хутро",),
    "Штучне хутро": ("Екохутро",),
    "Екохутро": ("Штучне хутро",),
    "Шерсть": ("Вовна",),  # QA-001: у неї «всередині шерсть», на Шафі — «Вовна»
}


def _lining_in_text(choice: str, text: str) -> bool:
    if choice == "Натуральне хутро":
        choice = "Хутро"
    if choice == "Вовна":
        return in_text("Вовна", text) or in_text("Шерсть", text)
    if choice == "Екохутро":
        return in_text("Екохутро", text) or (in_text("Штучне", text) and in_text("Хутро", text))
    return all(in_text(word, text) for word in choice.split())


def check_lining(value: Any, path: str | None, post_text: str) -> Field:
    """Підкладка — лише названа в тексті і лише з довідника розділу; інакше порожньо, не жовте."""
    values = _as_list(value) or []
    if path is None:
        return Field("material_lining", values)
    spec = catalog.characteristic(path, "material_lining")
    if spec is None:
        return Field("material_lining", [])
    kept: list[str] = []
    for v in values:
        for candidate in (
            v,
            *_LINING_ALIASES.get(catalog.canonical(v, tuple(_LINING_ALIASES)) or "", ()),
        ):
            choice = catalog.canonical(candidate, spec["choices"])
            if choice:
                break
        if choice and _lining_in_text(choice, post_text):
            kept.append(choice)
    return Field("material_lining", list(dict.fromkeys(kept))[: spec["max"]])


def check_season(value: Any, path: str | None, post_text: str) -> Field:
    """Сезон: ознаки в тексті вирішує код (зима, мембрана → демісезон); жовте — лише суперечність."""
    code, doubt = rules.season(post_text)
    if code is None:
        return check_values("season", value, path)
    if path is not None and catalog.characteristic(path, "season") is None:
        return Field("season", [])
    field = check_values("season", code, path)
    if doubt:
        return Field(field.name, field.value, True, _join(field.reason, doubt))
    return field


def check_teen(value: Any, path: str | None) -> Field:
    if catalog.characteristic(path, "teen") is None or not isinstance(value, bool):
        return Field("teen", None)
    return Field("teen", value)


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
    if len(names) == 1:  # старі пости — одне фото (CORE-005): переставляти нічого
        return Field("photo_order", names)
    order = _strings(value)
    if order is None or sorted(order) != sorted(names):
        return Field("photo_order", names, True, "порядок фото від ШІ не збігається з папкою")
    if order != names:
        return Field("photo_order", order, True, "ШІ переставило фото")
    return Field("photo_order", names)


# Сумнів ШІ про стать / групу — коли групу вирішив код, він не рахується.
_GENDER_DOUBT = re.compile(r"жіноч|чоловіч|стат|унісекс|дитяч|дівч|хлопч|доросл", re.IGNORECASE)
# Поля, де сумнів ШІ не робить жовтим (CORE-007): сезон і підкладку вирішує текст, колір —
# найближчий з довідника.
NO_DOUBT = frozenset({"season", "material_lining", "colors"})


def _with_doubts(fields: list[Field], doubts: Any, decided: set[str]) -> list[Field]:
    """Сумніви ШІ: поле жовтіє, причина додається до причини коду.

    `decided` — поля, які вирішив код за текстом; «section» там — лише щодо статі й групи.
    """
    reasons: dict[str, list[str]] = {}
    for doubt in doubts if isinstance(doubts, list) else []:
        if isinstance(doubt, dict) and isinstance(doubt.get("field"), str):
            reason = _text(doubt.get("reason")) or "без причини"
            name = doubt["field"]
            if name in NO_DOUBT or (name in decided and name != "section"):
                continue
            if name == "section" and name in decided and _GENDER_DOUBT.search(reason):
                continue
            reasons.setdefault(name, []).append(reason)
    result = []
    for field in fields:
        if field.name not in reasons:
            result.append(field)
            continue
        doubt = _join(*("ШІ не впевнене: " + r for r in reasons[field.name]))
        result.append(Field(field.name, field.value, True, _join(field.reason, doubt)))
    return result


def _ai_kid(answer: dict[str, Any]) -> bool:
    """ШІ пише в сумнівах про розділ, що модель може бути дитяча (ознаки на фото, CORE-009 A3)."""
    doubts = answer.get("doubts")
    return any(
        isinstance(d, dict)
        and d.get("field") == "section"
        and _KID_DOUBT.search(_text(d.get("reason")) or "")
        for d in (doubts if isinstance(doubts, list) else [])
    )


def _code_sizes(
    answer: dict[str, Any], sizes: Field, path: str | None, post_text: str
) -> tuple[Field, bool]:
    """Розмір за текстом: «вказаний X, ідуть на Y» кодом (§7); один розмір у тексті, і ШІ дало
    його ж — сумнів ШІ не рахується. Розпаровка й US/UK — як було, крім ярлика UK/US з «ідуть на»
    (v1.6: тоді розмір — «ідуть на»)."""
    if rules.unpaired(
        post_text
    ):  # CORE-009: одна зрозуміла причина замість «нове — …» і сумніву ШІ
        keep = None if sizes.reason in (None, NEW_ONE_SIZE) else sizes.reason
        return Field("sizes", sizes.value, True, _join(keep, UNPAIRED)), True
    foreign = answer.get("size_system") in (sizes_mod.US, sizes_mod.UK)
    if foreign and not rules.foreign_label(post_text):
        return sizes, False
    label = rules.label_sizes(post_text)
    if label is not None:
        return check_sizes(label, path, post_text)[0], True
    found = rules.text_sizes(post_text)
    one = len(found) == 1 and sizes.value == [_number(found[0])]
    return sizes, one and not sizes.yellow


def check_answer(
    answer: dict[str, Any], photo_names: list[str], post_text: str, edited: str = ""
) -> list[Field]:
    """Відповідь ШІ → поля бланка в порядку FIELDS, з жовтими позначками й причинами.

    `edited` — її відредаговані назва й опис (CORE-008 B): стать, тип розділу, підкладку й сезон
    правила шукають і в них; решта (розмір, ціна, матеріал) — лише в пості.
    """
    words = f"{post_text}\n{edited}" if edited else post_text
    section, kind = check_section(answer.get("section"))
    path = catalog.find_section(section.value) if section.value else None  # None — розділу немає
    sizes, first_size = check_sizes_any(answer, path, kind, post_text)
    if not rules.unpaired(post_text) and (label := rules.label_sizes(post_text)):
        first_size = _parse_size(label[0])  # QA-001: групу каже ярлик з тексту, не відповідь ШІ
    section, group_decided = check_group(section, kind, first_size, words, _ai_kid(answer))
    section = check_shoe_type(section, words)
    moved = catalog.find_section(section.value) if section.value else None
    if moved != path:  # код переніс у іншу групу чи тип — розміри за списком нового розділу
        path, kind = moved, catalog.section_kind(moved) if moved else None
        sizes, first_size = check_sizes_any(answer, path, kind, post_text)
    sizes, sizes_decided = _code_sizes(answer, sizes, path, post_text)
    if not section.value:  # розділу немає — Картка в «Проблемах»; друге жовте «немає розміру» — шум
        sizes, sizes_decided = Field("sizes", sizes.value), True
    decided = {"section"} if group_decided else set()
    if group_decided and catalog.characteristic(path, "teen") is None:
        decided.add("teen")  # код переніс у доросле — «Категорія» (дитячий / підліток) не про нього
    if sizes_decided:
        decided.add("sizes")
    gender, gender_decided = check_gender(answer.get("gender"), path, kind, words)
    if gender_decided:
        decided.add("gender")
    if len(photo_names) == 1:
        decided.add("photo_order")
    fields = [
        check_title(answer.get("title")),
        section,
        sizes,
        check_colors(answer.get("colors")),
        Field("brand", _text(answer.get("brand"))),
        check_material(answer.get("material_upper"), path, post_text),
        check_lining(answer.get("material_lining"), path, words),
        check_season(answer.get("season"), path, words),
        check_values("type", answer.get("type"), path, single=True),
        check_values("style", answer.get("style"), path, single=True),
        check_teen(answer.get("teen"), path),
        gender,
        check_keywords(answer.get("keywords")),
        check_photo_order(answer.get("photo_order"), photo_names),
    ]
    return _with_doubts(fields, answer.get("doubts"), decided)


# --- Чи потрібне ШІ після зміни інструкції (CORE-009 B) ---

# Версія інструкції ШІ (`prompts/blank.md`): зростає, коли інструкція змінюється так, що ШІ може
# відповісти інакше. 1 — до QA-001 (і всі бланки без збереженої відповіді), 2 — QA-001 (v1.6),
# 3 — CORE-009 (v1.7: 40, «можливо дитяча модель»), 4 — CORE-010 (v1.8: рукавички, шарфи, шапки).
# Разом з нею піднімається `rules.RULES_VERSION` — тоді програма на старті перерахує Картки й
# позначить, яким потрібне ШІ.
PROMPT_VERSION = 5

# Слова назви лише з тексту (v1.6): (слово в назві, корінь у тексті) — «замшеві» є, коли в тексті «замша».
_TITLE_WORDS = (
    ("оригінал", "оригінал"), ("шкірян", "шкір"), ("замшев", "замш"), ("термо", "термо"),
    ("зимов", "зим"), ("мембран", "мембран"),
)  # fmt: skip


def _answer_size(answer: dict[str, Any], text: str) -> float | None:
    label = rules.label_sizes(text)
    sizes = label or _strings(answer.get("sizes")) or []
    return _parse_size(sizes[0]) if sizes else None


def _qa001_changes(answer: dict[str, Any], text: str) -> bool:
    """v1.6: «Челсі» лише з гумками; слова назви — лише з тексту; без сумніву про мембрану; 41–42 —
    без сумніву про стать за явними жіночими ознаками (40 тепер вирішує код, CORE-009)."""
    if answer.get("type") == "Челсі":
        return True
    title, lower = (_text(answer.get("title")) or "").casefold(), text.casefold()
    if any(word in title and stem not in lower for word, stem in _TITLE_WORDS):
        return True
    size = _answer_size(answer, text)
    gender_open = size is not None and 41 <= size < 43 and rules.said_gender(text) is None
    for doubt in answer.get("doubts") or []:
        reason = _text(doubt.get("reason")) if isinstance(doubt, dict) else None
        if not reason:
            continue
        if "мембран" in reason.casefold():
            return True
        if gender_open and doubt.get("field") == "section" and _GENDER_DOUBT.search(reason):
            return True
    return False


def _core009_changes(answer: dict[str, Any], text: str) -> bool:
    """v1.7: 35–36 — ШІ шукає на фото дитячі ознаки; 40 — чоловіче лише за явними ознаками."""
    size = _answer_size(answer, text)
    if size is None or rules.said_gender(text) is not None:
        return False
    if 35 <= size < 37:
        return not rules.adult_signs(text)
    return size == 40 and (_text(answer.get("section")) or "").startswith(catalog.MEN)


def _core010_changes(answer: dict[str, Any], text: str) -> bool:
    """v1.8: рукавички, шарфи, шапки дозволені — стара відповідь без розділу для такого поста."""
    group = accessory_group(text)
    return group is not None and catalog.has_group(group) and not _text(answer.get("section"))


def _core011_changes(answer: dict[str, Any], text: str) -> bool:
    """v1.9: рукавички, шарфи, шапки — будь-який матеріал зі списку; стара відповідь знала лише шкіру."""
    path = _text(answer.get("section"))
    spec = catalog.characteristic(path, "material_upper") if catalog.wide_material(path) else None
    if spec is None:
        return False
    given = {m.casefold() for m in _as_list(answer.get("material_upper")) or []}
    return any(c.casefold() not in given and _material_in_text(c, text) for c in spec["choices"])


# (версія, чи могла зміна інструкції цієї версії змінити відповідь)
PROMPT_CHANGES = (
    (2, _qa001_changes),
    (3, _core009_changes),
    (4, _core010_changes),
    (5, _core011_changes),
)


def needs_ai(prompt_version: int, answer: dict[str, Any], post_text: str) -> bool:
    """Відповідь ШІ за старшою інструкцією, і нова могла б її змінити — варто перезаповнити.

    Зміни лише коду ШІ не потребують — їх застосовує «Перерахувати правилами».
    """
    return any(
        version > prompt_version and changes(answer, post_text)
        for version, changes in PROMPT_CHANGES
    )


def make_client(api_key: str) -> anthropic.Anthropic:
    """Клієнт з ключем, переданим явно: SDK не шукатиме інших облікових даних."""
    return anthropic.Anthropic(api_key=api_key)
