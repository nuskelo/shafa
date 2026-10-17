"""Підставна Шафа (UI-002, D5) — лише для тестів і «мавпи»: `robot --publish --fake-shafa`.

Локальний сервер на 127.0.0.1 з формою й кнопкою «Додати річ»; справжній двигун публікації
(черга, замок, квитки захисту, ліміт доби, Пауза/Стоп) працює проти нього. Браузер у цьому режимі
пропускає ЛИШЕ запити на підставну Шафу — будь-який інший (shafa.ua, інтернет) блокується й
записується. Профіль Chrome — тимчасовий, не профіль робота.
"""

import contextlib
import itertools
import json
import random
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from shafa.robot import browser, guard
from shafa.robot.form import FieldMissing, Submitted, outcome

CREATE_PATH = "/api/create"
PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8"><title>Підставна Шафа</title>
</head><body><h1>Підставна Шафа — не справжній сайт</h1>
<img src="https://shafa.ua/static/logo.png" alt="">
<form id="f"><input name="titleUk"><button type="submit">Додати річ</button></form>
<p id="r"></p>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = JSON.stringify({operationName: "WEB_CreateProduct",
    query: "mutation WEB_CreateProduct { createProduct { createdProduct { id } } }"});
  const r = await fetch("/api/create", {method: "POST", body,
    headers: {"Content-Type": "application/json"}});
  document.getElementById("r").textContent = await r.text();
});
</script></body></html>"""


def allowed(url: str, base: str) -> bool:
    """Лише підставна Шафа: `http://127.0.0.1:<порт>/…`. Усе інше — блок."""
    return url == base or url.startswith(base + "/")


class _Handler(BaseHTTPRequestHandler):
    # ID різні й між запусками: інакше «мавпа» не відрізнила б подвійну публікацію від збігу.
    counter = itertools.count(900_000_000 + int(time.time() * 10) % 99_000_000)

    def do_GET(self) -> None:
        self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path != CREATE_PATH:
            self._send(404, b"{}", "application/json")
            return
        time.sleep(random.uniform(0.2, 1.0))
        body = {"data": {"createProduct": {"createdProduct": {"id": next(self.counter)}}}}
        self._send(200, json.dumps(body).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class Server:
    """Підставна Шафа у фоновому потоці."""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSession(browser.Session):
    """Сесія робота проти підставної Шафи: ті самі квитки на «Додати річ», інша мережа."""

    def __init__(self, playwright: Any, base: str, headless: bool = True) -> None:
        self._setup(allow_upload=True, publish=True)
        self.base = base
        self.profile = tempfile.TemporaryDirectory(prefix="fake-shafa-")
        self.context = playwright.chromium.launch_persistent_context(
            self.profile.name, channel="chrome", headless=headless
        )
        self.context.route("**/*", self._route)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("dialog", self._dialog)

    def _route(self, route: Any, request: Any) -> None:
        url = request.url
        if not allowed(url, self.base):
            self.blocked.append((url.split("?")[0], "не підставна Шафа — заблоковано"))
            route.abort()
            return
        if request.method == "POST" and url.split("?")[0] == self.base + CREATE_PATH:
            if self._create_ticket is None:  # квиток — як для справжньої Шафи
                self.blocked.append((url, "мутація WEB_CreateProduct без квитка"))
                route.abort()
                return
            self._create_ticket = None
            self.creates += 1
        route.continue_()

    def open_form(self) -> browser.Opened:
        self.page.goto(self.base + "/new", wait_until="load")
        return browser.Opened(browser.FORM, self.page.url)

    def leave_result_page(self) -> None:
        with contextlib.suppress(Exception):  # як у справжньої сесії
            self.page.goto(self.base + "/new", wait_until="commit", timeout=5000)

    def outside(self) -> list[str]:
        """Запити, що намагались піти не на підставну Шафу (заблоковані)."""
        return [url for url, why in self.blocked if why.startswith("не підставна")]

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self.profile.cleanup()


class FakeDriver:
    """Заповнення «як людина», але без полів: кроки лише чекають. Клік «Додати річ» — справжній."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.page = session.page

    @staticmethod
    def _step() -> None:
        time.sleep(random.uniform(0.05, 0.2))

    def set_photos(self, paths: list[str]) -> int:
        self._step()
        return len(paths)

    def check_own_photos(self) -> bool:
        return False

    def fill(self, name: str, value: str, label: str) -> None:
        self._step()

    def choose_category(self, parts: list[str], slug: str) -> None:
        self._step()

    def pick(self, label: str, values: list[str]) -> None:
        self._step()

    def keywords(self, words: list[str]) -> None:
        self._step()

    def brand(self, name: str) -> bool:
        self._step()
        return True

    def submit(self) -> Submitted:
        button = self.page.locator("button[type=submit]").filter(has_text=guard.PUBLISH_BUTTON)
        if button.count() != 1:
            raise FieldMissing(f"Кнопка «{guard.PUBLISH_BUTTON}»")
        creates = self.session.creates
        body, failure = None, None
        try:
            with self.page.expect_response(
                lambda r: r.request.method == "POST" and r.url.endswith(CREATE_PATH),
                timeout=30000,
            ) as info:
                self.session.click_publish(button.first)
            body = info.value.json()
        except Exception as error:  # noqa: BLE001 — як у справжнього драйвера
            failure = f"{type(error).__name__}: {str(error)[:200]}"
        return outcome(self.session.creates > creates, False, body, failure)


def pause_seconds(low: float, high: float) -> float:
    """Пауза між товарами в підставному режимі — секунди, а не хвилини."""
    return random.uniform(1.0, 3.0)
