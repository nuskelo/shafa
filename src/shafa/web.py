"""Екран перевірки: локальний вебсервер (лише 127.0.0.1) і сторінки.

Логіка запиту — `App.handle(Request) -> Response`, без мережі (так її перевіряють тести).
`serve` лише обгортає її в `ThreadingHTTPServer`.

Захист (рішення D6): кожен запит — лише з `Host` 127.0.0.1:порт або localhost:порт (чужий домен,
що вказує на 127.0.0.1, не прочитає сторінок); зміни — лише POST з токеном цього запуску.
"""

import email.policy
import hmac
import os
import re
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import jinja2
from PIL import UnidentifiedImageError

from shafa import (
    ai,
    batch,
    catalog,
    config,
    db,
    dup,
    fill,
    health,
    options,
    photos,
    review,
    rules,
    site,
)
from shafa.db import Status
from shafa.export import parse_export
from shafa.folders import PUBLISHED_DIR, base_name, is_old, item_folder, list_folders, old_root
from shafa.robot import browser, control, lock, publish, rehearse
from shafa.robot import listings as listings_mod
from shafa.robot.plan import Precheck, precheck
from shafa.scan import STICKY, scan

HEALTH_EVERY = 30  # секунд: «Перевірити ще раз» не частіше
COLLECT_EVERY = 180  # секунд між перевірками готових пакетів, поки є незібрані

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 1_000_000
MAX_UPLOAD = 50_000_000  # Експорт каналу (`result.json` її каналу — ≈ 4 МБ)
UPLOAD_PATHS = {"/export"}

PACKAGE = Path(__file__).resolve().parent
STATIC = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}

TAB_TITLES = {
    Status.REVIEW: "На перевірку",
    Status.APPROVED: "Схвалені",
    Status.PUBLISHED: "Опубліковані",
    Status.PROBLEM: "Проблеми",
    "sessions": "Сесії",
    Status.ARCHIVED: "Архів",
}
CARD_TABS = (Status.REVIEW, Status.APPROVED, Status.PROBLEM, Status.ARCHIVED)
MY_ITEMS = "https://shafa.ua/uk/my/clothes/active"
STICKY_TITLES = {  # проблеми публікації — простими словами, без кодів
    "publish_uncertain": "Не впевнений, чи товар з'явився на Шафі — перевір",
    "publish_error": "Шафа не прийняла товар",
    "possible_duplicate": "Схожий товар уже є на Шафі",
    "already_published": "Цей пост уже опубліковано з іншої папки",
    "shafa_declined": "Шафа відхилила оголошення після публікації",
    "photos_missing": "Фото поста не знайдено в папці",  # без запису Картка-Проблема давала 500
}
MESSAGES = {
    "saved": "Збережено",
    "approved": "Схвалено",
    "rejected": f"Відхилено — Картка в Архіві {review.ARCHIVE_DAYS} днів",
    "approved_done": "Схвалено. Усе переглянуто — більше Карток у цьому списку немає",
    "rejected_done": f"Відхилено — Картка в Архіві {review.ARCHIVE_DAYS} днів. "
    "Усе переглянуто — більше Карток у цьому списку немає",
    "unapproved": "Схвалення скасовано — Картка знову на перевірці",
    "restored": "Повернуто на перевірку",
    "section_yellow": "Схвалити не можна: розділ жовтий — змініть його або натисніть «Так, вірно»",
    "no_section": "Схвалити не можна: немає розділу",
    "section_unknown": "Схвалити не можна: такого розділу немає на Шафі — оберіть зі списку",
    "precheck": "Схвалити не можна: робот не зможе опублікувати — причина в червоному блоці",
    "no_sizes": "Схвалити не можна: немає розміру",
    "no_colors": "Схвалити не можна: немає кольору",
    "no_gender": "Схвалити не можна: для малюків Шафа вимагає «Стать»",
    "babies_size": "Схвалити не можна: «Для малюків» — лише до 22 розміру, оберіть «Для дівчаток» чи «Для хлопчиків»",
    "bad": "Не збережено поля, позначені червоним",
    "stale": "Картку вже змінено — сторінку оновлено",
    "photos_retry": "Картку повернуто на перевірку — перегляньте й схваліть знову",
    "robot_retry": "Картку повернуто в Схвалені — робот спробує ще раз",
    "recheck_new": "Папку перевірено: усе добре, Картка чекає на ШІ",
    "recheck_problem": "Папку перевірено: проблема лишилась",
    "on_shafa_block": "Цей товар уже є на Шафі — повертати його на перевірку й публікувати вдруге не можна",
    "sticky": "Спершу перевірте Шафу і скористайтеся кнопками нижче",
    "on_shafa": "Картку перенесено в Опубліковані",
    "bad_link": "Це не посилання на Шафу. Вставте посилання на товар з shafa.ua або його номер",
    "listing_found": "У «Мої речі» вже є товар із такою назвою — відкрийте його (посилання в "
    "картці): якщо це він, натисніть «На Шафі є». Повтор публікації дасть дубль",
    "not_on_shafa": "Картку повернуто в Схвалені — робот опублікує її при наступному запуску",
    "other_item": "Картку повернуто в Схвалені — на схожість її більше не перевірятимемо",
    "duplicate_archived": "Картку перенесено в Архів",
    "robot_started": "Робот запускається — за хвилину з'явиться вікно Chrome",
    "robot_cannot_start": "Робота не запущено",
    "robot_paused": "Пауза: робот допише поточний товар і зачекає",
    "robot_resumed": "Продовжуємо",
    "robot_stopping": "Стоп: робот допише поточний товар і зупиниться",
    "robot_not_running": "Робот зараз не публікує",
    "robot_login": "Відкриваю вікно входу на Шафу — увійдіть і закрийте вікно",
    "robot_busy": "Робот уже працює",
    "robot_login_done": "Закриваю вікно входу — за кілька секунд можна натискати «Старт»",
    "robot_no_login_window": "Вікно входу вже закрито",
    "robot_fake_login": "У підставному режимі вхід на Шафу не потрібен",
    "rehearse_started": "Пробний прогін запускається — за хвилину з'явиться вікно Chrome",
    "rehearse_busy": "Робот зайнятий (йде публікація чи інша справа) — пробний прогін після неї",
    "rehearse_cannot": "Пробний прогін зараз не запустити: немає Схвалених або робот ще запускається",
    "rehearse_fake": "У підставному режимі пробного прогону немає",
    "rehearse_setup": "Пробний прогін не запущено: неправильне налаштування кількості Карток",
    "health_started": "Перевіряю — результат з'явиться на панелі за кілька секунд",
    "health_running": "Перевірка вже йде",
    "health_wait": "Щойно перевіряли — зачекайте пів хвилини",
    "ai_started": "Оновлюю: шукаю нові папки й відправляю на ШІ — результат з'явиться на панелі",
    "ai_busy": "ШІ вже працює — зачекайте хвилину",
    "refill_started": "Перезаповнюю: картки пішли на ШІ пакетом — результати зазвичай до години, "
    "ваші правки збережуться",
    "recheck_done": "Перераховано за новими правилами, без ШІ — ваші правки збережено",
    "ai_retry_done": "ШІ заповнило бланк заново — ваші правки збережено",
    "ai_retry_failed": "ШІ не вдалося — старий бланк лишився (подробиці на панелі)",
    "ai_budget": "Ліміт витрат на ШІ за добу вичерпано — спробуйте завтра",
    "ai_no_key": "Немає ключа ШІ (ANTHROPIC_API_KEY) — скажіть Власнику",
    "ai_settings": "Неправильні налаштування ШІ — скажіть Власнику (подробиці на панелі)",
    "ai_photo": "Фото Картки не відкривається — ШІ не запускалось",
    "bot_started": "Запускаю бота — за кілька секунд він з'явиться на панелі",
    "bot_running": "Бот уже працює",
    "bot_no_token": "Бот не налаштований: немає TELEGRAM_BOT_TOKEN у .env — скажіть Власнику",
}
PROBLEM_HINTS = {
    "bad_folder_name": "Перейменуйте папку на номер поста (наприклад, 1342).",
    "duplicate_post": "Залиште одну папку для поста, другу перейменуйте чи приберіть.",
    "post_not_found": "Такого поста немає в Експорті. Перевірте номер у назві папки.",
    "no_text": "У пості немає тексту — такий товар оформлюється вручну.",
    "no_price": "У пості немає ціни — допишіть ціну в пост і зробіть новий Експорт.",
    "many_items": "У пості кілька товарів — такий пост оформлюється вручну. Пост з кількома розмірами однієї пари розбирається сам, якщо кожен розмір — окремим пунктом «* 43 розмір. Устілка …».",
    "size_gone": "Цей розмір прибрали з поста. Якщо його продано — «В архів». Якщо ні — поверніть пункт у пост і натисніть «Перевірити ще раз».",
    "no_photos": "Додайте в папку фото .jpg.",
    "too_many_photos": "Залиште в папці не більше 10 фото.",
    "folder_missing": "Папку перейменовано або видалено.",
    "robot_field_missing": "Шафа, мабуть, змінила форму. Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "ai_error": "ШІ не заповнило бланк. «Перевірити ще раз» поверне Картку в чергу на ШІ.",
    "publish_uncertain": "Робот натиснув «Додати річ», але не дочекався відповіді. Перевірте на Шафі, чи товар з'явився. Робот сам не повторює.",
    "possible_duplicate": "Схожий товар уже є на Шафі або цей пост уже опубліковано. Повторне оголошення заборонене правилами Шафи.",
    "shafa_declined": "Шафа перевірила оголошення вже після публікації й відхилила його. Відкрийте його на Шафі й виправте вручну або приберіть Картку в Архів. Робот цей товар вдруге не публікує.",
    "publish_error": "Шафа не прийняла товар (текст помилки вище). Перевірте Шафу й скажіть Власнику.",
    "robot_logged_out": "Робот не увійшов на Шафу. Власник: python -m shafa robot --login. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_brand_missing": "Бренд є у довіднику Шафи, але робот його не знайшов. Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_photos": "Не всі фото завантажились на Шафу. «Перевірити ще раз» поверне Картку в Схвалені.",
    "photos_missing": "Фото поста не знайдено в папці. Додайте фото і натисніть «Перевірити ще раз».",
    "stale_rules": "Картку схвалено за старими правилами. «Перевірити ще раз» перерахує її правилами (без ШІ) і поверне на перевірку: перегляньте й схваліть знову.",
    "robot_not_ready": "Картку не можна передати роботу (текст вище). «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_not_sent": "Форма на Шафі не надіслалась (товар не створено). Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_publish_blocked": "Захист робота не пропустив запит публікації — товар не створено. Скажіть Власнику.",
}
# UI-006: рядок Проблеми у списку — коротка мітка і що робити (довгий текст для Картки — `PROBLEM_HINTS`).
PROBLEM_KINDS = {
    "bad_folder_name": (
        "Назва папки",
        "перейменуй папку на номер поста, потім «Перевірити ще раз»",
    ),
    "duplicate_post": ("Дві папки", "залиш одну папку для поста, потім «Перевірити ще раз»"),
    "post_not_found": ("Поста немає", "перевір номер у назві папки, потім «Перевірити ще раз»"),
    "no_text": ("Нема тексту", "такий товар оформлюється вручну — відкрий Картку"),
    "no_price": (
        "Нема ціни",
        "допиши ціну в пост, зроби новий Експорт і натисни «Перевірити ще раз»",
    ),
    "many_items": ("Кілька товарів", "опублікуй вручну на Шафі — робот такі пости не бере"),
    "size_gone": (
        "Розмір прибрано",
        "якщо розмір продано — нічого не роби; якщо ні — поверни пункт у пост",
    ),
    "no_section": ("Нема розділу", "опублікуй вручну — підходящого розділу на Шафі немає"),
    "no_photos": ("Нема фото", "додай в папку фото .jpg, потім «Перевірити ще раз»"),
    "too_many_photos": (
        "Забагато фото",
        "залиш в папці не більше 10 фото, потім «Перевірити ще раз»",
    ),
    "folder_missing": (
        "Нема папки",
        "папку перейменовано чи видалено — поверни її, потім «Перевірити ще раз»",
    ),
    "ai_error": ("ШІ не заповнив", "«Перевірити ще раз» — Картка знову піде на ШІ"),
    "stale_rules": ("Старі правила", "«Перевірити ще раз» — перерахує без ШІ, потім схвали знову"),
    "possible_duplicate": ("Схожий є на Шафі", "відкрий Картку і порівняй"),
    "already_published": ("Уже опубліковано", "цей пост уже на Шафі — відкрий Картку"),
    "publish_uncertain": ("Не впевнений", "перевір «Мої речі» на Шафі, потім відкрий Картку"),
    "publish_error": ("Шафа не прийняла", "відкрий Картку"),
    "shafa_declined": (
        "Шафа не прийняла",
        "відкрий Картку: виправ оголошення на Шафі або прибери в архів",
    ),
    "photos_missing": ("Фото", "перевір папку з фото, потім «Перевірити ще раз»"),
    "robot_photos": ("Фото", "перевір папку з фото, потім «Перевірити ще раз»"),
    "post_deleted": (
        "Пост видалено",
        "пост прибрано з каналу — схоже, продано; публікувати не треба",
    ),
}
ROBOT_KIND = ("Робот", "«Перевірити ще раз» — робот спробує знову")
OTHER_KIND = ("Проблема", "відкрий Картку")
# Кнопки в рядку Проблеми — лише ті, що працюють на сервері: `publish.to_archive` приймає тільки ці коди.
ARCHIVE_CODES = ("possible_duplicate", "already_published", "shafa_declined")
LINK_CODES = (
    "publish_uncertain",
    "publish_error",
)  # їх вирішує людина на Картці (посилання на товар)

SECURITY_HEADERS = {
    # Сторінки беруть лише свої файли: жодних зовнішніх скриптів, стилів, шрифтів, фото.
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # Не `no-referrer`: з ним Chrome шле у формах `Origin: null`, і кожен POST — 403 (знайшла «мавпа»).
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

_ITEM = re.compile(r"/item/(\d+)")
_SESSION = re.compile(r"/session/(\d+)")
_PHOTO = re.compile(r"/(photo|thumb)/(\d+)/(\d+)")
_DUPLICATE_PHOTO = re.compile(r"/duplicate/(\d+)\.jpg")
_STATIC = re.compile(r"/static/([\w.-]+)")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    form: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)  # назви малими літерами
    files: dict[str, bytes] = field(default_factory=dict)  # файли з multipart-форми

    def arg(self, name: str) -> str:
        return (self.query.get(name) or [""])[0]


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def text(status: int, message: str) -> Response:
    return Response(status, message.encode("utf-8"), "text/plain; charset=utf-8")


def redirect(location: str) -> Response:
    return Response(303, headers={"Location": location})


@dataclass
class AiState:
    """Панель ШІ: черга й що зараз робиться."""

    ready: int = 0  # готові до відправки
    young: int = 0  # пост молодший за 7 днів
    pending: int = 0  # чекають відповіді ШІ
    wait_days: int = batch.WAIT_DAYS
    busy: str | None = None
    note: str | None = None
    refillable: int = 0  # Карток, які можна перезаповнити (CORE-008)
    recheckable: int = 0  # Карток за старшими правилами коду — перерахувати без ШІ (CORE-009)


@dataclass
class HealthView:
    """Блок самоперевірки на панелі: `code` — running / none / ok / problems."""

    code: str
    at: str = ""
    rows: list[health.Check] = field(default_factory=list)  # червоні, потім жовті
    ok: int = 0


@dataclass
class BotState:
    """Панель: стан процесу бота."""

    code: str  # running / stopped / failed / no_token
    text: str
    can_start: bool = False


def _spawn_bot(args: list[str]) -> object:
    return control.spawn(args, config.data_dir() / "bot-run.log", program="bot")


def _thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


def _real_client() -> Any:
    return ai.make_client(config.api_key())


class App:
    def __init__(
        self,
        db_path: Path,
        root: Path,
        cache_dir: Path,
        port: int = DEFAULT_PORT,
        channel_id: str | None = None,
        token: str | None = None,
        fake_shafa: bool = False,
        spawner: Callable[[list[str]], object] | None = None,
        lock_file: Path | None = None,
        ai_client: Callable[[], Any] = _real_client,
        run: Callable[[Callable[[], None]], None] = _thread,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        bot_spawner: Callable[[list[str]], object] = _spawn_bot,
        bot_lock: Path | None = None,
        on_quit: Callable[[], list[str]] | None = None,
        health_check: Callable[[], list[health.Check]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.root = root
        self.cache_dir = cache_dir
        self.port = port
        self.channel_id = channel_id
        self.token = token or secrets.token_urlsafe(32)
        self.fake_shafa = fake_shafa
        self.spawner = spawner or control.spawn
        self.lock_file = lock_file
        self.process = None  # останній запущений з екрана процес робота
        self.started_at = 0.0
        # ШІ: одна робота за раз (Оновити, збір результатів, «спробуй ще раз») — без подвійних оплат.
        self.ai_client, self.run, self.clock, self.today = ai_client, run, clock, today
        self.ai_lock = threading.Lock()
        self.ai_busy: str | None = None
        self.ai_note: str | None = None
        self.collected_at: float | None = None
        self.bot_spawner = bot_spawner
        self.bot_lock = bot_lock
        self.on_quit = on_quit  # «Вийти» — лише коли програму запущено з ярлика
        self.health_check = health_check or self._real_health
        self.health_lock = threading.Lock()
        self.health_running = False
        self.health_started: float | None = None
        self.health_result: tuple[str, list[health.Check]] | None = None
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(PACKAGE / "templates"),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["dmy"] = dmy

    # --- Захист ---

    def _allowed_hosts(self) -> set[str]:
        return {f"{HOST}:{self.port}", f"localhost:{self.port}"}

    def _origin_ok(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin in {f"http://{h}" for h in self._allowed_hosts()}

    def _token_ok(self, request: Request) -> bool:
        sent = (request.form.get("token") or [""])[0]
        return hmac.compare_digest(sent.encode("utf-8"), self.token.encode("utf-8"))

    # --- Запит ---

    def handle(self, request: Request) -> Response:
        if request.headers.get("host") not in self._allowed_hosts():
            response = text(403, "Невідома адреса")
        elif request.method == "POST" and not (
            self._origin_ok(request) and self._token_ok(request)
        ):
            response = self.page(
                "error.html",
                403,
                message="Сторінка застаріла. Відкрийте вкладку заново.",
            )
        else:
            try:
                response = self.route(request)
            except Exception:  # noqa: BLE001 — будь-яка помилка: сторінка, а не обрив з'єднання
                traceback.print_exc()
                response = self.page("error.html", 500, message="Щось пішло не так.")
        response.headers = {**SECURITY_HEADERS, **response.headers}
        return response

    def route(self, request: Request) -> Response:
        path = request.path
        get = request.method == "GET"
        if path == "/" and get:
            return redirect("/" + Status.REVIEW)
        if path == "/published" and get:
            return self.published_page(request)
        if path == "/sessions" and get:
            return self.sessions_page(request)
        if (match := _SESSION.fullmatch(path)) and get:
            return self.session_page(int(match[1]), request)
        if path == "/robot/panel" and get:
            return self.panel(request)
        if path == "/robot" and request.method == "POST":
            return self.robot_post(request)
        if path == "/ai" and request.method == "POST":
            return self.ai_post(request)
        if path == "/refill" and get:
            return self.refill_page(request)
        if path == "/refill" and request.method == "POST":
            return self.refill_post(request)
        if path == "/health" and request.method == "POST":
            return self.health_post(request)
        if path == "/bot" and request.method == "POST":
            return self.bot_post(request)
        if path == "/settings" and get:
            return self.settings_page(request)
        if path == "/settings" and request.method == "POST":
            return self.settings_post(request)
        if path == "/export" and request.method == "POST":
            return self.export_post(request)
        if path == "/listings/reread" and request.method == "POST":
            return self.reread_post(request)
        if path == "/site/release-sold" and request.method == "POST":
            return self.release_sold_post(request)
        if (match := _DUPLICATE_PHOTO.fullmatch(path)) and get:
            return self.duplicate_photo(int(match[1]))
        if path == "/quit" and request.method == "POST" and self.on_quit is not None:
            done = self.on_quit()
            return self.page("quit.html", done=done)
        if path.strip("/") in CARD_TABS and get:
            return self.tab_page(Status(path.strip("/")), request)
        if match := _ITEM.fullmatch(path):
            item_id = int(match[1])
            if get:
                return self.card_page(item_id, request)
            if request.method == "POST":
                return self.card_post(item_id, request)
        if (match := _PHOTO.fullmatch(path)) and get:
            return self.photo(match[1], int(match[2]), int(match[3]))
        if (match := _STATIC.fullmatch(path)) and get and match[1] in STATIC:
            body = (PACKAGE / "static" / match[1]).read_bytes()
            return Response(200, body, STATIC[match[1]])
        return text(404, "Не знайдено")

    def connect(self):
        return db.connect(self.db_path)

    def page(self, template: str, status: int = 200, **context) -> Response:
        html = self.env.get_template(template).render(token=self.token, local=local_time, **context)
        return Response(status, html.encode("utf-8"))

    def _panel_context(self, conn) -> dict:
        """Смужка стану: що показати і якого кольору кружечок кожної плитки (UI-004)."""
        robot, ai_, bot = self.robot_state(conn), self.ai_state(conn), self.bot_state(conn)
        site_, health_ = self.site_state(conn), self.health_view()
        return {
            "robot": robot, "ai": ai_, "bot": bot, "site": site_, "health": health_,
            "tones": {
                "robot": ROBOT_TONES.get(robot.code, "warn"),
                "ai": "ok" if ai_.busy else ("warn" if ai_.note else "ok"),
                "bot": BOT_TONES.get(bot.code, "warn"),
                "site": "warn" if site_ and site_[3] else "ok",
                "health": HEALTH_TONES.get(health_.code, "idle")
                if health_.code != "problems"
                else ("bad" if any(r.status == health.FAIL for r in health_.rows) else "warn"),
            },
        }  # fmt: skip

    def _chrome(self, conn, request: Request) -> dict:
        """Спільне для всіх сторінок: вкладки з лічильниками і панель робота."""
        counts = review.counts(conn)
        counts["sessions"] = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        return {
            "tabs": TAB_TITLES,
            "counts": counts,
            **self._panel_context(conn),
            "can_quit": self.on_quit is not None,
            "back": safe_back(request.path),
            "fake": self.fake_shafa,
        }

    # --- Налаштування (без секретів) ---

    def settings_page(
        self,
        request: Request,
        values: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        warnings: list[str] | None = None,
        message: str | None = None,
    ) -> Response:
        conn = self.connect()
        try:
            return self.page(
                "settings.html",
                **self._chrome(conn, request),
                current="settings",
                options=options.OPTIONS,
                blocks=settings_blocks(),
                values=values or options.current(),
                snapshot=listings_mod.snapshot_info(conn),
                held=site.summary(conn)[3],
                errors=errors or {},
                warnings=warnings or [],
                message=message or MESSAGES.get(request.arg("msg")),
            )
        finally:
            conn.close()

    def settings_post(self, request: Request) -> Response:
        form = {name: (values or [""])[0] for name, values in request.form.items()}
        errors, warnings = options.save(form)
        if errors:
            return self.settings_page(
                request, form, errors, warnings, "Не збережено: виправте поля, позначені червоним"
            )
        self.root = Path(form[options.ROOT_SETTING].strip().strip('"'))  # екран — одразу
        message = "Збережено. Робот і бот візьмуть нові значення з наступного запуску."
        return self.settings_page(request, warnings=warnings, message=message)

    def reread_post(self, request: Request) -> Response:
        """«Перечитати зараз»: знімок «Мої речі» застарів — робот прочитає кабінет на старті."""
        conn = self.connect()
        try:
            listings_mod.reset(conn)
        finally:
            conn.close()
        message = "Добре: робот перечитає «Мої речі» на Шафі під час наступного запуску (5–10 хв)."
        return self.settings_page(request, message=message)

    def release_sold_post(self, request: Request) -> Response:
        """«Надіслати відкладені Продано» (SITE-003): Власник перевірив канал — запобіжник знято."""
        conn = self.connect()
        try:
            count = site.release_held(conn)
        finally:
            conn.close()
        message = (
            f"Добре: «Продано» для {count} пар надішле бот, поки програма відкрита."
            if count
            else "Відкладених «Продано» немає."
        )
        return self.settings_page(request, message=message)

    def _duplicate_file(self, item_id: int) -> Path:
        return self.cache_dir.parent / listings_mod.PHOTOS_DIR / f"{item_id}.jpg"

    def duplicate_photo(self, item_id: int) -> Response:
        """Фото схожого оголошення, яке зберіг робот (шлях — з номера Картки, не із запиту)."""
        try:
            body = self._duplicate_file(item_id).read_bytes()
        except OSError:
            return text(404, "Фото не знайдено")
        return Response(200, body, "image/jpeg")

    def export_post(self, request: Request) -> Response:
        """«Завантажити експорт каналу» (TD-021): `result.json` → пости в базу і зіставлення папок."""
        data = request.files.get("export") or b""
        try:
            posts = parse_export(data)
        except ValueError:
            return self.settings_page(
                request, message="Це не експорт каналу Telegram (потрібен файл result.json)"
            )
        conn = self.connect()
        try:
            known = {r[0] for r in conn.execute("SELECT post_id FROM posts")}
            result = scan(conn, posts, list_folders(self.root, self.channel_id))
        finally:
            conn.close()
        added = len(set(posts) - known)
        message = (
            f"Експорт завантажено: постів {len(posts)}, нових {added}. "
            f"Папок {result.folders}, у «Проблемах» {len(result.problems)}."
        )
        return self.settings_page(request, message=message)

    # --- Самоперевірка (UI-003) ---

    def _real_health(self) -> list[health.Check]:
        return health.run_all(config.data_dir(), self.root, deps=health.real_deps())

    def start_health(self, throttle: bool = True, delay: float = 0.0) -> str:
        """Запустити перевірку у фоні. Повертає код повідомлення: `health_started`,
        `health_running` (вже йде) або `health_wait` (кнопку — не частіше ніж раз на 30 с).
        `delay` — почекати перед перевіркою (на старті бот ще підіймається)."""
        with self.health_lock:
            if self.health_running:
                return "health_running"
            started = self.health_started
            if throttle and started is not None and self.clock() - started < HEALTH_EVERY:
                return "health_wait"
            self.health_running = True
            self.health_started = self.clock()

        def job() -> None:
            try:
                if delay:
                    time.sleep(delay)
                checks = self.health_check()
                self.health_result = (datetime.now().astimezone().isoformat(), checks)
            except Exception:  # noqa: BLE001 — перевірка не повинна валити програму
                traceback.print_exc()
            finally:
                self.health_running = False

        self.run(job)
        return "health_started"

    def health_view(self) -> HealthView:
        if self.health_running:
            return HealthView("running")
        if self.health_result is None:
            return HealthView("none")
        at, checks = self.health_result
        rows = [c for c in checks if c.status == health.FAIL]
        rows += [c for c in checks if c.status == health.WARN]
        ok = sum(1 for c in checks if c.status == health.OK)
        return HealthView("problems" if rows else "ok", at, rows, ok)

    def health_post(self, request: Request) -> Response:
        back = safe_back((request.form.get("back") or [""])[0])
        sep = "&" if "?" in back else "?"
        return redirect(f"{back}{sep}msg={self.start_health()}")

    # --- Бот ---

    def _bot_lock(self) -> Path:
        from shafa.bot import run

        return self.bot_lock or config.data_dir() / run.LOCK_NAME

    def bot_state(self, conn) -> BotState:
        from shafa.bot import run

        row = conn.execute("SELECT error FROM bot_status WHERE id = 1").fetchone()
        error = row["error"] if row else None
        if lock.busy(self._bot_lock()) is not None:
            return BotState("running", f"працює{', але: ' + error if error else ''}")
        if config.setting(run.TOKEN) is None:
            return BotState("no_token", "не налаштований (немає токена)")
        if error:
            return BotState("failed", f"зупинився: {error}", can_start=True)
        return BotState("stopped", "вимкнений", can_start=True)

    def start_bot(self) -> str:
        """Запустити бота окремим процесом (як робота). Повертає код повідомлення."""
        from shafa.bot import run

        if config.setting(run.TOKEN) is None:
            return "bot_no_token"
        if lock.busy(self._bot_lock()) is not None:
            return "bot_running"
        self.bot_spawner(["--db", str(self.db_path), "--root", str(self.root)])
        return "bot_started"

    def bot_post(self, request: Request) -> Response:
        action = (request.form.get("action") or [""])[0]
        back = safe_back((request.form.get("back") or [""])[0])
        sep = "&" if "?" in back else "?"
        if action != "start":
            return text(400, "Невідома дія")
        return redirect(f"{back}{sep}msg={self.start_bot()}")

    # --- ШІ ---

    def ai_state(self, conn) -> AiState:
        """Стан для панелі; заодно — збір готових пакетів раз на кілька хвилин (у фоні)."""
        self._maybe_collect(conn)
        try:
            wait_days = batch.limits().wait_days
        except batch.SetupError as error:
            return AiState(busy=self.ai_busy, note=str(error))
        q = batch.queue(conn, self.today(), wait_days)
        return AiState(
            len(q.ready), q.young, q.pending, wait_days, self.ai_busy, self.ai_note,
            len(fill.refill_candidates(conn)), len(fill.recheck_candidates(conn)),
        )  # fmt: skip

    def _maybe_collect(self, conn) -> None:
        if not batch.open_batches(conn):
            return
        if self.collected_at is not None and self.clock() - self.collected_at < COLLECT_EVERY:
            return
        self._start(self._collect, "забираю результати ШІ…")

    def _start(self, job: Callable[[], str | None], busy: str) -> bool:
        """Запустити роботу ШІ у фоні; зайнято — False. Результат — рядок на панель."""
        if not self.ai_lock.acquire(blocking=False):
            return False
        self.ai_busy = busy

        def work() -> None:
            try:
                note = job()
                if note:
                    self.ai_note = note
            except config.ConfigError:
                self.ai_note = MESSAGES["ai_no_key"]
            except batch.SetupError as error:
                self.ai_note = str(error)
            except Exception:  # noqa: BLE001 — фонова робота не валить сервер
                traceback.print_exc()
                self.ai_note = "ШІ: щось пішло не так — скажіть Власнику"
            finally:
                self.ai_busy = None
                self.ai_lock.release()

        self.run(work)
        return True

    def _collect(self) -> str | None:
        self.collected_at = self.clock()
        conn = self.connect()
        try:
            return batch.collected_text(batch.collect(conn, self.ai_client(), self.today()))
        finally:
            conn.close()

    def _update(self) -> str | None:
        """«Оновити»: нові папки (тексти постів — з бази) → забрати готове → новий пакет."""
        self.collected_at = self.clock()
        lim = batch.limits()
        conn = self.connect()
        try:
            scan(conn, {}, list_folders(self.root, self.channel_id))
            client = self.ai_client()
            collected = batch.collected_text(batch.collect(conn, client, self.today()))
            sent = batch.send(conn, client, self.root, self.today(), lim)
            return "; ".join(t for t in (collected, batch.sent_text(sent, lim)) if t)
        finally:
            conn.close()

    def ai_post(self, request: Request) -> Response:
        action = (request.form.get("action") or [""])[0]
        back = safe_back((request.form.get("back") or [""])[0])
        sep = "&" if "?" in back else "?"
        if action == "recheck":
            return redirect(f"{back}{sep}msg={self.recheck()}")
        if action != "update":
            return text(400, "Невідома дія")
        started = self._start(self._update, "оновлюю й відправляю на ШІ…")
        return redirect(f"{back}{sep}msg={'ai_started' if started else 'ai_busy'}")

    def recheck(self) -> str:
        """«Перерахувати правилами»: одразу, без ШІ (CORE-009 B). Повертає код повідомлення."""
        if not self.ai_lock.acquire(blocking=False):
            return "ai_busy"
        try:
            conn = self.connect()
            try:
                fill.recheck(conn)
            finally:
                conn.close()
        finally:
            self.ai_lock.release()
        return "recheck_done"

    def _send_refill(self) -> str | None:
        """Відправити позначені для перезаповнення (нові папки не шукаємо) — пакетом."""
        self.collected_at = self.clock()
        lim = batch.limits()
        conn = self.connect()
        try:
            client = self.ai_client()
            collected = batch.collected_text(batch.collect(conn, client, self.today()))
            sent = batch.send(conn, client, self.root, self.today(), lim)
            return "; ".join(t for t in (collected, batch.sent_text(sent, lim)) if t)
        finally:
            conn.close()

    def refill_page(self, request: Request) -> Response:
        """«Перезаповнити»: спершу — скільки карток і скільки це коштує."""
        conn = self.connect()
        try:
            count, cost, skipped = batch.refill_estimate(conn, self.root)
            return self.page(
                "refill.html",
                **self._chrome(conn, request),
                current=None,
                message=None,
                count=count,
                skipped=skipped,
                cost=batch.dollars(cost),
                budget=batch.dollars(batch.limits().daily_micro_usd),
            )
        except batch.SetupError as error:
            return self.page("error.html", 400, message=str(error))
        finally:
            conn.close()

    def refill_post(self, request: Request) -> Response:
        """Позначити Картки (усі не перевірені чи одну) й відправити їх на ШІ пакетом."""
        item = (request.form.get("item") or [""])[0]
        conn = self.connect()
        try:
            if item:
                ids = [int(item)] if item.isdigit() else []
            else:
                ids = [r["id"] for r in fill.refill_candidates(conn)]
            if not ids or not fill.mark_refill(conn, ids):
                return redirect(f"/{Status.REVIEW}?msg=stale")
        finally:
            conn.close()
        started = self._start(self._send_refill, "перезаповнюю: відправляю на ШІ…")
        return redirect(f"/{Status.REVIEW}?msg={'refill_started' if started else 'ai_busy'}")

    def ai_retry(self, conn, item_id: int) -> str:
        """«ШІ, спробуй ще раз»: одразу, звичайним запитом без кешу (D2). Повертає код повідомлення."""
        if not self.ai_lock.acquire(blocking=False):
            return "ai_busy"
        try:
            row = fill.retry_row(conn, item_id)
            if row is None:
                return "stale"
            planned = fill.plan(conn, self.root, row)
            if isinstance(planned, fill.Skipped):
                return "ai_photo"
            try:
                left = batch.budget_left(conn, self.today(), batch.limits())
                client = self.ai_client()
            except batch.SetupError as error:
                self.ai_note = str(error)
                return "ai_settings"
            except config.ConfigError:
                return "ai_no_key"
            if planned.estimate.cost_micro_usd > left:
                return "ai_budget"
            result = fill.retry(conn, client, planned)
            if result.error:
                self.ai_note = f"«ШІ, спробуй ще раз» ({planned.folder_name}): {result.error}"
                return "ai_retry_failed"
            return "ai_retry_done"
        finally:
            self.ai_lock.release()

    # --- Робот ---

    def site_state(self, conn) -> tuple[int, int, int, int] | None:
        """Рядок панелі «Сайт: чекає N · вийде сьогодні M · продано за тиждень K · відкладено
        (запобіжник) L» (SITE-002, SITE-003)."""
        return site.summary(conn) if site.enabled() else None

    def robot_state(self, conn) -> control.State:
        process = self.process
        code = process.poll() if process is not None else None
        starting = process is not None and code is None and time.monotonic() - self.started_at < 60
        failed = process is not None and code not in (None, 0, 130)
        state = control.state(conn, self.lock_file, starting=starting, failed=failed)
        if state.code == "failed":
            state.text += ": " + last_log_line(control.log_path())
        return state

    def panel(self, request: Request) -> Response:
        conn = self.connect()
        try:
            template = "panel_mini.html" if request.arg("mini") else "panel.html"
            html = self.env.get_template(template).render(
                token=self.token, local=local_time, back=safe_back(request.arg("back")),
                fake=self.fake_shafa, **self._panel_context(conn),
            )  # fmt: skip
        finally:
            conn.close()
        return Response(200, html.encode("utf-8"))

    def robot_post(self, request: Request) -> Response:
        action = (request.form.get("action") or [""])[0]
        back = safe_back((request.form.get("back") or [""])[0])
        sep = "&" if "?" in back else "?"
        conn = self.connect()
        try:
            if action == "start":
                state = self.robot_state(conn)
                if not state.can_start:
                    return redirect(f"{back}{sep}msg=robot_cannot_start")
                args = ["--publish", "--db", str(self.db_path), "--root", str(self.root)]
                self._spawn(args + (["--fake-shafa"] if self.fake_shafa else []))
                return redirect(f"{back}{sep}msg=robot_started")
            if action == "rehearse":
                return self._rehearse(conn, back, sep)
            if action == "login":
                if self.fake_shafa:
                    return redirect(f"{back}{sep}msg=robot_fake_login")
                if lock.busy(self.lock_file):
                    return redirect(f"{back}{sep}msg=robot_busy")
                self._spawn(["--login"])
                return redirect(f"{back}{sep}msg=robot_login")
            if action == "login_done":  # запасне: вікно входу не закривається хрестиком
                busy = lock.busy(self.lock_file)
                if not busy or busy.get("mode") != "login":
                    return redirect(f"{back}{sep}msg=robot_no_login_window")
                browser.login_done_flag().touch()
                return redirect(f"{back}{sep}msg=robot_login_done")
            commands = {"pause": "pause", "resume": None, "stop": "stop"}
            if action in commands:
                done = publish.request(conn, commands[action])
                message = {"pause": "robot_paused", "resume": "robot_resumed",
                           "stop": "robot_stopping"}[action]  # fmt: skip
                return redirect(f"{back}{sep}msg={message if done else 'robot_not_running'}")
            return text(400, "Невідома дія")
        finally:
            conn.close()

    def _rehearse(self, conn, back: str, sep: str) -> Response:
        """«Перевірити завтрашні» (QA-003): той самий замок, що й публікація — разом ніколи."""
        if self.fake_shafa:
            return redirect(f"{back}{sep}msg=rehearse_fake")
        if lock.busy(self.lock_file):
            return redirect(f"{back}{sep}msg=rehearse_busy")
        if "rehearse" not in self.robot_state(conn).buttons:
            return redirect(f"{back}{sep}msg=rehearse_cannot")
        try:
            count = rehearse.count_setting()
        except rehearse.SetupError:
            return redirect(f"{back}{sep}msg=rehearse_setup")
        args = ["--rehearse", "--db", str(self.db_path), "--root", str(self.root)]
        self._spawn([*args, "--count", str(count)])
        return redirect(f"{back}{sep}msg=rehearse_started")

    def _spawn(self, args: list[str]) -> None:
        self.process = self.spawner(args)
        self.started_at = time.monotonic()

    # --- Сторінки ---

    def tab_page(self, status: Status, request: Request) -> Response:
        conn = self.connect()
        try:
            for item_id in review.purge_archive(conn):
                photos.drop_thumbnails(self.cache_dir, item_id)
            query = request.arg("q").strip()
            cards = review.list_tab(conn, status, query)
            checks = {c.id: self._precheck(c) for c in cards}
            marks = {c.id: card_mark(c, checks[c.id]) for c in cards}
            filters = []
            chosen = self._chosen(status, request.arg("f"))
            if status == Status.REVIEW:  # UI-004: «Усі / Треба глянути / Готові»
                warn = sum(1 for m in marks.values() if m.tone != "ok")
                for key, label, count in (
                    ("", "Усі", len(cards)),
                    ("warn", "Треба глянути", warn),
                    ("ok", "Готові", len(cards) - warn),
                ):
                    args = {k: v for k, v in (("f", key), ("q", query)) if v}
                    href = f"/{status}" + (f"?{urlencode(args)}" if args else "")
                    filters.append((key, label, count, href))
                cards = self._filtered(cards, marks, chosen)
            on_shafa = {  # Картка з `shafa_id` з Архіву не повертається (як на самій Картці)
                r[0] for r in conn.execute("SELECT id FROM items WHERE shafa_id IS NOT NULL")
            }
            return self.page(
                {Status.PROBLEM: "problems.html", Status.ARCHIVED: "archived.html"}.get(
                    status, "list.html"
                ),
                **self._chrome(conn, request),
                current=status,
                cards=cards,
                checks=checks,
                marks=marks,
                rows={c.id: problem_row(c) for c in cards} if status == Status.PROBLEM else {},
                on_shafa=on_shafa,
                my_items=MY_ITEMS,
                list_url=f"/{status}" + (f"?{urlencode({'q': query})}" if query else ""),
                filters=filters,
                chosen=chosen,
                query=query,
                message=MESSAGES.get(request.arg("msg")),
                deletion_date=review.deletion_date,
            )
        finally:
            conn.close()

    def _precheck(self, card: review.Card) -> Precheck:
        """Що скаже робот про Картку (QA-002) — лише «На перевірку» і «Схвалені»."""
        if card.status not in (Status.REVIEW, Status.APPROVED):
            return Precheck(None, [])
        return precheck(card, self.root)

    @staticmethod
    def _chosen(status: str, value: str) -> str:
        """Фільтр списку: лише в «На перевірку», лише `warn`/`ok`."""
        return value if status == Status.REVIEW and value in ("warn", "ok") else ""

    @staticmethod
    def _filtered(cards: list[review.Card], marks: dict, chosen: str) -> list[review.Card]:
        if not chosen:
            return cards
        return [c for c in cards if (marks[c.id].tone != "ok") == (chosen == "warn")]

    def _from(self, request: Request, card: review.Card) -> str:
        """Список, з якого відкрили Картку (UI-005): `from` у адресі, інакше сторінка, з якої прийшли
        (Referer), інакше вкладка самої Картки."""
        for raw, hosts in (
            (request.arg("from"), None),
            (request.headers.get("referer", ""), self._allowed_hosts()),
        ):
            if found := safe_list(raw, hosts):
                return found
        return f"/{card.status}" if card.status in LIST_TABS else "/" + Status.REVIEW

    def _nav(self, conn, card: review.Card, from_: str) -> "Nav":
        """Верх Картки: куди «назад», «3 з 8» і яка Картка наступна — у тому ж списку (фільтр, пошук)."""
        url = urlsplit(from_)
        status = Status(url.path.strip("/"))
        nav = Nav(from_, TAB_TITLES[status])
        if status not in CARD_TABS:  # «Опубліковані» без порядку й фільтрів
            return nav
        query = parse_qs(url.query)
        text_, chosen = (
            (query.get("q") or [""])[0],
            self._chosen(status, (query.get("f") or [""])[0]),
        )
        cards = review.list_tab(conn, status, text_)
        if chosen:
            marks = {c.id: card_mark(c, self._precheck(c)) for c in cards}
            cards = self._filtered(cards, marks, chosen)
        ids = [c.id for c in cards]
        if card.id in ids:
            at = ids.index(card.id)
            nav.index, nav.total = at + 1, len(ids)
            following = ids[at + 1 : at + 2]
        else:  # Картка вже не підпадає під фільтр (напр., підтвердила останнє жовте) — далі перша
            following = ids[:1]
        if following:
            nav.next_url = item_url(following[0], from_)
        return nav

    def card_page(self, item_id: int, request: Request) -> Response:
        conn = self.connect()
        try:
            card = review.load_card(conn, item_id)
            if card is None:
                return text(404, "Картку не знайдено")
            bad = {name for name in request.query.get("bad", []) if name in review.SPEC}
            row = conn.execute(
                "SELECT duplicate_url, duplicate_name, shafa_url, shafa_id FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            check = self._precheck(card)
            from_ = self._from(request, card)
            block = card.approve_block() if card.status == Status.REVIEW else None
            return self.page(
                "card.html",
                **self._chrome(conn, request),
                robot_error=check.error,
                robot_notes=check.notes,
                duplicate_url=shafa_link(row["duplicate_url"]),
                duplicate_name=row["duplicate_name"],
                ai_verdict=dup.verdict_text(card.problem_text),
                duplicate_photo=self._duplicate_file(item_id).is_file(),
                shafa_url=shafa_link(row["shafa_url"]),
                has_shafa_id=row["shafa_id"] is not None,
                my_items=MY_ITEMS,
                sticky=STICKY,
                sticky_titles=STICKY_TITLES,
                current=card.status,
                card=card,
                bad=bad,
                bad_input=review.BAD_INPUT,
                message=MESSAGES.get(request.arg("msg")),
                block=MESSAGES.get(block or ""),
                sections=catalog.sections(),
                hint=PROBLEM_HINTS.get(card.problem_code or ""),
                problem_archive=problem_actions(card.problem_code)[0],
                problem_recheck=problem_actions(card.problem_code)[1],
                deletion_date=review.deletion_date,
                editable=card.status == Status.REVIEW,
                nav=self._nav(conn, card, from_),
                layout=card_layout(card, bad, block),
                attention=attention(card),
                post_price=rules.price_from_text(card.post_text) if card.post_text else None,
                was=was_values(card),
            )
        finally:
            conn.close()

    def card_post(self, item_id: int, request: Request) -> Response:
        action = (request.form.get("action") or [""])[0]
        from_ = safe_list((request.form.get("from") or [""])[0])  # звідки відкрили (UI-005)
        following = safe_next((request.form.get("next") or [""])[0])
        conn = self.connect()
        try:
            card = review.load_card(conn, item_id)
            if card is None:
                return text(404, "Картку не знайдено")

            stay = (request.form.get("stay") or [""])[
                0
            ] == "list"  # кнопка зі списку Проблем (UI-006)

            def back(**params) -> str:
                if (
                    stay and from_ and list(params) == ["msg"]
                ):  # повідомлення — у списку, не на Картці
                    return with_msg(from_, params["msg"])
                return card_url(item_id, from_, **params)

            if action in ("save", "approve", "folder_order") or action.startswith("confirm:"):
                form, confirm = dict(request.form), action.removeprefix("confirm:")
                if action == "folder_order":  # порядок фото як у папці — і це підтвердження
                    by_position = sorted(card.photos, key=lambda p: p.position)
                    form["photo_order"] = ["\n".join(p.file_name for p in by_position)]
                    confirm = "photo_order"
                bad = review.save(conn, card, form, confirm or None)
                if bad:
                    return redirect(back(msg="bad", bad=bad))
                if action != "approve":
                    return redirect(back(msg="saved"))
            if action in review.ACTIONS:
                message = review.act(conn, item_id, action, self.root)
                if message in review.APPROVE_BLOCKS or message in ("stale", review.ON_SHAFA):
                    return redirect(back(msg=message))
                if from_ is None:  # старий виклик без списку
                    return redirect(f"/{card.status}?msg={message}")
                if message in ("approved", "rejected"):  # «Схвалити і далі» / «Відхилити»
                    if following:
                        return redirect(with_msg(following, message))
                    message += "_done"
                return redirect(with_msg(from_, message))
            decisions = {
                "on_shafa": lambda: publish.on_shafa(
                    conn, self.root, item_id, (request.form.get("shafa_link") or [""])[0]
                ),
                "not_on_shafa": lambda: publish.not_on_shafa(conn, item_id),
                "other_item": lambda: publish.other_item(conn, item_id),
                "to_archive": lambda: publish.to_archive(conn, item_id),
            }
            if action in decisions:
                message = decisions[action]()
                if message in ("stale", "bad_link", "listing_found"):
                    return redirect(back(msg=message))
                return redirect(f"/{Status.PROBLEM}?msg={message}")
            if action == "refill":
                if not fill.mark_refill(conn, [item_id]):
                    return redirect(back(msg="stale"))
                started = self._start(self._send_refill, "перезаповнюю: відправляю на ШІ…")
                return redirect(
                    f"/{Status.REVIEW}?msg={'refill_started' if started else 'ai_busy'}"
                )
            if action == "ai_retry":
                message = self.ai_retry(conn, item_id)
                return redirect(back(msg=message))
            if action == "recheck":
                result = review.recheck(conn, item_id, self.root, self.channel_id)
                if result.code in ("photos_retry", "recheck_done"):
                    return redirect(f"/{Status.REVIEW}?msg={result.code}")
                if result.code in ("robot_retry", "recheck_new"):
                    return redirect(f"/{Status.PROBLEM}?msg={result.code}")
                return redirect(back(msg=result.code))
            return text(400, "Невідома дія")
        finally:
            conn.close()

    def published_page(self, request: Request) -> Response:
        conn = self.connect()
        try:
            query = request.arg("q").strip()
            cards = review.list_tab(conn, Status.PUBLISHED, query)
            extra = {
                r["id"]: (shafa_link(r["shafa_url"]), r["published_at"])
                for r in conn.execute(
                    "SELECT id, shafa_url, published_at FROM items WHERE status = ?",
                    (Status.PUBLISHED,),
                )
            }
            cards.sort(key=lambda c: extra[c.id][1] or "", reverse=True)
            return self.page(
                "published.html",
                **self._chrome(conn, request),
                current=Status.PUBLISHED,
                cards=cards,
                extra=extra,
                query=query,
                message=MESSAGES.get(request.arg("msg")),
            )
        finally:
            conn.close()

    def sessions_page(self, request: Request) -> Response:
        conn = self.connect()
        try:
            sessions = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 200").fetchall()
            return self.page(
                "sessions.html",
                **self._chrome(conn, request),
                current="sessions",
                sessions=sessions,
                s_tones={r["id"]: session_tone(r) for r in sessions},
                reasons=control.LAST,
                message=MESSAGES.get(request.arg("msg")),
            )
        finally:
            conn.close()

    def session_page(self, session_id: int, request: Request) -> Response:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return text(404, "Сесію не знайдено")
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM items WHERE session_id = ? ORDER BY published_at, id",
                    (session_id,),
                )
            ]
            cards = [c for i in ids if (c := review.load_card(conn, i))]
            return self.page(
                "session.html",
                **self._chrome(conn, request),
                current="sessions",
                session=row,
                tone=session_tone(row),
                cards=cards,
                rows={c.id: problem_row(c) for c in cards if c.status == Status.PROBLEM},
                reasons=control.LAST,
                message=None,
            )
        finally:
            conn.close()

    def photo(self, kind: str, item_id: int, position: int) -> Response:
        """Фото лише з папок Карток у базі: шлях будується з бази, не із запиту."""
        conn = self.connect()
        try:
            row = conn.execute(
                """
                SELECT folder_name, file_name, status FROM photos JOIN items ON items.id = item_id
                WHERE item_id = ? AND position = ? AND purged_at IS NULL
                """,
                (item_id, position),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return text(404, "Фото не знайдено")
        root = (old_root() if is_old(row["folder_name"]) else self.root).resolve()
        folders = [item_folder(self.root, row["folder_name"]).resolve()]
        if row["status"] == Status.PUBLISHED:  # робот переносить папку в «Опубліковано» (TD-016)
            folders.insert(0, root / PUBLISHED_DIR / base_name(row["folder_name"]))
        path = next((p for f in folders if (p := (f / row["file_name"]).resolve()).is_file()), None)
        if path is None or not path.is_relative_to(root):
            return text(404, "Фото не знайдено")
        try:
            if kind == "thumb":
                body = photos.cached_thumbnail(self.cache_dir, item_id, path)
            else:
                body = path.read_bytes()  # оригінал: браузер сам повертає за EXIF
        except (OSError, UnidentifiedImageError):
            return text(404, "Фото не відкривається")
        return Response(200, body, "image/jpeg")


def parse_multipart(
    content_type: str, body: bytes
) -> tuple[dict[str, list[str]], dict[str, bytes]]:
    """multipart/form-data → (поля, файли). Без `cgi` (його в новіших Python немає)."""
    message = BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    form: dict[str, list[str]] = {}
    files: dict[str, bytes] = {}
    if not message.is_multipart():
        return form, files
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files[name] = data
        else:
            form.setdefault(name, []).append(data.decode("utf-8", errors="replace"))
    return form, files


def safe_back(path: str) -> str:
    """Куди повернутись після кнопки панелі: лише свій шлях (не `//чужий.сайт`)."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/" + Status.REVIEW
    return path


def shafa_link(url: str | None) -> str | None:
    """Посилання з бази — у сторінку лише якщо це справді shafa.ua (не `javascript:`)."""
    return url if url and url.startswith("https://shafa.ua/") else None


LIST_TABS = (*CARD_TABS, Status.PUBLISHED)


def item_url(item_id: int, from_: str | None = None) -> str:
    return f"/item/{item_id}" + (f"?{urlencode({'from': from_})}" if from_ else "")


def card_url(item_id: int, from_: str | None = None, **params) -> str:
    """Адреса Картки з повідомленням; `from` лише коли форма його принесла (UI-005)."""
    args = ({"from": from_} if from_ else {}) | params
    return f"/item/{item_id}" + (f"?{urlencode(args, doseq=True)}" if args else "")


def with_msg(url: str, message: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}{urlencode({'msg': message})}"


def safe_list(value: str, hosts: set[str] | None = None) -> str | None:
    """Адреса нашого списку: `/review?f=warn&q=…` (або повна з нашим `Host` — з Referer).

    Усе інше (чужий сайт, інший шлях, зайві параметри) — None; інші параметри вирізаються."""
    if "\\" in value:
        return None
    url = urlsplit(value)
    if hosts is not None:
        if url.scheme != "http" or url.netloc not in hosts:
            return None
    elif url.scheme or url.netloc:
        return None
    name = url.path.strip("/")
    if url.path != f"/{name}" or name not in LIST_TABS:
        return None
    query = parse_qs(url.query)
    args = {}
    if name == Status.REVIEW and (query.get("f") or [""])[0] in ("warn", "ok"):
        args["f"] = query["f"][0]
    if (query.get("q") or [""])[0]:
        args["q"] = query["q"][0]
    return f"/{name}" + (f"?{urlencode(args)}" if args else "")


def safe_next(value: str) -> str | None:
    """Куди йти після «Схвалити і далі»: лише Картка нашої програми (`/item/N?from=…`)."""
    if "\\" in value:
        return None
    url = urlsplit(value)
    match = _ITEM.fullmatch(url.path)
    if url.scheme or url.netloc or not match:
        return None
    return item_url(int(match[1]), safe_list((parse_qs(url.query).get("from") or [""])[0]))


@dataclass
class Nav:
    """Верх Картки: список, звідки прийшли, місце в ньому і наступна Картка."""

    back: str
    title: str
    index: int | None = None
    total: int | None = None
    next_url: str | None = None


MAIN_ROWS = (  # поля, які правлять найчастіше: у рядку їх стільки, скільки в кортежі
    ("title",), ("price", "sizes"), ("section",), ("brand", "condition"), ("colors",),
    ("season",), ("material_upper",),
)  # fmt: skip
MAIN_FIELDS = {name for row in MAIN_ROWS for name in row}
MISSING = {  # чому не можна схвалити → яке поле порожнє
    "no_section": "section", "section_unknown": "section", "section_yellow": "section",
    "no_sizes": "sizes", "babies_size": "sizes", "no_colors": "colors", "no_gender": "gender",
}  # fmt: skip


@dataclass
class Layout:
    """Як розкласти поля Картки: жовті з основних — угору, решта основних — рядками, рідкі — у «Ще поля»."""

    yellow: list[review.FieldView]
    rows: list[list[review.FieldView]]
    rare: list[review.FieldView]
    rare_open: bool
    missing: set[str]


def card_layout(card: review.Card, bad: set[str], block: str | None) -> Layout:
    visible = {f.spec.name: f for f in card.visible if f.spec.widget != "order"}
    yellow = [f for name, f in visible.items() if f.yellow and name in MAIN_FIELDS]
    taken = {f.spec.name for f in yellow}
    rows = [[visible[n] for n in row if n in visible and n not in taken] for row in MAIN_ROWS]
    rare = [f for name, f in visible.items() if name not in MAIN_FIELDS]
    missing = {MISSING[block]} if block in MISSING else set()
    opened = any(f.yellow or f.spec.name in bad or f.spec.name in missing for f in rare)
    return Layout(yellow, [r for r in rows if r], rare, opened, missing)


WAS = re.compile(r"було (.+?) →")


def was_values(card: review.Card) -> dict[str, str]:
    """«було …» для жовтих полів, де це відомо: сезон, який змінило правило (CORE-016)."""
    found = WAS.search(card.problem_text or "")
    if card.status == Status.REVIEW and card.problem_code == review.SEASON_CHANGED and found:
        return {"season": found[1]}
    return {}


@dataclass
class Attention:
    """Жовта примітка вгорі Картки: що перевірити і чому."""

    title: str
    lines: list[str]
    note: str | None  # примітка CORE-015/CORE-016 — «Пост змінено…», «Сезон змінено правилом…»


def short_label(field: review.FieldView) -> str:
    return field.spec.label.split(",")[0]


def attention(card: review.Card) -> Attention | None:
    if card.status != Status.REVIEW:
        return None
    yellow = [f for f in card.visible if f.yellow]
    note = card.problem_text if card.problem_code in review.NOTE_CODES else None
    if not yellow and not note:
        return None
    if not yellow:  # лише примітка «Пост змінено…» / «Сезон змінено правилом…»
        return Attention(note, [], None)
    lines = [
        f"{short_label(f)}: {f.reason or 'перевірте'}"
        if len(yellow) > 1
        else f.reason or "перевірте"
        for f in yellow[:2]
    ]
    if len(yellow) > 2:
        lines.append(f"Ще {len(yellow) - 2} — у полях нижче")
    names = ", ".join(short_label(f) for f in yellow)
    title = f"Перевір {plural(len(yellow), 'поле', 'поля', 'полів')}: {names}"
    return Attention(title, lines, note)


ROBOT_TONES = {  # кружечок плитки «Робот»: ok — працює, idle — стоїть, warn — увага, bad — помилка
    "running": "ok", "starting": "ok", "pausing": "ok", "stopping": "ok", "rehearsing": "ok",
    "stopped": "idle", "paused": "idle",
    "login_needed": "warn", "login_open": "warn", "busy": "warn",
    "crashed": "bad", "failed": "bad",
}  # fmt: skip
BOT_TONES = {"running": "ok", "stopped": "idle", "no_token": "warn", "failed": "bad"}
HEALTH_TONES = {"ok": "ok", "running": "idle", "none": "idle"}


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 пропуск», «2 пропуски», «5 пропусків»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


@dataclass
class Mark:
    """Мітка на фото Картки у списку: `ok` — «Готово», `warn` — жовта, `bad` — червона."""

    tone: str
    text: str


NOTE_TITLES = {
    review.POST_CHANGED: "Пост змінено",
    review.PHOTOS_CHANGED: "Фото змінено",
    review.SEASON_CHANGED: "Сезон змінено",
}


def card_mark(card: review.Card, check: Precheck) -> Mark | None:
    """Що сказати про Картку одним коротким підписом; None — вкладки без мітки (опубліковані, архів)."""
    if card.status == Status.PROBLEM:
        return Mark("bad", "Проблема")
    if card.status not in (Status.REVIEW, Status.APPROVED):
        return None
    if check.error:
        return Mark("bad", "Робот не зможе опублікувати")
    parts = []
    if card.status == Status.REVIEW and card.problem_code in review.NOTE_CODES:
        parts.append(NOTE_TITLES[card.problem_code])
    skips = len(check.notes)
    if card.yellow:
        if len(parts) + (1 if skips else 0) == 0:
            parts.append(plural(card.yellow, "жовте поле", "жовті поля", "жовтих полів"))
        else:
            parts.append(plural(card.yellow, "жовте", "жовті", "жовтих"))
    if skips:
        parts.append(plural(skips, "пропуск", "пропуски", "пропусків"))
    return Mark("warn", " · ".join(parts)) if parts else Mark("ok", "Готово")


def settings_blocks() -> list[tuple[str, list]]:
    """Налаштування за блоками (UI-006): поля й порядок ті самі, що в `options.OPTIONS`."""
    own = {
        "Сайт: «Продано»": (
            site.SEND_SETTING, site.WAIT_SETTING, site.SOLD_SETTING, site.FUSE_SETTING
        ),
        "«Мої речі» на Шафі": (listings_mod.DAYS_SETTING,),
    }  # fmt: skip
    taken = {name for names in own.values() for name in names}
    blocks = [("Ліміти й робот", [o for o in options.OPTIONS if o.name not in taken])]
    blocks += [
        (title, [o for o in options.OPTIONS if o.name in names]) for title, names in own.items()
    ]
    return blocks


@dataclass
class ProblemRow:
    """Рядок Проблеми (UI-006): що сталось, що робити, які кнопки."""

    label: str
    why: str
    todo: str
    details: str  # повний текст причини, коли `why` — коротка фраза зі `STICKY_TITLES`
    archive: bool
    recheck: bool
    my_items: bool


def problem_actions(code: str | None) -> tuple[bool, bool]:
    """(`to_archive`, `recheck`) — ті самі кнопки, що на Картці (`card.html`), і лише робочі на сервері:
    `publish_uncertain`/`publish_error` вирішуються посиланням на Картці, решта STICKY — без повтору."""
    if code in LINK_CODES:
        return False, False
    if code in ARCHIVE_CODES:
        return True, False
    return False, True


def problem_row(card: review.Card) -> ProblemRow:
    code = card.problem_code or ""
    label, todo = PROBLEM_KINDS.get(code) or (
        ROBOT_KIND if code.startswith("robot_") else OTHER_KIND
    )
    short = STICKY_TITLES.get(code)
    text_ = card.problem_text or ""
    archive, recheck = problem_actions(code)
    return ProblemRow(
        label=label,
        why=short or text_,
        todo=todo,
        details=text_ if short and text_ and text_ != short else "",
        archive=archive,
        recheck=recheck,
        my_items=code in LINK_CODES,
    )


CALM_STOPS = ("done", "limit", "stop")  # сесія закінчилась як задумано


def session_tone(row) -> str:
    """Колір сесії: `run` — працює, `ok` — спокійний кінець, `warn` — зупинилась з причини, `idle` — без причини."""
    if row["ended_at"] is None:
        return "run"
    if row["stop_code"] in CALM_STOPS:
        return "ok"
    return "warn" if row["stop_code"] or row["stop_reason"] else "idle"


def dmy(stamp: str | None) -> str:
    """`2026-03-13T10:00:00` → `13.03.2026`; не дата — як є."""
    try:
        return datetime.fromisoformat(stamp or "").strftime("%d.%m.%Y")
    except ValueError:
        return stamp or ""


def local_time(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return stamp


def last_log_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "журналу немає"
    return lines[-1][:200] if lines else "журнал порожній"


# --- Сервер ---


class Handler(BaseHTTPRequestHandler):
    app: App

    def do_GET(self) -> None:
        self._run("GET")

    def do_POST(self) -> None:
        self._run("POST")

    def _run(self, method: str) -> None:
        url = urlsplit(self.path)
        form: dict[str, list[str]] = {}
        files: dict[str, bytes] = {}
        if method == "POST":
            upload = url.path in UPLOAD_PATHS
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= (MAX_UPLOAD if upload else MAX_BODY):
                self._send(text(413, "Завеликий запит"))
                return
            raw = self.rfile.read(length)
            kind = self.headers.get("Content-Type") or ""
            if upload and kind.startswith("multipart/form-data"):
                form, files = parse_multipart(kind, raw)
            else:
                form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        request = Request(
            method,
            url.path,
            parse_qs(url.query),
            form,
            {k.lower(): v for k, v in self.headers.items()},
            files,
        )
        self._send(self.app.handle(request))

    def _send(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # браузер пішов, не дочекавшись (гортали фото) — це не помилка

    def log_message(self, format: str, *args) -> None:
        pass  # без журналу кожного запиту в консолі


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR дозволяє іншій програмі сісти на той самий порт — там вимкнено.
    allow_reuse_address = os.name != "nt"


def make_server(app: App, port: int = DEFAULT_PORT) -> Server:
    """Сервер лише на 127.0.0.1; порт зайнятий — будь-який вільний."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    try:
        server = Server((HOST, port), handler)
    except OSError:
        print(f"Порт {port} зайнятий, беру інший", file=sys.stderr)
        server = Server((HOST, 0), handler)
    app.port = server.server_address[1]
    return server
