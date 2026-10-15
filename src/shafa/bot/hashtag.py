"""Хештег поста, як ставить його Продавчиня (правило з аналізу Експорту, BOT-001 ітерація 2).

- Розмір: «ідуть на Y» важливіший за вказаний; «ідуть на A-B» → B (рішення Власника); інакше
  перший розмір («38 розмір», «38-39 розмір» → 38). Дробовий — униз: 39,5 → 39. → `#VS_39_розмір`.
- Сумки, гаманці, рюкзаки, ремені → `#VS_сумки`. Інше без розміру (рукавиці, шарфи) — без хештега.
- Взуття без розміру або розмір поза 15–50 → «незрозуміло»: бот питає її, прев'ю не показує.
- Хештег уже є в тексті — нічого не додаємо.
"""

import re

UNCLEAR = "unclear"
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

# Перше значуще слово поста — що це за річ (прикметники пропускаємо).
_WORD = re.compile(r"[а-яєіїґʼ']{4,}", re.IGNORECASE)
_ADJECTIVES = re.compile(
    r"^(нов|шкір|замш|лак|текстил|оригінал|жіноч|чоловіч|дитяч|стильн|гарн|велик|малень|"
    r"натурал|спортивн|зимов|літн|демісезон|брендов|класичн|модн|красив|зручн|легк|тепл|"
    r"чорн|біл|сір|син|червон|коричнев|беж|зелен|рожев|жовт)"
)
_BAGS = re.compile(r"^(сумк|сумоч|гаман|рюкзак|ремін|ремен|клатч|поясн|портмоне|косметич)")
_SHOES = re.compile(
    r"^(кросів|черев|напівчерев|кед|чобіт|чобот|туфл|босоніж|сандал|мокасин|лофер|сліпон|балетк|"
    r"шльопан|вʼєтнам|в'єтнам|угг|бутс|дутик|ботильйон|ботфорт|капц|тапоч|сабо|мюлі|сникер|"
    r"снікер|мартінс|топсайдер|еспадрил|гумов|кросов)"
)


def _floor(text: str) -> int:
    return int(float(text.replace(",", ".")))


def _first_word(text: str) -> str:
    for word in _WORD.findall(text.lower()):
        if not _ADJECTIVES.match(word):
            return word
    return ""


def size_of(text: str) -> int | None:
    """Розмір для хештега або None, якщо розміру в тексті немає."""
    goes = GOES.search(text)
    if goes:
        return _floor(goes[2] or goes[1])
    found = [m for m in (SIZE.search(text), SIZE_AFTER.search(text)) if m]
    if not found:
        return None
    return _floor(min(found, key=lambda m: m.start())[1])  # «38 розмір» чи «розмір 38»


def hashtag(text: str) -> str | None:
    """`#VS_N_розмір`, `#VS_сумки`, None (хештег не потрібен або вже є) чи UNCLEAR."""
    if EXISTING.search(text):
        return None
    size = size_of(text)
    if size is not None:
        return f"#VS_{size}_розмір" if MIN_SIZE <= size <= MAX_SIZE else UNCLEAR
    word = _first_word(text)
    if _BAGS.match(word):
        return BAG_TAG
    if _SHOES.match(word):
        return UNCLEAR
    return None
