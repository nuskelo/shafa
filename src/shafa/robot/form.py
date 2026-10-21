"""Заповнення форми «Додати товар» за кроками з `plan` (ROBOT-001) і клік «Додати річ» (ROBOT-002).

`execute` знає лише інтерфейс драйвера (тести — з підробним). `PageDriver` — справжній, через
`browser.Session`: усі кліки й клавіші — крізь захист. Поле не знайдено → `FieldMissing`: робот
зупиняється, знімає екран, Картка йде в «Проблеми» (не вгадуємо).
"""

import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from shafa import config, review
from shafa.db import Status, now
from shafa.robot import browser, guard, lang
from shafa.robot.plan import PlanError, Step, plan

MAX_CARDS = 3  # більше за один прогін не можна (тікет)
UPLOAD_TIMEOUT = 120  # с на всі фото


SUBMIT_TIMEOUT = 60  # с на відповідь Шафи після «Додати річ»; без певної відповіді — «Перевірте»
# с: скільки чекати «мережа стихла» після розділу (на Шафі фонові запити не вщухають)
IDLE_WAIT = 10


class FieldMissing(Exception):
    """Поля форми не знайдено (Шафа змінила вигляд?). `args[0]` — назва поля на Шафі.

    `details` — що є на формі натомість (для журналу, CORE-008 D): кнопки блоку й підписи розміру.
    `field` і `markup` — поле-список, де зупинився робот, і розмітка його блоку (ROBOT-004, дамп).
    """

    def __init__(self, label: str, details: str = "", field: str = "", markup: str = "") -> None:
        super().__init__(label)
        self.details = details
        self.field = field
        self.markup = markup


class BrandMissing(Exception):
    """Бренд є у довіднику Шафи (знімок), а наживо не знайшовся — збій робота (ROBOT-006).
    `args[0]` — бренд. Картка йде в «Проблеми», сесія триває."""


@dataclass(frozen=True)
class BrandMiss:
    """Пошук бренду наживо нічого не дав: що бачив робот (для журналу, скриншота й розмітки)."""

    name: str
    options: list[str]  # тексти варіантів «як є» (repr покаже `\\xa0`)
    markup: str = ""

    def details(self) -> str:
        shown = self.options[:SHOWN_OPTIONS]
        more = (
            f" … ще {len(self.options) - SHOWN_OPTIONS}" if len(self.options) > len(shown) else ""
        )
        return f"бренд {self.name!r}; варіанти ({len(self.options)}): {shown!r}{more}"


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
    def pick(self, label: str, values: list[str]) -> list[str] | None: ...
    def keywords(self, words: list[str]) -> None: ...

    brand_miss: BrandMiss | None  # що бачив останній невдалий пошук бренду

    def reset(self) -> None: ...  # новий товар — стан попереднього не переходить (ROBOT-008)
    def brand(self, name: str) -> bool: ...
    def untouched(self, labels: list[str]) -> list[str]: ...
    def verify(self) -> None: ...
    def submit(self) -> Submitted: ...


def execute(steps: list[Step], driver: Driver, strict: bool = False) -> list[str]:
    """Виконати кроки по черзі. Розмір/колір/стан/характеристики — вибір у блоці з підписом.
    Наприкінці — звірка заповненого (`verify`): не збіглось → `FieldMissing`.

    Повертає нотатки. Бренду немає на Шафі ні у знімку, ні наживо — поле лишається порожнім, лише
    нотатка (OQ-004, ROBOT-006). `strict` (публікація): бренд є у знімку, а наживо не знайшовся →
    `BrandMissing`, не всі фото → `PhotosShort` — замість нотаток.
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
                if step.known:
                    if strict:
                        raise BrandMissing(step.value)
                    notes.append(
                        f"Бренд «{step.value}» є у довіднику Шафи, але робот його не знайшов"
                    )
                else:
                    notes.append(f"бренд {step.value} не в списку Шафи — лишено порожнім")
        elif step.kind in ("condition", "size_system"):
            notes += driver.pick(step.label, [step.value]) or []
        elif step.kind == "choice" and not step.required:
            notes += optional(driver, step)
        elif step.kind in ("sizes", "size_block", "colors", "choice"):
            notes += driver.pick(step.label, step.value) or []
        elif step.kind == "untouched":
            notes += driver.untouched(step.value)
        else:
            raise ValueError(f"Невідомий крок {step.kind}")
    driver.verify()  # Шафа могла переписати вже заповнене — перед публікацією ще раз звіряємо
    return notes


def optional(driver: Driver, step: Step) -> list[str]:
    """Необов'язкова характеристика («Додаткові характеристики», без * на Шафі): не знайшли поле чи
    значення — не зупиняємо сесію, пропускаємо й пишемо примітку (ROBOT-009). Список варіантів з
    форми лишається в примітці — для діагностики."""
    try:
        return driver.pick(step.label, step.value) or []
    except FieldMissing as error:
        if cleanup := getattr(driver, "abandon", None):
            cleanup(step.label)
        why = f" ({error.details})" if error.details else ""
        wanted = ", ".join(step.value)
        return [
            f"Необов'язкове поле «{step.label}» не заповнено: на формі не знайдено {wanted}{why}"
        ]


# --- Вибір у полі (ROBOT-003) ---

NO_OPTIONS = ("доступних опцій немає", "no options")  # напис порожнього списку — не варіант
SHOWN_OPTIONS = 30  # скільки варіантів списку писати в журнал


class Widget(Protocol):
    """Блок поля на формі. `listed` — у блоці є список з пошуком (combobox), вибране — чипи «×»."""

    listed: bool

    def chips(self) -> list[str]: ...
    def remove(self, chip: str) -> None: ...
    def press(self, value: str) -> bool: ...  # кнопка з таким значенням: клік → True, немає → False
    def search(self, text: str) -> list[str]: ...  # набрати в списку, повернути видимі варіанти
    def choose(self, option: str) -> None: ...
    def close(self) -> None: ...
    def describe(self) -> str: ...


def same(a: str, b: str) -> bool:
    """Той самий текст без регістру й без мови: «Европейский» = «Європейський» (ROBOT-005)."""
    return lang.equivalent(a, b)


def among(value: str, values: list[str]) -> bool:
    return any(same(value, v) for v in values)


ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def brand_key(text: str) -> str:
    """Бренд для порівняння: усі пробіли (і `\\xa0`) — один звичайний, без невидимих, без регістру
    і без діакритики («Waldläufer» = «Waldlaufer», ROBOT-012). Точний збіг після цього, не схожість."""
    plain = unicodedata.normalize("NFKD", text.translate(ZERO_WIDTH))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(plain.split()).casefold()


# Підказка форми, а не значення поля: «Додайте заголовок(до +11%)», «Виберіть…» (ROBOT-006)
HINT = re.compile(
    r"^\s*(додайте|добавьте|введіть|введите|виберіть|выберите|оберіть|select|add|enter|choose)\b"
    r"|\(\s*до\s*\+\d+\s*%\s*\)",
    re.IGNORECASE,
)


def is_hint(text: str) -> bool:
    return bool(HINT.search(text))


def exact(options: list[str], value: str) -> str | None:
    """Точний збіг серед варіантів (без регістру, як у бренді)."""
    return next((o for o in options if same(o, value)), None)


def list_details(label: str, chips: list[str], options: list[str]) -> str:
    """Для журналу: що вибрано в полі й що є у списку."""
    shown = [o for o in options if not among(o, list(NO_OPTIONS))]
    more = f" … ще {len(shown) - SHOWN_OPTIONS}" if len(shown) > SHOWN_OPTIONS else ""
    return (
        f"поле «{label}»; вибрано: {', '.join(chips) or 'нічого'}; "
        f"у списку ({len(shown)}): {', '.join(shown[:SHOWN_OPTIONS]) or 'порожньо'}{more}"
    )


def pick_values(widget: Widget, label: str, values: list[str]) -> list[str]:
    """Вибрати `values` у полі. Повертає нотатки (що Шафа поставила сама і що прибрано).

    Список: уже вибране потрібне — не чіпаємо; чужі чипи (Шафа заповнює сама після розділу) —
    прибираємо. Після набору варіантів немає → очистити поле, прочитати весь список, точний збіг.
    Не знайшли — `FieldMissing` з чипами й варіантами. Наприкінці чипи мають бути рівно наші.
    """
    notes = []
    kept = widget.chips() if widget.listed else []
    for chip in kept:
        if not among(chip, values):
            widget.remove(chip)
            notes.append(f"{label}: Шафа поставила сама «{chip}» — прибрано")
    kept = [c for c in kept if among(c, values)]
    chosen = []
    for value in values:
        if among(value, kept) or widget.press(value):
            continue
        if not widget.listed:
            raise FieldMissing(f"{label}: {value}", widget.describe())
        options = widget.search(value)
        found = exact(options, value)
        if not found:
            options = widget.search("")
            found = exact(options, value)
        if not found:
            raise FieldMissing(f"{label}: {value}", list_details(label, widget.chips(), options))
        widget.choose(found)
        chosen.append(value)
    if not widget.listed:
        return notes
    widget.close()
    now = widget.chips()
    lost = [v for v in kept + chosen if not among(v, now)]
    alien = [c for c in now if not among(c, values)]
    if lost or alien:
        raise FieldMissing(
            f"{label}: на формі {', '.join(now) or 'порожньо'}, треба {', '.join(values)}",
            list_details(label, now, []),
        )
    return notes


def same_text(got: str, value: str, price: bool = False) -> bool:
    """Значення поля збігається з потрібним (ціна — за цифрами: «1 200» = «1200»)."""
    if price:
        return re.sub(r"\D", "", got) == re.sub(r"\D", "", value)
    return " ".join(got.split()) == " ".join(value.split())


# --- Справжній драйвер ---

# Підписи блоків у формі (з дослідження 13.10.2026); пошук — за початком тексту підпису.
LABELS = {
    "Стан": "Виберіть стан",
    "Колір": "Виберіть до 2 відтінків",
    "Ключові слова": "Введіть ключові слова",
    "Бренд": "Бренд",
    "Розмір": "Розмір",
}

# Кнопка «×» чипа (вибране значення списку): react-select — клас «…Remove», aria-label «Remove …».
REMOVE = "[class*=emove], [aria-label^='Remove'], [aria-label^='Видалити'], [aria-label^='Удалить']"

# Блок, де вибрано максимум, не має поля введення — лише чипи (ROBOT-012): шукаємо й за кнопкою «×».
FIND_BLOCK = """([labels, mark]) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const own = el => norm([...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));
  const starts = t => labels.some(label => t === label || t.startsWith(label + ' ')
    || t.startsWith(label + '*') || t.startsWith(label + ' ('));
  for (const el of document.querySelectorAll('body *')) {
    if (!starts(own(el)) || el.closest('header, nav, footer')) continue;
    let box = el;
    for (let i = 0; i < 6 && box; i++, box = box.parentElement) {
      if (box.querySelector("button, input[role=combobox], input[type=checkbox], __REMOVE__")) {
        box.setAttribute('data-robot-block', mark);
        el.setAttribute('data-robot-label', mark);
        return true;
      }
    }
  }
  return false;
}""".replace("__REMOVE__", REMOVE)


OPTION = "[role=option], [class*=option], [class*=Option]"

# Що є на формі, коли потрібного поля немає: кнопки й вкладки блоку (якщо блок знайдено) та
# підписи зі словом «розмір» (вкладки систем розмірів: «Європейський», «Український»…). Не
# підписи: чипи (вибрані значення й ключові слова), варіанти списків, поля введення.
LIST_FIELDS = f"""(mark) => {{
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const texts = els => [...new Set(els.map(e => norm(e.innerText || e.textContent))
    .filter(t => t && t.length <= 40))].slice(0, 40);
  const chip = e => e.closest("{OPTION}, [class*=ultiValue], [class*=ulti-value], [class*=hip]")
    || [e, e.parentElement, e.parentElement?.parentElement].some(
      b => b && [...b.children].some(c => c !== e && c.matches("{REMOVE}")));
  const block = mark ? document.querySelector(`[data-robot-block='${{mark}}']`) : null;
  return {{
    block: block ? texts([...block.querySelectorAll(
      'button, [role=tab], [role=radio], [role=option], label')]) : [],
    sizes: texts([...document.querySelectorAll('body *')].filter(
      e => !e.children.length && /розмір|размер/i.test(e.textContent || '')
        && !e.closest('input, textarea, [contenteditable]') && !chip(e))),
  }};
}}"""

# Чипи блоку (ROBOT-004): чип — елемент з текстом і однією кнопкою «прибрати» без тексту: «×»
# текстом, SVG, кнопка, role=button, aria-label, клас remove|delete|clear|close|cross. Не чип:
# кнопка очищення всього поля (клас «indicator»; спільна для кількох чипів; поруч із полем
# введення) і значок біля підпису поля. Кнопка чипа позначається `data-robot-chip` (номер).
CHIPS = f"""(mark) => {{
  const norm = s => (s || '').replace(/[×✕✖]/g, ' ').replace(/\\s+/g, ' ').trim();
  const text = e => norm(e.innerText || e.textContent);
  const cls = e => String(e.getAttribute('class') || '');
  const block = document.querySelector(`[data-robot-block='${{mark}}']`);
  if (!block) return [];
  block.querySelectorAll('[data-robot-chip]').forEach(e => e.removeAttribute('data-robot-chip'));
  const label = block.querySelector(`[data-robot-label='${{mark}}']`);
  const button = e => !e.matches('input, textarea, select, option, [role=option]') && (
    e.matches("{REMOVE}, svg, button, [role=button], [aria-label]")
    || /remove|delete|clear|close|cross/i.test(cls(e))
    || (!e.children.length && /^[×✕✖]$/.test((e.textContent || '').trim())));
  const clearAll = e => /indicator/i.test(cls(e))
    || /clear.?all|очистити все|очистить все/i.test(e.getAttribute('aria-label') || '');
  const icons = new Set();
  for (const e of block.querySelectorAll('*')) {{
    if (!button(e) || text(e) || e.querySelector('input')) continue;
    let icon = e;  // найвищий елемент без тексту навколо кнопки
    while (icon.parentElement && icon.parentElement !== block && !text(icon.parentElement)
           && !icon.parentElement.querySelector('input')) icon = icon.parentElement;
    if ([icon, ...icon.querySelectorAll('*')].some(clearAll)) continue;
    icons.add(icon);
  }}
  const used = new Set(), chips = [];
  for (const icon of icons) {{
    const box = icon.parentElement;
    if (!box || box === block || used.has(box) || box.querySelector('input')) continue;
    if (label && (box.contains(label) || label.contains(box))) continue;
    if ([...icons].filter(i => box.contains(i)).length !== 1) continue;
    const name = text(box);
    if (!name || name.length > 80) continue;
    used.add(box);
    icon.setAttribute('data-robot-chip', String(chips.length));
    chips.push(name);
  }}
  return chips;
}}"""

# Видимі варіанти відкритого списку (найглибші елементи-варіанти), позначені `data-robot-option`.
OPTIONS = f"""() => {{
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  document.querySelectorAll('[data-robot-option]').forEach(e => e.removeAttribute('data-robot-option'));
  const found = [...document.querySelectorAll("{OPTION}")].filter(e =>
    !e.querySelector("{OPTION}") && e.getClientRects().length
    && !/notice|NoOptions|no-options/i.test(String(e.className || '')));
  return found.map((e, i) => {{
    e.setAttribute('data-robot-option', String(i));
    return norm(e.innerText || e.textContent);
  }});
}}"""

# Ті самі варіанти, але тексти «як є» (без стискання пробілів): для журналу, `repr` покаже `\\xa0`.
OPTIONS_RAW = f"""() => [...document.querySelectorAll("{OPTION}")].filter(e =>
  !e.querySelector("{OPTION}") && e.getClientRects().length
  && !/notice|NoOptions|no-options/i.test(String(e.className || ''))).map(e => e.textContent || '')"""

# Розмітка блоку поля і відкритих меню поза ним (ROBOT-004): копії без скриптів і без `value`
# прихованих полів введення (там бувають токени). Меню всередині блоку — уже в блоці.
MENU = "[role=listbox], [class*=menu], [class*=Menu]"
MARKUP = f"""(mark) => {{
  const block = document.querySelector(`[data-robot-block='${{mark}}']`);
  if (!block) return [];
  const clean = el => {{
    const copy = el.cloneNode(true);
    copy.querySelectorAll('script, style, noscript').forEach(e => e.remove());
    copy.querySelectorAll('input[type=hidden]').forEach(e => e.removeAttribute('value'));
    return copy.outerHTML;
  }};
  const menus = [...document.querySelectorAll("{MENU}")].filter(e => e.getClientRects().length
    && !block.contains(e) && !e.closest('header, nav, footer'));
  return [block, ...menus.filter(m => !menus.some(o => o !== m && o.contains(m)))].map(clean);
}}"""
MARKUP_LIMIT = 50 * 1024  # байтів у файлі розмітки

LIST_WAIT = 5  # с чекаємо, поки список покаже варіанти
FIELD_WAIT = (
    2000  # мс на поле введення списку: після вибору максимуму Шафа його прибирає (ROBOT-012)
)
BRAND_WAIT = 6  # с чекаємо точний збіг бренду (список домальовується)
BRAND_STILL = 3  # с без змін у непорожньому списку — далі чекати нічого


def markup(page: Any, mark: str | None) -> str:
    """Розмітка блоку поля й відкритого меню варіантів — для файлу поруч зі скриншотом."""
    if mark is None:
        return ""
    try:
        parts = page.evaluate(MARKUP, mark)
    except Exception:  # noqa: BLE001 — діагностика не має ховати справжню причину
        return ""
    if not parts:
        return ""
    text = "<!-- блок поля -->\n" + parts[0]
    if parts[1:]:
        text += "\n<!-- меню варіантів -->\n" + "\n".join(parts[1:])
    return text


def save_markup(error: FieldMissing, shot: Path) -> Path | None:
    """Розмітка поля-списку поруч зі скриншотом: `<час>-<пост>-<поле>.html`, до 50 КБ."""
    if not error.markup:
        return None
    field = re.sub(r'[\\/:*?"<>|\s]+', "-", error.field).strip("-.") or "поле"
    path = shot.with_name(f"{shot.stem.removesuffix('-нема-поля')}-{field}.html")
    data = error.markup.encode("utf-8")[:MARKUP_LIMIT].decode("utf-8", errors="ignore")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    except OSError:
        return None
    return path


def describe_form(page: Any, mark: str | None = None, typed: set[str] | None = None) -> str:
    """Перелік назв полів і вкладок розміру на формі — для журналу, коли поля не знайдено.

    `typed` — що робот сам набрав (ключові слова, назва…): такі тексти — не підписи.
    """
    try:
        found = page.evaluate(LIST_FIELDS, mark)
    except Exception:  # noqa: BLE001 — діагностика не має ховати справжню причину
        return ""
    typed_keys = {" ".join(t.split()).casefold() for t in typed or ()}
    labels = [
        t for t in found.get("sizes") or [] if " ".join(t.split()).casefold() not in typed_keys
    ]
    parts = []
    if found.get("block"):
        parts.append("у блоці: " + ", ".join(found["block"]))
    if labels:
        parts.append("підписи розміру: " + ", ".join(labels))
    return "; ".join(parts) or "на формі не знайдено нічого зі словом «розмір»"


class PageWidget:
    """Блок поля на справжній сторінці (усі кліки й клавіші — крізь захист `Session`)."""

    def __init__(self, driver: "PageDriver", label: str, mark: str) -> None:
        self.driver, self.label, self.mark = driver, label, mark
        self.page, self.session = driver.page, driver.session
        self.block = self.page.locator(f"[data-robot-block='{mark}']")
        self.input = self.block.locator("input[role=combobox]")
        # Поле-список і без поля введення: вибрано максимум — Шафа його прибирає (ROBOT-012)
        self.listed = bool(self.input.count()) or bool(self.chips())

    def chips(self) -> list[str]:
        return list(self.page.evaluate(CHIPS, self.mark))

    def remove(self, chip: str) -> None:
        chips = self.chips()  # заново: номери чипів після кожної зміни інші
        index = next(i for i, c in enumerate(chips) if same(c, chip))
        button = self.block.locator(f"[data-robot-chip='{index}']")
        self.session.click(button.first, f"{self.label}: прибрати «{chip}»")

    def press(self, value: str) -> bool:
        for spelling in lang.variants(value):  # Шафа показує то українською, то російською
            button = self.block.get_by_role("button", name=spelling, exact=True)
            if not button.count() and not self.listed:
                button = self.block.get_by_text(spelling, exact=True)
            if button.count():
                self.session.click(button.first, f"{self.label}: {value}")
                return True
        return False

    def options(self) -> list[str]:
        return [o for o in self.page.evaluate(OPTIONS) if not among(o, list(NO_OPTIONS))]

    def has_input(self) -> bool:
        """Поле введення є (чекаємо `FIELD_WAIT`, не 30 с): після вибору максимуму його немає."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            self.input.first.wait_for(state="attached", timeout=FIELD_WAIT)
        except PlaywrightTimeout:
            return False
        return True

    def search(self, text: str) -> list[str]:
        if not self.has_input():
            return []  # місця для нового значення немає — далі `FieldMissing` з чипами
        self.input.first.click()
        self.input.first.fill(text)
        browser.pause()
        deadline = time.monotonic() + LIST_WAIT
        options = self.options()
        while not options and time.monotonic() < deadline:
            if not text:
                self.session.press("ArrowDown")  # порожнє поле: список відкривається стрілкою
            self.page.wait_for_timeout(500)
            options = self.options()
        return options

    def choose(self, option: str) -> None:
        index = next((i for i, o in enumerate(self.page.evaluate(OPTIONS)) if o == option), None)
        if index is None:
            raise FieldMissing(
                f"{self.label}: {option}", list_details(self.label, self.chips(), [])
            )
        self.session.click(
            self.page.locator(f"[data-robot-option='{index}']").first, f"{self.label}: {option}"
        )
        self.session.press("Escape")

    def close(self) -> None:
        """Закрити список. Немає поля введення — закрито й порожньо, не помилка (ROBOT-012)."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if self.input.count():
            try:
                if self.input.first.input_value(timeout=FIELD_WAIT):
                    self.input.first.fill("", timeout=FIELD_WAIT)
            except PlaywrightTimeout:
                pass  # поле зникло між перевіркою й читанням
        self.session.press("Escape")
        browser.pause(0.5, 1.0)

    def describe(self) -> str:
        return describe_form(self.page, self.mark, self.driver.typed)


class PageDriver:
    """Дії у формі через `Session` (захищені кліки й клавіші)."""

    def __init__(self, session: browser.Session) -> None:
        self.session = session
        self.page = session.page
        self._marks = 0
        self.typed: set[str] = set()  # що робот набрав сам — не підписи полів (діагностика)
        self.texts: dict[str, tuple[str, str]] = {}  # name → (підпис, значення): звірка в кінці
        self.lists: dict[str, list[str]] = {}  # підпис списку → наші значення: звірка в кінці
        self.brand_miss: BrandMiss | None = None

    def reset(self) -> None:
        """Початок нової Картки: звірка в кінці — лише з її власним (ROBOT-008).

        Драйвер живе всю сесію; без цього `verify` порівнював би порожню форму нової Картки
        зі списками й текстами попередньої.
        """
        self.typed.clear()
        self.texts.clear()
        self.lists.clear()
        self.brand_miss = None

    def _find(self, label: str) -> str | None:
        self._marks += 1
        mark = f"b{self._marks}"
        return (
            mark
            if self.page.evaluate(FIND_BLOCK, [lang.variants(LABELS.get(label, label)), mark])
            else None
        )

    def _block(self, label: str) -> Any:
        mark = self._find(label)
        if mark is None:
            raise FieldMissing(label, describe_form(self.page, typed=self.typed))
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
        """Очистити поле, ввести значення і прочитати назад (Шафа сама ставить ціну, ROBOT-003).
        Не збіглось і після другої спроби — `FieldMissing`: з чужим значенням не публікуємо."""
        field = self.page.locator(f"[name='{name}']")
        if not field.count():
            raise FieldMissing(label)
        self.typed.add(value)
        for _ in range(2):
            field.first.fill("")
            field.first.fill(value)
            browser.pause()
            got = field.first.input_value()
            if same_text(got, value, price=name == "price"):
                self.texts[name] = (label, value)
                return
        raise FieldMissing(f"{label}: на формі {got[:40]}, треба {value[:40]}")

    def choose_category(self, parts: list[str], slug: str) -> None:
        try:
            self.session.choose_category(parts, slug)
        except LookupError as error:
            raise FieldMissing(f"Розділ: {error}") from error
        self._network_idle()
        self.session.wait_autofill()  # Шафа сама заповнює характеристики — дочекатися кінця

    def _network_idle(self) -> None:
        """Коротке очікування «мережа стихла»: Шафа постійно щось питає, тож тайм-аут — не помилка
        (ROBOT-009). Далі `wait_autofill` і пошук поля чекають самі."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            self.page.wait_for_load_state("networkidle", timeout=IDLE_WAIT * 1000)
        except PlaywrightTimeout:
            pass

    def abandon(self, label: str) -> None:
        """Поле пропущено (необов'язкове, ROBOT-009): закрити відкритий список і прибрати набране."""
        mark = self._find(label)
        if mark is not None:
            PageWidget(self, label, mark).close()

    def _widget(self, label: str) -> PageWidget:
        self._block(label)
        return PageWidget(self, label, f"b{self._marks}")

    def pick(self, label: str, values: list[str]) -> list[str]:
        widget = self._widget(label)
        try:
            notes = pick_values(widget, label, values)
        except FieldMissing as error:
            if widget.listed:  # зупинка в полі-списку — розмітка для файлу (ROBOT-004)
                error.field, error.markup = label, markup(self.page, widget.mark)
            raise
        if widget.listed:
            self.lists[label] = widget.chips()  # pick_values уже звірив: чипи — рівно наші
        return notes

    def untouched(self, labels: list[str]) -> list[str]:
        """Поля, яких робот не заповнює: не чіпаємо, лише пишемо, що Шафа поставила сама."""
        notes = []
        for label in labels:
            mark = self._find(label)
            chips = self.page.evaluate(CHIPS, mark) if mark else []
            chips = [c for c in chips if not is_hint(c)]  # підказки форми — не значення
            if chips:
                notes.append(f"на формі Шафа поставила сама: {label}: {', '.join(chips)}")
        return notes

    def verify(self) -> None:
        """Після всіх кроків: тексти й списки на формі — рівно наші (Шафа могла переписати)."""
        for name, (label, value) in self.texts.items():
            field = self.page.locator(f"[name='{name}']")
            got = field.first.input_value() if field.count() else ""
            if not same_text(got, value, price=name == "price"):
                raise FieldMissing(f"{label}: на формі {got[:40]}, треба {value[:40]}")
        for label, values in self.lists.items():
            mark = self._find(label)
            chips = self.page.evaluate(CHIPS, mark) if mark else []
            if lang.keys(chips) != lang.keys(values):
                raise FieldMissing(
                    f"{label}: на формі {', '.join(chips) or 'порожньо'}, треба {', '.join(values)}",
                    field=label,
                    markup=markup(self.page, mark),
                )

    def brand(self, name: str) -> bool:
        """Пошук у полі «Бренд»: клікаємо лише варіант, що точно збігається (після стискання
        пробілів і без регістру). Список може домальовуватись — опитуємо до `BRAND_WAIT` с.
        Не знайшли — `brand_miss` (що бачили) і `False`. «Додати бренд» — ніколи."""
        self.brand_miss = None
        mark = self._find("Бренд")
        if mark is None:
            raise FieldMissing("Бренд", describe_form(self.page, typed=self.typed))
        combo = self.page.locator(f"[data-robot-block='{mark}']").locator("input[role=combobox]")
        if not combo.count():
            raise FieldMissing("Бренд")
        combo.first.click()
        combo.first.fill(name)
        self.typed.add(name)
        browser.pause(1.0, 1.5)
        want = brand_key(name)
        deadline = time.monotonic() + BRAND_WAIT
        previous, still = None, 0.0
        while True:
            shown = self.page.evaluate(OPTIONS)  # позначає варіанти `data-robot-option`
            options = [o for o in shown if not among(o, list(NO_OPTIONS))]
            index = next((i for i, o in enumerate(shown) if brand_key(o) == want), None)
            if index is not None:
                option = self.page.locator(f"[data-robot-option='{index}']")
                self.session.click(option.first, f"бренд {name}")
                return True
            # Список не змінюється вже BRAND_STILL с (і не порожній) — далі чекати нічого
            still = still + 0.5 if options and options == previous else 0.0
            previous = options
            if time.monotonic() >= deadline or still >= BRAND_STILL:
                break
            self.page.wait_for_timeout(500)
        try:
            raw = [o for o in self.page.evaluate(OPTIONS_RAW) if not among(o, list(NO_OPTIONS))]
        except Exception:  # noqa: BLE001 — діагностика не має ховати справжню причину
            raw = options
        self.brand_miss = BrandMiss(name, raw, markup(self.page, mark))
        combo.first.fill("")
        self.session.press("Escape")
        return False

    def keywords(self, words: list[str]) -> None:
        combo = self._block("Ключові слова").locator("input[role=combobox]")
        if not combo.count():
            raise FieldMissing("Ключові слова")
        for word in words:
            self.typed.add(word)
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
            if dump := save_markup(error, shot):
                log(f"  · розмітка поля: {dump}")
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
