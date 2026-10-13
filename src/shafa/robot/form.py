"""Заповнення форми «Додати товар» за кроками з `plan` — БЕЗ публікації (ROBOT-001).

`execute` знає лише інтерфейс драйвера (тести — з підробним). `PageDriver` — справжній, через
`browser.Session`: усі кліки й клавіші — крізь захист. Поле не знайдено → `FieldMissing`: робот
зупиняється, знімає екран, Картка йде в «Проблеми» (не вгадуємо).
"""

import re
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from shafa import config, review
from shafa.db import Status, now
from shafa.robot import browser
from shafa.robot.plan import PlanError, Step, plan

MAX_CARDS = 3  # більше за один прогін не можна (тікет)
UPLOAD_TIMEOUT = 120  # с на всі фото


class FieldMissing(Exception):
    """Поля форми не знайдено (Шафа змінила вигляд?). `args[0]` — назва поля на Шафі."""


class Driver(Protocol):
    def set_photos(self, paths: list[str]) -> int: ...
    def check_own_photos(self) -> bool: ...
    def fill(self, name: str, value: str, label: str) -> None: ...
    def choose_category(self, parts: list[str], slug: str) -> None: ...
    def pick(self, label: str, values: list[str]) -> None: ...
    def keywords(self, words: list[str]) -> None: ...
    def brand(self, name: str) -> bool: ...


def execute(steps: list[Step], driver: Driver) -> list[str]:
    """Виконати кроки по черзі. Розмір/колір/стан/характеристики — вибір у блоці з підписом.

    Повертає нотатки (бренд не знайшовся на Шафі — поле лишилось порожнім).
    """
    notes = []
    for step in steps:
        if step.kind == "photos":
            shown = driver.set_photos(step.value)
            if shown < len(step.value):
                notes.append(
                    f"Фото у формі: {shown} з {len(step.value)} — частина не завантажилась"
                )
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
            raise FieldMissing(label)
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
        browser.pause(3.0, 4.0)
        counter = self.page.get_by_text(re.compile(r"\(\d+ з \d+ завантажено\)")).first
        match = re.search(r"\((\d+) з", counter.inner_text()) if counter.count() else None
        return int(match.group(1)) if match else len(paths)

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
        block = self._block(label)
        for value in values:
            button = block.get_by_role("button", name=value, exact=True)
            if not button.count():
                button = block.get_by_text(value, exact=True)
            if button.count():
                self.session.click(button.first, f"{label}: {value}")
                continue
            combo = block.locator("input[role=combobox]")
            if not combo.count():
                raise FieldMissing(f"{label}: {value}")
            combo.first.click()
            combo.first.fill(value)
            browser.pause()
            option = self.page.locator("[class*=option]").get_by_text(value, exact=True)
            if not option.count():
                raise FieldMissing(f"{label}: {value}")
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
        if not session.open_form():
            log("Немає входу на Шафу в профілі робота. Запустіть: python -m shafa robot --login")
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
