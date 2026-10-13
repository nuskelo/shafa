"""Екран перевірки: локальний вебсервер (лише 127.0.0.1) і сторінки.

Логіка запиту — `App.handle(Request) -> Response`, без мережі (так її перевіряють тести).
`serve` лише обгортає її в `ThreadingHTTPServer`.

Захист (рішення D6): кожен запит — лише з `Host` 127.0.0.1:порт або localhost:порт (чужий домен,
що вказує на 127.0.0.1, не прочитає сторінок); зміни — лише POST з токеном цього запуску.
"""

import hmac
import os
import re
import secrets
import sys
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jinja2
from PIL import UnidentifiedImageError

from shafa import catalog, db, photos, review
from shafa.db import Status
from shafa.folders import list_folders
from shafa.scan import STICKY, rescan_folder

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 1_000_000

PACKAGE = Path(__file__).resolve().parent
STATIC = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}

TAB_TITLES = {
    Status.REVIEW: "На перевірку",
    Status.APPROVED: "Схвалені",
    Status.PROBLEM: "Проблеми",
    Status.ARCHIVED: "Архів",
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
    "bad": "Не збережено поля, позначені червоним",
    "stale": "Картку вже змінено — сторінку оновлено",
    "robot_retry": "Картку повернуто в Схвалені — робот спробує ще раз",
    "recheck_new": "Папку перевірено: усе добре, Картка чекає на ШІ",
    "recheck_problem": "Папку перевірено: проблема лишилась",
    "sticky": "Спершу перевірте Шафу. Повернути цю Картку зможе лише людина (кнопки — згодом)",
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
    "robot_brand_missing": "Бренду немає в списку Шафи (OQ-004). Скажіть Власнику. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_photos": "Не всі фото завантажились на Шафу. «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_not_ready": "Картку не можна передати роботу (текст вище). «Перевірити ще раз» поверне Картку в Схвалені.",
    "robot_publish_blocked": "Захист робота не пропустив запит публікації — товар не створено. Скажіть Власнику.",
}

SECURITY_HEADERS = {
    # Сторінки беруть лише свої файли: жодних зовнішніх скриптів, стилів, шрифтів, фото.
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # Не `no-referrer`: з ним Chrome шле у формах `Origin: null`, і кожен POST — 403 (знайшла «мавпа»).
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

_ITEM = re.compile(r"/item/(\d+)")
_PHOTO = re.compile(r"/(photo|thumb)/(\d+)/(\d+)")
_STATIC = re.compile(r"/static/([\w.-]+)")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    form: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)  # назви малими літерами

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


class App:
    def __init__(
        self,
        db_path: Path,
        root: Path,
        cache_dir: Path,
        port: int = DEFAULT_PORT,
        channel_id: str | None = None,
        token: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.root = root
        self.cache_dir = cache_dir
        self.port = port
        self.channel_id = channel_id
        self.token = token or secrets.token_urlsafe(32)
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
        if path.strip("/") in TAB_TITLES and get:
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
        html = self.env.get_template(template).render(token=self.token, **context)
        return Response(status, html.encode("utf-8"))

    def _chrome(self, conn) -> dict:
        """Спільне для всіх сторінок: вкладки з лічильниками."""
        return {"tabs": TAB_TITLES, "counts": review.counts(conn)}

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
                **self._chrome(conn),
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
            return self.page(
                "card.html",
                **self._chrome(conn),
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
            if action == "recheck" and (card.problem_code or "").startswith("robot_"):
                if review.robot_retry(conn, item_id):
                    return redirect(f"/{Status.PROBLEM}?msg=robot_retry")
                return redirect(f"{back}?msg=stale")
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

    def photo(self, kind: str, item_id: int, position: int) -> Response:
        """Фото лише з папок Карток у базі: шлях будується з бази, не із запиту."""
        conn = self.connect()
        try:
            row = conn.execute(
                """
                SELECT folder_name, file_name FROM photos JOIN items ON items.id = item_id
                WHERE item_id = ? AND position = ? AND purged_at IS NULL
                """,
                (item_id, position),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return text(404, "Фото не знайдено")
        root = self.root.resolve()
        path = (root / row["folder_name"] / row["file_name"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return text(404, "Фото не знайдено")
        try:
            if kind == "thumb":
                body = photos.cached_thumbnail(self.cache_dir, item_id, path)
            else:
                body = path.read_bytes()  # оригінал: браузер сам повертає за EXIF
        except (OSError, UnidentifiedImageError):
            return text(404, "Фото не відкривається")
        return Response(200, body, "image/jpeg")


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
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                self._send(text(413, "Завеликий запит"))
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            form = parse_qs(body, keep_blank_values=True)
        request = Request(
            method,
            url.path,
            parse_qs(url.query),
            form,
            {k.lower(): v for k, v in self.headers.items()},
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
