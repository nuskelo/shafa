"""CORE-020: літнє взуття чекає весни — на ШІ воно йде лише з дати з налаштувань.

З 1 вересня (`START`, фіксовано) до дати `SHAFA_SUMMER_FROM` (ДД.ММ, типово 01.03) літні Картки
не йдуть на ШІ: восени-взимку вони продаються погано й займають місця в денному ліміті. Порожнє
налаштування — не відкладати. Лише черга ШІ (`batch.queue`): заповнені й схвалені Картки не чіпаємо.
"""

import re
from datetime import date, datetime

from shafa import config
from shafa.bot import hashtag
from shafa.rules import _WINTER_SIGN  # одна ознака зими на весь код (CORE-007)

SETTING = "SHAFA_SUMMER_FROM"
DEFAULT = "01.03"
OFF = "off"  # порожнє в `.env` = «немає» (`config.setting`), тож «не відкладати» пишемо словом
START = (9, 1)  # (місяць, день): з якого дня літнє починає чекати

_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})")
_WORD = re.compile(r"[a-zа-яєіїґʼ'’]{3,}")
_SENTENCE_END = re.compile(r"[.!?\n]")
# Літні типи: слово за початком (відмінки й множина). «Сабо» й «crocs» — лише ціле слово.
_SUMMER = re.compile(
    r"^(босоніж|босонож|сандал|шльопан|шльопк|сланц|в[ʼ'’]?єтнам|вьетнам|балетк|еспадр|мюл|"
    r"аквашуз|сабо$|крокс|crocs?$)"
)


class DateError(ValueError):
    """Дата не ДД.ММ чи її не існує."""


def parse(raw: str) -> tuple[int, int]:
    """«01.03» → (3, 1). 29.02 дозволено (рік заздалегідь невідомий)."""
    found = _DATE.fullmatch(raw.strip())
    if not found:
        raise DateError(raw)
    day, month = int(found[1]), int(found[2])
    try:
        date(2024, month, day)  # високосний, щоб 29.02 була справжньою датою
    except ValueError:
        raise DateError(raw) from None
    return month, day


def show(point: tuple[int, int]) -> str:
    return f"{point[1]:02d}.{point[0]:02d}"


def configured() -> tuple[int, int] | None:
    """Дата виходу літнього на ШІ з налаштувань; None — не відкладати. `DateError` — зіпсовано."""
    raw = config.setting(SETTING)
    if raw is None:
        return parse(DEFAULT)
    if raw.strip().lower() == OFF:
        return None
    return parse(raw)


def waiting(today: datetime | date, until: tuple[int, int] | None) -> bool:
    """Зараз між 1 вересня і датою `until` (рік не важить: 01.03 — це наступної весни)."""
    if until is None or until == START:
        return False
    now = (today.month, today.day)
    if until > START:  # дата в межах тієї ж осені: «з 1.09 до 15.10»
        return START <= now < until
    return now >= START or now < until


def _first_type(text: str) -> bool | None:
    """Чи перший тип у тексті літній; None — типу не знайшли."""
    for word in _WORD.findall(text.lower()):
        if _SUMMER.match(word):
            return True
        if hashtag.item_kind(word):  # кросівки, туфлі, сумка… — вирішує перше слово-тип
            return False
    return None


def is_summer(text: str) -> bool:
    """Літня пара: тип (перше слово-тип першого речення, інакше всього тексту) — літній і в тексті
    немає ознак зими («утеплені босоніжки», «сабо з хутром»)."""
    first = _SENTENCE_END.split(text, maxsplit=1)[0]
    kind = _first_type(first)
    if kind is None:
        kind = _first_type(text)
    return bool(kind) and not _WINTER_SIGN.search(text)
