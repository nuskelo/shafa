"""Поля Картки, які заповнює код за `docs/rules.md`: ціна, стан, опис, устілка, мембрана."""

import re

# «1800грн», «600 грн», «1100грн.», «950гр». Дробову частину («26,5 гр») за ціну не беремо.
PRICE = re.compile(r"(?<![\d.,])(\d+)\s*гр(?:н)?\.?(?!\w)", re.IGNORECASE)


def price_from_text(text: str) -> int | None:
    """Остання ціна в тексті (стара закреслена завжди стоїть перед новою) або None."""
    prices = PRICE.findall(text)
    return int(prices[-1]) if prices else None


def shafa_price(price: int) -> int:
    """Ціна на Шафі: +15%, вгору до десятка, оминаючи 450–500 і 950–1000. Лише цілі числа."""
    p = -(-price * 115 // 1000) * 10
    if 450 <= p <= 500:
        return 501
    if 950 <= p <= 1000:
        return 1001
    return p


NEW = "Новий"
IDEAL = "Ідеальний"

# Ціле слово «нов-»: «новий», «нові», «нових», «новому», «нової»… «Нова пошта» й «нова колекція» —
# не стан; «новорічні», «інноваційна», «комбінований» відсіює межа слова.
NEW_WORD = re.compile(
    r"(?<![\w'’ʼ])нов(?:ий|а|е|і|ого|ому|ої|ою|им|их|ими)(?![\w'’ʼ])(?!\s+(?:пошт|колекці))",
    re.IGNORECASE,
)


def condition(text: str) -> str:
    """Стан товару: «Новий», якщо в тексті є слово «нов-», інакше «Ідеальний»."""
    return NEW if NEW_WORD.search(text) else IDEAL


# Крапка після цих скорочень не кінчає речення: «Dr. Martens», «38 р.», «26 см.», «т.д.».
ABBREVIATIONS = re.compile(r"(?<!\w)(?:dr|р|см|т\.д)$", re.IGNORECASE)
SENTENCE_END = re.compile(r"\n|[.!?](?=\s|$)")


def first_sentence_end(text: str) -> int:
    """Індекс кінця першого речення (разом із розділовим знаком) у тексті без пробілів з краю."""
    for match in SENTENCE_END.finditer(text):
        if match.group() == "." and ABBREVIATIONS.search(text[: match.start()]):
            continue
        return match.end()
    return len(text)


SIGNATURE = re.compile(r"Пишіть сюди\s*👉?\s*@\w+", re.IGNORECASE)
MENTION = re.compile(r"(?<!\w)@\w+")
LINK = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
PHONE = re.compile(r"(?<![\d+])(?:\+?38[\s-]?)?\(?0\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
HASHTAG = re.compile(r"(?<!\w)#\w+")
STATE_WORD = re.compile(r"(?<!\w)стан(?:і|у|ом)?(?!\w)", re.IGNORECASE)
STATE_SENTENCE = {NEW: "Стан нових, без дефектів.", IDEAL: "Ідеальний стан, без дефектів."}


def clean_description(text: str, state: str) -> str:
    """Опис для Шафи: без назви (першого речення), ціни, підпису, контактів і хештегів.

    Наприкінці — речення про стан, якщо в тексті його ще немає.
    """
    text = text.strip()
    text = text[first_sentence_end(text) :]
    for pattern in (SIGNATURE, LINK, MENTION, PHONE, HASHTAG, PRICE):
        text = pattern.sub(" ", text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    lines = [re.sub(r" (?=[,.!?])", "", line) for line in lines]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if STATE_WORD.search(text):
        return text
    if text and text[-1] not in ".!?":
        text += "."
    return f"{text} {STATE_SENTENCE[state]}".strip()
