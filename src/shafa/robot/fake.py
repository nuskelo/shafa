"""Підставна Шафа (UI-002, D5) — лише для тестів і «мавпи»: `robot --publish --fake-shafa`.

Локальний сервер на 127.0.0.1 з формою й кнопкою «Додати річ»; справжній двигун публікації
(черга, замок, квитки захисту, ліміт доби, Пауза/Стоп) працює проти нього. Браузер у цьому режимі
пропускає ЛИШЕ запити на підставну Шафу — будь-який інший (shafa.ua, інтернет) блокується й
записується. Профіль Chrome — тимчасовий, не профіль робота.
"""

import contextlib
import itertools
import json
import os
import random
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from shafa.robot import browser, guard
from shafa.robot.form import FieldMissing, PhotosShort, Submitted, outcome

CREATE_PATH = "/api/create"
UPLOAD_PATH = (
    "/api/upload"  # ROBOT-015: вибір фото на формі — запит, який рахує `FakeSession.uploads`
)
PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8"><title>Підставна Шафа</title>
</head><body><h1>Підставна Шафа — не справжній сайт</h1>
<img src="https://shafa.ua/static/logo.png" alt="">
%(modal)s
<form id="f"><input name="titleUk"><button type="submit">Додати річ</button></form>
<input type="file" id="ph" multiple><p id="cnt-box"></p>
<p id="r"></p>
<script>
const PHOTOS = "%(photos)s", LATE_MS = %(late)d;
let picks = 0;
document.getElementById("ph").addEventListener("change", async (e) => {
  const n = e.target.files.length;
  if (!n) return;  // очищення поля — не вибір фото
  picks += 1;
  for (let i = 0; i < n; i++) await fetch("/api/upload", {method: "POST", body: "photo"});
  const draw = (shown) => {
    document.getElementById("cnt-box").textContent = "(" + shown + " з " + n + " завантажено)";
  };
  if (PHOTOS === "photos_late") { draw(0); setTimeout(() => draw(n), LATE_MS); }
  else if (PHOTOS === "photos_zero" || (PHOTOS === "photos_retry" && picks < 2)) draw(0);
  else draw(PHOTOS === "photos_part" ? n - 1 : n);
});
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = JSON.stringify({operationName: "WEB_CreateProduct",
    query: "mutation WEB_CreateProduct { createProduct { createdProduct { id } } }"});
  const r = await fetch("/api/create", {method: "POST", body,
    headers: {"Content-Type": "application/json"}});
  document.getElementById("r").textContent = await r.text();
});
%(script)s
</script></body></html>"""

# QA-004: вікно від'ємного балансу (ROBOT-014). «Підтвердити» — пастка: клік по ній пише `paid`.
MODAL = """<div role="dialog" id="bal" style="position:fixed;top:40px;left:20%%;width:60%%;
background:#fff;border:1px solid #888;padding:24px;z-index:9">
<button id="x" style="position:absolute;top:8px;right:8px">×</button>
<h2>У вас від'ємний баланс</h2>
<p>Очікується списання 193.15 грн, інакше деактивація акаунта.</p>
<button id="pay">Підтвердити</button></div>
<p>Ваш від'ємний баланс −193.15 грн</p>"""
MODAL_SCRIPT = """
document.getElementById("pay").addEventListener("click", () => fetch("/api/pay", {method: "POST"}));
%s"""
CLOSE_SCRIPT = (
    'document.getElementById("x").addEventListener("click", () => '
    'document.getElementById("bal").remove());'
)


LATE_MS = 30000  # `photos_late`: лічильник фото з'являється із запізненням (тести міняють)


def page(scenario: str = "ok") -> str:
    """Сторінка форми для сценарію: з вікном балансу (закривається / не закривається) чи без;
    лічильник фото — за сценарієм `photos_*` (ROBOT-015)."""
    fields = {
        "modal": "",
        "script": "",
        "photos": scenario if scenario.startswith("photos_") else "",
        "late": LATE_MS,
    }
    if scenario == "balance":
        fields |= {"modal": MODAL, "script": MODAL_SCRIPT % CLOSE_SCRIPT}
    if scenario == "balance_stuck":
        fields |= {"modal": MODAL, "script": MODAL_SCRIPT % ""}
    return PAGE % fields


# Сценарії підставної Шафи (QA-004). Обирається при кожному відкритті форми; `slow` тримається
# на всі спроби Картки (ROBOT-009), решта — разова.
SCENARIOS = {
    "ok": "усе гаразд",
    "balance": "вікно від'ємного балансу, закривається хрестиком",
    "balance_stuck": "вікно від'ємного балансу, не закривається",
    "login": "Шафа веде на вхід",
    "slow": "тайм-аут при заповненні (усі спроби)",
    "nofield": "поле форми не знайдено",
    "photos": "форма показує не всі фото",
    # Лише для справжнього `PageDriver.set_photos` (ROBOT-015): `FakeDriver` їх не відрізняє від `ok`.
    "photos_late": "лічильник фото з'являється із запізненням",
    "photos_retry": "перший вибір фото — «0 з N», після повтору — усі",
    "photos_zero": "лічильник фото завжди «0 з N»",
    "photos_part": "завантажено не всі фото («N-1 з N»)",
    "uncertain": "клік пройшов, відповіді з id немає",
}
SCENARIOS_ENV = "SHAFA_FAKE_SCENARIOS"  # «ok:60,balance:5,…» — вага кожного; порожньо — лише ok
SEED_ENV = "SHAFA_FAKE_SEED"
SEQUENCE_ENV = (
    "SHAFA_FAKE_SEQUENCE"  # «ok,nofield,ok» — перші відкриття форми по черзі, далі за вагами
)
LOG_ENV = "SHAFA_FAKE_LOG"  # файл (JSON-рядки): сценарії, створення, спроби «заплатити»
SLOW_OPENS = 3  # скільки відкриттів форми підряд `slow` (= ATTEMPTS у publish.py)


class Scenarios:
    """Черга сценаріїв: випадковий вибір із вагами й фіксованим сідом (відтворюється)."""

    def __init__(
        self,
        weights: dict[str, int] | None = None,
        seed: int = 0,
        sequence: list[str] | None = None,
    ) -> None:
        weights = weights or {"ok": 1}
        self.sequence = list(sequence or [])
        if bad := sorted((set(weights) | set(self.sequence)) - set(SCENARIOS)):
            raise ValueError(f"невідомий сценарій підставної Шафи: {', '.join(bad)}")
        self.names, self.weights = list(weights), list(weights.values())
        self.random = random.Random(seed)
        self.slow_left = 0

    @classmethod
    def from_env(cls) -> "Scenarios":
        weights: dict[str, int] = {}
        for part in (os.environ.get(SCENARIOS_ENV) or "").split(","):
            if part.strip():
                name, _, weight = part.partition(":")
                weights[name.strip()] = int(weight or 1)
        sequence = [x.strip() for x in (os.environ.get(SEQUENCE_ENV) or "").split(",") if x.strip()]
        scenarios = cls(weights, int(os.environ.get(SEED_ENV) or 0) + launches(), sequence)
        write_log(event="launch")
        return scenarios

    def next(self) -> str:
        if self.slow_left:
            self.slow_left -= 1
            return "slow"
        if self.sequence:
            name = self.sequence.pop(0)
        else:
            name = self.random.choices(self.names, self.weights)[0]
        if name == "slow":
            self.slow_left = SLOW_OPENS - 1
        return name


def launches() -> int:
    """Скільки разів підставну Шафу вже запускали (за журналом): кожен запуск робота — свій
    порядок сценаріїв, але той самий сід дає ту саму послідовність запусків."""
    path = os.environ.get(LOG_ENV)
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8") as log:
            return sum('"event": "launch"' in line for line in log)
    except OSError:
        return 0


def write_log(**event: Any) -> None:
    """Рядок у `SHAFA_FAKE_LOG` (якщо задано): «мавпа» за ним рахує створення й сценарії."""
    path = os.environ.get(LOG_ENV)
    if path:
        with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as out:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")


def allowed(url: str, base: str) -> bool:
    """Лише підставна Шафа: `http://127.0.0.1:<порт>/…`. Усе інше — блок."""
    return url == base or url.startswith(base + "/")


class _Handler(BaseHTTPRequestHandler):
    # ID різні й між запусками: інакше «мавпа» не відрізнила б подвійну публікацію від збігу.
    counter = itertools.count(900_000_000 + int(time.time() * 10) % 99_000_000)

    def do_GET(self) -> None:
        scenario = self.path.partition("?s=")[2] or "ok"
        self._send(200, page(scenario).encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/api/pay":  # робот ніколи не платить: це пастка для «мавпи»
            write_log(event="paid")
            self._send(200, b"{}", "application/json")
            return
        if self.path == UPLOAD_PATH:
            self._send(200, b"{}", "application/json")
            return
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

    scenario = "ok"  # сценарій відкритої форми (QA-004)
    scenarios: Scenarios | None = None

    def __init__(
        self,
        playwright: Any,
        base: str,
        headless: bool = True,
        scenarios: Scenarios | None = None,
    ) -> None:
        self._setup(allow_upload=True, publish=True)
        self.base = base
        self.scenarios = scenarios or Scenarios.from_env()
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
        if request.method == "POST" and url.split("?")[0] == self.base + UPLOAD_PATH:
            self.uploads += 1
        if request.method == "POST" and url.split("?")[0] == self.base + CREATE_PATH:
            if self._create_ticket is None:  # квиток — як для справжньої Шафи
                self.blocked.append((url, "мутація WEB_CreateProduct без квитка"))
                route.abort()
                return
            write_log(event="create", item=self._create_ticket)
            self._create_ticket = None
            self.creates += 1
        route.continue_()

    def open_form(self) -> browser.Opened:
        self.scenario = self.scenarios.next() if self.scenarios else "ok"
        write_log(event="open", scenario=self.scenario)
        if self.scenario == "login":
            return browser.Opened(browser.NO_LOGIN, self.base + "/login")
        self.page.goto(f"{self.base}/new?s={self.scenario}", wait_until="load")
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

    def reset(self) -> None:
        pass

    @staticmethod
    def _step() -> None:
        time.sleep(random.uniform(0.05, 0.2))

    def set_photos(self, paths: list[str]) -> int:
        self._step()
        if self.session.scenario == "photos":
            raise PhotosShort(f"Шафа показує 1 фото з {len(paths)}")
        return len(paths)

    def check_own_photos(self) -> bool:
        return False

    def fill(self, name: str, value: str, label: str) -> None:
        self._step()
        if self.session.scenario == "nofield":
            raise FieldMissing(label or name)
        if self.session.scenario == "slow":
            raise TimeoutError("Timeout 30000ms exceeded (підставна Шафа: повільна сторінка)")

    def choose_category(self, parts: list[str], slug: str) -> None:
        self._step()

    def pick(self, label: str, values: list[str]) -> None:
        self._step()

    def keywords(self, words: list[str]) -> None:
        self._step()

    def brand(self, name: str) -> bool:
        self._step()
        return True

    def untouched(self, labels: list[str]) -> list[str]:
        return []

    def verify(self) -> None:
        pass

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
        if self.session.scenario == "uncertain":  # Шафа створила, але відповідь не дійшла
            body, failure = None, "TimeoutError: відповідь createProduct не дійшла"
        return outcome(self.session.creates > creates, False, body, failure)


def pause_seconds(low: float, high: float) -> float:
    """Пауза між товарами в підставному режимі — секунди, а не хвилини."""
    return random.uniform(1.0, 3.0)
