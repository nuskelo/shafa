"""Захист від публікації — три шари (рішення D8). Чисті функції: тести без браузера.

1. Клік: ніколи `type=submit` і ніколи кнопки зі словами публікації чи змін оголошення.
2. Мережа: на Шафу йдуть лише GraphQL-запити на читання; з мутацій — тільки завантаження фото
   (`UploadPhoto` на `/api/v3/graphiql`, рішення D3) і лише коли воно дозволене. Усе інше — блок.
3. Клавіатура: Enter — лише в полі ключових слів (Enter у полі назви відправив би форму).
"""

import json
import re
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import urlsplit

FORBIDDEN_CLICK = re.compile(
    r"додати\s*річ|опублік|публікув|зберегти|додати\s*бренд|видалити|надіслати|оплат|купити|"
    r"вийти|продати|розмістити|імпорт|підняти|реклам|подарунок",
    re.IGNORECASE,
)

SHAFA_HOST = "shafa.ua"
GRAPHQL_PATHS = {
    "/api/v3/graphiql",
    "/api/v3/graphiql-batch",
    "/api/v4/graphiql/",
    "/api/v5/graphql",
}
UPLOAD_URL = "https://shafa.ua/api/v3/graphiql"
UPLOAD_OPERATION = "UploadPhoto"
UPLOAD_FIELD = "uploadPhoto"
READ_METHODS = {"GET", "HEAD", "OPTIONS"}

_OPERATION = re.compile(r"(?<![\w$])(query|mutation|subscription)\b")
_NAME = re.compile(r"[_A-Za-z]\w*")


class Forbidden(Exception):
    """Робот спробував заборонену дію. Зупинка всього прогону."""


def click_allowed(text: str, type_attr: str | None = None) -> bool:
    """Чи можна клікнути елемент з таким текстом (разом із value/aria-label) і типом."""
    if (type_attr or "").lower() == "submit":
        return False
    return not FORBIDDEN_CLICK.search(text or "")


def key_allowed(key: str, in_keywords: bool) -> bool:
    """Enter дозволений лише в полі ключових слів."""
    return key.lower() != "enter" or in_keywords


def has_mutation(query: str) -> bool:
    """Чи є в документі GraphQL мутація чи підписка (обережно: будь-яка згадка — так)."""
    return any(m.group(1) != "query" for m in _OPERATION.finditer(query))


def root_fields(query: str) -> list[str]:
    """Поля верхнього рівня першої операції: `mutation X($a: T) { a: f(x: 1) { y } g }` → f, g."""
    start = query.find("{")
    if start < 0:
        return []
    fields, depth, i, paren = [], 0, start, 0
    while i < len(query):
        ch = query[i]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and paren == 0 and (match := _NAME.match(query, i)):
            name = match.group()
            i = match.end()
            rest = query[i:].lstrip()
            if rest.startswith(":"):  # аліас: справжнє ім'я далі
                continue
            spread = query[: match.start()].rstrip().endswith("...")
            fields.append("..." + name if spread else name)  # фрагмент може ховати інші поля
            continue
        i += 1
    return fields


def _multipart_fields(content_type: str, body: bytes) -> dict[str, str]:
    """Текстові поля multipart (файл пропускаємо)."""
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode("latin-1") + body
    )
    fields = {}
    if not message.is_multipart():
        return fields
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name and not part.get_filename():
            fields[name] = part.get_content() if part.get_content_maintype() == "text" else ""
    return fields


def request_verdict(
    method: str, url: str, content_type: str, body: bytes | None, allow_upload: bool
) -> tuple[bool, str]:
    """Чи пропустити запит браузера (дозволено, причина)."""
    if method.upper() in READ_METHODS:
        return True, "читання"
    parts = urlsplit(url)
    if parts.hostname != SHAFA_HOST and not (parts.hostname or "").endswith("." + SHAFA_HOST):
        return False, "надсилання на чужий сайт"
    if parts.hostname != SHAFA_HOST or parts.path not in GRAPHQL_PATHS:
        return False, "надсилання не на GraphQL Шафи"
    body = body or b""
    if "multipart/form-data" in content_type.lower():
        if not allow_upload:
            return False, "завантаження файлу не дозволене"
        try:
            fields = _multipart_fields(content_type, body)
        except (ValueError, LookupError):
            return False, "незрозумілий multipart"
        query = fields.get("query", "")
        ok = (
            f"{parts.scheme}://{parts.hostname}{parts.path}" == UPLOAD_URL
            and fields.get("operationName") == UPLOAD_OPERATION
            and root_fields(query) == [UPLOAD_FIELD]
            and len(_OPERATION.findall(query)) == 1
        )
        return (True, "завантаження фото") if ok else (False, "multipart — не завантаження фото")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False, "не JSON"
    items = data if isinstance(data, list) else [data]
    if not items or not all(isinstance(i, dict) for i in items):
        return False, "не GraphQL"
    for item in items:
        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            return False, "запит без тексту (не перевірити)"
        if has_mutation(query):
            return False, f"мутація {item.get('operationName') or '?'}"
    return True, "читання GraphQL"
