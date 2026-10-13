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
