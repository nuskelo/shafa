"""Нові пари з бота → сайт Viktoria Shoes через API інтеграції (SITE-001).

Пост, опублікований ботом («Опублікувати»), стає в чергу `site_jobs` (SQLite — переживає перезапуск
і дні без інтернету). Окремий фоновий потік процесу бота шле пару на сайт: поля
(`PUT /products/by-post/{номер}`), потім фото по одному (`…/photos/{i}`, 0 — головне). Обрив —
продовжуємо з того самого `i` (повтор безпечний: сайт замінює фото на місці).

- Канал ніколи не чекає на сайт: бот лише записує рядок у чергу; мережа — тільки в потоці.
- Ручні пости каналу й старі (`borh`) у чергу не потрапляють: ставить лише бот після публікації.
- Перемикач «Відправляти на сайт» (`SHAFA_SITE_SEND`) типово вимкнений: вимкнено — у чергу не
  ставимо, а наявна черга стоїть і чекає. Ключа немає — інтеграція вимкнена.
- 429/503 — чекаємо `Retry-After`; обрив і 5xx — повтор із зростаючою паузою; 401 чи 404 на всьому —
  черга стоїть до нового ключа чи перезапуску, Власнику одне повідомлення; 400/409 — не повторюємо,
  Власнику й Продавчині номер поста.
- Ключ — лише в заголовку запиту: ні в журналі, ні в помилках (помилки `httpx2` замінюються своїм
  текстом, `from None`).
"""

import base64
import io
import json
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx2
from PIL import Image, ImageOps, UnidentifiedImageError

from shafa import ai, config, fill, folders, photos, rules
from shafa import sizes as sizes_mod
from shafa.catalog import Kind
from shafa.db import now

API = "https://api.viktoriashoes.com.ua/api/integration/v1"
KEY = "SITE_INTEGRATION_KEY"
SEND_SETTING = "SHAFA_SITE_SEND"  # «Відправляти на сайт» (типово — ні)
TIMEOUT = 30.0
PHOTO_TIMEOUT = 120.0  # контракт: обробка фото — кілька секунд, таймаут клієнта ≥ 90 с
PHOTO_MAX_BYTES = 5 * 1024 * 1024  # контракт: до 5 МБ і до 25 Мп
PHOTO_MAX_PIXELS = 25_000_000
PHOTO_MAX_INDEX = 19  # контракт: index 0…19
GAP = 1.2  # секунд між запитами: ліміт 60 за хвилину спільний з імпортом сайту
IDLE = 10.0  # перевірка черги, коли робити нічого
RETRY_AFTER_DEFAULT = 60  # 429/503 без `Retry-After`
BACKOFF = (30, 60, 120, 300, 600, 1800)  # обрив, 5xx: далі — раз на 30 хв, хоч кілька днів
SUBCATEGORIES_TTL = timedelta(days=1)
PRICE_MAX = 100_000
TEXT_MAX = 20  # size, lengthSm, brand
CONDITIONS = ("Новий", "Ідеальний", "Відмінний", "Хороший")
MISSING = "пари немає на сайті (404 на фото)"  # позначка: поля шлемо вдруге лише раз
AI_MAX_TOKENS = 1000


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "так", "yes", "true")


def enabled() -> bool:
    """Перемикач «Відправляти на сайт» у «Налаштуваннях»."""
    return _flag(config.setting(SEND_SETTING))


def key() -> str | None:
    return config.setting(KEY)


# --- HTTP ---


class SiteError(Exception):
    """Запит до сайту не вдався. Текст — без ключа й адреси.

    `status` None — немає зв'язку або обрив (запит міг дійти; повтор безпечний — усе ідемпотентне).
    """

    def __init__(
        self,
        text: str,
        status: int | None = None,
        retry_after: int | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(text)
        self.status = status
        self.retry_after = retry_after
        self.fields = fields  # назви полів з `errors` відповіді 400 (без текстів)


def _retry_after(response: httpx2.Response) -> int | None:
    raw = response.headers.get("Retry-After", "").strip()
    return int(raw) if raw.isdigit() else None


def _error_fields(body: Any) -> tuple[str, ...]:
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, dict):
        return ()
    return tuple(str(name).removeprefix("$.")[:40] for name in errors)[:5]


class Site:
    def __init__(self, key: str, client: httpx2.Client | None = None, base: str = API) -> None:
        self._headers = {"X-Integration-Key": key}
        self._client = client or httpx2.Client(timeout=TIMEOUT)
        self._base = base

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        files: dict | None = None,
        timeout: float = TIMEOUT,
    ) -> Any:
        try:
            response = self._client.request(
                method,
                self._base + path,
                headers=self._headers,
                json=body,
                files=files,
                timeout=timeout,
            )
        except httpx2.HTTPError:
            raise SiteError("немає зв'язку з сайтом") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code in (200, 201):
            return data
        raise SiteError(
            f"сайт відповів {response.status_code}",
            response.status_code,
            _retry_after(response),
            _error_fields(data) if response.status_code == 400 else (),
        ) from None

    def subcategories(self) -> list[dict]:
        data = self._request("GET", "/subcategories")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def put_fields(self, post_id: int, fields: dict) -> dict:
        data = self._request("PUT", f"/products/by-post/{post_id}", body=fields)
        return data if isinstance(data, dict) else {}

    def put_photo(self, post_id: int, index: int, data: bytes) -> dict:
        files = {"file": (f"photo-{index}.jpg", data, "image/jpeg")}
        result = self._request(
            "PUT", f"/products/by-post/{post_id}/photos/{index}", files=files, timeout=PHOTO_TIMEOUT
        )
        return result if isinstance(result, dict) else {}


# --- Поля пари ---

_STATE = r"стан\w*\s*[:–—-]?\s*"
STATES = (
    ("Ідеальний", re.compile(rf"ідеальн\w*\s+стан|{_STATE}ідеальн", re.IGNORECASE)),
    ("Відмінний", re.compile(rf"відмінн\w*\s+стан|{_STATE}відмінн", re.IGNORECASE)),
    ("Хороший", re.compile(rf"хорош\w*\s+стан|{_STATE}хорош", re.IGNORECASE)),
)


def condition(text: str) -> str | None:
    """Стан за текстом: «нові» / «в стані нових» → Новий (те саме слово, що й для Шафи), «в
    ідеальному стані» → Ідеальний, «у відмінному» → Відмінний, «в хорошому» → Хороший; інакше
    None — не слати."""
    if rules.NEW_WORD.search(text):
        return "Новий"
    return next((name for name, pattern in STATES if pattern.search(text)), None)


def code_fields(text: str, posted_at: str) -> dict[str, Any]:
    """Поля, які рахує код: ціна з тексту (як у Telegram, без +15% Шафи), стан, устілка, опис."""
    fields: dict[str, Any] = {"title": None, "description": text, "postedAt": posted_at}
    price = rules.price_from_text(text)
    if price is not None and 1 <= price <= PRICE_MAX:
        fields["price"] = price
    length = rules.insole_measured(text)
    if length and len(length) <= TEXT_MAX:
        fields["lengthSm"] = length
    state = condition(text)
    if state:
        fields["condition"] = state
    return fields


AI_SYSTEM = """Ти допомагаєш завести пару взуття чи аксесуар на сайт магазину вживаних речей.
Є фото товару (головне) і текст оголошення з Telegram. Поверни JSON за схемою:
- subcategoryId — id підкатегорії сайту лише з переліку нижче, і лише коли ти впевнений (стать,
  дитяче, сезон, тип видно з фото й тексту). Сумніваєшся — null.
- brand — бренд, як його пишуть (з тексту або чітко видний на фото). Не видно — null.
- sizes — розмір з ярлика, як у тексті: «38», «38,5». «Вказаний X, ідуть на Y» — X. Немає — [].
- size_system — система розміру в тексті: EU, US або UK (немає позначки — EU)."""

AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subcategoryId": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "brand": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "sizes": {"type": "array", "items": {"type": "string"}},
        "size_system": {"type": "string", "enum": list(sizes_mod.SYSTEMS)},
    },
    "required": ["subcategoryId", "brand", "sizes", "size_system"],
    "additionalProperties": False,
}


class AiRetry(Exception):
    """ШІ тимчасово недоступне (немає зв'язку, 429, 5xx) — спробувати пізніше."""


def build_ai_request(text: str, photo: photos.PreparedPhoto | None, subs: list[dict]) -> dict:
    listing = "\n".join(
        f"{s.get('id')} — {s.get('categoryName', '')} / {s.get('name', '')}" for s in subs
    )
    content: list[dict[str, Any]] = []
    if photo is not None:
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
    content.append(
        {
            "type": "text",
            "text": f"Текст оголошення:\n{ai.post_for_ai(text)}\n\nПідкатегорії сайту:\n"
            + (listing or "(немає)"),
        }
    )
    return {
        "model": ai.MODEL,
        "max_tokens": AI_MAX_TOKENS,
        "system": AI_SYSTEM,
        "thinking": {"type": "between_tools"},  # мислення вимкнене, як у бланку (CORE-003 D4)
        "output_config": {"format": {"type": "json_schema", "schema": AI_SCHEMA}},
        "messages": [{"role": "user", "content": content}],
    }


def _kind(sub: dict | None) -> Kind | None:
    """Група розміру за назвою категорії сайту («Жіноче взуття» → жіноча таблиця)."""
    name = str((sub or {}).get("categoryName", "")).lower()
    for word, kind in (("жіноч", Kind.WOMEN), ("чолов", Kind.MEN), ("дитяч", Kind.KIDS)):
        if word in name:
            return kind
    return None


def check_ai(answer: dict[str, Any], subs: list[dict]) -> dict[str, Any]:
    """Відповідь ШІ → поля для сайту. Сумнів — поле не шлемо (сайт поставить своє)."""
    fields: dict[str, Any] = {}
    by_id = {str(s.get("id")): s for s in subs if s.get("id")}
    sub_id = answer.get("subcategoryId")
    sub = by_id.get(sub_id) if isinstance(sub_id, str) else None
    if sub is not None:
        fields["subcategoryId"] = sub_id
    brand = answer.get("brand")
    if isinstance(brand, str) and brand.strip() and len(brand.strip()) <= TEXT_MAX:
        fields["brand"] = brand.strip()
    raw = answer.get("sizes")
    first = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], str) else None
    system = answer.get("size_system")
    if first and system in (sizes_mod.US, sizes_mod.UK):
        converted, doubt = sizes_mod.to_eu([first], system, _kind(sub))
        first = converted[0] if converted and doubt is None else None
    value = sizes_mod.parse(first) if first else None
    if value is not None and 15 <= value <= 50:
        fields["size"] = f"{value:g}"
    return fields


# --- Фото ---


def photo_files(root: Path, folder_name: str) -> list[Path] | None:
    """Фото поста в порядку каналу (1.jpg, 2.jpg …; 0 на сайті — перше). Папка переїхала в
    «Опубліковано» — беремо звідти. Папки немає — None."""
    for folder in (root / folder_name, root / folders.PUBLISHED_DIR / folder_name):
        if folder.is_dir():
            files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".jpg"]
            return sorted(
                files,
                key=lambda f: (
                    not f.stem.isdigit(),
                    int(f.stem or 0) if f.stem.isdigit() else 0,
                    f.name,
                ),
            )
    return None


def for_site(data: bytes) -> bytes:
    """Фото як є, якщо влазить у ліміти сайту; інакше зменшена копія JPEG (оригінал не чіпаємо)."""
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if len(data) <= PHOTO_MAX_BYTES and width * height <= PHOTO_MAX_PIXELS:
            return data
        copy = ImageOps.exif_transpose(image).convert("RGB")
    scale = min(1.0, (PHOTO_MAX_PIXELS / (copy.width * copy.height)) ** 0.5)
    while True:
        size = (max(1, int(copy.width * scale)), max(1, int(copy.height * scale)))
        out = io.BytesIO()
        copy.resize(size).save(out, "JPEG", quality=90)
        if out.tell() <= PHOTO_MAX_BYTES:
            return out.getvalue()
        scale *= 0.8


# --- Черга ---


def enqueue(
    conn: sqlite3.Connection, post_id: int, folder_name: str, posted_at: str, text: str
) -> bool:
    """Пост від бота → у чергу сайту (якщо перемикач увімкнено і ключ є). Повтор не дублює."""
    if not enabled() or key() is None:
        return False
    stamp = now()
    with conn:
        conn.execute(
            "INSERT INTO site_jobs (post_id, folder_name, posted_at, text, state, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, 'fields', ?, ?) ON CONFLICT (post_id) DO NOTHING",
            (post_id, folder_name, posted_at, text, stamp, stamp),
        )
    return True


@dataclass
class Notify:
    """Куди писати людям (у процесі бота — Telegram). Тексти — без ключа і без текстів поста."""

    owner: Callable[[str], None]
    seller: Callable[[str], None]


class Halt(Exception):
    """401 / 404 на всьому: черга стоїть до нового ключа чи перезапуску."""


class Worker:
    """Один крок — один запит до сайту (поля або одне фото). `step()` повертає, скільки чекати."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        notify: Notify,
        site_for: Callable[[str], Site] = Site,
        ai_client: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        log: Callable[[str], None] = print,
    ) -> None:
        self.conn, self.root, self.notify, self.clock, self.log = conn, root, notify, clock, log
        self._site_for = site_for
        self._ai_client = ai_client or (lambda: ai.make_client(config.api_key()))
        self._site: Site | None = None
        self._site_key: str | None = None
        self._halted_key: str | None = None
        self._pause_until: datetime | None = None
        self._said: set[str] = set()

    def _once(self, what: str, text: str) -> None:
        if what not in self._said:
            self._said.add(what)
            self.log(text)

    def _stamp(self, delta: float = 0) -> str:
        return (self.clock() + timedelta(seconds=delta)).isoformat(timespec="seconds")

    def _client(self, site_key: str) -> Site:
        if self._site is None or self._site_key != site_key:
            if self._site is not None:
                self._site.close()
            self._site, self._site_key = self._site_for(site_key), site_key
        return self._site

    def close(self) -> None:
        if self._site is not None:
            self._site.close()

    def step(self) -> float:
        if not enabled():
            self._halted_key = None  # вимкнули й увімкнули — пробуємо знову
            self._once("off", "Сайт: «Відправляти на сайт» вимкнено — черга чекає")
            return IDLE
        self._said.discard("off")
        site_key = key()
        if site_key is None:
            self._once("nokey", f"Сайт: немає {KEY} — інтеграція вимкнена")
            return IDLE
        self._said.discard("nokey")
        if self._halted_key == site_key:
            return IDLE
        if self._pause_until is not None and self.clock() < self._pause_until:
            return (self._pause_until - self.clock()).total_seconds()
        job = self.conn.execute(
            "SELECT * FROM site_jobs WHERE state IN ('fields', 'photos') "
            "AND (next_at IS NULL OR next_at <= ?) ORDER BY created_at, post_id LIMIT 1",
            (self._stamp(),),
        ).fetchone()
        if job is None:
            return IDLE
        site = self._client(site_key)
        try:
            if job["state"] == "fields":
                self._send_fields(site, job)
            else:
                self._send_photo(site, job)
        except Halt as halt:
            self._halted_key = site_key
            self.log(f"Сайт: черга зупинена — {halt}")
            self.notify.owner(f"Сайт: {halt}. Черга сайту стоїть; нічого не загублено.")
            return IDLE
        except AiRetry as error:
            return self._later(job, f"ШІ: {error}")
        except SiteError as error:
            return self._failed_request(job, error)
        return GAP

    # --- Кроки ---

    def _set(self, post_id: int, **values: Any) -> None:
        values["updated_at"] = now()
        names = ", ".join(f"{k} = ?" for k in values)
        with self.conn:
            self.conn.execute(
                f"UPDATE site_jobs SET {names} WHERE post_id = ?", (*values.values(), post_id)
            )

    def _subcategories(self, site: Site) -> list[dict]:
        row = self.conn.execute(
            "SELECT value, fetched_at FROM site_cache WHERE name = 'subcategories'"
        ).fetchone()
        if row and datetime.fromisoformat(row["fetched_at"]) > self.clock() - SUBCATEGORIES_TTL:
            return json.loads(row["value"])
        try:
            subs = site.subcategories()
        except SiteError as error:
            if error.status is None or error.status in (401, 404, 429, 503) or error.status >= 500:
                raise
            self.log(f"Сайт: підкатегорії не отримано ({error})")
            return []
        with self.conn:
            self.conn.execute(
                "INSERT INTO site_cache (name, value, fetched_at) VALUES ('subcategories', ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET value = excluded.value, "
                "fetched_at = excluded.fetched_at",
                (json.dumps(subs, ensure_ascii=False), self._stamp()),
            )
        return subs

    def _ai_fields(self, job: sqlite3.Row, subs: list[dict]) -> dict[str, Any]:
        """Підкатегорія, бренд, розмір — ШІ одразу (не пакетом). Будь-яка невдача, крім
        тимчасової, — без цих полів."""
        try:
            client = self._ai_client()
        except config.ConfigError:
            self._once("noai", "Сайт: немає ключа ШІ — підкатегорію, бренд і розмір не шлемо")
            return {}
        files = photo_files(self.root, job["folder_name"]) or []
        try:
            photo = photos.prepare(files[0]) if files else None
        except (OSError, UnidentifiedImageError):
            photo = None
        request = build_ai_request(job["text"], photo, subs)
        try:
            message = client.messages.create(**request)
        except (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as error:
            raise AiRetry(ai.error_text(error)) from None
        except anthropic.APIStatusError as error:
            self.log(f"Сайт: пост {job['post_id']} — {ai.error_text(error)}, без полів ШІ")
            fill.log_request(self.conn, None, None, ai.error_text(error))
            self.conn.commit()
            return {}
        try:
            answer, usage = ai.parse(message)
        except ai.AiError as error:
            self.log(f"Сайт: пост {job['post_id']} — {error}, без полів ШІ")
            fill.log_request(self.conn, None, error.usage, str(error))
            self.conn.commit()
            return {}
        fill.log_request(self.conn, None, usage, None)
        self.conn.commit()
        return check_ai(answer, subs)

    def _fields(self, site: Site, job: sqlite3.Row) -> dict[str, Any]:
        if job["fields"]:
            return json.loads(job["fields"])
        subs = self._subcategories(site)
        fields = {**code_fields(job["text"], job["posted_at"]), **self._ai_fields(job, subs)}
        self._set(job["post_id"], fields=json.dumps(fields, ensure_ascii=False))
        return fields

    def _send_fields(self, site: Site, job: sqlite3.Row) -> None:
        post_id = job["post_id"]
        answer = site.put_fields(post_id, self._fields(site, job))  # 404 — `_failed_request`
        self._set(
            post_id, state="photos", next_photo=0, attempts=0, next_at=None,
            error=MISSING if job["error"] == MISSING else None,
        )  # fmt: skip
        status = answer.get("status")
        self.log(f"Сайт: пост {post_id} — поля прийнято ({status or 'без статусу'})")
        self._warnings(post_id, answer.get("warnings"))

    def _warnings(self, post_id: int, warnings: Any) -> None:
        if not isinstance(warnings, list) or not warnings:
            return
        parts = [
            f"{str(w.get('field', '?'))[:30]} — {str(w.get('code', '?'))[:30]}"
            for w in warnings
            if isinstance(w, dict)
        ]
        if parts:
            line = "; ".join(parts[:10])
            self.log(f"Сайт: пост {post_id}, попередження: {line}")
            self.notify.owner(f"Сайт, пост {post_id}: {line}")

    def _send_photo(self, site: Site, job: sqlite3.Row) -> None:
        post_id, index = job["post_id"], job["next_photo"]
        files = photo_files(self.root, job["folder_name"])
        if files is None:
            self._give_up(job, f"папки «{job['folder_name']}» з фото немає", seller=False)
            return
        if index >= len(files) or index > PHOTO_MAX_INDEX:
            self._set(post_id, state="done", next_at=None, error=None, attempts=0)
            self.log(f"Сайт: пост {post_id} — готово, фото: {min(len(files), PHOTO_MAX_INDEX + 1)}")
            return
        try:
            data = for_site(files[index].read_bytes())
        except (OSError, UnidentifiedImageError):
            self._give_up(job, f"фото {files[index].name} не відкривається", seller=False)
            return
        try:
            site.put_photo(post_id, index, data)
        except SiteError as error:
            if error.status != 404:
                raise
            if job["error"] == MISSING:
                raise Halt("сайт каже «пари немає» навіть після повторних полів (404)") from None
            self.log(f"Сайт: пост {post_id} — пари немає (404 на фото), шлю поля ще раз")
            self._set(post_id, state="fields", error=MISSING)
            return
        done = index + 1 >= len(files) or index + 1 > PHOTO_MAX_INDEX
        self._set(
            post_id, next_photo=index + 1, attempts=0, next_at=None, error=None,
            state="done" if done else "photos",
        )  # fmt: skip
        if done:
            self.log(f"Сайт: пост {post_id} — готово, фото: {index + 1}")

    # --- Невдачі ---

    def _later(self, job: sqlite3.Row, why: str) -> float:
        attempts = job["attempts"] + 1
        wait = BACKOFF[min(attempts, len(BACKOFF)) - 1]
        self._set(job["post_id"], attempts=attempts, next_at=self._stamp(wait), error=why[:200])
        self.log(f"Сайт: пост {job['post_id']} — {why}; повтор через {wait} с")
        return GAP

    def _give_up(self, job: sqlite3.Row, why: str, seller: bool = True) -> None:
        post_id = job["post_id"]
        self._set(post_id, state="failed", next_at=None, error=why[:200])
        self.log(f"Сайт: пост {post_id} — не повторюю: {why}")
        self.notify.owner(f"Сайт не прийняв пару з поста {post_id}: {why}. Повторно не шлю.")
        if seller:
            self.notify.seller(
                f"Пару з поста {post_id} не вдалося завести на сайт — додай її в адмінці сайту "
                "руками. Власник уже знає."
            )

    def _failed_request(self, job: sqlite3.Row, error: SiteError) -> float:
        status = error.status
        if status in (429, 503):
            wait = error.retry_after or RETRY_AFTER_DEFAULT
            self._pause_until = self.clock() + timedelta(seconds=wait)
            self._set(job["post_id"], next_at=self._stamp(wait), error=str(error))
            self.log(f"Сайт: {error} — чекаю {wait} с (Retry-After)")
            return float(wait)
        if status == 401:
            self._halted_key = self._site_key
            self.log("Сайт: ключ не прийнято (401) — черга зупинена")
            self.notify.owner(
                f"Сайт: ключ не прийнято (401). Перевір {KEY} і перезапусти програму. Черга сайту "
                "стоїть; нічого не загублено."
            )
            return IDLE
        if status == 404:  # поля чи підкатегорії: інтеграцію вимкнено
            self._halted_key = self._site_key
            self.log("Сайт: 404 — інтеграцію на сайті вимкнено, черга зупинена")
            self.notify.owner(
                "Сайт: інтеграцію на сайті вимкнено (404) — скажи команді сайту. Черга сайту "
                "стоїть; нічого не загублено."
            )
            return IDLE
        if status is None or status >= 500:
            return self._later(job, str(error))
        why = f"{error}" + (f" (поля: {', '.join(error.fields)})" if error.fields else "")
        if status == 409:
            why += " — конфлікт (наприклад, у пари вже 20 фото)"
        self._give_up(job, why)
        return GAP


def run(
    worker: Worker, stop: threading.Event, sleep: Callable[[float], bool] | None = None
) -> None:
    """Потік: крок за кроком, доки `stop` не встановлено. Падіння кроку не зупиняє потік."""
    wait = sleep or stop.wait
    while not stop.is_set():
        try:
            pause = worker.step()
        except Exception as error:  # noqa: BLE001 — потік сайту не має падати назавжди
            worker.log(f"Сайт: збій кроку ({type(error).__name__}) — спробую пізніше")
            pause = IDLE * 6
        wait(max(0.0, min(pause, 3600.0)))


def start(
    db_path: Path, root: Path, notify: Notify, log: Callable[[str], None] = print
) -> threading.Event:
    """Фоновий потік сайту в процесі бота (своє з'єднання з базою). Повертає `stop`."""
    from shafa import db

    stop = threading.Event()

    def body() -> None:
        conn = db.connect(db_path)
        worker = Worker(conn, root, notify, log=log)
        try:
            run(worker, stop)
        finally:
            worker.close()
            conn.close()

    threading.Thread(target=body, name="site", daemon=True).start()
    return stop
