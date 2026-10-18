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

from shafa import ai, batch, catalog, config, db, fill, options, photos, review, site
from shafa.db import Status
from shafa.export import parse_export
from shafa.folders import PUBLISHED_DIR, is_old, item_folder, list_folders, old_root
from shafa.robot import browser, control, lock, publish
from shafa.robot import listings as listings_mod
from shafa.scan import STICKY, rescan_folder, scan

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
}
MESSAGES = {
    "saved": "Збережено",
    "approved": "Схвалено",
    "rejected": f"Відхилено — Картка в Архіві {review.ARCHIVE_DAYS} днів",
    "unapproved": "Схвалення скасовано — Картка знову на перевірці",
    "restored": "Повернуто на перевірку",
    "section_yellow": "Схвалити не можна: розділ жовтий — змініть його або натисніть «Так, вірно»",
    "no_section": "Схвалити не можна: немає розділу",
    "section_unknown": "Схвалити не можна: такого розділу немає на Шафі — оберіть зі списку",
    "no_sizes": "Схвалити не можна: немає розміру",
    "no_colors": "Схвалити не можна: немає кольору",
    "no_gender": "Схвалити не можна: для малюків Шафа вимагає «Стать»",
    "babies_size": "Схвалити не можна: «Для малюків» — лише до 22 розміру, оберіть «Для дівчаток» чи «Для хлопчиків»",
    "bad": "Не збережено поля, позначені червоним",
    "stale": "Картку вже змінено — сторінку оновлено",
    "robot_retry": "Картку повернуто в Схвалені — робот спробує ще раз",
    "recheck_new": "Папку перевірено: усе добре, Картка чекає на ШІ",
    "recheck_problem": "Папку перевірено: проблема лишилась",
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
    "many_items": "У пості кілька товарів — такий пост оформлюється вручну.",
    "no_photos": "Додайте в папку фото .jpg.",
    "too_many_photos": "Залиште в папці не більше 10 фото.",
    "folder_missing": "Папку перейменовано або видалено.",
    "robot_field_missing": "Шафа, мабуть, змінила форму. Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "ai_error": "ШІ не заповнило бланк. «Перевірити ще раз» поверне Картку в чергу на ШІ.",
    "publish_uncertain": "Робот натиснув «Додати річ», але не дочекався відповіді. Перевірте на Шафі, чи товар з'явився. Робот сам не повторює.",
    "possible_duplicate": "Схожий товар уже є на Шафі або цей пост уже опубліковано. Повторне оголошення заборонене правилами Шафи.",
    "publish_error": "Шафа не прийняла товар (текст помилки вище). Перевірте Шафу й скажіть Власнику.",
    "robot_logged_out": "Робот не увійшов на Шафу. Власник: python -m shafa robot --login. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_brand_missing": "Бренд є у довіднику Шафи, але робот його не знайшов. Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_photos": "Не всі фото завантажились на Шафу. «Перевірити ще раз» поверне Картку в Схвалені.",
    "stale_rules": "Картку схвалено за старими правилами. «Перевірити ще раз» перерахує її правилами (без ШІ) і поверне на перевірку: перегляньте й схваліть знову.",
    "robot_not_ready": "Картку не можна передати роботу (текст вище). «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_not_sent": "Форма на Шафі не надіслалась (товар не створено). Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_publish_blocked": "Захист робота не пропустив запит публікації — товар не створено. Скажіть Власнику.",
}

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
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(PACKAGE / "templates"),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

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

    def _chrome(self, conn, request: Request) -> dict:
        """Спільне для всіх сторінок: вкладки з лічильниками і панель робота."""
        counts = review.counts(conn)
        counts["sessions"] = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        return {
            "tabs": TAB_TITLES,
            "counts": counts,
            "robot": self.robot_state(conn),
            "ai": self.ai_state(conn),
            "bot": self.bot_state(conn),
            "site": self.site_state(conn),
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
            html = self.env.get_template("panel.html").render(
                token=self.token, robot=self.robot_state(conn), ai=self.ai_state(conn),
                bot=self.bot_state(conn), site=self.site_state(conn),
                back=safe_back(request.arg("back")), fake=self.fake_shafa,
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
            return self.page(
                "list.html",
                **self._chrome(conn, request),
                current=status,
                cards=cards,
                query=query,
                message=MESSAGES.get(request.arg("msg")),
                deletion_date=review.deletion_date,
            )
        finally:
            conn.close()

    def card_page(self, item_id: int, request: Request) -> Response:
        conn = self.connect()
        try:
            card = review.load_card(conn, item_id)
            if card is None:
                return text(404, "Картку не знайдено")
            bad = {name for name in request.query.get("bad", []) if name in review.SPEC}
            row = conn.execute(
                "SELECT duplicate_url, duplicate_name, shafa_url FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            return self.page(
                "card.html",
                **self._chrome(conn, request),
                duplicate_url=shafa_link(row["duplicate_url"]),
                duplicate_name=row["duplicate_name"],
                duplicate_photo=self._duplicate_file(item_id).is_file(),
                shafa_url=shafa_link(row["shafa_url"]),
                my_items=MY_ITEMS,
                sticky=STICKY,
                sticky_titles=STICKY_TITLES,
                current=card.status,
                card=card,
                bad=bad,
                bad_input=review.BAD_INPUT,
                message=MESSAGES.get(request.arg("msg")),
                block=MESSAGES.get(card.approve_block() or ""),
                sections=catalog.sections(),
                hint=PROBLEM_HINTS.get(card.problem_code or ""),
                deletion_date=review.deletion_date,
                editable=card.status == Status.REVIEW,
            )
        finally:
            conn.close()

    def card_post(self, item_id: int, request: Request) -> Response:
        action = (request.form.get("action") or [""])[0]
        conn = self.connect()
        try:
            card = review.load_card(conn, item_id)
            if card is None:
                return text(404, "Картку не знайдено")
            back = f"/item/{item_id}"
            if action in ("save", "approve", "folder_order") or action.startswith("confirm:"):
                form, confirm = dict(request.form), action.removeprefix("confirm:")
                if action == "folder_order":  # порядок фото як у папці — і це підтвердження
                    by_position = sorted(card.photos, key=lambda p: p.position)
                    form["photo_order"] = ["\n".join(p.file_name for p in by_position)]
                    confirm = "photo_order"
                bad = review.save(conn, card, form, confirm or None)
                if bad:
                    return redirect(back + "?" + urlencode({"msg": "bad", "bad": bad}, doseq=True))
                if action != "approve":
                    return redirect(back + "?msg=saved")
            if action in review.ACTIONS:
                message = review.act(conn, item_id, action)
                if message in review.APPROVE_BLOCKS or message == "stale":
                    return redirect(f"{back}?msg={message}")
                return redirect(f"/{card.status}?msg={message}")
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
                    return redirect(f"{back}?msg={message}")
                return redirect(f"/{Status.PROBLEM}?msg={message}")
            if action == "refill":
                if not fill.mark_refill(conn, [item_id]):
                    return redirect(f"{back}?msg=stale")
                started = self._start(self._send_refill, "перезаповнюю: відправляю на ШІ…")
                return redirect(
                    f"/{Status.REVIEW}?msg={'refill_started' if started else 'ai_busy'}"
                )
            if action == "ai_retry":
                message = self.ai_retry(conn, item_id)
                return redirect(f"{back}?msg={message}")
            if action == "recheck" and (card.problem_code or "").startswith("robot_"):
                if review.robot_retry(conn, item_id):
                    return redirect(f"/{Status.PROBLEM}?msg=robot_retry")
                return redirect(f"{back}?msg=stale")
            if action == "recheck" and card.problem_code == fill.STALE_RULES:
                fill.recheck(conn, [item_id])
                return redirect(f"/{Status.REVIEW}?msg=recheck_done")
            if action == "recheck" and card.problem_code in STICKY:
                return redirect(f"{back}?msg=sticky")
            if action == "recheck":
                folders = list_folders(self.root, self.channel_id)
                status = rescan_folder(conn, card.folder_name, folders)
                if status == Status.NEW:
                    return redirect(f"/{Status.PROBLEM}?msg=recheck_new")
                return redirect(f"{back}?msg={'recheck_problem' if status else 'stale'}")
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
                cards=cards,
                reasons=control.LAST,
                sticky_titles=STICKY_TITLES,
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
            folders.insert(0, root / PUBLISHED_DIR / row["folder_name"])
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
