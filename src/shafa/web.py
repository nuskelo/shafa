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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple
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
    events,
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
from shafa.scan import NO_SECTION_TEXT, OLD_NO_SECTION_TEXT, STICKY, scan

HEALTH_EVERY = 30  # секунд: «Перевірити ще раз» не частіше
COLLECT_EVERY = 180  # секунд між перевірками готових пакетів, поки є незібрані
FINISHED_SHOWN = timedelta(days=2)  # так довго панель пам'ятає «Готово о …» (UX-001)

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
    "publish_error": "Шафа не прийняла при публікації",
    "possible_duplicate": "Схожий товар уже є на Шафі",
    "already_published": "Цей пост уже опубліковано з іншої папки",
    "shafa_declined": "Шафа відхилила оголошення",
    "photos_missing": "Фото поста не знайдено в папці",  # без запису Картка-Проблема давала 500
}
MESSAGES = {
    "saved": "Збережено",
    "approved": "Схвалено",
    "rejected": f"Картку «{{name}}» відкладено в Архів на {review.ARCHIVE_DAYS} днів.",
    "approved_next": "Схвалено «{name}» → у «Схвалених». Це наступна Картка.",
    "approved_left": "Схвалено. Лишилось {n} пропущених — ось перша з них.",
    "approved_done": "Схвалено. На перевірці більше нічого немає — можна натискати «Старт» угорі.",
    "rejected_left": f"Картку «{{name}}» відкладено в Архів на {review.ARCHIVE_DAYS} днів. "
    "Лишилось {n} пропущених — ось перша з них.",
    "rejected_done": f"Картку «{{name}}» відкладено в Архів на {review.ARCHIVE_DAYS} днів. "
    "На перевірці більше нічого немає — можна натискати «Старт» угорі.",
    "saved_no_price": "Збережено, але без ціни Картку не схвалити.",
    "no_price": "Схвалити не можна: немає ціни",
    "unapproved": "Схвалення скасовано — можна правити. Потім знову «Схвалити».",
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
    "recheck_many_items": "Проблема лишилась: у пості досі кілька пар чи розмірів. Опублікуйте на "
    "Шафі вручну («Опублікувала вручну») або «В архів».",
    "recheck_no_section": "Проблема лишилась: робот публікує лише взуття й аксесуари. Цю річ "
    "опублікуйте на Шафі вручну («Опублікувала вручну») або «В архів».",
    "to_review": "Картку повернуто на перевірку — виправте поля й схваліть знову",
    "no_restore": "Такий пост робот не опублікує — повертати його на перевірку не можна",
    "on_shafa_block": "Цей товар уже є на Шафі — повертати його на перевірку й публікувати вдруге не можна",
    "sticky": "Спершу перевірте Шафу і скористайтеся кнопками нижче",
    "on_shafa": "Картку перенесено в Опубліковані",
    "bad_link": "Це не посилання на Шафу. Вставте посилання на товар з shafa.ua або його номер",
    "listing_found": "У «Мої речі» вже є товар із такою назвою — відкрийте його (посилання в "
    "картці): якщо це він, натисніть «На Шафі є». Повтор публікації дасть дубль",
    "not_on_shafa": "Картку повернуто в Схвалені — робот опублікує її при наступному запуску",
    "other_item": "Картку повернуто в Схвалені — на схожість її більше не перевірятимемо",
    "duplicate_archived": "Картку перенесено в Архів",
    "robot_started": "Робот запускається — за хвилину відкриється окреме вікно Chrome. Не закривайте "
    "його і нічого в ньому не натискайте (згорнути можна). Не закривайте кришку ноутбука, поки "
    "робот працює. Стан — тут, на панелі.",
    "robot_cannot_start": "Робота не запущено: зараз це неможливо",
    "robot_cannot_start_busy": "Робота не запущено: робот уже працює",
    "robot_cannot_start_none": "Робота не запущено: немає Схвалених Карток",
    "robot_cannot_start_limit": "Робота не запущено: на сьогодні все — ліміт доби вичерпано",
    "robot_cannot_start_login": "Робота не запущено: спершу увійдіть на Шафу",
    "robot_cannot_start_setup": "Робота не запущено: у налаштуваннях неправильні межі на день",
    "robot_paused": "Пауза: робот допише поточний товар і зачекає",
    "robot_resumed": "Продовжуємо",
    "robot_stopping": "Стоп: робот допише поточний товар і зупиниться",
    "robot_not_running": "Робот зараз не публікує",
    "robot_login": "Відкриваю вікно входу на Шафу — увійдіть і закрийте вікно",
    "robot_busy": "Робот уже працює",
    "robot_login_done": "Закриваю вікно входу — за кілька секунд можна натискати «Старт»",
    "robot_no_login_window": "Вікно входу вже закрито",
    "robot_fake_login": "У підставному режимі вхід на Шафу не потрібен",
    "rehearse_started": "Пробна перевірка запускається — за хвилину відкриється окреме вікно Chrome. "
    "Нічого не публікується; не закривайте вікно, поки воно працює",
    "rehearse_busy": "Робот зайнятий (йде публікація чи інша справа) — пробна перевірка після неї",
    "rehearse_cannot": "Пробну перевірку зараз не запустити: немає Схвалених або робот ще запускається",
    "rehearse_fake": "У підставному режимі пробної перевірки немає",
    "rehearse_setup": "Пробну перевірку не запущено: неправильне налаштування кількості Карток",
    "health_started": "Перевіряю — результат з'явиться на панелі за кілька секунд",
    "health_running": "Перевірка вже йде",
    "health_wait": "Щойно перевіряли — зачекайте пів хвилини",
    "ai_started": "Шукаю нові пости й відправляю на ШІ — результат з'явиться на панелі",
    "ai_busy": "ШІ вже працює — зачекайте хвилину",
    "refill_started": "Картки пішли на ШІ пакетом — результати зазвичай до години, "
    "ваші правки збережуться",
    "ai_unavailable": "ШІ зараз недоступний — Картку поставлено в чергу; скажіть Власнику.",
    "recheck_done": "Картки оновлено за новими правилами, без ШІ — ваші правки збережено",
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
    "many_items": "У пості кілька пар чи розмірів — робот публікує лише одну пару на пост. Опублікуйте на Шафі вручну (і натисніть «Опублікувала вручну») або «В архів».",
    "size_gone": "Розмір {size}прибрали з поста. Продано — «В архів». Не продано — поверніть розмір у пост і «Перевірити ще раз».",
    "no_photos": "Додайте в папку фото .jpg.",
    "too_many_photos": "Залиште в папці не більше 10 фото.",
    "folder_missing": "Папку перейменовано або видалено.",
    "robot_field_missing": "Шафа, мабуть, змінила форму. «Повернути на перевірку» — виправте поля й схваліть знову; «Перевірити ще раз» — робот спробує ще раз без змін. Якщо повторюється — скажіть Власнику.",
    "ai_error": "ШІ не заповнило бланк. «Перевірити ще раз» поверне Картку в чергу на ШІ.",
    "publish_uncertain": "Робот натиснув «Додати річ», але не дочекався відповіді. Перевірте на Шафі, чи товар з'явився. Робот сам не повторює.",
    "possible_duplicate": "Схожий товар уже є на Шафі або цей пост уже опубліковано. Повторне оголошення заборонене правилами Шафи.",
    "shafa_declined": "Шафа перевірила оголошення вже після публікації й відхилила його. Відкрийте його на Шафі й виправте вручну або приберіть Картку в «В архів». Робот цей товар вдруге не публікує.",
    "publish_error": "Шафа не прийняла товар — що саме вона відповіла, написано вище. «Повернути на перевірку» — виправте Картку й схваліть знову. Якщо товар усе ж з'явився на Шафі — вставте посилання нижче.",
    "robot_logged_out": "Робот вийшов із Шафи. Натисніть на панелі «Увійти на Шафу», увійдіть як завжди й закрийте вікно. Потім «Перевірити ще раз».",
    "robot_brand_missing": "Бренд є у довіднику Шафи, але робот його не знайшов. «Перевірити ще раз» — робот спробує ще раз; «Повернути на перевірку» — перевірте бренд у Картці. Якщо повторюється — скажіть Власнику.",
    "robot_photos": "Не всі фото завантажились на Шафу. «Перевірити ще раз» — робот спробує ще раз; «Повернути на перевірку» — перевірте фото в Картці.",
    "photos_missing": "Фото поста не знайдено в папці. Додайте фото і натисніть «Перевірити ще раз». Якщо пару продано — «В архів».",
    "stale_rules": "Програму оновлено — Картку треба переглянути ще раз. «Перевірити ще раз» поверне її в «На перевірку», правки збережуться.",
    "robot_not_ready": "Картку не можна передати роботу (причина вище). «Перевірити ще раз» — робот спробує ще раз; «Повернути на перевірку» — виправте Картку.",
    "robot_not_sent": "Форма на Шафі не надіслалась (товар не створено). «Перевірити ще раз» — робот спробує ще раз; «Повернути на перевірку» — перевірте Картку. Якщо повторюється — скажіть Власнику.",
    "robot_publish_blocked": "Захист робота не пропустив запит публікації — товар не створено. Скажіть Власнику.",
    "robot_slow": "Шафа довго не відповідала. «Перевірити ще раз» — робот спробує ще раз.",
}
# UI-006: рядок Проблеми у списку — коротка мітка і що робити (довгий текст для Картки — `PROBLEM_HINTS`).
PROBLEM_KINDS = {
    "bad_folder_name": (
        "Назва папки",
        "перейменуйте папку на номер поста, потім «Перевірити ще раз»",
    ),
    "duplicate_post": ("Дві папки", "залиште одну папку для поста, потім «Перевірити ще раз»"),
    "post_not_found": ("Поста немає", "перевірте номер у назві папки, потім «Перевірити ще раз»"),
    "no_text": ("Нема тексту", "такий товар оформлюється вручну — відкрийте Картку"),
    "no_price": (
        "Нема ціни",
        "допишіть ціну в пост, зробіть новий Експорт і натисніть «Перевірити ще раз»",
    ),
    "many_items": (
        "Кілька пар у пості",
        "опублікуйте на Шафі вручну («Опублікувала вручну») або «В архів» — робот бере одну пару на пост",
    ),
    "size_gone": (
        "Розмір прибрано",
        "продано — «В архів»; не продано — поверніть розмір у пост і «Перевірити ще раз»",
    ),
    "no_section": (
        "Не взуття й не аксесуар",
        "опублікуйте на Шафі вручну («Опублікувала вручну») або «В архів»",
    ),
    "no_photos": ("Нема фото", "додайте в папку фото .jpg, потім «Перевірити ще раз»"),
    "too_many_photos": (
        "Забагато фото",
        "залиште в папці не більше 10 фото, потім «Перевірити ще раз»",
    ),
    "folder_missing": (
        "Нема папки",
        "папку перейменовано чи видалено — поверніть її, потім «Перевірити ще раз»",
    ),
    "ai_error": ("ШІ не заповнив", "«Перевірити ще раз» — Картка знову піде на ШІ"),
    "stale_rules": (
        "Старі правила",
        "«Перевірити ще раз» — Картка повернеться в «На перевірку», правки збережуться",
    ),
    "possible_duplicate": (
        "Схожий є на Шафі",
        "відкрийте Картку й порівняйте; якщо пару вже опублікували самі — «Опублікувала вручну»",
    ),
    "already_published": ("Уже опубліковано", "цей пост уже на Шафі — відкрийте Картку"),
    "publish_uncertain": ("Не впевнений", "перевірте «Мої речі» на Шафі, потім відкрийте Картку"),
    "publish_error": (
        "Шафа не прийняла при публікації",
        "відкрийте Картку: «Повернути на перевірку» й виправте",
    ),
    "shafa_declined": (
        "Шафа відхилила оголошення",
        "відкрийте Картку: виправте оголошення на Шафі або «В архів»",
    ),
    "photos_missing": (
        "Фото",
        "перевірте папку з фото, потім «Перевірити ще раз»; продано — «В архів»",
    ),
    "robot_photos": (
        "Фото",
        "«Перевірити ще раз» — робот спробує ще раз, або перевірте фото в Картці",
    ),
    "robot_logged_out": (
        "Нема входу на Шафу",
        "натисніть на панелі «Увійти на Шафу», потім «Перевірити ще раз»",
    ),
    "post_deleted": (
        "Пост видалено",
        "пост прибрано з каналу — схоже, продано; публікувати не треба",
    ),
}
ROBOT_KIND = (
    "Робот",
    "«Перевірити ще раз» — робот спробує знову; «Повернути на перевірку» — виправте поля",
)
OTHER_KIND = ("Проблема", "відкрийте Картку")
# Кнопки в рядку Проблеми — лише ті, що працюють на сервері: `publish.to_archive` приймає тільки ці
# коди (і всі `robot_…`, див. `publish.can_archive`).
ARCHIVE_CODES = publish.ARCHIVABLE
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

    ready: int = 0  # пройшли очікування, але ще НЕ відправлені на ШІ
    young: int = 0  # пост молодший за 7 днів
    pending: int = 0  # ШІ зараз заповнює
    wait_days: int = batch.WAIT_DAYS
    busy: str | None = None
    note: str | None = None
    refillable: int = 0  # Карток, які можна перезаповнити (CORE-008)
    recheckable: int = 0  # Карток за старшими правилами коду — перерахувати без ШІ (CORE-009)
    # UX-001: усе, що про пакет, — з бази, тож переживає перезапуск програми
    counters: list[tuple[str, str | None]] = field(default_factory=list)  # (текст, посилання)
    filling: str | None = None  # «ШІ заповнює 30 Карток (відправлено о 11:45)…»
    checking: str | None = None  # «Перевіряю, чи готово, кожні 3 хв…»
    finished: str | None = None  # «Готово о 12:21: 28 Карток у «На перевірку»…»
    held: str | None = None  # «705 чекають своєї черги: …»
    new_posts: int = 0  # пости бота, з яких ще немає Карток (потрібна кнопка)
    cost: str | None = None  # «≈ 1,36 $» — лише під «Детальніше»
    error: bool = False  # немає ключа, бюджет, збій — єдине, що фарбує кружок жовтим
    wait_text: str = ""  # «7 днів»


@dataclass(frozen=True)
class Outcome:
    """Чим закінчилась робота ШІ у фоні: що написати на панелі."""

    text: str | None = None
    error: bool = False
    keep: bool = False  # нічого нового — стару примітку не чіпаємо


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
        self.spawned_at: datetime | None = None  # UX-002: коли екран запустив процес робота
        # ШІ: одна робота за раз (Оновити, збір результатів, «спробуй ще раз») — без подвійних оплат.
        self.ai_client, self.run, self.clock, self.today = ai_client, run, clock, today
        self.ai_lock = threading.Lock()
        self.ai_busy: str | None = None
        self.ai_note: str | None = None
        self.ai_note_error = False
        self.ai_note_at: datetime | None = None
        self.collected_at: float | None = None
        self.collected_wall: datetime | None = None
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
        self.env.filters["card_name"] = card_name
        self.env.filters["plain"] = plain
        self.env.filters["names"] = names
        self.env.globals["archive_reason"] = archive_reason
        self.env.globals["plural"] = plural

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
                "ai": "warn" if ai_.error and not ai_.busy else "ok",
                "bot": BOT_TONES.get(bot.code, "warn"),
                "site": "warn" if site_ and site_[3] else "ok",
                "health": HEALTH_TONES.get(health_.code, "idle")
                if health_.code != "problems"
                else ("bad" if any(r.status == health.FAIL for r in health_.rows) else "warn"),
            },
        }  # fmt: skip

    def _counts(self, conn) -> dict:
        counts = review.counts(conn)
        counts["sessions"] = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        return counts

    def _chrome(self, conn, request: Request) -> dict:
        """Спільне для всіх сторінок: вкладки з лічильниками і панель робота."""
        counts = self._counts(conn)
        return {
            "tabs": TAB_TITLES,
            "counts": counts,
            **self._panel_context(conn),
            "can_quit": self.on_quit is not None,
            "undo": self._undo(conn, request),
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

    def _hm(self, stamp: str | datetime) -> str:
        """Година:хвилина місцевого часу (з ISO UTC з бази чи з `today()`)."""
        moment = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        return moment.astimezone(self.today().tzinfo).strftime("%H:%M")

    def _note(self, text: str | None, error: bool = False) -> None:
        self.ai_note, self.ai_note_error = text or None, error and bool(text)
        self.ai_note_at = self.today() if text else None

    def ai_state(self, conn) -> AiState:
        """Стан для панелі; заодно — збір готових пакетів раз на кілька хвилин (у фоні)."""
        self._maybe_collect(conn)
        note = f"{self._hm(self.ai_note_at)} · {self.ai_note}" if self.ai_note_at else None
        try:
            lim = batch.limits()
        except batch.SetupError as error:
            return AiState(busy=self.ai_busy, note=str(error), error=True)
        today = self.today()
        q = batch.queue(conn, today, lim.wait_days)
        active, done = batch.active(conn), batch.last_finished(conn)
        to_check = review.counts(conn)[Status.REVIEW]
        held = batch.held_back(conn, q.ready, lim)
        state = AiState(
            len(q.ready), q.young, active.cards if active else 0, lim.wait_days, self.ai_busy,
            note, len(fill.refill_candidates(conn)), len(fill.recheck_candidates(conn)),
            held=batch.held_text(held) if held else None,
            new_posts=conn.execute(
                "SELECT count(*) FROM posts p WHERE p.source = 'bot' "
                "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.post_id = p.post_id)"
            ).fetchone()[0],
            error=self.ai_note_error,
            wait_text=batch.days_word(lim.wait_days),
        )  # fmt: skip
        state.counters = [
            (text, href)
            for text, href in (
                (f"Чекають на ШІ: {state.ready}" if state.ready else None, None),
                (f"ШІ зараз заповнює: {state.pending}" if state.pending else None, None),
                (f"Готові до перевірки: {to_check}" if to_check else None, f"/{Status.REVIEW}"),
            )
            if text
        ]
        if active:
            since = datetime.fromisoformat(active.since)
            state.filling = (
                f"ШІ заповнює {events.cards(active.cards)} (відправлено о {self._hm(since)}). "
                f"Зазвичай готово до {self._hm(since + timedelta(hours=1))} — "
                "вони самі з'являться в «На перевірку», натискати нічого не треба."
            )
            every = f"Перевіряю, чи готово, кожні {COLLECT_EVERY // 60} хв"
            state.checking = (
                f"{every} · востаннє о {self._hm(self.collected_wall)} — ще не готово."
                if self.collected_wall
                else f"{every}."
            )
            state.cost = f"{batch.approx_usd(active.estimate)} (зазвичай менше)"
        elif done and datetime.fromisoformat(done.at) > today - FINISHED_SHOWN:
            parts = [f"{events.cards(done.ok)} у «На перевірку»"] if done.ok else []
            if done.problems:
                parts.append(f"{done.problems} — у «Проблеми»")
            if done.returned:
                parts.append(f"{events.cards(done.returned)} ШІ не встиг обробити — знову в черзі")
            state.finished = f"Готово о {self._hm(done.at)}: " + ", ".join(parts) + "."
        return state

    def _maybe_collect(self, conn) -> None:
        if not batch.open_batches(conn):
            return
        if self.collected_at is not None and self.clock() - self.collected_at < COLLECT_EVERY:
            return
        self._start(self._collect, "перевіряю, чи готові результати… (кілька секунд)")

    def _start(self, job: Callable[[], Outcome | None], busy: str) -> bool:
        """Запустити роботу ШІ у фоні; зайнято — False. Результат — примітка на панелі."""
        if not self.ai_lock.acquire(blocking=False):
            return False
        self.ai_busy = busy

        def work() -> None:
            try:
                outcome = job() or Outcome()
                if not outcome.keep:
                    self._note(outcome.text, outcome.error)
            except config.ConfigError:
                self._note(MESSAGES["ai_no_key"], True)
            except batch.SetupError as error:
                self._note(str(error), True)
            except Exception:  # noqa: BLE001 — фонова робота не валить сервер
                traceback.print_exc()
                self._note("ШІ: щось пішло не так — скажіть Власнику", True)
            finally:
                self.ai_busy = None
                self.ai_lock.release()

        self.run(work)
        return True

    def _checked(self) -> None:
        self.collected_at, self.collected_wall = self.clock(), self.today()

    def _collect(self) -> Outcome:
        self._checked()
        conn = self.connect()
        try:
            report = batch.collect(conn, self.ai_client(), self.today())
        finally:
            conn.close()
        text = batch.collect_problem_text(report)
        if text:
            return Outcome(text, error=bool(report.note))
        done = report.filled or report.errors or report.returned or report.lost
        return Outcome() if done else Outcome(keep=True)  # підсумок успіху панель бере з бази

    def _collect_and_send(self, scan_new: bool) -> Outcome:
        """Забрати готове й відправити новий пакет (перед цим — за потреби нові папки)."""
        self._checked()
        lim = batch.limits()
        conn = self.connect()
        try:
            if scan_new:
                scan(conn, {}, list_folders(self.root, self.channel_id))
            client = self.ai_client()
            report = batch.collect(conn, client, self.today())
            sent = batch.send(conn, client, self.root, self.today(), lim)
        finally:
            conn.close()
        texts = (batch.collect_problem_text(report), batch.sent_text(sent, lim, panel=True))
        return Outcome(
            "; ".join(t for t in texts if t),
            error=bool(sent.error or sent.plan.over_budget or report.note),
        )

    def _update(self) -> Outcome:
        """«Знайти нові пости і відправити на ШІ»: нові папки (тексти постів — з бази) → забрати
        готове → новий пакет."""
        return self._collect_and_send(scan_new=True)

    def ai_post(self, request: Request) -> Response:
        action = (request.form.get("action") or [""])[0]
        back = safe_back((request.form.get("back") or [""])[0])
        sep = "&" if "?" in back else "?"
        if action == "recheck":
            return redirect(f"{back}{sep}msg={self.recheck()}")
        if action != "update":
            return text(400, "Невідома дія")
        started = self._start(self._update, "шукаю нові пости й відправляю…")
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

    def _send_refill(self) -> Outcome:
        """Відправити позначені для перезаповнення (нові папки не шукаємо) — пакетом."""
        return self._collect_and_send(scan_new=False)

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
            message = self._refill_message(conn)
        finally:
            conn.close()
        return redirect(f"/{Status.REVIEW}?msg={message}")

    def ai_unavailable(self, conn) -> bool:
        """Нема ключа ШІ чи вичерпано бюджет на добу — пакет зараз не піде (B3, UX-004)."""
        try:
            self.ai_client()
            return batch.budget_left(conn, self.today(), batch.limits()) <= 0
        except (config.ConfigError, batch.SetupError):
            return True

    def _refill_message(self, conn) -> str:
        """Відправити на ШІ пакетом і сказати правду: недоступний ШІ — не «пішли на ШІ»."""
        if self.ai_unavailable(conn):
            return "ai_unavailable"
        return (
            "refill_started" if self._start(self._send_refill, "відправляю на ШІ…") else "ai_busy"
        )

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
                self._note(str(error), True)
                return "ai_settings"
            except config.ConfigError:
                return "ai_no_key"
            if planned.estimate.cost_micro_usd > left:
                return "ai_budget"
            result = fill.retry(conn, client, planned)
            if result.error:
                self._note(f"«ШІ, спробуй ще раз» ({planned.folder_name}): {result.error}", True)
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
        state = control.state(
            conn, self.lock_file, starting=starting, failed=failed, spawned=self.spawned_at,
            never_logged=not self.fake_shafa and not _logged_in(),
        )  # fmt: skip
        if state.code in ("failed", "crashed") and failed:
            state.details.append(
                last_log_line(control.log_path())
            )  # сирий рядок — лише в «Детальніше»
        return state

    def panel(self, request: Request) -> Response:
        conn = self.connect()
        try:
            template = "panel_mini.html" if request.arg("mini") else "panel.html"
            html = self.env.get_template(template).render(
                token=self.token, local=local_time, back=safe_back(request.arg("back")),
                fake=self.fake_shafa, counts=self._counts(conn), **self._panel_context(conn),
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
                    why = control.WHY_CODES.get(state.why_code)
                    return redirect(f"{back}{sep}msg=robot_cannot_start{'_' + why if why else ''}")
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
        self.spawned_at = datetime.now().astimezone()

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
            chosen = self._chosen(status, request.arg("f"))
            kind = self._kind(status, request.arg("k"))
            everything = cards
            filters, kinds, cards = self._groups(status, cards, marks, chosen, kind, query)
            empty_note = (
                self._empty_note(status, everything, marks, chosen, kind, query)
                if not cards
                else None
            )
            on_shafa = {  # Картка з `shafa_id` з Архіву не повертається (як на самій Картці)
                r["id"]: no_restore_reason(r["problem_code"], r["shafa_id"])
                for r in conn.execute(
                    "SELECT id, problem_code, shafa_id FROM items WHERE shafa_id IS NOT NULL "
                    f"OR problem_code IN ({', '.join('?' * len(review.NO_RESTORE))})",
                    review.NO_RESTORE,
                )
            }  # CORE-017: «кілька товарів» / «нема розділу» теж не повертаються
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
                kinds=kinds,
                kind=kind,
                is_old=is_old,
                reset_url=f"/{status}"
                + (
                    f"?{urlencode({n: v for n, v in (('f', chosen), ('k', kind)) if v})}"
                    if chosen or kind
                    else ""
                ),
                chosen=chosen,
                query=query,
                message=self._message(conn, request),
                deletion_date=review.deletion_date,
                start_hint=self.start_hint(conn) if status == Status.APPROVED else "",
                empty_note=empty_note,
            )
        finally:
            conn.close()

    def _message(self, conn, request: Request) -> str | None:
        """Повідомлення зверху за `msg`; `n` — скільки пропущених, `ok` — яку Картку схвалено (UX-003).

        Кінець черги додає, що робити далі: «Натисніть «Старт» — сьогодні робот опублікує до N із M»."""
        key = request.arg("msg")
        text_ = MESSAGES.get(key)
        if text_ is None:
            return None
        if "{n}" in text_:
            count = request.arg("n")
            text_ = text_.replace("{n}", count if count.isdigit() else "кілька")
        if "{name}" in text_:
            approved = request.arg("ok")
            card = review.load_card(conn, int(approved)) if approved.isdigit() else None
            text_ = text_.replace("{name}", card_name(card) if card else "Картку")
        if key in ("approved_done", "rejected_done") and (hint := self.start_hint(conn)):
            text_ = f"{text_} {hint}"
        return text_

    def start_hint(self, conn) -> str:
        """«Натисніть «Старт» — сьогодні робот опублікує до N із M Схвалених.» (те саме N, що в панелі
        робота), порожньо, коли робота зараз не запустити чи публікувати нічого."""
        state = self.robot_state(conn)
        if not state.can_start or not state.queue_today:
            return ""
        return (
            f"Натисніть «Старт» — сьогодні робот опублікує до {state.queue_today} "
            f"із {state.approved} Схвалених."
        )

    def _precheck(self, card: review.Card) -> Precheck:
        """Що скаже робот про Картку (QA-002) — лише «На перевірку» і «Схвалені»."""
        if card.status not in (Status.REVIEW, Status.APPROVED):
            return Precheck(None, [])
        return precheck(card, self.root)

    def _empty_note(self, status, cards, marks, chosen, kind, query):
        """Порожній відфільтрований список, а в «На перевірку» ще є Картки (B2, UX-003): що лишилось і
        куди йти. `(текст, посилання, підпис посилання)` або None."""
        if status != Status.REVIEW or query or not (chosen or kind) or not cards:
            return None

        def href(f: str, k: str) -> str:
            args = {n: v for n, v in (("f", f), ("k", k)) if v}
            return f"/{status}" + (f"?{urlencode(args)}" if args else "")

        if chosen:
            other = "ok" if chosen == "warn" else "warn"
            left = len(self._filtered(cards, marks, other, kind))
            if left:
                waits = "чекає" if left % 10 == 1 and left % 100 != 11 else "чекають"
                if chosen == "warn":
                    lead, what = "Жовтих більше немає.", f"без жовтого {waits} на «Схвалити»"
                else:
                    lead, what = "Карток без жовтого немає.", f"з жовтим {waits}"
                note = f"{lead} Ще {plural(left, 'Картка', 'Картки', 'Карток')} {what}."
                return note, href(other, kind), "Переглянути їх"
        left = len(cards)
        return (
            f"У цьому списку більше нічого немає. Ще {plural(left, 'Картка', 'Картки', 'Карток')} на перевірці.",
            href("", ""),
            "Показати всі",
        )

    @staticmethod
    def _chosen(status: str, value: str) -> str:
        """Фільтр списку: лише в «На перевірку», лише `warn`/`ok`."""
        return value if status == Status.REVIEW and value in ("warn", "ok") else ""

    @staticmethod
    def _kind(status: str, value: str) -> str:
        """UI-008: «Нові (бот) / Старі» — `new`/`old`, у «На перевірку» і «Схвалених»."""
        return value if status in KIND_TABS and value in ("new", "old") else ""

    @staticmethod
    def _filtered(
        cards: list[review.Card], marks: dict, chosen: str, kind: str = ""
    ) -> list[review.Card]:
        if chosen:
            cards = [c for c in cards if (marks[c.id].tone != "ok") == (chosen == "warn")]
        if kind:
            cards = [c for c in cards if is_old(c.folder_name) == (kind == "old")]
        return cards

    def _groups(self, status, cards, marks, chosen, kind, query):
        """Пілюлі обох груп (UI-004 `f`, UI-008 `k`) і відфільтрований список.

        Лічильник кожної групи рахується з урахуванням вибору в іншій і пошуку `q`."""
        filters, kinds = [], []

        def href(f: str, k: str) -> str:
            args = {n: v for n, v in (("f", f), ("k", k), ("q", query)) if v}
            return f"/{status}" + (f"?{urlencode(args)}" if args else "")

        if status == Status.REVIEW:
            base = self._filtered(cards, marks, "", kind)
            warn = sum(1 for c in base if marks[c.id].tone != "ok")
            for key, label, count in (
                ("", "Усі", len(base)),
                ("warn", "Треба глянути", warn),
                ("ok", "Без жовтого", len(base) - warn),
            ):
                filters.append((key, label, count, href(key, kind)))
        if status in KIND_TABS:
            base = self._filtered(cards, marks, chosen)
            old = sum(1 for c in base if is_old(c.folder_name))
            for key, label, count in (
                ("", "Усі", len(base)),
                ("new", "З бота", len(base) - old),
                ("old", "Старі пости", old),
            ):
                kinds.append((key, label, count, href(chosen, key)))
        return filters, kinds, self._filtered(cards, marks, chosen, kind)

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

    def _list_ids(self, conn, status: Status, from_: str) -> list[int]:
        """Картки списку `from_` у порядку списку: з тим самим фільтром і пошуком, що бачить вона."""
        query = parse_qs(urlsplit(from_).query)
        text_, chosen, kind = (
            (query.get("q") or [""])[0],
            self._chosen(status, (query.get("f") or [""])[0]),
            self._kind(status, (query.get("k") or [""])[0]),
        )
        cards = review.list_tab(conn, status, text_)
        if chosen or kind:
            marks = {c.id: card_mark(c, self._precheck(c)) for c in cards} if chosen else {}
            cards = self._filtered(cards, marks, chosen, kind)
        return [c.id for c in cards]

    def _nav(self, conn, card: review.Card, from_: str) -> "Nav":
        """Верх Картки: куди «назад», «3 з 8» і яка Картка наступна — у тому ж списку (фільтр, пошук)."""
        status = Status(urlsplit(from_).path.strip("/"))
        nav = Nav(from_, TAB_TITLES[status])
        if status not in CARD_TABS:  # «Опубліковані» без порядку й фільтрів
            return nav
        ids = self._list_ids(conn, status, from_)
        if card.id in ids:
            at = ids.index(card.id)
            nav.index, nav.total = at + 1, len(ids)
            following = ids[at + 1 : at + 2]
            if at:
                nav.prev_url = item_url(ids[at - 1], from_)
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
                "SELECT duplicate_url, duplicate_name, shafa_url, shafa_id, problem_code FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            for name in bad:  # помилкове значення лишається в полі, як вона його ввела
                typed = request.query.get(f"v_{name}")
                if typed:
                    shown = [t[:200] for t in typed][: len(card.fields[name].inputs)]
                    card.fields[name] = replace(
                        card.fields[name], inputs=[*shown, *card.fields[name].inputs[len(shown) :]]
                    )
            check = self._precheck(card)
            acts = problem_actions(card.problem_code, card.folder_name)
            from_ = self._from(request, card)
            block = card.approve_block() if card.status == Status.REVIEW else None
            todo = approve_todo(card, block, check.error) if card.status == Status.REVIEW else None
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
                has_shafa_id=row["shafa_id"] is not None
                or row["problem_code"] in review.NO_RESTORE,
                my_items=MY_ITEMS,
                sticky=STICKY,
                sticky_titles=STICKY_TITLES,
                current=card.status,
                card=card,
                bad=bad,
                bad_input=review.BAD_INPUT,
                message=self._message(conn, request),
                todo=todo,
                sections=catalog.sections(),
                hint=problem_hint(card),
                problem_archive=acts.archive,
                problem_recheck=acts.recheck,
                problem_to_review=acts.to_review,
                problem_manual=acts.manual,
                confirm_archive=card.problem_code in review.NO_RESTORE,
                waiting=self._waiting(conn, card),
                no_restore_reason=(
                    no_restore_reason(card.problem_code, row["shafa_id"])
                    if card.status == Status.ARCHIVED
                    else None
                ),
                deletion_date=review.deletion_date,
                editable=card.status == Status.REVIEW,
                nav=self._nav(conn, card, from_),
                layout=card_layout(card, bad, block),
                attention=attention(card),
                post_price=rules.price_from_text(card.post_text) if card.post_text else None,
                price_hint=price_hint(card),
                insole_hint=insole_hint(card),
                gender=gender_buttons(card),
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

            if action in ("save", "approve", "folder_order") or action.startswith(
                ("confirm:", "gender:")
            ):
                form, confirm = dict(request.form), action.removeprefix("confirm:")
                if action == "folder_order":  # порядок фото як у папці — і це підтвердження
                    by_position = sorted(card.photos, key=lambda p: p.position)
                    form["photo_order"] = ["\n".join(p.file_name for p in by_position)]
                    confirm = "photo_order"
                if action.startswith(
                    "gender:"
                ):  # «Це жіночі / Це чоловічі» — правка розділу людиною
                    target = review.gender_target(card.path, action.removeprefix("gender:"))
                    if target is None or card.status != Status.REVIEW:
                        return redirect(back(msg="stale"))
                    form["section"], confirm = [target], "section"
                bad = review.save(conn, card, form, confirm or None)
                if bad:
                    typed = {f"v_{n}": form[n] for n in bad if n in form}
                    return redirect(back(msg="bad", bad=bad, **typed))
                if action != "approve":
                    fresh = review.load_card(conn, item_id)
                    empty = fresh is not None and not fresh.fields["price"].value
                    return redirect(back(msg="saved_no_price" if empty else "saved"))
            sure = (request.form.get("sure") or [""])[0] == "1"
            if action == "to_archive" and card.problem_code in review.NO_RESTORE and not sure:
                return self._confirm(
                    conn, request, card, action, from_, stay,
                    "Картку не можна буде повернути. Точно в архів?",
                    "Так, в архів", "Ні, назад",
                )  # fmt: skip
            if action in review.ACTIONS:
                message = review.act(conn, item_id, action, self.root, confirmed=sure)
                if message == review.CONFIRM_RESTORE:  # B10: продану пару — лише після питання
                    return self._confirm(
                        conn, request, card, action, from_, stay,
                        "Цей пост видалено з каналу — пару, мабуть, продано. Повернути все одно?",
                        "Так, повернути", "Ні, лишити в Архіві",
                    )  # fmt: skip
                if message == review.PRECHECK:
                    fresh = review.load_card(conn, item_id)
                    if fresh is not None and not fresh.fields["price"].value:
                        message = "no_price"
                if message in (*review.APPROVE_BLOCKS, "no_price", "stale", review.ON_SHAFA):
                    return redirect(back(msg=message))
                if from_ is None:  # старий виклик без списку
                    return redirect(f"/{card.status}?msg={message}")
                if message == "unapproved":  # лишаємось на Картці — поля знову активні
                    return redirect(back(msg=message))
                if message in ("approved", "rejected"):  # «Схвалити і далі» / «Відхилити»
                    if following and message == "approved":
                        return redirect(with_params(following, msg="approved_next", ok=item_id))
                    if following:
                        return redirect(with_params(following, msg=message, ok=item_id))
                    return redirect(self._queue_end(conn, from_, message, item_id))
                return redirect(with_msg(from_, message))
            decisions = {
                "on_shafa": lambda: publish.on_shafa(
                    conn, self.root, item_id, (request.form.get("shafa_link") or [""])[0]
                ),
                "not_on_shafa": lambda: publish.not_on_shafa(conn, item_id),
                "other_item": lambda: publish.other_item(conn, item_id),
                "to_archive": lambda: publish.to_archive(conn, item_id),
                "to_review": lambda: "to_review" if review.to_review(conn, item_id) else "stale",
            }
            if action in decisions:
                message = decisions[action]()
                if message in ("stale", "bad_link", "listing_found"):
                    return redirect(back(msg=message))
                if message == "to_review":
                    return redirect(f"/{Status.REVIEW}?msg={message}")
                return redirect(f"/{Status.PROBLEM}?msg={message}")
            if action == "refill":
                if not fill.mark_refill(conn, [item_id]):
                    return redirect(back(msg="stale"))
                return redirect(f"/{Status.REVIEW}?msg={self._refill_message(conn)}")
            if action == "ai_retry":
                message = self.ai_retry(conn, item_id)
                return redirect(back(msg=message))
            if action == "recheck":
                result = review.recheck(conn, item_id, self.root, self.channel_id)
                if result.code in ("photos_retry", "recheck_done"):
                    return redirect(f"/{Status.REVIEW}?msg={result.code}")
                if result.code in ("robot_retry", "recheck_new"):
                    return redirect(f"/{Status.PROBLEM}?msg={result.code}")
                if result.code == "recheck_problem":  # текст за кодом проблеми, що лишилась (B26)
                    left = card.problem_code if card.problem_code in review.NO_RESTORE else None
                    return redirect(back(msg=f"recheck_{left}" if left else result.code))
                return redirect(back(msg=result.code))
            return text(400, "Невідома дія")
        finally:
            conn.close()

    def _confirm(
        self, conn, request: Request, card, action: str, from_: str | None, stay: bool,
        question: str, yes: str, no: str,
    ) -> Response:  # fmt: skip
        """Сторінка-підтвердження незворотного (B10, B5/B14): без JS, та сама форма з `sure=1`."""
        back = from_ or item_url(card.id)
        return self.page(
            "confirm.html",
            **self._chrome(conn, request),
            current=None,
            message=None,
            card=card,
            action=action,
            question=question,
            yes=yes,
            no=no,
            from_=from_ or "",
            stay=bool(stay),
            back_url=back,
            reject=action == "to_archive",
        )

    def _waiting(self, conn, card: review.Card) -> str | None:
        """Картка `new` — її немає у вкладках; чому і до коли (B3, UX-004)."""
        if card.status != Status.NEW:
            return None
        row = conn.execute(
            """
            SELECT b.created_at FROM ai_batch_items i JOIN ai_batches b ON b.id = i.batch_id
            WHERE i.item_id = ? AND i.state = 'pending'
              AND b.status IN ('sending', 'uncertain', 'in_progress')
            ORDER BY b.id DESC LIMIT 1
            """,
            (card.id,),
        ).fetchone()
        if row is not None:
            sent = datetime.fromisoformat(row[0]).astimezone(self.today().tzinfo)
            return (
                f"Ця Картка чекає на ШІ (відправлено {sent:%d.%m} о {sent:%H:%M}). Зазвичай до години — "
                "потім з'явиться в «На перевірку». Правити зараз не можна."
            )
        if self.ai_unavailable(conn):
            return MESSAGES["ai_unavailable"] + " Правити зараз не можна."
        return (
            "Ця Картка чекає черги на ШІ — він візьме її, коли настане її день. "
            "Правити зараз не можна."
        )

    def _undo(self, conn, request: Request) -> int | None:
        """«Картку відкладено в Архів… [Повернути]»: id Картки, яку ще можна повернути."""
        key, item = request.arg("msg"), request.arg("ok")
        if not key.startswith("rejected") or not item.isdigit():
            return None
        row = conn.execute(
            "SELECT status, shafa_id FROM items WHERE id = ? AND purged_at IS NULL", (int(item),)
        ).fetchone()
        if row is None or row["status"] != Status.ARCHIVED or row["shafa_id"] is not None:
            return None
        return int(item)

    def _queue_end(self, conn, from_: str, message: str, item_id: int) -> str:
        """Далі не було: чесно сказати, що лишилось (B1, UX-003). Пропущені Картки цього ж списку →
        перша з них (по колу); немає — «більше нічого немає» і що робити далі."""
        review_list = from_ if urlsplit(from_).path == f"/{Status.REVIEW}" else f"/{Status.REVIEW}"
        undo = {"ok": item_id} if message == "rejected" else {}  # для «[Повернути]» (UX-004)
        left = self._list_ids(conn, Status.REVIEW, review_list)
        if left:
            return with_params(
                item_url(left[0], review_list), msg=f"{message}_left", n=len(left), **undo
            )
        if review.list_tab(
            conn, Status.REVIEW
        ):  # у списку з фільтром порожньо, а на перевірці ще є
            return with_params(review_list, msg=message, **undo)
        return with_params(review_list, msg=f"{message}_done", **undo)

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
                dups={
                    r["id"]: shafa_link(r["duplicate_url"])
                    for r in conn.execute(
                        "SELECT id, duplicate_url FROM items WHERE status = ? AND problem_code = ?",
                        (Status.PUBLISHED, "duplicate_checked"),
                    )
                },
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
                reason_of=control.reason_of,
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
                reason_of=control.reason_of,
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
KIND_TABS = (Status.REVIEW, Status.APPROVED)  # UI-008: тут працює фільтр «Нові / Старі»


def item_url(item_id: int, from_: str | None = None) -> str:
    return f"/item/{item_id}" + (f"?{urlencode({'from': from_})}" if from_ else "")


def card_url(item_id: int, from_: str | None = None, **params) -> str:
    """Адреса Картки з повідомленням; `from` лише коли форма його принесла (UI-005)."""
    args = ({"from": from_} if from_ else {}) | params
    return f"/item/{item_id}" + (f"?{urlencode(args, doseq=True)}" if args else "")


def with_msg(url: str, message: str) -> str:
    return with_params(url, msg=message)


def with_params(url: str, **params) -> str:
    return f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"


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
    if name in KIND_TABS and (query.get("k") or [""])[0] in ("new", "old"):
        args["k"] = query["k"][0]
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
    prev_url: str | None = None


MAIN_ROWS = (  # поля, які правлять найчастіше: у рядку їх стільки, скільки в кортежі
    ("title",), ("price", "sizes"), ("section",), ("brand", "condition"), ("colors",),
    ("season",), ("material_upper",),
)  # fmt: skip
MAIN_FIELDS = {name for row in MAIN_ROWS for name in row}
MISSING = {  # чому не можна схвалити → яке поле порожнє (UX-003: жовтий чи чужий розділ — не порожнє)
    "no_section": "section", "no_sizes": "sizes", "no_colors": "colors", "no_gender": "gender",
}  # fmt: skip
BLOCK_TODO = {  # чому не можна схвалити → що зробити, одним пунктом
    "no_section": "вкажіть розділ",
    "section_unknown": "оберіть розділ зі списку Шафи",
    "section_yellow": "перевірте розділ",
    "no_sizes": "вкажіть розмір",
    "babies_size": "оберіть «Для дівчаток» чи «Для хлопчиків» (малюки — лише до 22 розміру)",
    "no_colors": "вкажіть колір",
    "no_gender": "вкажіть стать",
}
NO_PRICE_ERROR = "Немає ціни"  # `robot.plan`: те саме, що «вкажіть ціну»


def approve_todo(card: review.Card, block: str | None, robot_error: str | None) -> str | None:
    """Усе, що заважає схвалити, одним блоком: «Щоб схвалити: 1) вкажіть ціну; 2) перевірте розділ»."""
    items = []
    if not card.fields["price"].value:
        items.append("вкажіть ціну")
    if block in BLOCK_TODO:
        items.append(BLOCK_TODO[block])
    if robot_error and robot_error != NO_PRICE_ERROR:
        items.append(f"робот не зможе опублікувати — {robot_error[:1].lower()}{robot_error[1:]}")
    if not items:
        return None
    if len(items) == 1:
        return f"Щоб схвалити: {items[0]}"
    return "Щоб схвалити: " + "; ".join(f"{i}) {item}" for i, item in enumerate(items, 1))


def card_name(card: review.Card | None) -> str:
    """Назва Картки для екрана: без слів «папка» і «borh» (UX-003)."""
    title = card.fields["title"].value if card else None
    if title:
        return title
    if card is not None and card.post_id:
        return f"Пост {card.post_id} (назви ще немає)"
    return "Картка без назви"


def price_hint(card: review.Card) -> str | None:
    """«У пості 1700 грн → на Шафі +15 % = 1960»."""
    posted = rules.price_from_text(card.post_text) if card.post_text else None
    if not posted:
        return None
    return f"У пості {posted} грн → на Шафі +15 % = {rules.shafa_price(posted)}"


def insole_hint(card: review.Card) -> str | None:
    """«Устілка: у пості 25,3 см (на Шафі: 25 і 25,5)»."""
    measured = rules.insole_measured(card.post_text or "")
    values = card.fields["insole"].value
    if not measured:
        return None
    shown = measured.replace(".", ",")
    on_shafa = " і ".join(f"{v:g}".replace(".", ",") for v in values or [])
    return f"Устілка: у пості {shown} см" + (f" (на Шафі: {on_shafa})" if on_shafa else "")


@dataclass
class GenderButtons:
    """Жовтий розділ про стать (UX-003): питання, коротка причина, повна причина, кнопки."""

    question: str
    short: str
    full: str | None  # показуємо під «Чому?», лише коли довша за перше речення
    choices: list[review.GenderChoice]


FIRST_SENTENCE = re.compile(r"^.+?[.!?](?=\s|$)", re.DOTALL)


def first_sentence(text_: str) -> str:
    found = FIRST_SENTENCE.match(text_.strip())
    return found[0] if found else text_.strip()


def gender_buttons(card: review.Card) -> GenderButtons | None:
    """Кнопки «Це жіночі / Це чоловічі» біля жовтого розділу, якщо сумнів саме про це і пара є в каталозі."""
    section = card.fields["section"]
    if card.status != Status.REVIEW or not section.yellow or not section.reason:
        return None
    if not ai.GENDER_DOUBT.search(section.reason):
        return None
    found = review.gender_choices(card.path)
    if found is None:
        return None
    short = first_sentence(section.reason)
    return GenderButtons(
        found[0], short, section.reason if short != section.reason.strip() else None, found[1]
    )


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
    if card.status == Status.REVIEW and not card.fields["price"].value:
        missing.add("price")
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
    gender = gender_buttons(card)

    def reason(f: review.FieldView) -> str:
        if gender and f.spec.name == "section":  # довга причина ШІ — під «Чому?» біля поля
            return gender.short
        return f.reason or "перевірте"

    lines = [f"{short_label(f)}: {reason(f)}" if len(yellow) > 1 else reason(f) for f in yellow[:2]]
    if len(yellow) > 2:
        lines.append(f"Ще {len(yellow) - 2} — у полях нижче")
    names = ", ".join(short_label(f) for f in yellow)
    title = f"Перевір {plural(len(yellow), 'поле', 'поля', 'полів')}: {names}"
    if gender and len(yellow) == 1:
        title = gender.question
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
    details: str  # повний текст причини, коли `why` — коротка фраза чи з неї прибрано службове
    archive: bool
    recheck: bool
    my_items: bool
    to_review: bool = False  # UX-004: «Повернути на перевірку (виправити поля)»
    manual: bool = False  # UX-004: «Опублікувала вручну» — посилання на товар
    confirm_archive: bool = False  # «В архів» без повернення — спершу підтвердження


class Actions(NamedTuple):
    archive: bool
    recheck: bool
    to_review: bool = False
    manual: bool = False


def problem_actions(code: str | None, folder_name: str | None = None) -> Actions:
    """Кнопки Проблеми — ті самі на Картці (`card.html`) і в списку, лише робочі на сервері:
    `publish_uncertain`/`publish_error` вирішуються посиланням на Картці, решта STICKY — без повтору.
    Старі пости (`borh-…`): «Перевірити ще раз» немає — перевіряти нема що (папки немає, B26)."""
    code = code or ""
    old = bool(folder_name) and is_old(folder_name)
    if code == "publish_error":
        return Actions(False, False, to_review=True)  # «На Шафі є» — своя форма на Картці
    if code in LINK_CODES:
        return Actions(False, False)
    manual = publish.can_publish_by_hand(code)
    if code == "robot_logged_out":
        return Actions(False, True)
    if code.startswith("robot_"):
        return Actions(True, True, to_review=True, manual=manual)
    if (
        code in review.NO_RESTORE
    ):  # CORE-017: і в архів, і «Перевірити ще раз» (пост могли виправити)
        return Actions(True, not old, manual=manual)
    if code in ARCHIVE_CODES:
        return Actions(True, code in ("size_gone", "photos_missing") and not old, manual=manual)
    return Actions(False, True)


# Службове в тексті причини — не для неї: назва файла скриншота.
_SCREEN = re.compile(r"\s*(?:Екран|Скриншот):\s*\S+\.png\.?")
_OLD_FOLDER = re.compile(r"(?i)\b(?:папк\w*\s+)?borh-(\d+)")


def no_restore_reason(code: str | None, shafa_id) -> str | None:
    """Чому Картку з Архіву не можна повернути на перевірку (B11)."""
    if shafa_id is not None:
        return "товар уже є на Шафі — другий раз публікувати не можна"
    if code in review.NO_RESTORE:
        return "робот не публікує такі пости (кілька пар чи не взуття) — опублікуйте вручну"
    return None


def archive_reason(card: review.Card) -> str:
    """Чому Картка в Архіві — для неї, без службових слів."""
    if card.problem_code == review.POST_DELETED:
        return "пост видалено з каналу — пару, мабуть, продано"
    if card.problem_text and card.problem_code in ("duplicate", "many_items", "no_section"):
        return plain(card.problem_text).rstrip(".")
    return "ви вирішили не публікувати" if not card.problem_code else "Картка не піде на Шафу"


def names(text_: str | None) -> str:
    """«Папка borh-3659» → «Старий пост 3659» (C29)."""
    return _OLD_FOLDER.sub(lambda m: f"Старий пост {m[1]}", text_ or "")


def plain(text_: str | None) -> str:
    """Текст причини для неї: без назви скриншота й без «borh»."""
    text_ = (text_ or "").replace(OLD_NO_SECTION_TEXT, NO_SECTION_TEXT)
    return names(_SCREEN.sub("", text_).strip())


def problem_hint(card: review.Card) -> str | None:
    """Довгий текст «що сталось і що робити» для Картки-Проблеми."""
    hint = PROBLEM_HINTS.get(card.problem_code or "")
    if hint and "{size}" in hint:
        found = re.search(r"Розмір (\S+) прибрано", card.problem_text or "")
        hint = hint.replace("{size}", f"{found[1]} " if found else "")
    return hint


def problem_row(card: review.Card) -> ProblemRow:
    code = card.problem_code or ""
    label, todo = PROBLEM_KINDS.get(code) or (
        ROBOT_KIND if code.startswith("robot_") else OTHER_KIND
    )
    short = STICKY_TITLES.get(code)
    full = names(card.problem_text)
    text_ = plain(card.problem_text)
    acts = problem_actions(code, card.folder_name)
    return ProblemRow(
        label=label,
        why=short or text_,
        todo=todo,
        details=full if text_ and ((short and text_ != short) or text_ != full) else "",
        archive=acts.archive,
        recheck=acts.recheck,
        my_items=code in LINK_CODES,
        to_review=acts.to_review,
        manual=acts.manual,
        confirm_archive=code in review.NO_RESTORE,
    )


CALM_STOPS = ("done", "limit", "stop")  # сесія закінчилась як задумано


def session_tone(row) -> str:
    """Колір сесії: `run` — працює, `ok` — спокійний кінець, `warn` — зупинилась з причини, `idle` — без причини."""
    if row["ended_at"] is None:
        return "run"
    if row["stop_code"] in CALM_STOPS:
        return (
            "warn" if row["problems"] else "ok"
        )  # UX-002: частина пішла в Проблеми — не «все добре»
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


def _logged_in() -> bool:
    """Профіль робота вже є (вхід на Шафу колись робили) — UX-002, кнопка «Увійти на Шафу»."""
    profile = browser.profile_dir()
    try:
        return profile.is_dir() and any(profile.iterdir())
    except OSError:
        return False


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
