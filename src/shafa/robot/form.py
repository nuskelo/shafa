"""Заповнення форми «Додати товар» за кроками з `plan` (ROBOT-001) і клік «Додати річ» (ROBOT-002).

`execute` знає лише інтерфейс драйвера (тести — з підробним). `PageDriver` — справжній, через
`browser.Session`: усі кліки й клавіші — крізь захист. Поле не знайдено → `FieldMissing`: робот
зупиняється, знімає екран, Картка йде в «Проблеми» (не вгадуємо).
"""

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from shafa import config, review
from shafa.db import Status, now
from shafa.robot import browser, guard
from shafa.robot.plan import PlanError, Step, plan

MAX_CARDS = 3  # більше за один прогін не можна (тікет)
UPLOAD_TIMEOUT = 120  # с на всі фото


SUBMIT_TIMEOUT = 60  # с на відповідь Шафи після «Додати річ»; без певної відповіді — «Перевірте»


class FieldMissing(Exception):
    """Поля форми не знайдено (Шафа змінила вигляд?). `args[0]` — назва поля на Шафі.

    `details` — що є на формі натомість (для журналу, CORE-008 D): кнопки блоку й підписи розміру.
    """

    def __init__(self, label: str, details: str = "") -> None:
        super().__init__(label)
        self.details = details


class BrandMissing(Exception):
    """Бренду немає у списку Шафи (публікація зупиняється, OQ-004). `args[0]` — бренд."""


class PhotosShort(Exception):
    """Форма показує не всі фото. `args[0]` — текст для Продавчині."""


@dataclass(frozen=True)
class Submitted:
    """Що сталося після «Додати річ».

    `sent` — запит публікації пройшов захист (пішов на Шафу); `blocked` — захист його зупинив.
    Успіх — лише `product_id` з відповіді `createProduct` (D6).
    """

    sent: bool
    blocked: bool = False
    product_id: int | None = None
    errors: list[str] = field(default_factory=list)
    failure: str | None = None


def outcome(sent: bool, blocked: bool, body: object, failure: str | None = None) -> Submitted:
    """Відповідь `WEB_CreateProduct` → результат. Усе незрозуміле — без `product_id`."""
    created = None
    errors: list[str] = []
    if isinstance(body, dict):
        result = (body.get("data") or {}).get("createProduct") or {}
        if isinstance(result, dict):
            product = result.get("createdProduct") or {}
            raw_id = product.get("id") if isinstance(product, dict) else None
            created = int(raw_id) if str(raw_id or "").isdigit() else None
            for error in result.get("errors") or []:
                messages = [m.get("message", "") for m in error.get("messages") or []]
                errors.append(f"{error.get('field') or 'форма'}: {'; '.join(messages)}")
        if not created and body.get("errors"):
            failure = failure or "; ".join(str(e.get("message", e)) for e in body["errors"])
    return Submitted(sent, blocked, created, errors, failure)


class Driver(Protocol):
    def set_photos(self, paths: list[str]) -> int: ...
    def check_own_photos(self) -> bool: ...
    def fill(self, name: str, value: str, label: str) -> None: ...
    def choose_category(self, parts: list[str], slug: str) -> None: ...
    def pick(self, label: str, values: list[str]) -> None: ...
    def keywords(self, words: list[str]) -> None: ...
    def brand(self, name: str) -> bool: ...
    def submit(self) -> Submitted: ...


def execute(steps: list[Step], driver: Driver, strict: bool = False) -> list[str]:
    """Виконати кроки по черзі. Розмір/колір/стан/характеристики — вибір у блоці з підписом.

    Повертає нотатки (бренд не знайшовся на Шафі — поле лишилось порожнім). `strict` (публікація):
    бренд не знайдено → `BrandMissing`, не всі фото → `PhotosShort` — замість нотаток.
    """
    notes = []
    for step in steps:
        if step.kind == "photos":
            shown = driver.set_photos(step.value)
            if shown < len(step.value):
                note = f"Фото у формі: {shown} з {len(step.value)} — частина не завантажилась"
                if strict:
                    raise PhotosShort(note)
                notes.append(note)
        elif step.kind == "own_photos":
            if not driver.check_own_photos():
                notes.append("Галочки «фото зроблені самостійно» у формі немає — не ставилась")
        elif step.kind == "text":
            driver.fill(step.target, step.value, step.label)
        elif step.kind == "category":
            driver.choose_category(step.value, step.target)
        elif step.kind == "keywords":
            driver.keywords(step.value)
        elif step.kind == "brand":
            if not driver.brand(step.value):
                if strict:
                    raise BrandMissing(step.value)
                notes.append(f"Бренду «{step.value}» немає на Шафі — не заповнено (OQ-004)")
        elif step.kind in ("condition", "size_system"):
            driver.pick(step.label, [step.value])
        elif step.kind in ("sizes", "colors", "choice"):
            driver.pick(step.label, step.value)
        else:
            raise ValueError(f"Невідомий крок {step.kind}")
    return notes


# --- Справжній драйвер ---

# Підписи блоків у формі (з дослідження 13.10.2026); пошук — за початком тексту підпису.
LABELS = {
    "Стан": "Виберіть стан",
    "Колір": "Виберіть до 2 відтінків",
    "Ключові слова": "Введіть ключові слова",
    "Бренд": "Бренд",
    "Розмір": "Розмір",
}

FIND_BLOCK = """([label, mark]) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const own = el => norm([...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));
  const starts = t => t === label || t.startsWith(label + ' ') || t.startsWith(label + '*') || t.startsWith(label + ' (');
  for (const el of document.querySelectorAll('body *')) {
    if (!starts(own(el)) || el.closest('header, nav, footer')) continue;
    let box = el;
    for (let i = 0; i < 6 && box; i++, box = box.parentElement) {
      if (box.querySelector('button, input[role=combobox], input[type=checkbox]')) {
        box.setAttribute('data-robot-block', mark);
        return true;
      }
    }
  }
  return false;
}"""


# Що є на формі, коли потрібного поля немає: кнопки й вкладки блоку (якщо блок знайдено) та всі
# підписи зі словом «розмір» (вкладки систем розмірів: «Європейський», «Український»…).
LIST_FIELDS = """(mark) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const texts = els => [...new Set(els.map(e => norm(e.innerText || e.textContent))
    .filter(t => t && t.length <= 40))].slice(0, 40);
  const block = mark ? document.querySelector(`[data-robot-block='${mark}']`) : null;
  return {
    block: block ? texts([...block.querySelectorAll(
      'button, [role=tab], [role=radio], [role=option], label')]) : [],
    sizes: texts([...document.querySelectorAll('body *')].filter(
      e => !e.children.length && /розмір/i.test(e.textContent || ''))),
  };
}"""


def describe_form(page: Any, mark: str | None = None) -> str:
    """Перелік назв полів і вкладок розміру на формі — для журналу, коли поля не знайдено."""
    try:
        found = page.evaluate(LIST_FIELDS, mark)
    except Exception:  # noqa: BLE001 — діагностика не має ховати справжню причину
        return ""
    parts = []
    if found.get("block"):
        parts.append("у блоці: " + ", ".join(found["block"]))
    if found.get("sizes"):
        parts.append("підписи розміру: " + ", ".join(found["sizes"]))
    return "; ".join(parts) or "на формі не знайдено нічого зі словом «розмір»"


class PageDriver:
    """Дії у формі через `Session` (захищені кліки й клавіші)."""

    def __init__(self, session: browser.Session) -> None:
        self.session = session
        self.page = session.page
        self._marks = 0

    def _block(self, label: str) -> Any:
        self._marks += 1
        mark = f"b{self._marks}"
        if not self.page.evaluate(FIND_BLOCK, [LABELS.get(label, label), mark]):
            raise FieldMissing(label, describe_form(self.page))
        return self.page.locator(f"[data-robot-block='{mark}']")

    def set_photos(self, paths: list[str]) -> int:
        """Завантажити фото у форму; повертає, скільки форма показує завантаженими."""
        inputs = self.page.locator("input[type=file]")
        if not inputs.count():
            raise FieldMissing("Фото")
        before = self.session.uploads
        inputs.first.set_input_files(paths)
        deadline = time.monotonic() + UPLOAD_TIMEOUT
        while self.session.uploads - before < len(paths):
            if time.monotonic() > deadline:
                raise FieldMissing("Фото (не завантажились)")
            self.page.wait_for_timeout(500)
        counter = self.page.get_by_text(re.compile(r"\(\d+ з \d+ завантажено\)")).first
        shown = 0
        for _ in range(40):  # останнє фото форма показує не одразу — до 20 с
            match = re.search(r"\((\d+) з", counter.inner_text()) if counter.count() else None
            shown = int(match.group(1)) if match else len(paths)
            if shown >= len(paths):
                break
            self.page.wait_for_timeout(500)
        browser.pause(2.0, 3.0)
        return shown

    def check_own_photos(self) -> bool:
        """Галочка «фото зроблені самостійно» (D3). У формі 13.10.2026 її немає — тоді False."""
        text = self.page.get_by_text("самостійно").locator("visible=true")
        if not text.count():
            return False
        box = text.first.locator("xpath=ancestor-or-self::label[1]")
        target = box if box.count() else text.first
        checkbox = target.locator("input[type=checkbox]")
        if checkbox.count() and checkbox.first.is_checked():
            return True
        self.session.click(target.first if box.count() else target, "фото самостійні")
        return True

    def fill(self, name: str, value: str, label: str) -> None:
        field = self.page.locator(f"[name='{name}']")
        if not field.count():
            raise FieldMissing(label)
        field.first.fill(value)
        browser.pause()

    def choose_category(self, parts: list[str], slug: str) -> None:
        try:
            self.session.choose_category(parts, slug)
        except LookupError as error:
            raise FieldMissing(f"Розділ: {error}") from error
        self.page.wait_for_load_state("networkidle")

    def pick(self, label: str, values: list[str]) -> None:
        block, mark = self._block(label), f"b{self._marks}"
        for value in values:
            button = block.get_by_role("button", name=value, exact=True)
            if not button.count():
                button = block.get_by_text(value, exact=True)
            if button.count():
                self.session.click(button.first, f"{label}: {value}")
                continue
            combo = block.locator("input[role=combobox]")
            if not combo.count():
                raise FieldMissing(f"{label}: {value}", describe_form(self.page, mark))
            combo.first.click()
            combo.first.fill(value)
            browser.pause()
            option = self.page.locator("[class*=option]").get_by_text(value, exact=True)
            if not option.count():
                raise FieldMissing(f"{label}: {value}", describe_form(self.page, mark))
            self.session.click(option.first, f"{label}: {value}")
            self.session.press("Escape")

    def brand(self, name: str) -> bool:
        """Пошук у полі «Бренд»: лише варіант, що точно збігається. «Додати бренд» — ніколи."""
        combo = self._block("Бренд").locator("input[role=combobox]")
        if not combo.count():
            raise FieldMissing("Бренд")
        combo.first.click()
        combo.first.fill(name)
        browser.pause(1.5, 2.5)
        options = self.page.locator("[class*=option]")
        texts = options.all_inner_texts()  # список перемальовується — читаємо все за раз
        found = next((x.strip() for x in texts if x.strip().casefold() == name.casefold()), None)
        if found:
            exact = re.compile(rf"^\s*{re.escape(found)}\s*$")
            option = options.filter(has_text=exact)
            if option.count():
                self.session.click(option.first, f"бренд {name}")
                return True
        combo.first.fill("")
        self.session.press("Escape")
        return False

    def keywords(self, words: list[str]) -> None:
        combo = self._block("Ключові слова").locator("input[role=combobox]")
        if not combo.count():
            raise FieldMissing("Ключові слова")
        for word in words:
            combo.first.fill(word)
            browser.pause(0.5, 1.0)
            self.session.press("Enter", in_keywords=True)

    def _click_publish(self, button: Any) -> str | None:
        """Клік за квитком. Після кліку Шафа веде на сторінку реклами: клік може «зависнути» на
        навігації, хоч товар уже створено, — таку помилку не рахуємо (відповідь `createProduct`
        усе одно прийде). Відмова захисту — це `Forbidden`, її не ковтаємо. Повертає опис, якщо
        клік завершився помилкою (для журналу)."""
        try:
            self.session.click_publish(button)
        except guard.Forbidden:
            raise
        except Exception as error:  # noqa: BLE001 — тайм-аут навігації після кліку
            return f"клік: {type(error).__name__}: {str(error)[:120]}"
        return None

    def submit(self) -> Submitted:
        """Клік «Додати річ» за квитком і відповідь Шафи. Помилка до кліку — `FieldMissing`."""
        exact = re.compile(rf"^\s*{guard.PUBLISH_BUTTON}\s*$")
        button = self.page.locator("button[type=submit]").filter(has_text=exact)
        button = button.locator("visible=true")
        if button.count() != 1:
            raise FieldMissing(f"Кнопка «{guard.PUBLISH_BUTTON}»")
        creates, blocked = self.session.creates, len(self.session.blocked)
        body, failure = None, None
        try:
            with self.page.expect_response(
                lambda r: r.request.method == "POST" and is_create_request(r.request),
                timeout=SUBMIT_TIMEOUT * 1000,
            ) as info:
                failure = self._click_publish(button.first)
            body = info.value.json()
        except Exception as error:  # noqa: BLE001 — тайм-аут, обрив, не JSON, відмова захисту
            failure = f"{type(error).__name__}: {str(error)[:200]}"
        refused = [r for _, r in self.session.blocked[blocked:] if browser.PUBLISH.search(r)]
        return outcome(self.session.creates > creates, bool(refused), body, failure)


def is_create_request(request: Any) -> bool:
    try:
        data = json.loads(request.post_data or "")
    except (ValueError, TypeError):
        return False
    return guard.is_create(request.url, data)


# --- Пробне заповнення ---


def find_card(conn: sqlite3.Connection, post_id: int) -> review.Card | None:
    row = conn.execute(
        "SELECT id FROM items WHERE post_id = ? AND purged_at IS NULL ORDER BY id LIMIT 1",
        (post_id,),
    ).fetchone()
    return review.load_card(conn, row[0]) if row else None


def to_problem(conn: sqlite3.Connection, item_id: int, code: str, text: str) -> None:
    """Картка робота → «Проблеми»; «Перевірити ще раз» поверне її в Схвалені."""
    with conn:
        conn.execute(
            "UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (Status.PROBLEM, code, text, now(), item_id, Status.APPROVED),
        )


def dry_fill(
    conn: sqlite3.Connection,
    root: Path,
    post_ids: list[int],
    session: Any,
    make_driver: Callable[[Any], Driver],
    screens: Path,
    log: Callable[[str], None] = print,
) -> list[Path]:
    """Заповнити форму для кожної Картки й зняти екран. Нічого не публікує. Повертає скріншоти."""
    if not 1 <= len(post_ids) <= MAX_CARDS:
        raise ValueError(f"За прогін — від 1 до {MAX_CARDS} Карток")
    screens.mkdir(parents=True, exist_ok=True)
    shots = []
    for post_id in post_ids:
        card = find_card(conn, post_id)
        if card is None:
            log(f"Пост {post_id}: Картки немає")
            continue
        try:
            steps, notes = plan(card, root)
        except PlanError as error:
            log(f"Пост {post_id}: {error}")
            continue
        opened = session.open_form()
        if not opened.ok:
            if opened.state == browser.NO_LOGIN:
                log(
                    "Немає входу на Шафу в профілі робота. Запустіть: python -m shafa robot --login"
                )
            else:
                shot = screens / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-нема-форми.png"
                session.page.screenshot(path=str(shot), full_page=True)
                log(f"Шафа не показала форму: {opened.detail()}. Екран: {shot}")
            break
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        try:
            notes += execute(steps, make_driver(session))
        except FieldMissing as error:
            shot = screens / f"{stamp}-{post_id}-нема-поля.png"
            session.page.screenshot(path=str(shot), full_page=True)
            to_problem(
                conn, card.id, "robot_field_missing", f"Робот не знайшов на Шафі поле «{error}»"
            )
            log(f"Пост {post_id}: не знайдено поле «{error}» — Картка в Проблемах, екран {shot}")
            if error.details:
                log(f"  · на формі: {error.details}")
            break
        if attempts := session.publish_attempts():
            raise RuntimeError(f"Заблоковано спроби публікації: {attempts}")
        shot = screens / f"{stamp}-{post_id}.png"
        session.page.screenshot(path=str(shot), full_page=True)
        shots.append(shot)
        log(f"Пост {post_id}: форму заповнено, НЕ опубліковано. Екран: {shot}")
        for note in notes:
            log(f"  · {note}")
    return shots


def screens_dir() -> Path:
    return config.data_dir() / "robot-screens"
