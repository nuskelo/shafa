"""Екран перевірки без HTTP: поля Картки, ручні правки, дії зі статусами, очищення архіву.

Значення поля: ручна правка → інакше бланк ШІ (`blanks`) або поле коду (рахується з тексту поста).
Правка знімає жовте; «Так, вірно» записує правку з тим самим значенням.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from shafa import catalog, rules
from shafa.catalog import Kind
from shafa.db import Status, now
from shafa.folders import natural_key

ARCHIVE_DAYS = 7
TABS = (Status.REVIEW, Status.APPROVED, Status.PROBLEM, Status.ARCHIVED)


@dataclass(frozen=True)
class Spec:
    """Поле Картки на екрані. `widget` — вид поля вводу (див. шаблон `card.html`)."""

    name: str
    label: str
    widget: str
    options: tuple[str, ...] = ()
    code: bool = False  # рахує код з тексту поста, а не ШІ


SPECS = (
    Spec("title", "Назва", "text"),
    Spec("section", "Розділ", "section"),
    Spec("sizes", "Розмір", "sizes"),
    Spec("colors", "Колір", "colors", catalog.COLORS),
    Spec("brand", "Бренд", "text"),
    Spec("material_upper", "Матеріал верху", "list"),
    Spec("material_lining", "Підкладка", "text"),
    Spec("membrane", "Мембрана", "text", code=True),
    Spec("insole", "Довжина устілки, см", "numbers", code=True),
    Spec("season", "Сезон", "select", catalog.SEASONS),
    Spec("type", "Тип", "text"),
    Spec("style", "Стиль", "text"),
    Spec("teen", "Для підлітків", "yesno"),
    Spec("gender", "Стать", "select", catalog.GENDERS),
    Spec("keywords", "Ключові слова", "list"),
    Spec("description", "Опис", "textarea", code=True),
    Spec("price", "Ціна на Шафі, грн", "price", code=True),
    Spec("condition", "Стан", "condition", (rules.NEW, rules.IDEAL), code=True),
    Spec("photo_order", "Порядок фото", "order"),  # показується над фото, не в полях
)
SPEC = {s.name: s for s in SPECS}
LIST_WIDGETS = {"sizes", "colors", "list", "numbers", "order"}

# Що не так із введеним значенням — за видом поля (текст для Продавчині).
BAD_INPUT = {
    "colors": "оберіть колір зі списку",
    "select": "оберіть зі списку",
    "price": "ціна — ціле число гривень",
    "condition": "оберіть стан",
}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def as_form(spec: Spec, value: Any) -> list[str]:
    """Значення поля → значення полів вводу на сторінці."""
    if spec.widget in ("sizes", "colors"):
        return ([*(value or []), "", ""])[:2]
    if spec.widget == "list":
        return [", ".join(value or [])]
    if spec.widget == "numbers":
        return ["; ".join(_number(x) for x in value or [])]
    if spec.widget == "order":
        return ["\n".join(value or [])]
    if spec.widget == "yesno":
        return [{True: "yes", False: "no"}.get(value, "")]
    return ["" if value is None else str(value)]


def parse(spec: Spec, inputs: list[str]) -> Any:
    """Значення полів вводу → значення поля. Неправильне — ValueError."""
    texts = [t.replace("\r\n", "\n").strip() for t in inputs]
    first = texts[0] if texts else ""
    if spec.widget == "sizes":
        return [t.replace(".", ",") for t in texts if t]
    if spec.widget == "colors":
        colors = [catalog.canonical(t, spec.options) for t in texts if t]
        if None in colors:
            raise ValueError(spec.widget)
        return list(dict.fromkeys(colors))
    if spec.widget == "list":
        return [part.strip() for part in first.split(",") if part.strip()]
    if spec.widget == "numbers":
        return [float(n.replace(",", ".")) for n in _NUMBER.findall(first)]
    if spec.widget == "order":
        return [line.strip() for line in first.split("\n") if line.strip()]
    if spec.widget == "yesno":
        return {"yes": True, "no": False}.get(first)
    if spec.widget == "price":
        if not first:
            return None
        if not first.isdigit() or int(first) == 0:
            raise ValueError(spec.widget)
        return int(first)
    if not first:
        if spec.widget == "condition":
            raise ValueError(spec.widget)
        return None
    if spec.widget in ("select", "condition"):
        found = catalog.canonical(first, spec.options)
        if found is None:
            raise ValueError(spec.widget)
        return found
    if spec.widget == "section":
        found, _ = catalog.find_section(first)
        return found or catalog.normalize_section(first)
    return first


@dataclass(frozen=True)
class FieldView:
    spec: Spec
    value: Any
    inputs: list[str]
    yellow: bool
    reason: str | None
    edited: bool


@dataclass(frozen=True)
class Photo:
    position: int
    file_name: str


@dataclass(frozen=True)
class Card:
    id: int
    folder_name: str
    post_id: int | None
    status: str
    problem_code: str | None
    problem_text: str | None
    archived_at: str | None
    post_date: str | None
    post_text: str | None
    fields: dict[str, FieldView]
    photos: list[Photo]

    @property
    def kind(self) -> Kind | None:
        section = self.fields["section"].value
        return catalog.section_kind(section) if section else None

    @property
    def visible(self) -> list[FieldView]:
        """Поля для показу: «Для підлітків» — лише дитяче, «Стать» — лише аксесуари."""
        hidden = set()
        if self.kind is not Kind.KIDS:
            hidden.add("teen")
        if self.kind is not Kind.ACCESSORY:
            hidden.add("gender")
        return [f for f in self.fields.values() if f.spec.name not in hidden]

    @property
    def yellow(self) -> int:
        return sum(f.yellow for f in self.visible)

    def approve_block(self) -> str | None:
        """Чому не можна схвалити (код повідомлення) або None."""
        section = self.fields["section"]
        if section.yellow:
            return "section_yellow"
        if not section.value:
            return "no_section"
        return None


def _code_values(text: str, condition: str | None) -> dict[str, Any]:
    price = rules.price_from_text(text)
    condition = condition or rules.condition(text)
    return {
        "membrane": rules.membrane(text),
        "insole": rules.insole_lengths(text),
        "description": rules.clean_description(text, condition),
        "price": rules.shafa_price(price) if price else None,
        "condition": condition,
    }


def load_card(conn: sqlite3.Connection, item_id: int) -> Card | None:
    """Картка з усіма полями або None (немає або архів уже очищено)."""
    row = conn.execute(
        """
        SELECT items.*, posts.date AS post_date, posts.text AS post_text
        FROM items LEFT JOIN posts USING (post_id)
        WHERE items.id = ? AND items.purged_at IS NULL
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    blanks = {
        r["field"]: (json.loads(r["value"]), bool(r["yellow"]), r["reason"])
        for r in conn.execute("SELECT * FROM blanks WHERE item_id = ?", (item_id,))
    }
    edits = {
        r["field"]: json.loads(r["value"])
        for r in conn.execute("SELECT field, value FROM edits WHERE item_id = ?", (item_id,))
    }
    code = _code_values(row["post_text"], edits.get("condition")) if row["post_text"] else {}

    fields = {}
    for spec in SPECS:
        value, yellow, reason = blanks.get(spec.name, (None, False, None))
        if spec.code:
            value = code.get(spec.name)
        edited = spec.name in edits
        if edited:
            value, yellow = edits[spec.name], False
        if value is None and spec.widget in LIST_WIDGETS:
            value = []
        fields[spec.name] = FieldView(
            spec, value, as_form(spec, value), yellow, reason if yellow else None, edited
        )

    photos = [
        Photo(r["position"], r["file_name"])
        for r in conn.execute(
            "SELECT position, file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
        )
    ]
    order = fields["photo_order"].value
    rank = {name: i for i, name in enumerate(order)}
    photos.sort(key=lambda p: (rank.get(p.file_name, len(rank)), p.position))

    return Card(
        row["id"],
        row["folder_name"],
        row["post_id"],
        row["status"],
        row["problem_code"],
        row["problem_text"],
        row["archived_at"],
        row["post_date"],
        row["post_text"],
        fields,
        photos,
    )


def _current(field: FieldView) -> Any:
    """Поточне значення так, як воно прийшло б зі сторінки без змін (для порівняння)."""
    try:
        return parse(field.spec, field.inputs)
    except ValueError:  # напр., сезону від ШІ немає в довіднику — будь-яке введене вже зміна
        return ValueError


def save(
    conn: sqlite3.Connection, card: Card, form: dict[str, list[str]], confirm: str | None = None
) -> list[str]:
    """Зберегти змінені поля Картки `review` як правки; `confirm` — поле «Так, вірно».

    Правильні поля зберігаються, неправильні — ні. Повертає назви неправильних полів.
    """
    if card.status != Status.REVIEW:
        return []
    bad, changed = [], {}
    for field in card.visible:
        name = field.spec.name
        inputs = form.get(name, field.inputs if name == confirm else None)
        if inputs is None:
            continue
        try:
            value = parse(field.spec, inputs)
        except ValueError:
            bad.append(name)
            continue
        if name == "photo_order" and sorted(value) != sorted(p.file_name for p in card.photos):
            bad.append(name)
            continue
        if name == confirm or value != _current(field):
            changed[name] = value
    with conn:
        stamp = now()
        conn.executemany(
            """
            INSERT INTO edits (item_id, field, value, edited_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (item_id, field) DO UPDATE SET value = excluded.value,
                                                       edited_at = excluded.edited_at
            """,
            [
                (card.id, name, json.dumps(value, ensure_ascii=False), stamp)
                for name, value in changed.items()
            ],
        )
    return bad


# Дія → (зі статусу, у статус, код повідомлення після).
ACTIONS = {
    "approve": (Status.REVIEW, Status.APPROVED, "approved"),
    "reject": (Status.REVIEW, Status.ARCHIVED, "rejected"),
    "unapprove": (Status.APPROVED, Status.REVIEW, "unapproved"),
    "restore": (Status.ARCHIVED, Status.REVIEW, "restored"),
}


def act(conn: sqlite3.Connection, item_id: int, action: str) -> str:
    """Змінити статус Картки. Повертає код повідомлення (див. `web.MESSAGES`)."""
    source, target, done = ACTIONS[action]
    if action == "approve":
        card = load_card(conn, item_id)
        block = card.approve_block() if card and card.status == source else None
        if block:
            return block
    archived_at = now() if target == Status.ARCHIVED else None
    with conn:
        cursor = conn.execute(
            """
            UPDATE items SET status = ?, archived_at = ?, updated_at = ?
            WHERE id = ? AND status = ? AND purged_at IS NULL
            """,
            (target, archived_at, now(), item_id, source),
        )
    return done if cursor.rowcount == 1 else "stale"


def purge_archive(conn: sqlite3.Connection, today: datetime | None = None) -> list[int]:
    """Архів старший за 7 днів: прибрати фото-список, бланк і правки, лишити прихований запис.

    Запис лишається `archived`, тож повторний `scan` не створить Картку знову. Папку з фото не
    чіпає. Повертає id очищених Карток.
    """
    cutoff = ((today or datetime.fromisoformat(now())) - timedelta(days=ARCHIVE_DAYS)).isoformat(
        timespec="seconds"
    )
    with conn:
        ids = [
            r[0]
            for r in conn.execute(
                """
                SELECT id FROM items
                WHERE status = ? AND purged_at IS NULL AND archived_at IS NOT NULL
                  AND archived_at < ?
                """,
                (Status.ARCHIVED, cutoff),
            )
        ]
        for table in ("photos", "blanks", "edits"):
            conn.executemany(f"DELETE FROM {table} WHERE item_id = ?", [(i,) for i in ids])
        conn.executemany("UPDATE items SET purged_at = ? WHERE id = ?", [(now(), i) for i in ids])
    return ids


def deletion_date(archived_at: str) -> datetime:
    """Коли Картку з архіву буде прибрано (місцевий час)."""
    return (datetime.fromisoformat(archived_at) + timedelta(days=ARCHIVE_DAYS)).astimezone()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Скільки Карток у кожному статусі (без очищеного архіву)."""
    result = {s.value: 0 for s in Status}
    for status, count in conn.execute(
        "SELECT status, count(*) FROM items WHERE purged_at IS NULL GROUP BY status"
    ):
        result[status] = count
    return result


def _matches(card: Card, words: list[str]) -> bool:
    haystack = " ".join(
        str(x or "")
        for x in (
            card.fields["title"].value,
            card.fields["brand"].value,
            card.post_id,
            card.folder_name,
            card.post_text,
        )
    ).casefold()
    return all(w in haystack for w in words)


def list_tab(conn: sqlite3.Connection, status: str, query: str = "") -> list[Card]:
    """Картки вкладки: старі пости першими; пошук — усі слова в назві, бренді, номері чи тексті."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE status = ? AND purged_at IS NULL", (status,)
        )
    ]
    cards = [c for c in (load_card(conn, i) for i in ids) if c is not None]
    words = query.casefold().split()
    if words:
        cards = [c for c in cards if _matches(c, words)]
    cards.sort(key=lambda c: (c.post_date is None, c.post_date or "", natural_key(c.folder_name)))
    return cards
