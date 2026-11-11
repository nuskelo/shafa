"""KLB-001: захист робота Клубку — чисті функції (тести без браузера й мережі).

**Головна відмінність від Шафи:** на Клубку небезпечні дії — звичайні GET-посилання
(`/renew/all`, `/renew/{id}`, `?pg=renew-used`, `?pg=marksold`), тож «GET = читання» тут не діє.

1. Клік: ніколи `type=submit`, ніколи посилання (`a[href]`, і елемент усередині нього), ніколи
   слова публікації, змін, оновлення, продажу, видалення, реклами, оплати, виходу. Єдиний виняток
   (D1, Архітектор 10.11): підказка бренду — `a.dropdown-item` з `href` рівно `#` усередині `#filter2`.
2. Мережа на `klubok.com` і піддоменах: будь-який запит (і GET), чий шлях або запит містить слова
   `renew|marksold|delete|remove|promot|payment|logout|exit` (чи слово-токен `hot…`), — блок.
   POST — лише `/api/upload-used-photo` і лише коли завантаження дозволене; `/api/add-edit-used` і
   будь-який інший POST — блок. GET-навігація — лише за списком; картинки, стилі, скрипти — можна.
3. Клавіатура: Enter не можна ніде (у полі назви він відправив би форму).

KLB-003 — лише в режимі публікації (`publishing=True`; dry-fill, підставний прогін і знімок — як
були):
4. `Ticket` на одну Картку: видається після успішної звірки форми, пропускає **один** POST рівно
   `https://klubok.com/api/add-edit-used` і гасне на ньому; другий — блок. Без квитка — блок.
5. Клік «Опубликовать» — лише `#btn-submit`, лише з живим квитком і не в режимі «тисне людина».
6. reCAPTCHA: POST лише на `https://www.google.com/recaptcha/…` (GET чужих хостів — як і раніше,
   читання). Інших чужих POST — ні.
`/payment/{id}` і `/error429` лишаються заблокованими: після відповіді `add-edit-used` сторінка
сама переходить туди — це очікувано (позначка в `blocked`), а не причина зупинки.
"""

import re
from urllib.parse import unquote, urlsplit

FORBIDDEN_CLICK = re.compile(
    r"Опубликовать|Редактировать|Обновить|продан|Удалить|горячим|Реклам|Оплат|VIP|Эконом|Быстро|"
    r"Выйти",
    re.IGNORECASE,
)
BRAND_LIST = "filter2"  # id блоку бренду на формі: підказки — `a.dropdown-item` усередині нього

HOST = "klubok.com"
UPLOAD_PATH = "/api/upload-used-photo"
SUBMIT_PATH = "/api/add-edit-used"
SUBMIT_URL = f"https://{HOST}{SUBMIT_PATH}"
SUBMIT_BUTTON = "btn-submit"  # id кнопки «Опубликовать»
RECAPTCHA = "https://www.google.com/recaptcha/"
SUBMITTED = "публікація за квитком"  # причина в `request_verdict`: запит публікації пішов
AFTER_SUBMIT = re.compile(r"/payment/\d+|/error429")  # куди сторінка йде сама після відповіді
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
DANGER_SUBSTRINGS = ("renew", "marksold", "delete", "remove", "promot", "payment", "logout", "exit")
DANGER_TOKEN = "hot"  # `hot…` лише як слово: «photo» у `upload-used-photo` — не «hot»
ASSET = re.compile(
    r"\.(?:js|css|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|eot|map)$",
    re.IGNORECASE,
)
# GET-сторінки й API, куди робот може ходити: форма додавання, редагування, її сторінка керування.
PAGES = (
    re.compile(r"/item/add"),
    re.compile(r"/item/edit-ad"),
    re.compile(r"/users/\d+/manage"),
    re.compile(r"/api/1(?:/.*)?"),
    re.compile(r"/api/get-brands"),
    re.compile(r"/api/get-items"),
)


class Forbidden(Exception):
    """Робот спробував заборонену дію. Зупинка всього прогону."""


class Ticket:
    """Дозвіл рівно на один запит публікації однієї Картки (KLB-003 D6)."""

    def __init__(self, item_id: int) -> None:
        self.item_id = item_id
        self.used = False

    @property
    def alive(self) -> bool:
        return not self.used

    def use(self) -> bool:
        """Погасити квиток; False — вже погашений (другий запит — блок)."""
        if self.used:
            return False
        self.used = True
        return True


def submit_click_allowed(element_id: str | None, ticket: Ticket | None, human: bool) -> bool:
    """Чи може робот сам клікнути «Опубликовать»: лише `#btn-submit`, живий квиток, не `--human-click`."""
    return element_id == SUBMIT_BUTTON and ticket is not None and ticket.alive and not human


def click_allowed(
    text: str,
    type_attr: str | None = None,
    *,
    anchor_href: str | None = None,
    anchor_classes: str = "",
    anchor_in_brand_list: bool = False,
) -> bool:
    """Чи можна клікнути елемент.

    `text` — innerText, value, aria-label елемента (і посилання, у якому він лежить);
    `anchor_href` — `href` найближчого `a[href]` (None — елемент не посилання й не в посиланні).
    """
    if (type_attr or "").lower() == "submit":
        return False
    if FORBIDDEN_CLICK.search(text or ""):
        return False
    if anchor_href is None:
        return True
    return anchor_href == "#" and anchor_in_brand_list and "dropdown-item" in anchor_classes.split()


def key_allowed(key: str) -> bool:
    """Enter заборонений скрізь: на Клубку він надіслав би форму."""
    return "enter" not in key.lower()


def dangerous(url: str) -> bool:
    """Шлях чи запит містить слово небезпечної дії (оновити, продано, видалити, оплата, вихід…)."""
    parts = urlsplit(url)
    text = f"{parts.path}?{parts.query}"
    for _ in range(2):  # %2F, %252F
        text = unquote(text)
    if any(word in text.lower() for word in DANGER_SUBSTRINGS):
        return True
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)  # camelCase → слова
    return any(t.startswith(DANGER_TOKEN) for t in re.split(r"[^a-z0-9]+", words.lower()))


def is_klubok(host: str | None) -> bool:
    host = (host or "").lower()
    return host == HOST or host.endswith("." + HOST)


def request_verdict(
    method: str,
    url: str,
    content_type: str = "",
    allow_upload: bool = False,
    *,
    publishing: bool = False,
    ticket: Ticket | None = None,
) -> tuple[bool, str]:
    """Чи пропустити запит браузера (дозволено, причина). `publishing`/`ticket` — лише сесія
    публікації KLB-003: квиток гасне, коли пропущено запит публікації."""
    parts = urlsplit(url)
    method = method.upper()
    if not is_klubok(parts.hostname):
        if method in READ_METHODS:
            return True, "читання (чужий хост: шрифти, стилі, скрипти)"
        if publishing and method == "POST" and url.startswith(RECAPTCHA):
            return True, "reCAPTCHA"
        return False, "надсилання на чужий сайт"
    if dangerous(url):
        return False, "небезпечна адреса Клубку (оновити/продано/видалити/оплата/вихід)"
    if method == "POST":
        if parts.path == UPLOAD_PATH and parts.hostname == HOST:
            if not allow_upload:
                return False, "завантаження фото не дозволене"
            if "multipart/form-data" not in content_type.lower():
                return False, "завантаження фото: не multipart"
            return True, "завантаження фото"
        if (
            publishing
            and parts.hostname == HOST
            and parts.scheme == "https"
            and (parts.path == SUBMIT_PATH)
        ):
            if ticket is None:
                return False, "публікація без квитка"
            if not ticket.use():
                return False, "публікація: квиток уже використано"
            return True, SUBMITTED
        return False, f"POST {parts.path} заборонений"
    if method not in READ_METHODS:
        return False, f"метод {method} заборонений"
    if ASSET.search(parts.path):
        return True, "ресурс сторінки"
    if parts.hostname == HOST and any(p.fullmatch(parts.path.rstrip("/") or "/") for p in PAGES):
        return True, "читання сторінки зі списку"
    return False, f"навігація {parts.path} не зі списку"
