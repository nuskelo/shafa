"""KLB-008: хвилі черги Клубку для старих постів (до бота) і грубе зіставлення з її оголошеннями.

Правила — порт скриптів Архітектора з аналізу 11.11 (`cls.py`, `match.py`), без ШІ:
- `kind` — тип речі за **першим рядком** поста; перший збіг у порядку `RULES`
  (сумки → зима → туфлі → демі → спорт → літо → домашнє → одяг), інакше «інше»;
- `wave_of` — хвиля 1 (зима, демі, сумки) чи 4 (спорт, літо, туфлі, домашнє); одяг й інше — None
  (не імпортуємо);
- `Matcher.match` — чи пара вже є на Клубку за текстом поста: бренд латиницею (спільне слово з
  назвою оголошення), розмір ±0,5 (якщо відомі обидва), ціна ±25 %. «є» — найкращий збіг з ціною,
  «схоже» — збіг без ціни, «?» — у пості немає латиниці, «немає» — нічого.
Точне правило дубля (`klubok.dup`) працює після ШІ в черзі; це — щоб не платити ШІ за пари,
які вже на Клубку (R4).
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

RULES: tuple[tuple[str, str], ...] = (
    ("сумки", r"сумк|гаманец|гаманц|портмоне|рюкзак|клатч|барсетк|косметичк"),
    ("зима", r"зимов|термо|\bуги\b|\bугі\b|угг|дутик|сноубут|чоботи|чобітк|валянк|mou\b|хутр"),
    ("туфлі", r"туфл|лофер|мокасин|броги|оксфорд|балетк|мюлі|мюлях"),
    ("демі", r"черевик|ботильйон|челсі|челси|берц|мартінс|martens|ботфорт|напівчобот"),
    ("спорт", r"кросівк|кеди|кедах|сліпон|снікер|бутси|сороконож"),
    ("літо", r"босоніж|сандал|шльопан|сабо|в\'єтнам|вʼєтнам|еспадрил|крокс|crocs"),
    ("домашнє", r"тапоч|капці|тапк"),
    ("одяг", (r"куртк|пальт|светр|сукн|штан|джинс|кофт|шапк|шарф|рукавиц|футболк|сорочк|пуховик"
              r"|жилет|комбінезон")),
)  # fmt: skip
OTHER = "інше"
WAVES = {"сумки": 1, "зима": 1, "демі": 1, "спорт": 4, "літо": 4, "туфлі": 4, "домашнє": 4}

ON = "є"
SIMILAR = "схоже"
NO_LATIN = "?"
NONE = "немає"

_SIZE = re.compile(
    r"(?<![\d,.])(\d{1,2}(?:[,.]5)?)(?:\s*[-–/]\s*(\d{1,2}(?:[,.]5)?))?\s*(?:розм|размер|р\.|р\b)",
    re.IGNORECASE,
)
_GO = re.compile(r"[ІіIi]дуть на\s*(\d{1,2}(?:[,.]5)?)(?:\s*[-–/]\s*(\d{1,2}(?:[,.]5)?))?")
_LATIN = re.compile(r"[a-z][a-z0-9&\-]{1,}")
_NOT_BRAND = {"gore", "tex", "gore-tex", "vs", "the"}
_PRICE = re.compile(r"(\d[\d\s]{1,6})\s*грн")
PRICE_SPREAD = 0.25
SIZE_SPREAD = 0.5


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def kind(text: str) -> str:
    """Тип речі за першим рядком поста (`RULES`), інакше «інше»."""
    line = first_line(text.lower())
    for name, pattern in RULES:
        if re.search(pattern, line):
            return name
    return OTHER


def wave_of(kind_: str) -> int | None:
    """Хвиля типу: 1, 4 або None (одяг, інше — не беремо)."""
    return WAVES.get(kind_)


def sizes(text: str) -> set[float]:
    """Розміри з тексту: «Ідуть на 38» і «38 розміру» (діапазон — обидва краї), 15–50."""
    found: set[float] = set()
    for pattern in (_GO, _SIZE):
        for match in pattern.finditer(text):
            for group in match.groups():
                if group:
                    value = float(group.replace(",", "."))
                    if 15 <= value <= 50:
                        found.add(value)
    return found


def latin(text: str) -> set[str]:
    """Слова латиницею (бренд): «Ecco», «Tommy Hilfiger» → {ecco, tommy, hilfiger}."""
    words = _LATIN.findall(text.lower().replace("ё", "е"))
    return {w for w in words if w not in _NOT_BRAND}


def price(text: str) -> int | None:
    """Остання ціна «… грн» у тексті."""
    found = _PRICE.findall(text)
    return int(found[-1].replace(" ", "")) if found else None


@dataclass(frozen=True)
class Listing:
    id: int
    name: str
    price: int
    sizes: frozenset[float]
    words: frozenset[str]


@dataclass(frozen=True)
class Match:
    verdict: str  # ON / SIMILAR / NO_LATIN / NONE
    listing: Listing | None = None


class Matcher:
    """Її активні оголошення (`klubok_listings`, `sold = 0`) з індексом за словами латиницею."""

    def __init__(self, rows: Iterable[tuple[int, str, int | None]]) -> None:
        self._index: dict[str, list[Listing]] = defaultdict(list)
        for listing_id, name, cost in rows:
            listing = Listing(
                listing_id, name, cost or 0, frozenset(sizes(name)), frozenset(latin(name))
            )
            for word in listing.words:
                self._index[word].append(listing)

    def match(self, text: str) -> Match:
        """Чи пара з поста `text` уже на Клубку (див. модуль)."""
        wanted = sizes(first_line(text) + " " + text[:300])
        words = latin(first_line(text))
        cost = price(text)
        best: tuple[int, Listing, bool] | None = None
        seen: dict[int, Listing] = {}
        for word in sorted(words):
            for listing in self._index.get(word, ()):
                seen.setdefault(listing.id, listing)
        for listing in seen.values():
            if (
                wanted
                and listing.sizes
                and not any(abs(a - b) <= SIZE_SPREAD for a in wanted for b in listing.sizes)
            ):
                continue
            close = bool(
                cost
                and listing.price
                and abs(cost - listing.price) / max(cost, listing.price) <= PRICE_SPREAD
            )
            score = len(words & listing.words) + (2 if close else 0)
            if best is None or score > best[0]:
                best = (score, listing, close)
        if best is not None:
            return Match(ON if best[2] else SIMILAR, best[1])
        return Match(NO_LATIN if not words else NONE)
