"""Поля Картки, які заповнює код за `docs/rules.md`: ціна, стан, опис, устілка, мембрана."""

import re
from dataclasses import dataclass
from itertools import pairwise

# «1800грн», «600 грн», «1100грн.», «950гр», «9см.1300грн». Дробову частину («26,5 гр») не беремо.
PRICE = re.compile(r"(?<!\d)(?<!\d[.,])(\d+)\s*гр(?:н)?\.?(?!\w)", re.IGNORECASE)


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
# Службові рядки підпису поста бота (BOT-007): рядок сайту (з 👈 і без) і «Всі/Більше/Додаткові фото
# в коментарях» — не частина опису товару.
COMMENTS = re.compile(r"^\s*(всі|більше|додаткові)\s+фото\s+в\s+коментар\w*\W*$", re.IGNORECASE)
SITE_LINE = re.compile(r"^\s*🛍\s*більше\s+взуття\s+на\s+сайті\s*(?:👈\s*)?\W*$", re.IGNORECASE)
SHOP_LINE = re.compile(r"^\s*viktoria\s+shoes\s*(?:👈\s*)?\W*$", re.IGNORECASE)  # BOT-008
MENTION = re.compile(r"(?<!\w)@\w+")
LINK = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.IGNORECASE)
PHONE = re.compile(r"(?<![\d+])(?:\+?38[\s-]?)?\(?0\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
HASHTAG = re.compile(r"(?<!\w)#\w+")
STATE_WORD = re.compile(r"(?<!\w)стан(?:і|у|ом)?(?!\w)", re.IGNORECASE)
STATE_SENTENCE = {NEW: "Стан нових, без дефектів.", IDEAL: "Ідеальний стан, без дефектів."}


def _without_price(text: str) -> str:
    """Прибрати ціну: останню й закреслені перед нею впритул. «Куплені за NNNгрн» лишається."""
    prices = list(PRICE.finditer(text))
    if not prices:
        return text
    start = prices[-1].start()
    for before in reversed(prices[:-1]):
        if text[before.end() : start].strip():
            break
        start = before.start()
    return text[:start] + " " + text[prices[-1].end() :]


def without_service_lines(text: str) -> str:
    """Без рядків сайту, «фото в коментарях» і «Пишіть сюди…» (підпис поста бота)."""
    lines = [
        line
        for line in text.split("\n")
        if not (
            COMMENTS.match(line)
            or SITE_LINE.match(line)
            or SHOP_LINE.match(line)
            or SIGNATURE.search(line)
        )
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_description(text: str, state: str) -> str:
    """Опис для Шафи: без назви (першого речення), ціни, підпису, контактів і хештегів.

    Наприкінці — речення про стан, якщо в тексті його ще немає.
    """
    text = without_service_lines(text.strip())
    text = _without_price(text[first_sentence_end(text) :])
    for pattern in (SIGNATURE, LINK, MENTION, PHONE, HASHTAG):
        text = pattern.sub(" ", text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    lines = [re.sub(r" (?=[,.!?])", "", line) for line in lines]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if STATE_WORD.search(text):
        return text
    if text and text[-1] not in ".!?":
        text += "."
    return f"{text} {STATE_SENTENCE[state]}".strip()


# «Устілка 26см», «Устілка до згину на п'ятку 26см, від краю до краю 27,5см», «УстілкаN,Nсм».
# «Ширина устілки NNсм» — не довжина (і з одруківкою в слові «ширина»).
INSOLE = re.compile(r"устілк[а-яіїєґ]*", re.IGNORECASE)
WIDTH_BEFORE = re.compile(r"ши\w*ина\s*$", re.IGNORECASE)
# Кінець заміру: «ширина», новий рядок, кінець речення (крапка не в числі) чи дужки.
INSOLE_END = re.compile(r"ши\w*ина|\n|\.(?!\d)|\)", re.IGNORECASE)
LENGTH = re.compile(r"(?<![\d.,])(\d+)(?:[.,](\d+))?(?![\d.,])(?!\s*(?:розмір|р\.))")


def _halves(whole: str, fraction: str | None) -> list[int]:
    """Замір → сусіди сітки 0,5 у половинках сантиметра: 25,8 → [51, 52], 25,5 → [51]."""
    tenths = int(whole) * 10 + (int(fraction[0]) if fraction else 0)
    low, rest = divmod(tenths, 5)
    return [low] if rest == 0 else [low, low + 1]


def _insole_numbers(text: str) -> list[tuple[str, str]]:
    """Заміри довжини устілки в тексті: (ціла частина, дробова або "") у порядку появи."""
    numbers: list[tuple[str, str]] = []
    for match in INSOLE.finditer(text):
        if WIDTH_BEFORE.search(text[: match.start()]):
            continue
        rest = text[match.end() :]
        end = INSOLE_END.search(rest)
        numbers += LENGTH.findall(rest[: end.start()] if end else rest)
    return numbers


def insole_measured(text: str) -> str | None:
    """Перший замір устілки, як у тексті, з крапкою: «24,5см» → «24.5» (для сайту, SITE-001)."""
    numbers = _insole_numbers(text)
    if not numbers:
        return None
    whole, fraction = numbers[0]
    return f"{whole}.{fraction}" if fraction else whole


def insole_lengths(text: str) -> list[float]:
    """Довжина устілки для Шафи: усі заміри, сусіди сітки 0,5 і все між мін і макс, крок 0,5."""
    halves: set[int] = set()
    for whole, fraction in _insole_numbers(text):
        halves.update(_halves(whole, fraction))
    if not halves:
        return []
    if len(halves) == 1 and min(halves) % 2:
        halves.add(min(halves) - 1)  # одиночне X,5 → X і X,5
    return [h / 2 for h in range(min(halves), max(halves) + 1)]


GORE_TEX = re.compile(r"gore[\s-]?tex|(?<!\w)gtx(?!\w)", re.IGNORECASE)
MEMBRANE = re.compile(r"мембран", re.IGNORECASE)


def membrane(text: str) -> str | None:
    """Мембрана: «Gore-Tex», «Є» (будь-яка інша) або None — поле не заповнюється."""
    if GORE_TEX.search(text):
        return "Gore-Tex"
    if MEMBRANE.search(text):
        return "Є"
    return None


def many_items(text: str) -> bool:
    """Кілька товарів в одному пості: 2+ рядки, кожен зі своєю ціною.

    Закреслена стара ціна стоїть у тому самому рядку, що й нова, тож окремим товаром не рахується.
    """
    return sum(1 for line in text.splitlines() if PRICE.search(line)) >= 2


# --- CORE-007: рішення за текстом, де ШІ вагалося (`docs/rules.md` v1.4) ---

WINTER = "Зима"
DEMI = "Демісезон"
# Ознаки зими: «на хутрі» (не «без хутра»), «утеплені», «зимові», «уги/угги», «дутики»,
# «на флісі» чи «всередині фліс» (QA-001: те саме). Мембрана й «не промокають» без них — демісезон.
# CORE-016: «термо» — не ознака зими, так Вікторія називає мембранне взуття (`_TERMO` нижче).
_WINTER_SIGN = re.compile(
    r"(?<!без )хутр|утепл|зимов|(?<![\w'’ʼ])у[гґ]{1,2}(?:и|і|ів|ах)?(?![\w'’ʼ])|"
    r"(?<!\w)ugg|дутик|(?<!без )(?<!мікро)фліс",
    re.IGNORECASE,
)
_TERMO = re.compile(r"термо", re.IGNORECASE)
# Тип взуття, до якого «термо» стосується: кросівки / кеди (+ опечатка «ккросівки») — демісезон,
# черевики й вище — демісезон і зима (CORE-016).
_SNEAKERS = re.compile(r"к?кросівк|(?<![\w'’ʼ])кед(?:и|ів|ах|ами)?(?![\w'’ʼ])", re.IGNORECASE)
_BOOTS = re.compile(r"черевик|чобіт|чобот|сапожк|ботильйон", re.IGNORECASE)
_WET = re.compile(
    r"мембран|gore[\s-]?tex|(?<!\w)gtx(?!\w)|не\s+промока|водонепроникн", re.IGNORECASE
)
# Сезон, названий прямо (зимові — ознака зими, вона й так дає «Зима»).
_SEASON_NAMED = (
    ("Літо", re.compile(r"(?<!\w)літн|(?<!\w)на\s+літо", re.IGNORECASE)),
    (DEMI, re.compile(r"демісезон|міжсезон", re.IGNORECASE)),
    ("Весна", re.compile(r"(?<!\w)весн(?:а|ою|у|яні|яне|яний)?(?!\w)", re.IGNORECASE)),
    ("Осінь", re.compile(r"(?<!\w)осін(?:ь|ні|нє|ній)|(?<!\w)осен", re.IGNORECASE)),
)


def has_termo(text: str) -> bool:
    return bool(_TERMO.search(text))


def _termo_season(text: str, section: str | None) -> list[str]:
    """«Термо» без ознак зими: кросівки / кеди — Демісезон, черевики, чоботи, сапожки, ботильйони
    й невідомий тип — Демісезон і Зима. Тип — з тексту, а коли в тексті його нема — з розділу."""
    if _BOOTS.search(text):
        return [DEMI, WINTER]
    if _SNEAKERS.search(text):
        return [DEMI]
    kind = section.rsplit("/", 1)[-1] if section else ""
    if _SNEAKERS.search(kind):
        return [DEMI]
    return [DEMI, WINTER]


def season(text: str, section: str | None = None) -> tuple[list[str] | None, str | None]:
    """Сезон за текстом: (значення, причина жовтого). (None, None) — текст мовчить, вирішує ШІ.

    Названо прямо — як у тексті; «літні» разом з ознаками зими — текст суперечить сам собі.
    `section` — розділ Картки (шлях Шафи): тип для «термо», коли в тексті його нема (CORE-016).
    """
    winter = bool(_WINTER_SIGN.search(text))
    named = [name for name, pattern in _SEASON_NAMED if pattern.search(text)]
    if named:
        if winter and "Літо" in named:
            return named, "у тексті і літо, і ознаки зими — перевір сезон"
        return named + ([WINTER] if winter and DEMI not in named else []), None
    if winter:
        return [WINTER], None
    if _TERMO.search(text):
        return _termo_season(text, section), None
    if _WET.search(text):
        return [DEMI], None
    return None, None


# Версія правил, за якими заповнено бланк: зростає, коли правила змінюються так, що старий бланк
# варто перезаповнити (CORE-008 A4). 1 — до CORE-007 (у базі NULL), 2 — CORE-007,
# 3 — SITE-003 (35+ без дитячих слів — доросле), 4 — CORE-009 (v1.7), 5 — CORE-010 (v1.8: стать аксесуара),
# 6 — CORE-011 (v1.9: аксесуари без розміру, широкий матеріал). Нова версія — програма на
# старті перераховує бланки правилами без ШІ (CORE-009 B); зміна інструкції ШІ — ще й
# `ai.PROMPT_VERSION`.
RULES_VERSION = 6

# Поля, які Продавчиня виправляє руками і які правила читають разом із текстом поста (CORE-008 B).
EDIT_TEXT_FIELDS = ("title", "description")


def edit_text(edits: dict[str, object]) -> str:
    """Її відредаговані назва й опис одним текстом — правила сезону, статі, підкладки й типу
    дивляться в нього поруч із текстом поста. Ціна, розмір, стан беруться лише з поста."""
    parts = [edits.get(name) for name in EDIT_TEXT_FIELDS]
    return "\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


WOMEN = "women"
MEN = "men"
GIRLS = "girls"
BOYS = "boys"
_SAID = (
    (GIRLS, re.compile(r"для\s+дівчин(?:ки|очки|ок)|дівчач|(?<!\w)дівчинк", re.IGNORECASE)),
    (BOYS, re.compile(r"для\s+хлопч|хлопчач|(?<!\w)хлопчик", re.IGNORECASE)),
    (WOMEN, re.compile(r"жіноч|для\s+жін(?:ок|ки)", re.IGNORECASE)),
    (MEN, re.compile(r"чоловіч|для\s+чоловік", re.IGNORECASE)),
)
UNISEX = re.compile(r"унісекс|unisex", re.IGNORECASE)
_ADULT = re.compile(r"каблук|підбор|шпильк", re.IGNORECASE)


def said_gender(text: str) -> str | None:
    """Для кого річ за текстом: «girls» / «boys» / «women» / «men» або None.

    Сказано двоє («і на жіночу, і на чоловічу ногу») чи «унісекс» — None: вирішує розмір.
    """
    found = [who for who, pattern in _SAID if pattern.search(text)]
    if len(found) != 1 or UNISEX.search(text):
        return None
    return found[0]


# Дитячі слова (SITE-003 6): без них 35+ — доросле. «Для дівчинки/хлопчика» — ще й стать (`_SAID`).
_KID = re.compile(
    r"дитяч|для\s+(?:дівчин|хлопч)|(?<!\w)дівчинк|(?<!\w)хлопчик|підліт|шкільн", re.IGNORECASE
)


def kid_words(text: str) -> bool:
    """У тексті сказано, що річ дитяча: «дитячі», «для дівчинки», «підліткові», «шкільні»."""
    return bool(_KID.search(text))


# Дитяча лінійка моделі (CORE-009 A3): «Kids», «Junior», «Jr», «J» після моделі («Speedcross J»).
_KID_MODEL = re.compile(r"(?<![\w])(?:kids?|junior|jr|джуніор)(?![\w])", re.IGNORECASE)
_J_AFTER_MODEL = re.compile(r"(?<=[A-Za-z0-9])\s+J(?![\w'’ʼ])")


def kid_model(text: str) -> bool:
    """У назві моделі ознака дитячої лінійки: Kids, Junior, Jr, «J» після моделі."""
    return bool(_KID_MODEL.search(text) or _J_AFTER_MODEL.search(text))


def adult_signs(text: str) -> bool:
    """Ознаки дорослого жіночого взуття для 35–36: каблук, «жіночі»."""
    return bool(_ADULT.search(text)) or said_gender(text) == WOMEN


_SIZE = r"(\d{2}(?:[.,]5)?)"
# Ярлик буває не європейський: «вказаний розмір 5» — UK/US (QA-001), тому тут і одна цифра.
_LABEL_SIZE = r"(\d{1,2}(?:[.,]5)?)(?![\d.,]*\s*см)"
# Ярлик, менший за це, — не європейський (у Шафі найменший розмір — 15, у малюків).
FOREIGN_LABEL = 15
# «Вказаний 39 розмір, ідуть на 38»; «на ярлику 39, ідуть на 38»; «розмір вказаний 39».
_LABEL = re.compile(
    rf"(?:вказан\w*|на\s+ярлик\w*|на\s+бирц\w*)\s*(?:розмір\w*)?\s*[:-]?\s*{_LABEL_SIZE}|"
    rf"розмір\w*\s+вказан\w*\s*[:-]?\s*{_LABEL_SIZE}",
    re.IGNORECASE,
)
_GOES = re.compile(rf"(?<!\w)[іий]?дуть\s*(?:на\s*)?{_SIZE}(?!\s*[-–/]\s*\d)", re.IGNORECASE)
_GOES_ANY = re.compile(r"(?<!\w)[іий]?дуть\s*(?:на\s*)?\d", re.IGNORECASE)
# Розмір у тексті: «43 розмір», «розмір 43», «43 і 45 розміри», «розміри 37 та 38».
_SIZE_LIST = rf"{_SIZE}((?:\s*(?:,|і|й|та)\s*\d{{2}}(?:[.,]5)?)*)"
_SIZES_BEFORE = re.compile(rf"(?<![\d.,]){_SIZE_LIST}\s*(?:-?(?:го|й|ий))?\s*розм", re.IGNORECASE)
_SIZES_AFTER = re.compile(
    rf"розмір\w*\s*[:-]?\s*{_SIZE_LIST}(?![\d.,]\d)(?!\s*(?:см|мм|\*|[xх×]\s*\d))", re.IGNORECASE
)
_RANGE_AFTER = re.compile(r"^\s*[-–/]\s*\d")
# «В наявності декілька розмірів» — кілька товарів (QA-001: пости-списки з «*» на кожен розмір).
_SEVERAL = re.compile(r"(?<!\w)(?:де)?кілька\s+розмір|(?<!\w)різн\w*\s+розмір", re.IGNORECASE)
# Розпаровка: лівий і правий різні — це не два товари, лишається жовтим.
# «Правий 37, лівий 38» — не «правильну підтримку» (CORE-009: хибна розпаровка в пілоті).
_UNPAIRED = re.compile(r"(?<!\w)(?:лів|прав)(?:ий|а|е|ого|ому|ої|у)(?!\w)|розпаров", re.IGNORECASE)


def _value(text: str) -> float:
    return float(text.replace(",", "."))


def _shown(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def label_sizes(text: str) -> list[str] | None:
    """§7 «вказаний X, ідуть на Y» кодом: на Шафу — [Y] і для вживаного, і для нового (CORE-009,
    рішення Власника: так вона ставить сама, 1405). None — правила немає.

    Обидва не європейські («9,5 US, ідуть на 10 US») — None: переводить таблиця US/UK.
    """
    label, goes = _LABEL.search(text), _GOES.search(text)
    if not label or not goes:
        return None
    y = _value(goes[1])
    return [_shown(y)] if y >= FOREIGN_LABEL else None


def foreign_label(text: str) -> bool:
    """«Вказаний розмір 5, ідуть на 37»: ярлик не європейський — розмір дає «ідуть на» (v1.6)."""
    label, goes = _LABEL.search(text), _GOES.search(text)
    if not label or not goes:
        return False
    return _value(label[1] or label[2]) < FOREIGN_LABEL <= _value(goes[1])


def text_sizes(text: str) -> list[float]:
    """Розміри, названі в тексті поруч зі словом «розмір» (без діапазонів «38-39»), по порядку."""
    found: list[float] = []
    for pattern in (_SIZES_BEFORE, _SIZES_AFTER):
        for match in pattern.finditer(text):
            if not match[2] and _RANGE_AFTER.match(text[match.end(1) :]):
                continue  # «38-39 розмір» — один товар на два розміри
            found += [_value(n) for n in re.findall(_SIZE, match[1] + match[2])]
    return list(dict.fromkeys(found))


_LETTER = r"(?:\d?X{0,3}S|M|L|\d?X{0,3}L)"
# «розмір S/M», «розмір: XL», «р. M», «size L». Лише поруч зі словом: окремий «M» чи «L» у тексті
# (ініціал, модель) — не розмір.
_LETTERS_AFTER = re.compile(
    rf"(?:розмір\w*|размер\w*|size|(?<!\w)р\.)\s*[:-]?\s*"
    rf"({_LETTER}(?:\s*(?:[/,-]|і|й|та)\s*{_LETTER})*)(?![\w/])",
    re.IGNORECASE,
)


def letter_sizes(text: str) -> list[str]:
    """Літерні розміри аксесуара з тексту поста («розмір S/M» → S, M), великими, по порядку, без
    повторів. Немає слова «розмір» поруч із літерою — порожньо (CORE-012)."""
    found: list[str] = []
    for match in _LETTERS_AFTER.finditer(text):
        found += re.findall(_LETTER, match[1], re.IGNORECASE)
    return list(dict.fromkeys(f.upper() for f in found))


def many_sizes(text: str) -> bool:
    """Два й більше різних розміри в пості (43 і 45; 37 і 38) — кілька товарів.

    «Вказаний X, ідуть на Y» і розпаровка (лівий/правий) — один товар. Розміри ближче 0,5 — теж.
    Але «декілька розмірів» чи два різні «вказаний X» в одному пості — кілька товарів (QA-001).
    """
    if _UNPAIRED.search(text):
        return False
    labels = {_value(m[1] or m[2]) for m in _LABEL.finditer(text)}
    if _SEVERAL.search(text) or len(labels) >= 2:
        return True
    if _GOES_ANY.search(text):
        return False
    sizes = sorted(text_sizes(text))
    return any(b - a > 0.5 for a, b in pairwise(sizes))


def unpaired(text: str) -> bool:
    return bool(_UNPAIRED.search(text))


# --- CORE-014: кілька розмірів однієї пари в одному пості → окрема Картка на кожен розмір ---


@dataclass(frozen=True)
class SizeVariant:
    size: str  # як на Шафі: «43», «37,5»; «вказаний X, ідуть на Y» → Y
    text: str  # текст поста для цієї Картки: спільний текст + лише її пункт


_BULLET = re.compile(r"^\s*[*•\-–]\s*")
# Пункт ділиться на речення крапкою не в числі («29,3см», «24.5» — не межа) і на частини комою.
_BULLET_SENTENCE = re.compile(r"(?<!\d)\.|\.(?!\d)")
_BULLET_PART = re.compile(r",(?!\d)")
# Частина пункту без устілки: лише розмір, ярлик, «ідуть на», ціна — інакше пункт про щось інше.
_BULLET_SIZE = re.compile(
    rf"(?:(?:вказан\w*|на\s+ярлик\w*|на\s+бирц\w*)\s*)?(?:розмір\w*\s*[:-]?\s*)?{_LABEL_SIZE}"
    r"\s*(?:-?(?:го|й|ий)\s*)?(?:розмір\w*)?|"
    rf"[іий]?дуть\s*(?:на\s*)?{_SIZE}|"
    r"(?:ціна\s*[:-]?\s*)?(?:\d+\s*гр(?:н)?\s*)+",
    re.IGNORECASE,
)
_SEVERAL_SENTENCE = re.compile(rf"[^.!?\n]*(?:{_SEVERAL.pattern})[^.!?\n]*[.!?]?", re.IGNORECASE)
_PRICE_WORD = re.compile(r"(?<!\w)ціна(?!\w)|[\W_]", re.IGNORECASE)


def _bullet_size(line: str) -> str | None:
    """Розмір пункту, якщо пункт — рівно один розмір, устілка й ціна. Інакше None."""
    for sentence in _BULLET_SENTENCE.split(line):
        if INSOLE.search(sentence):  # «Устілка …», «Ширина устілки …» — заміри
            continue
        for part in _BULLET_PART.split(sentence):
            if part.strip() and not _BULLET_SIZE.fullmatch(part.strip()):
                return None
    if label := label_sizes(line):
        return label[0]
    goes = list(_GOES.finditer(line))
    if len(goes) == 1 and not _LABEL.search(line):
        return _shown(_value(goes[0][1]))
    sizes = text_sizes(line)
    if len(sizes) != 1 or _GOES_ANY.search(line) or len(_LABEL.findall(line)) > 1:
        return None
    return _shown(sizes[0])


def _other_size_tag(tag: str, size: str) -> bool:
    """Хештег з номером іншого розміру («#VS_45_розмір» у Картки 43)."""
    numbers = re.findall(r"\d{2}", tag)
    return bool(numbers) and size.split(",")[0] not in numbers


def _variant_text(lines: list[str], own: int, size: str, own_price: bool) -> str:
    kept = []
    for i, line in enumerate(lines):
        if i == own:
            kept.append(_BULLET.sub("", line).rstrip())
            continue
        if _BULLET.match(line):
            continue
        new = _SEVERAL_SENTENCE.sub("", line)
        new = HASHTAG.sub(lambda m: "" if _other_size_tag(m[0], size) else m[0], new)
        if own_price:  # своя ціна в пункті — спільна в тексті зайва
            new = PRICE.sub("", new)
        if line.strip() and not _PRICE_WORD.sub("", new).strip():
            continue  # рядок був лише про інший розмір чи спільну ціну
        kept.append(re.sub(r"[ \t]{2,}", " ", new).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def size_variants(text: str) -> list[SizeVariant] | None:
    """Пост з пунктами «* 43 розмір. Устілка 28см…» — по Картці на розмір (CORE-014).

    Лише коли все однозначно: ≥ 2 пункти (`*`, `•`, `-`, `–`), у кожному рівно один розмір (з
    урахуванням «вказаний X, ідуть на Y» → Y), лише заміри й ціна; розміри різні; у спільному тексті
    розмірів немає й цін не більше однієї; пункт без ціни бере спільну. Інакше None — як раніше,
    `many_items` (різні моделі, пункт без розміру, два розміри в пункті, «43 і 45» без пунктів).
    """
    lines = text.splitlines()
    bullets = [i for i, line in enumerate(lines) if _BULLET.match(line)]
    if len(bullets) < 2:
        return None
    sizes = [_bullet_size(_BULLET.sub("", lines[i])) for i in bullets]
    if None in sizes or len(set(sizes)) != len(sizes):
        return None
    common = "\n".join(
        HASHTAG.sub(" ", _SEVERAL_SENTENCE.sub(" ", line))
        for i, line in enumerate(lines)
        if i not in bullets
    )
    if text_sizes(common) or _LABEL.search(common) or _GOES_ANY.search(common):
        return None
    if sum(1 for line in common.splitlines() if PRICE.search(line)) > 1:
        return None
    priced = [bool(PRICE.search(lines[i])) for i in bullets]
    if not all(priced) and not PRICE.search(common):
        return None  # пункт без ціни, а спільної немає
    return [
        SizeVariant(size, _variant_text(lines, i, size, own_price))
        for i, size, own_price in zip(bullets, sizes, priced, strict=True)
    ]
