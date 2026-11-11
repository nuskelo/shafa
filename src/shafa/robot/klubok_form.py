"""KLB-001: робот заповнює форму додавання Клубку; KLB-003: та сама сесія публікує за квитком.

`Session` — браузер із `klubok_guard` (клік, мережа, клавіші). `KlubokDriver` — поля форми з
`fields.KlubokAd`; після заповнення `verify` звіряє глобальний `formData` (назва, ціна, стать,
стан, розміри, колір, бренд, категорія, порядок фото) з тим, що мало бути. Звичайний `click` ніколи
не тисне «Опубликовать» (`klubok_guard.click_allowed`); публікація — лише в сесії з
`publishing=True`: `issue_ticket` після звірки → `click_submit` (чи людина, `--human-click`) →
`wait_submit`. `LiveSession` — живий Клубок з профілем `<дані>/klubok-profile/`.
"""

import contextlib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shafa import config, db
from shafa.klubok import catalog, fields, snapshot
from shafa.robot import browser, klubok_guard
from shafa.robot import form as shafa_form
from shafa.robot.klubok_guard import Forbidden

FORM_SELECTOR = "#addItemForm"
FORM_WAIT = 30.0  # с: форма з'являється не одразу
PHOTO_WAIT = 30.0  # с на одне фото: завантажилось і з'явилось у `formData.images`
FIELD_WAIT = 5.0  # с чекаємо поле чи список форми
BRAND_WAIT = 6.0  # с чекаємо підказки бренду
BRAND_TYPE_DELAY = 40  # мс між літерами, як у людини (сторінка чекає 500 мс після останньої)
SUFFIX = "(Кирилиця)"
SUBMIT_FETCH = 60.0  # с на відповідь сервера на запит публікації
LOGIN_PATHS = ("/signup", "/login")  # куди Клубок веде без входу (HYP-4: `/item/add` → `/signup`)

# Чим закінчилась відправка (KLB-003 D4.4)
SUCCESS, CAPTCHA, UNCERTAIN, NOT_SENT = "success", "captcha", "uncertain", "not_sent"


@dataclass(frozen=True)
class Submitted:
    outcome: str  # SUCCESS / CAPTCHA / UNCERTAIN / NOT_SENT
    klubok_id: int | None = None
    score: str | None = None
    why: str = ""
    http: int | None = None
    detail: str = ""  # для журналу сесії (KLB-007): тип і значення `used_id`, початок відповіді


def _playwright_error() -> type[Exception]:
    """Помилка Playwright (тайм-аут — її підклас); імпорт лише тут: без браузера модуль вантажиться."""
    from playwright.sync_api import Error

    return Error


class KlubokError(Exception):
    """Заповнити не вдалось: код (для «Проблем») і текст для людини."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


class Session:
    """Відкритий браузер із захистом `klubok_guard`. Підкласи задають профіль і адресу форми."""

    context: Any
    page: Any

    def _setup(self, allow_upload: bool, publishing: bool = False, human: bool = False) -> None:
        """`publishing` — сесія публікації KLB-003 (квиток, reCAPTCHA); `human` — «Опубликовать»
        тисне людина (`--human-click`), робот кнопку не чіпає ніколи."""
        self.allow_upload = allow_upload
        self.publishing = publishing
        self.human = human
        self.blocked: list[tuple[str, str]] = []
        self.uploads = 0
        self.ticket: klubok_guard.Ticket | None = None
        self.too_many: list[str] = []  # адреси Клубку, що відповіли 429
        self._reset_submit()

    def _reset_submit(self) -> None:
        self.sent = False  # guard пропустив запит публікації
        self.submit_result: tuple[int, str] | None = None  # (HTTP-статус, тіло)
        self.submit_failed: str | None = None
        self.fetch_timeout = SUBMIT_FETCH  # с: скільки чекати відповідь сервера на публікацію

    def _attach(self) -> None:
        self.context.route("**/*", self._route)
        self.context.on("response", self._on_response)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("dialog", lambda dialog: dialog.dismiss())

    def _on_response(self, response: Any) -> None:
        url = self.verdict_url(response.url)
        if (
            url is not None
            and response.status == 429
            and klubok_guard.is_klubok(urlsplit(url).hostname)
        ):
            self.too_many.append(url.split("?")[0])

    def verdict_url(self, url: str) -> str | None:
        """Адреса, яку оцінює guard; None — запит узагалі не туди (блок). Живий сайт — як є."""
        return url

    def form_url(self) -> str:
        raise NotImplementedError("Живий Клубок — з KLB-003")

    def _route(self, route: Any, request: Any) -> None:
        url = self.verdict_url(request.url)
        if url is None:
            self.blocked.append((request.url.split("?")[0], "не підставний Клубок — заблоковано"))
            route.abort()
            return
        allowed, reason = klubok_guard.request_verdict(
            request.method, url, request.headers.get("content-type", ""), self.allow_upload,
            publishing=self.publishing, ticket=self.ticket,
        )  # fmt: skip
        if allowed and reason == klubok_guard.SUBMITTED:
            self._send_submit(route)
        elif allowed:
            self.uploads += reason == "завантаження фото"
            route.continue_()
        else:
            path = urlsplit(url).path
            if (
                self.ticket is not None
                and self.ticket.used
                and klubok_guard.AFTER_SUBMIT.match(path)
            ):
                reason += " (очікувано: сторінка після відповіді)"
            self.blocked.append((url.split("?")[0], reason))
            route.abort()

    def _send_submit(self, route: Any) -> None:
        """Запит публікації (за квитком): відповідь береться тут, до сторінки. Сторінка одразу
        після неї йде на `/payment/…` — тоді тіло відповіді в Chrome вже не прочитати."""
        self.sent = True
        try:
            response = route.fetch(timeout=self.fetch_timeout * 1000)
            self.submit_result = (response.status, response.text())
        except Exception as error:  # noqa: BLE001 — запит пішов, відповіді немає
            self.submit_failed = f"{type(error).__name__}: {str(error)[:150]}"
            with contextlib.suppress(Exception):
                route.abort()
            return
        if response.status == 429:
            self.too_many.append(klubok_guard.SUBMIT_URL)
        with contextlib.suppress(Exception):  # сторінку могли закрити — відповідь у нас уже є
            route.fulfill(response=response)

    def unexpected_blocks(self) -> list[tuple[str, str]]:
        """Заблоковане, крім очікуваних переходів після відповіді й сторінки входу."""
        return [
            (url, why) for url, why in self.blocked
            if "очікувано" not in why and urlsplit(url).path not in LOGIN_PATHS
        ]  # fmt: skip

    # --- KLB-003: публікація ---

    def issue_ticket(self, item_id: int) -> klubok_guard.Ticket:
        """Квиток на одну Картку — лише в режимі публікації й після звірки форми."""
        if not self.publishing:
            raise Forbidden("Квиток публікації — лише в сесії публікації")
        self._reset_submit()
        self.ticket = klubok_guard.Ticket(item_id)
        return self.ticket

    def drop_ticket(self) -> None:
        self.ticket = None

    def click_submit(self) -> None:
        """Клік «Опубликовать» роботом: лише `#btn-submit`, живий квиток, не `--human-click`."""
        button = self.page.locator(f"#{klubok_guard.SUBMIT_BUTTON}")
        element_id = button.evaluate("e => e.id")
        if not klubok_guard.submit_click_allowed(element_id, self.ticket, self.human):
            raise Forbidden("Клік «Опубликовать» без квитка (чи в режимі «тисне людина»)")
        button.click(timeout=FIELD_WAIT * 1000 * 6)

    def wait_submit(self, limit: float, stop: Callable[[], bool] = lambda: False) -> "Submitted":
        """Чекати відповідь `add-edit-used` до `limit` с. Що б не сталось — результат, не виняток.
        `stop` — людина закрила вікно чи інше «далі чекати нема сенсу» (лише до відправки)."""
        deadline = time.monotonic() + limit
        while True:
            if self.submit_result is not None:
                return self._parse(*self.submit_result)
            if self.submit_failed is not None:
                return Submitted(UNCERTAIN, why=f"запит обірвано: {self.submit_failed}")
            if time.monotonic() >= deadline or (not self.sent and stop()):
                break
            try:
                self.page.wait_for_timeout(250)
            except _playwright_error():  # вікно закрили
                if self.submit_result is None and self.submit_failed is None:
                    break
        if self.sent:
            return Submitted(UNCERTAIN, why=f"немає відповіді за {limit:.0f} с")
        return Submitted(NOT_SENT, why=f"запит публікації не пішов за {limit:.0f} с")

    @staticmethod
    def _klubok_id(value: Any) -> int | None:
        """`used_id`: число чи рядок лише з цифр, > 0 (KLB-007: наживо прийшов рядок); `bool` — ні."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value) or None
        return None

    @staticmethod
    def _parse(status: int, text: str) -> "Submitted":
        head = f"відповідь: {text[:300]!r}"
        if status != 200:
            return Submitted(UNCERTAIN, why=f"HTTP {status}", http=status, detail=head)
        try:
            data = json.loads(text)
        except ValueError:
            return Submitted(UNCERTAIN, why="відповідь не JSON", detail=head)
        if not isinstance(data, dict):
            return Submitted(UNCERTAIN, why="відповідь не JSON-об'єкт", detail=head)
        used_id = data.get("used_id")
        klubok_id = Session._klubok_id(used_id)
        if data.get("status") == "success" and klubok_id is not None:
            return Submitted(SUCCESS, klubok_id=klubok_id)
        if data.get("status") == "Error":
            return Submitted(
                CAPTCHA, score=str(data.get("score", "?"))[:20],
                why=str(data.get("message") or "")[:200],
            )  # fmt: skip
        return Submitted(
            UNCERTAIN, why=f"невідома відповідь: status={str(data.get('status'))[:40]}",
            detail=f"used_id: {type(used_id).__name__} {str(used_id)[:40]!r}; {head}",
        )  # fmt: skip

    def user_id(self) -> str | None:
        """`#user_id` форми — хто увійшов (D2)."""
        try:
            return self.page.evaluate(
                "() => { const e = document.querySelector('#user_id'); return e ? e.value : null; }"
            )
        except _playwright_error():
            return None

    def fetch_text(self, path: str, timeout: float = 30.0) -> tuple[int, str]:
        """GET зсередини сторінки (cookies сесії, крізь guard: `context.request` іде повз `route`).
        Статус 0 — запит не вдався (guard, мережа, тайм-аут)."""
        result = self.page.evaluate(
            """async ([path, ms]) => {
              const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
              try {
                const r = await fetch(path, {credentials: 'same-origin', signal: c.signal});
                return [r.status, await r.text()];
              } catch (e) { return [0, String(e)]; } finally { clearTimeout(t); }
            }""",
            [path, int(timeout * 1000)],
        )
        return int(result[0]), str(result[1])

    def click(self, locator: Any, why: str = "") -> None:
        info = locator.evaluate(
            """e => {
              const a = e.closest('a[href]');
              const text = x => [x.innerText, x.value, x.getAttribute('aria-label')].join(' ');
              const control = e.closest('button, input');
              return {
                text: text(e) + ' ' + (a ? text(a) : ''),
                type: control ? control.type : null,
                href: a ? a.getAttribute('href') : null,
                classes: a ? a.className : '',
                inBrand: a ? !!a.closest('#filter2') : false,
              };
            }"""
        )
        if not klubok_guard.click_allowed(
            info["text"],
            info["type"],
            anchor_href=info["href"],
            anchor_classes=info["classes"] or "",
            anchor_in_brand_list=info["inBrand"],
        ):
            raise Forbidden(f"Заборонений клік: {info['text'].strip()[:60]!r} ({why})")
        locator.click()

    def press(self, key: str) -> None:
        if not klubok_guard.key_allowed(key):
            raise Forbidden(f"Клавіша {key} заборонена")
        self.page.keyboard.press(key)

    def open_form(self) -> browser.Opened:
        with contextlib.suppress(
            _playwright_error()
        ):  # навігація, яку не пустив guard, — «немає форми»
            self.page.goto(self.form_url(), wait_until="load")
        url = self.page.url
        if self.page.locator("#login").count() or urlsplit(url).path in LOGIN_PATHS:
            return browser.Opened(browser.NO_LOGIN, url)
        try:
            self.page.wait_for_selector(FORM_SELECTOR, timeout=FORM_WAIT * 1000)
        except _playwright_error():
            return browser.Opened(browser.NO_FORM, url)
        return browser.Opened(browser.FORM, url)

    def close(self) -> None:
        self.context.close()


KLUBOK_USER = snapshot.USER  # її акаунт: `#user_id` форми (D2) і `user_id` у `get-items` (D3)
FORM_URL = "https://klubok.com/item/add"


def profile_dir() -> Path:
    return config.data_dir() / "klubok-profile"


class LiveSession(Session):
    """Живий Клубок: видимий Chrome з профілем Клубку, guard публікації. Ctrl+C не закриває Chrome
    (`handle_sigint=False`): сесія доробляє поточну Картку сама."""

    def __init__(self, playwright: Any, publishing: bool = False, human: bool = False) -> None:
        self._setup(allow_upload=True, publishing=publishing, human=human)
        profile = profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        self.context = playwright.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False, no_viewport=True,  # KLB-007: вікно = екран
            args=list(browser.BACKGROUND_ARGS), handle_sigint=False,
        )  # fmt: skip
        self._attach()

    def form_url(self) -> str:
        return FORM_URL


def pick_brand(brand: str, options: list[str]) -> str | None:
    """Підказка Клубку для бренду: «Бренд (Кирилиця)», інакше перша, що починається з бренду."""
    wanted = brand.strip().casefold()
    starts = [o for o in options if o.strip().casefold().startswith(wanted)]
    exact = [o for o in starts if o.strip().casefold() == f"{wanted} {SUFFIX}".casefold()]
    return (exact or starts or [None])[0]


def verify_problems(ad: fields.KlubokAd, state: dict[str, Any]) -> list[str]:
    """Що в `formData` не збігається з тим, що мало бути (назви полів)."""
    item = state["userItem"]
    bad = []

    def check(name: str, ok: bool) -> None:
        if not ok:
            bad.append(name)

    check("назва", item["item_name"] == ad.name)
    check("опис", _lines(item["text"]) == _lines(ad.text))
    check("ціна", item["price"] == ad.price)
    check("категорія", str(state["categoryId"]) == str(ad.leaf.id))
    check("стать", item["sex"] == ad.sex if ad.sex is not None else item["sex"] in ("", None))
    check("стан", item["condition"] == ad.condition)
    check("розміри", sorted(item["size"]) == sorted(ad.sizes))
    check("колір", (item["color"] if item["color"] != "" else None) == ad.color)
    if ad.brand:
        check("бренд", item["brand_name"].casefold().startswith(ad.brand.casefold()))
    check("порядок фото", state["images"] == [p.name for p in ad.photos])
    return bad


def _lines(text: str) -> str:
    return re.sub(r"\r\n?", "\n", text).strip()


class KlubokDriver:
    """Заповнює форму Клубку (Playwright). Усі кліки — крізь `Session.click`."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.page = session.page
        self.notes: list[str] = []

    def loc(self, selector: str) -> Any:
        return self.page.locator(selector)

    def fill(self, ad: fields.KlubokAd) -> None:
        self.notes = []
        self.choose_category(ad.leaf)
        self.loc("#name").fill(ad.name, timeout=10_000)
        self.loc("textarea[name=text]").fill(ad.text, timeout=10_000)
        self.loc("#price").fill(str(ad.price), timeout=10_000)
        if ad.sex is not None:
            self._choose(f"#sex{ad.sex}", "стать")
        self._choose(f"#condition{ad.condition}", "стан")
        for size in ad.sizes:
            self._choose(f'input[name="size[]"][value="{size}"]', f"розмір {size}")
        if ad.color is not None:
            self._choose(f'input[name=color][value="{ad.color}"]', "колір")
        if ad.brand:
            self.brand(ad.brand)
        self.photos(list(ad.photos))

    def _choose(self, selector: str, what: str) -> None:
        target = self.loc(selector)
        try:
            target.wait_for(state="visible", timeout=FIELD_WAIT * 1000)
        except _playwright_error():
            raise KlubokError("klubok_field_missing", f"На формі немає поля: {what}") from None
        self.session.click(target, what)

    def choose_category(self, leaf: catalog.Leaf) -> None:
        """Шлях листка по селектах: категорія → підкатегорія → ціль (4-й рівень — в optgroup)."""
        path = leaf.path
        self._select("#category", path[0])
        self._select("select[name=subcategory]", path[1])
        if len(path) == 3:
            self._select("select[name=targetcategory]", path[2])
        elif len(path) == 4:
            self._select("select[name=targetcategory]", path[3], group=path[2])
        self.page.wait_for_function(
            "id => String(formData.categoryId()) === id", arg=str(leaf.id), timeout=5_000
        )

    def _select(self, selector: str, name: str, group: str | None = None) -> None:
        select = self.loc(selector)
        try:
            select.wait_for(state="visible", timeout=FIELD_WAIT * 1000)
        except _playwright_error():
            raise KlubokError(
                "klubok_field_missing", f"На формі немає списку категорій: {name}"
            ) from None
        index = select.evaluate(
            """(s, [name, group]) => {
              const options = Array.from(s.options);
              return options.findIndex(o =>
                o.textContent.trim() === name &&
                (group === null || (o.parentElement.tagName === 'OPTGROUP' &&
                                    o.parentElement.label === group)));
            }""",
            [name, group],
        )
        if index < 0:
            where = f"{group} / {name}" if group else name
            raise KlubokError("klubok_field_missing", f"У списку категорій немає «{where}»")
        select.select_option(index=index)

    def brand(self, brand: str) -> None:
        """Набрати бренд → підказка «Бренд (Кирилиця)» / перша, що починається з бренду → клік;
        немає — лишити набраний текст (живу поведінку перевіряє KLB-003)."""
        field = self.loc("#brand")
        field.click()
        field.fill("")
        field.press_sequentially(brand, delay=BRAND_TYPE_DELAY)
        options = self.loc("#filter2 .dropdown-item")
        try:
            options.first.wait_for(state="visible", timeout=BRAND_WAIT * 1000)
        except _playwright_error():
            self.notes.append(f"Бренд «{brand}»: підказок немає — лишився вільний текст")
            return
        names = [t.strip() for t in options.all_inner_texts()]
        chosen = pick_brand(brand, names)
        if chosen is None:
            self.notes.append(
                f"Бренд «{brand}»: серед підказок {names[:5]} його немає — вільний текст"
            )
            return
        self.session.click(options.nth(names.index(chosen)), "підказка бренду")
        self.notes.append(f"Бренд «{brand}»: вибрано підказку «{chosen}»")

    def photos(self, paths: list[Path]) -> None:
        """По одному: файл → чекаємо, поки в `formData.images` стане N і нічого не вантажиться."""
        chooser = self.loc("input[type=file]")
        for count, path in enumerate(paths, start=1):
            chooser.set_input_files(str(path))
            try:
                self.page.wait_for_function(
                    """n => formData.images.length === n && formData.numberOfUploading() === 0
                           && !formData.images.some(i => i.uploading)""",
                    arg=count,
                    timeout=PHOTO_WAIT * 1000,
                )
            except _playwright_error():
                raise KlubokError(
                    "klubok_photos",
                    f"Фото {count} з {len(paths)} ({path.name}) не завантажилось за {PHOTO_WAIT:.0f} с",
                ) from None

    def state(self) -> dict[str, Any]:
        return self.page.evaluate(
            """() => ({userItem: formData.userItem, categoryId: formData.categoryId(),
                       images: formData.images.map(i => i.title)})"""
        )

    def verify(self, ad: fields.KlubokAd) -> None:
        bad = verify_problems(ad, self.state())
        if bad:
            raise KlubokError(
                "klubok_verify", "Форма Клубку не збігається з планом: " + ", ".join(bad)
            )


def plan_lines(ad: fields.KlubokAd) -> list[str]:
    """Що буде на формі — для консолі (без повного тексту й шляхів до фото)."""
    sex = (
        {0: "Девочке", 1: "Мальчику", 2: "Любой пол"}.get(ad.sex, "—")
        if ad.sex is not None
        else "— (у дорослих немає)"
    )
    color = (
        "—"
        if ad.color is None
        else f"ключ {ad.color}" + (" (Разноцветный)" if ad.color == fields.COLOR_MULTI else "")
    )
    return [
        "Категорія: " + " / ".join(ad.leaf.path) + f" ({ad.leaf.id})",
        f"Назва: {ad.name}",
        "Опис: " + ad.text.replace("\n", " ⏎ ")[:120],
        f"Ціна: {ad.price}",
        f"Стать: {sex}",
        "Стан: " + ("Новое" if ad.condition == fields.CONDITION_NEW else "Б/у"),
        "Розміри: " + (", ".join(map(str, ad.sizes)) or "—"),
        f"Колір: {color}",
        f"Бренд: {ad.brand or '—'}",
        f"Фото: {len(ad.photos)} (по одному)",
    ]


def open_readonly(path: Path) -> sqlite3.Connection:
    """База лише для читання: стара схема — зрозуміла помилка, без міграції (рішення Архітектора)."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as error:
        raise KlubokError("klubok_db", f"Базу не відкрито лише для читання: {error}") from None
    if version != db.SCHEMA_VERSION:
        conn.close()
        raise KlubokError(
            "klubok_db",
            f"Версія бази {version}, програма очікує {db.SCHEMA_VERSION}: спершу відкрийте базу "
            "програмою (міграція), команда Клубку нічого не змінює",
        )
    return conn


def dry_fill(
    conn: sqlite3.Connection,
    root: Path,
    post_ids: list[int],
    session: Session,
    make_driver: Callable[[Session], KlubokDriver],
    screens: Path,
    log: Callable[[str], None] = print,
) -> list[Path]:
    """Заповнити форму Клубку для кожної Картки й зняти екран. Нічого не публікує, базу не пише."""
    if not 1 <= len(post_ids) <= shafa_form.MAX_CARDS:
        raise ValueError(f"За прогін — від 1 до {shafa_form.MAX_CARDS} Карток")
    screens.mkdir(parents=True, exist_ok=True)
    shots = []
    for post_id in post_ids:
        card = shafa_form.find_card(conn, post_id)
        if card is None:
            log(f"Пост {post_id}: Картки немає")
            continue
        ad = fields.ad(card, root)
        if isinstance(ad, fields.Skip):
            log(f"Пост {post_id}: Клубок не візьме — {ad.code}: {ad.text}")
            continue
        log(f"Пост {post_id} (статус Шафи: {card.status}): план для Клубку")
        for line in plan_lines(ad):
            log(f"  · {line}")
        opened = session.open_form()
        if not opened.ok:
            what = "немає входу" if opened.state == browser.NO_LOGIN else "немає форми"
            log(f"Пост {post_id}: Клубок — {what}. {opened.detail()}")
            return shots
        driver = make_driver(session)
        try:
            driver.fill(ad)
            driver.verify(ad)
        except KlubokError as error:
            log(f"Пост {post_id}: {error.code} — {error.text}")
            continue
        shot = screens / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-klubok-{post_id}.png"
        session.page.screenshot(path=str(shot), full_page=True)
        shots.append(shot)
        log(f"Пост {post_id}: форму Клубку заповнено й звірено, НЕ опубліковано. Екран: {shot}")
        for note in driver.notes:
            log(f"  · {note}")
    return shots
