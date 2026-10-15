"""Хештег поста, як ставить його Продавчиня (правило з аналізу Експорту, BOT-001 ітерація 2).

Головне (рішення Власника): бот НІКОЛИ не публікує без хештега мовчки — або хештег, або питання;
«без хештега» лише для речей, де його за правилом немає, і тоді прев'ю каже це явно.

- Розмір: «ідуть на Y» важливіший за вказаний; «ідуть на A-B» → B (рішення Власника); інакше
  перший розмір («38 розмір», «розмір 38», «38-39 розмір» → 38). Дробовий — униз. → `#VS_39_розмір`.
- Що за річ — за словами першого речення (будь-яке слово, не лише перше: «Тест 5: сумка…»), потім
  усього тексту: сумки, гаманці, рюкзаки, ремені → `#VS_сумки`; рукавиці, шарфи тощо — без хештега.
- Взуття без розміру або розмір поза 15–50 → спитати розмір. Річ не впізнано → спитати.
- Хештег уже є в тексті — нічого не додаємо.
"""

import re
from dataclasses import dataclass

ASK_SIZE = "size"  # питання: який розмір
ASK_KIND = "kind"  # питання: що це за річ
BAG_TAG = "#VS_сумки"
MIN_SIZE, MAX_SIZE = 15, 50

_SIZE_NUM = r"(\d{2}(?:[.,]5)?)"
GOES = re.compile(
    rf"(?<!\w)[іий]?дуть\s*(?:на\s*)?{_SIZE_NUM}(?:\s*[-–/]\s*{_SIZE_NUM})?", re.IGNORECASE
)
SIZE = re.compile(
    rf"(?<!\d)(?<!\d[.,]){_SIZE_NUM}(?:\s*[-–/]\s*{_SIZE_NUM})?\s*(?:-?(?:го|й|ий))?\s*розм",
    re.IGNORECASE,
)
# «розмір 38»; «розмір 30см*20см» — габарити сумки, не розмір.
SIZE_AFTER = re.compile(
    rf"розмір\w*\s*[:-]?\s*{_SIZE_NUM}(?![\d.,]\d)(?!\s*(?:см|мм|\*|[xх×]\s*\d))", re.IGNORECASE
)
EXISTING = re.compile(r"#VS_\S+", re.IGNORECASE)

_WORD = re.compile(r"[а-яєіїґʼ']{3,}", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?\n]")
_BAGS = re.compile(r"^(сумк|сумоч|гаман|рюкзак|ремін|ремен|клатч|поясн|портмоне|косметич|шопер)")
_SHOES = re.compile(
    r"^(кросів|кросов|черев|напівчерев|кед|чобіт|чобот|чобітк|туфл|босоніж|сандал|мокасин|лофер|"
    r"сліпон|балетк|шльопан|вʼєтнам|в'єтнам|угг|бутс|дутик|ботильйон|ботфорт|капц|тапоч|сабо|"
    r"мюлі|сникер|снікер|мартінс|топсайдер|еспадрил|гумов|сапог|кроксів|крокси)"
)
_OTHER = re.compile(
    r"^(рукав|рукавич|перчат|шарф|шапк|кепк|панам|хуст|палантин|окуляр|парасол|снуд|берет|"
    r"шкарпет|носк|бафф)"
)


@dataclass(frozen=True)
class Decision:
    """Що робити з хештегом: додати `tag`, нічого (з поясненням `note`) або спитати (`ask`)."""

    tag: str | None = None
    note: str = ""
    ask: str | None = None


def _floor(text: str) -> int:
    return int(float(text.replace(",", ".")))


def item_kind(text: str) -> str | None:
    """«bag» / «shoes» / «other» за першим упізнаним словом першого речення, потім усього тексту."""
    first = _SENTENCE_END.split(text, maxsplit=1)[0]
    for part in (first, text):
        for word in _WORD.findall(part.lower()):
            for kind, pattern in (("bag", _BAGS), ("shoes", _SHOES), ("other", _OTHER)):
                if pattern.match(word):
                    return kind
    return None


def size_of(text: str) -> int | None:
    """Розмір для хештега або None, якщо розміру в тексті немає."""
    goes = GOES.search(text)
    if goes:
        return _floor(goes[2] or goes[1])
    found = [m for m in (SIZE.search(text), SIZE_AFTER.search(text)) if m]
    if not found:
        return None
    return _floor(min(found, key=lambda m: m.start())[1])  # «38 розмір» чи «розмір 38»


def decide(text: str) -> Decision:
    existing = EXISTING.search(text)
    if existing:
        return Decision(note=f"хештег твій: {existing[0]}")
    size = size_of(text)
    if size is not None:
        if MIN_SIZE <= size <= MAX_SIZE:
            tag = f"#VS_{size}_розмір"
            return Decision(tag, f"хештег: {tag}")
        return Decision(ask=ASK_SIZE)
    kind = item_kind(text)
    if kind == "bag":
        return Decision(BAG_TAG, f"хештег: {BAG_TAG}")
    if kind == "other":
        return Decision(note="без хештега (рукавиці, шарфи тощо — за правилом без нього)")
    if kind == "shoes":
        return Decision(ask=ASK_SIZE)
    return Decision(ask=ASK_KIND)
