"""Браузер робота: видимий встановлений Chrome з окремим профілем у теці даних програми.

Кожен запит проходить через `guard.request_verdict`, кожен клік — через `guard.click_allowed`,
кожна клавіша — через `guard.key_allowed`. Пароль робот не бачить: вхід робить Власник руками.

Публікація (лише `Session(publish=True)`): `arm_publish` видає на Картку в `publishing` рівно
один квиток на клік «Додати річ» і один на запит `WEB_CreateProduct`; повторно на ту саму Картку —
ніколи. `disarm` гасить невикористані квитки (запізнілий запит після тайм-ауту буде заблоковано).
"""

import contextlib
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shafa import config
from shafa.db import Status
from shafa.robot import guard

NEW_URL = "https://shafa.ua/uk/new"
LOGIN_PATH = "/login"
FORM_FIELD = "input[name=titleUk]"
FORM_WAIT = 30.0  # с: на слабкому ноутбуці форма з'являється не одразу (FIX-002)
FORM_TICK = 0.5
LOGIN_WAIT = 15 * 60.0  # с: вікно входу закривається штатно, якщо його так і не закрили
LOGIN_DONE = "login-done"  # файл у теці даних: кнопка «Я увійшов — закрити вікно»

# Що показала Шафа на `/uk/new`.
FORM, NO_LOGIN, NO_FORM = "form", "login", "no_form"
# Сторінка шле операції з префіксом `WEB_` (`WEB_CreateProduct`), тож без `\b` на початку.
PUBLISH = re.compile(r"CreateProduct|EditProduct|Publish")


def profile_dir() -> Path:
    return config.data_dir() / "robot-profile"


def pause(low: float = 1.0, high: float = 2.5) -> None:
    """Темп людини."""
    time.sleep(random.uniform(low, high))


def operation(request: Any) -> str | None:
    """Назва GraphQL-операції запиту (для JSON-запитів), інакше None."""
    try:
        data = json.loads(request.post_data or "")
    except (ValueError, TypeError):
        return None
    item = data[0] if isinstance(data, list) and data else data
    return item.get("operationName") if isinstance(item, dict) else None


def catalog_slug(request: Any) -> str | None:
    """`catalogSlug` запиту `WEB_ProductFormCatalog` (форма питає поля обраного розділу)."""
    try:
        data = json.loads(request.post_data or "")
    except (ValueError, TypeError):
        return None
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and item.get("operationName") == "WEB_ProductFormCatalog":
            return (item.get("variables") or {}).get("catalogSlug")
    return None


@dataclass(frozen=True)
class Opened:
    """Чим закінчилось `open_form`: форма є / Шафа веде на вхід / ні того, ні іншого."""

    state: str
    url: str = ""
    waited: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state == FORM

    def detail(self) -> str:
        """Для `robot-run.log`: адреса без параметрів (там можуть бути службові токени)."""
        return f"адреса {self.url.split('?')[0] or '—'}, чекали {self.waited:.0f} с"


def wait_form(
    page: Any,
    timeout: float = FORM_WAIT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
) -> Opened:
    """Чекати, доки з'явиться форма або Шафа поведе на вхід; не дочекались — `NO_FORM`.

    Пауза — `page.wait_for_timeout`: `time.sleep` у синхронному Playwright зупиняє й обробку
    подій браузера.
    """
    sleep = sleep or (lambda seconds: page.wait_for_timeout(seconds * 1000))
    started = clock()
    while True:
        waited = clock() - started
        if LOGIN_PATH in page.url:
            return Opened(NO_LOGIN, page.url, waited)
        if _count(page, FORM_FIELD):
            return Opened(FORM, page.url, waited)
        if waited >= timeout:
            return Opened(NO_FORM, page.url, waited)
        sleep(FORM_TICK)


def _count(page: Any, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:  # noqa: BLE001 — сторінка саме переходить (контекст сторінки знищено)
        return 0


# CORE-007: вікно робота можна згорнути чи закрити іншими вікнами — Chrome не гальмує фонову
# вкладку (таймери, рендер), інакше робот на її ноутбуці «засинає».
BACKGROUND_ARGS = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)


def launch(playwright: Any) -> Any:
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    return open_profile(playwright.chromium, profile)


def open_profile(chromium: Any, profile: Path) -> Any:
    """Видимий встановлений Chrome з профілем робота; фонове гальмування вимкнене."""
    return chromium.launch_persistent_context(
        profile,
        channel="chrome",
        headless=False,
        viewport={"width": 1400, "height": 1000},
        args=list(BACKGROUND_ARGS),
    )


class Session:
    """Відкритий профіль робота із захистом.

    `allow_upload` — завантаження фото (`--dry-fill`, `--publish`); `publish` — режим публікації.
    """

    def __init__(self, playwright: Any, allow_upload: bool = False, publish: bool = False) -> None:
        self._setup(allow_upload or publish, publish)
        self.context = launch(playwright)
        self.context.route("**/*", self._route)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("dialog", self._dialog)

    def _setup(self, allow_upload: bool, publish: bool) -> None:
        self.allow_upload = allow_upload
        self.publish_mode = publish
        self.blocked: list[tuple[str, str]] = []
        self.uploads = 0
        self.creates = 0  # скільки запитів публікації пропущено за весь запуск
        self.armed: set[int] = set()  # Картки, на які вже видано квиток
        self._click_ticket: int | None = None
        self._create_ticket: int | None = None

    def arm_publish(self, item_id: int, status: str) -> None:
        """Квиток на одну публікацію Картки. Лише режим публікації, лише `publishing`, раз."""
        if not self.publish_mode:
            raise guard.Forbidden("Публікація поза режимом --publish")
        if status != Status.PUBLISHING:
            raise guard.Forbidden(f"Публікація Картки не в стані publishing ({status})")
        if item_id in self.armed:
            raise guard.Forbidden(f"Повторна публікація Картки {item_id}")
        self.armed.add(item_id)
        self._click_ticket = self._create_ticket = item_id

    def disarm(self) -> None:
        self._click_ticket = self._create_ticket = None

    def click_publish(self, locator: Any) -> None:
        """Клік «Додати річ» за квитком. Квиток згорає до кліку: другого кліку не буде."""
        if self._click_ticket is None:
            raise guard.Forbidden("Клік «Додати річ» без квитка")
        info = locator.evaluate(
            "e => ({text: [e.innerText, e.value, e.getAttribute('aria-label')].join(' '),"
            " type: e.getAttribute('type')})"
        )
        if not guard.publish_click_allowed(info["text"], info["type"]):
            raise guard.Forbidden(f"Не кнопка публікації: {info['text'].strip()[:60]!r}")
        self._click_ticket = None
        locator.click()

    @staticmethod
    def _dialog(dialog: Any) -> None:
        """«Покинути сторінку?» між Картками — так (форма не надсилається); інше — відмова."""
        if dialog.type == "beforeunload":
            dialog.accept()
        else:
            dialog.dismiss()

    def _route(self, route: Any, request: Any) -> None:
        allowed, reason = guard.request_verdict(
            request.method,
            request.url,
            request.headers.get("content-type", ""),
            request.post_data_buffer,
            self.allow_upload,
            allow_create=self._create_ticket is not None,
        )
        if allowed:
            self.uploads += reason == "завантаження фото"
            if reason == guard.PUBLISHED:
                self._create_ticket = None  # один запит на квиток
                self.creates += 1
            route.continue_()
        else:
            self.blocked.append((request.url.split("?")[0], reason))
            route.abort()

    def publish_attempts(self) -> list[str]:
        """Заблоковані спроби створити/змінити оголошення (має бути порожньо)."""
        return [r for _, r in self.blocked if PUBLISH.search(r)]

    def click(self, locator: Any, why: str = "") -> None:
        info = locator.evaluate(
            "e => ({text: [e.innerText, e.value, e.getAttribute('aria-label')].join(' '),"
            " type: e.getAttribute('type')})"
        )
        if not guard.click_allowed(info["text"], info["type"]):
            raise guard.Forbidden(f"Заборонений клік: {info['text'].strip()[:60]!r} ({why})")
        locator.click()
        pause()

    def press(self, key: str, in_keywords: bool = False) -> None:
        if not guard.key_allowed(key, in_keywords):
            raise guard.Forbidden(f"Клавіша {key} поза полем ключових слів")
        self.page.keyboard.press(key)

    def choose_category(self, parts: list[str] | tuple[str, ...], slug: str) -> dict:
        """Клікнути шлях розділу; повернути відповідь форми про розділ (звірену за `slug`).

        Вибір розділу — вікно з колонками: кожен рівень відкриває нову колонку праворуч. Тип може
        зватися як верхня гілка («Чоловічий одяг / Взуття / Аксесуари»), тож на рівнях після
        першого береться остання видима кнопка — з найновішої колонки. Звірка `slug` — страховка.
        """
        for i, name in enumerate(parts):
            buttons = self.page.get_by_role("button", name=name, exact=True).locator("visible=true")
            for _ in range(20):  # колонка з'являється не миттєво — до 10 с
                if buttons.count():
                    break
                self.page.wait_for_timeout(500)
            else:
                raise LookupError(name)
            button = buttons.last if i else buttons.first
            if i < len(parts) - 1:
                self.click(button, f"розділ {name}")
                continue
            with self.page.expect_response(
                lambda r: catalog_slug(r.request) is not None, timeout=30000
            ) as info:
                self.click(button, f"розділ {name}")
            got = catalog_slug(info.value.request)
            if got != slug:
                raise LookupError(f"{name}: Шафа відкрила розділ {got}, а не {slug}")
            body = info.value.json()
            if not isinstance(body, list):
                return body
            items = json.loads(info.value.request.post_data)
            ops = [i.get("operationName") for i in items]
            return body[ops.index("WEB_ProductFormCatalog")]
        raise LookupError("порожній шлях")

    def open_form(self) -> Opened:
        """Відкрити форму «Додати товар» і дочекатись її (до `FORM_WAIT`) або сторінки входу."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            self.page.goto(NEW_URL, wait_until="domcontentloaded", timeout=FORM_WAIT * 1000)
        except PlaywrightTimeout:
            pass  # повільна сторінка: що вона встигла показати — вирішить wait_form
        opened = wait_form(self.page)
        if opened.ok:
            pause(1.0, 2.0)  # форма ще дорисовує списки
        return opened

    def close(self) -> None:
        self.context.close()


def login_done_flag() -> Path:
    return config.data_dir() / LOGIN_DONE


def wait_for_manual_login(
    playwright: Any,
    limit: float = LOGIN_WAIT,
    flag: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Відкрити профіль для ручного входу Власника; чекати, поки вікно закриють.

    Без захисту мережі: вхід — це надсилання логіна, але його робить людина, не робот.
    Повертає, чим закінчилось: `closed` (вікно закрили), `done` (кнопка «Я увійшов» на екрані),
    `timeout` (минуло `limit`). В останніх двох браузер закривається штатно — cookies входу
    записуються на диск.
    """
    flag = flag or login_done_flag()
    flag.unlink(missing_ok=True)  # стара кнопка з попереднього разу не рахується
    context = launch(playwright)
    closed: list[bool] = []
    context.on("close", lambda _context: closed.append(True))
    page = context.pages[0] if context.pages else context.new_page()
    with contextlib.suppress(Exception):  # вікно закрили, поки сторінка вантажилась
        page.goto(NEW_URL)
    why = wait_window(context, closed, flag, limit, clock)
    flag.unlink(missing_ok=True)
    if not closed:
        with contextlib.suppress(Exception):  # браузер міг уже відключитись сам
            context.close()  # Chrome закривається штатно: cookies входу записуються на диск
    return why


def wait_window(
    context: Any,
    closed: list[bool],
    flag: Path,
    limit: float,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Чекати закриття вікна, кнопку «Я увійшов» або кінець часу.

    Пауза — через сторінку (`wait_for_timeout`): `time.sleep` не дає синхронному Playwright
    обробити подію закриття, і `context.pages` ніколи не порожніє (FIX-002, Windows).
    """
    deadline = clock() + limit
    while True:
        if closed or not context.pages:  # Windows: хрестик закриває Chrome; Mac — лише вікно
            return "closed"
        if flag.exists():
            return "done"
        if clock() >= deadline:
            return "timeout"
        try:
            context.pages[0].wait_for_timeout(FORM_TICK * 1000)
        except Exception:  # noqa: BLE001 — сторінку чи браузер закрили під час очікування
            time.sleep(0.1)
