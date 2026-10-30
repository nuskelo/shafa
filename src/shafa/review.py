"""Екран перевірки без HTTP: поля Картки, ручні правки, дії зі статусами, очищення архіву.

Значення поля: ручна правка → інакше бланк ШІ (`blanks`) або поле коду (рахується з тексту поста).
Правка знімає жовте; «Так, вірно» записує правку з тим самим значенням.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from shafa import ai, catalog, rules, variants
from shafa.catalog import Kind
from shafa.db import Status, now
from shafa.folders import base_name, natural_key, variant_size

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
    Spec("material_lining", "Матеріал підкладки", "list"),
    Spec("membrane", "Мембрана", "text", code=True),
    Spec("insole", "Довжина устілки, см", "numbers", code=True),
    Spec("season", "Сезон", "list"),
    Spec("type", "Тип", "choice"),
    Spec("style", "Стиль", "choice"),
    Spec("teen", "Для підлітків", "yesno"),
    Spec("gender", "Стать", "choice"),
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
    if spec.widget == "condition":
        found = catalog.canonical(first, spec.options)
        if found is None:
            raise ValueError(spec.widget)
        return found
    if spec.widget == "section":
        return catalog.find_section(first) or catalog.normalize_section(first)
    return first


@dataclass(frozen=True)
class FieldView:
    spec: Spec
    value: Any
    inputs: list[str]
    yellow: bool
    reason: str | None
    edited: bool
    options: tuple[str, ...] = ()  # варіанти саме цього розділу (зі знімка Шафи)


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
    queue_pos: int | None = None  # місце старої Картки у списку імпорту
    old_rules: bool = False  # нова інструкція ШІ могла змінити бланк — «Перезаповнити» (CORE-009 B)
    place: tuple[int, int] = (0, 0)  # CORE-014: «1 з 3» серед сестер-розмірів; (0, 0) — не варіант

    @property
    def variant(self) -> str | None:
        """Розмір Картки-варіанта поста з кількома розмірами (CORE-014) або None."""
        return variant_size(self.folder_name)

    @property
    def folder(self) -> str:
        """Папка з фото: у варіантів спільна, без `~<розмір>`."""
        return base_name(self.folder_name)

    @property
    def kind(self) -> Kind | None:
        section = self.fields["section"].value
        return catalog.section_kind(section) if section else None

    @property
    def path(self) -> str | None:
        """Розділ у знімку Шафи або None (порожній чи свій шлях)."""
        section = self.fields["section"].value
        return catalog.find_section(section) if section else None

    @property
    def visible(self) -> list[FieldView]:
        """Поля для показу: порожні поля, яких у цього типу на Шафі немає, сховані."""
        path = self.path
        hidden = set()
        for name in catalog.FIELD_TITLES:
            if path is None:
                absent = (name == "teen" and self.kind is not Kind.KIDS) or (
                    name == "gender" and self.kind not in (Kind.ACCESSORY, Kind.BABIES)
                )
            else:
                absent = catalog.characteristic(path, name) is None
            if absent and not self.fields[name].value:
                hidden.add(name)
        if path and not catalog.sizes(path) and not self.fields["sizes"].value:
            hidden.add("sizes")
        return [f for f in self.fields.values() if f.spec.name not in hidden]

    @property
    def yellow(self) -> int:
        return sum(f.yellow for f in self.visible)

    def approve_block(self) -> str | None:
        """Чому не можна схвалити (код повідомлення) або None."""
        section = self.fields["section"]
        if not section.value:
            return "no_section"
        path = self.path
        if path is None:  # свій шлях: Шафа такого розділу не має (D8) — «Так, вірно» не допоможе
            return "section_unknown"
        if section.yellow:
            return "section_yellow"
        if catalog.sizes(path) and not self.fields["sizes"].value:
            return "no_sizes"
        if catalog.section_kind(path) is Kind.BABIES and any(
            _size(s) > BABIES_MAX for s in self.fields["sizes"].value or []
        ):  # CORE-007: 23 і більше — ніколи не «малюки»
            return "babies_size"
        if catalog.section(path)["color_required"] and not self.fields["colors"].value:
            return "no_colors"
        gender = catalog.characteristic(path, "gender")
        if gender and gender["required"] and not self.fields["gender"].value:
            return "no_gender"
        return None


BABIES_MAX = 22


def _size(text: str) -> float:
    try:
        return float(str(text).replace(",", "."))
    except ValueError:
        return 0.0


# Ключові слова під розділ: слова статі й групи («чоловічі», «для жінок», «дитячі»…) міняються
# на слова розділу. Для дитячого — «дитячі» (без статі) і «для дівчинки» / «для хлопчика».
GROUP_ADJECTIVE = re.compile(
    r"(?<!\w)(чоловіч|жіноч|дівчач|хлопчач|дитяч)(?=[а-яіїєґ']*(?!\w))", re.IGNORECASE
)
GROUP_PHRASE = re.compile(
    r"(?<!\w)(для)\s+(чоловіків|жінок|дівчинки|дівчаток|хлопчика|хлопчиків|малюків)(?!\w)",
    re.IGNORECASE,
)


def _section_group(section: str) -> tuple[str, str] | None:
    """(корінь прикметника, «для …») для розділу або None — тоді слова не чіпаємо."""
    kind = catalog.section_kind(section)
    if kind is Kind.KIDS:
        girls = catalog.normalize_section(section).casefold().startswith(catalog.GIRLS.casefold())
        return "дитяч", "для дівчинки" if girls else "для хлопчика"
    return {
        Kind.WOMEN: ("жіноч", "для жінок"),
        Kind.MEN: ("чоловіч", "для чоловіків"),
        Kind.BABIES: ("дитяч", "для малюків"),
    }.get(kind)


def _same_case(word: str, like: str) -> str:
    return word[:1].upper() + word[1:] if like[:1].isupper() else word


def keywords_for_section(keywords: list[str], section: str | None) -> list[str]:
    """Ключові слова зі словами статі/групи розділу; повтори прибрано."""
    group = _section_group(section) if section else None
    if group is None:
        return keywords
    stem, phrase = group
    result: list[str] = []
    for keyword in keywords:
        keyword = GROUP_ADJECTIVE.sub(lambda m: _same_case(stem, m[1]), keyword)
        keyword = GROUP_PHRASE.sub(lambda m: _same_case(phrase, m[1]), keyword)
        if keyword.casefold() not in {k.casefold() for k in result}:
            result.append(keyword)
    return result


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


# Поля, значення яких — лише варіанти типу розділу (D7); True — одне значення.
CHOICE_FIELDS = {
    "material_upper": False,
    "material_lining": False,
    "season": False,
    "type": True,
    "style": True,
    "gender": True,
}


def _merge(field: FieldView, check: Any, options: tuple[str, ...]) -> FieldView:
    """Жовте від ШІ (якщо не правлено) і жовте від перевірки за Шафою — разом."""
    reasons = [r for r in (field.reason, check.reason if check.yellow else None) if r]
    yellow = field.yellow or check.yellow
    reason = "; ".join(dict.fromkeys(reasons)) if yellow else None
    return FieldView(field.spec, field.value, field.inputs, yellow, reason, field.edited, options)


def _check_against_shafa(fields: dict[str, FieldView], post_text: str) -> None:
    """Значення поза списком Шафи для розділу — жовті навіть після ручної правки (D6, D7)."""
    section = fields["section"]
    path = catalog.find_section(section.value) if section.value else None
    if section.value and path is None and not section.yellow:
        fields["section"] = FieldView(
            section.spec, section.value, section.inputs, True,
            "розділу немає в довіднику Шафи", section.edited,
        )  # fmt: skip
    fields["section"] = _merge(fields["section"], _OK, catalog.sections())
    sizes = fields["sizes"]
    check = ai.check_sizes(sizes.value, path, post_text)[0] if path else _OK
    fields["sizes"] = _merge(sizes, check, tuple(catalog.sizes(path)) if path else ())
    for name, single in CHOICE_FIELDS.items():
        spec = catalog.characteristic(path, name)
        check = ai.check_values(name, fields[name].value, path, single) if path else _OK
        fields[name] = _merge(fields[name], check, tuple(spec["choices"]) if spec else ())


_OK = ai.Field("", None)


def _apply_edited_text(fields: dict[str, FieldView], edits: dict[str, Any], post_text: str) -> None:
    """Її правка назви чи опису одразу діє на сезон (CORE-008 B): «зимові» → Зима кодом, не жовте.

    Сезон, який вона виправила сама, не чіпаємо.
    """
    extra = rules.edit_text(edits)
    season = fields["season"]
    if not extra or season.edited:
        return
    section = fields["section"].value
    path = catalog.find_section(section) if section else None
    code, _ = rules.season(f"{post_text}\n{extra}", path)
    if code is None:
        return
    checked = ai.check_season(season.value, path, f"{post_text}\n{extra}")
    fields["season"] = FieldView(
        season.spec, checked.value, as_form(season.spec, checked.value), checked.yellow,
        checked.reason, False, season.options,
    )  # fmt: skip


def load_card(conn: sqlite3.Connection, item_id: int) -> Card | None:
    """Картка з усіма полями або None (немає або архів уже очищено)."""
    row = conn.execute(
        """
        SELECT items.*, posts.date AS post_date, posts.text AS post_text,
               ai_answers.needs_ai AS needs_ai
        FROM items LEFT JOIN posts USING (post_id) LEFT JOIN ai_answers ON ai_answers.item_id = items.id
        WHERE items.id = ? AND items.purged_at IS NULL
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row["post_text"]:  # варіант бачить лише свій пункт поста (CORE-014)
        row["post_text"] = variants.text_for(row["folder_name"], row["post_text"])
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
        if isinstance(value, str) and spec.widget in LIST_WIDGETS:  # бланк до ROBOT-001: рядок
            value = [value]
        fields[spec.name] = FieldView(
            spec, value, as_form(spec, value), yellow, reason if yellow else None, edited
        )

    keywords = fields["keywords"]
    if not keywords.edited:  # ручну правку не чіпаємо
        value = keywords_for_section(keywords.value, fields["section"].value)
        fields["keywords"] = FieldView(
            keywords.spec,
            value,
            as_form(keywords.spec, value),
            keywords.yellow,
            keywords.reason,
            False,
        )

    _check_against_shafa(fields, row["post_text"] or "")
    _apply_edited_text(fields, edits, row["post_text"] or "")

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
        row["queue_pos"],
        bool(blanks) and bool(row["needs_ai"]),
        variants.place(conn, row["folder_name"], row["post_id"]),
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


# Чому «Схвалити» заборонене (коди повідомлень, див. `Card.approve_block`).
PRECHECK = "precheck"  # QA-002: робот Картку не опублікує — текст на самій Картці
APPROVE_BLOCKS = (
    PRECHECK,
    "section_yellow",
    "no_section",
    "section_unknown",
    "no_sizes",
    "no_colors",
    "no_gender",
)

# Дія → (зі статусу, у статус, код повідомлення після).
ACTIONS = {
    "approve": (Status.REVIEW, Status.APPROVED, "approved"),
    "reject": (Status.REVIEW, Status.ARCHIVED, "rejected"),
    "unapprove": (Status.APPROVED, Status.REVIEW, "unapproved"),
    "restore": (Status.ARCHIVED, Status.REVIEW, "restored"),
}


# CORE-015: примітка на Картці `review`, коли пост чи фото змінили після перевірки (у `problem_text`).
# Не проблема: Картка лишається «На перевірку»; зникає, коли її схвалюють.
POST_CHANGED, PHOTOS_CHANGED = "post_changed", "photos_changed"
SEASON_CHANGED = "season_changed"  # CORE-016: сезон змінено правилом, Картка була схвалена
NOTE_CODES = (POST_CHANGED, PHOTOS_CHANGED, SEASON_CHANGED)

# CORE-017: проблеми, які робот не розв'яже; після «В архів» їх не повертають на перевірку
NO_RESTORE = ("many_items", "no_section")
NO_RESTORE_MSG = "no_restore"

ON_SHAFA = "on_shafa_block"  # ROBOT-011: у Картки є `shafa_id` — товар уже на Шафі


def _on_shafa(conn: sqlite3.Connection, item_id: int) -> bool:
    row = conn.execute("SELECT shafa_id FROM items WHERE id = ?", (item_id,)).fetchone()
    return row is not None and row[0] is not None


def act(conn: sqlite3.Connection, item_id: int, action: str, root: Path | None = None) -> str:
    """Змінити статус Картки. Повертає код повідомлення (див. `web.MESSAGES`).

    `root` — Головна папка: з нею «Схвалити» ще й перевіряє, що робот Картку опублікує (QA-002)."""
    source, target, done = ACTIONS[action]
    if action in ("approve", "restore") and _on_shafa(conn, item_id):
        return ON_SHAFA  # ROBOT-011: що вже опубліковано, у чергу публікації не повертається
    if (
        action == "restore"
        and conn.execute(
            f"SELECT 1 FROM items WHERE id = ? AND problem_code IN ({', '.join('?' * len(NO_RESTORE))})",
            (item_id, *NO_RESTORE),
        ).fetchone()
    ):
        return NO_RESTORE_MSG  # CORE-017: пост із кількома товарами не можна схвалити
    if action == "approve":
        card = load_card(conn, item_id)
        block = card.approve_block() if card and card.status == source else None
        if block:
            return block
        if card and card.status == source:
            from shafa.robot import plan  # plan імпортує review

            if plan.precheck(card, root).error:
                return PRECHECK
    archived_at = now() if target == Status.ARCHIVED else None
    with conn:
        cursor = conn.execute(
            """
            UPDATE items SET status = ?, archived_at = ?, updated_at = ?
            WHERE id = ? AND status = ? AND purged_at IS NULL
            """,
            (target, archived_at, now(), item_id, source),
        )
        if action == "approve" and cursor.rowcount == 1:  # вона побачила примітку — знімаємо
            conn.execute(
                "UPDATE items SET problem_code = NULL, problem_text = NULL "
                f"WHERE id = ? AND problem_code IN ({', '.join('?' * len(NOTE_CODES))})",
                (item_id, *NOTE_CODES),
            )
        if action == "restore" and cursor.rowcount == 1:
            # DUP-001: ШІ відправив Картку в Архів як дубль, а вона її повертає — значить, це інший
            # товар: на схожі оголошення Картка більше не перевіряється (інакше ШІ знову б сказав «так»)
            conn.execute(
                "UPDATE items SET duplicate_ok_at = ?, problem_code = NULL, problem_text = NULL "
                "WHERE id = ? AND problem_code = 'duplicate'",
                (now(), item_id),
            )
    return done if cursor.rowcount == 1 else "stale"


def robot_retry(conn: sqlite3.Connection, item_id: int) -> bool:
    """Проблема робота (код `robot_…`) → назад у Схвалені. ШІ знову не викликається."""
    with conn:
        cursor = conn.execute(
            """
            UPDATE items SET status = ?, problem_code = NULL, problem_text = NULL, updated_at = ?
            WHERE id = ? AND status = ? AND problem_code LIKE 'robot\\_%' ESCAPE '\\'
            """,
            (Status.APPROVED, now(), item_id, Status.PROBLEM),
        )
    return cursor.rowcount == 1


def photos_retry(conn: sqlite3.Connection, item_id: int) -> bool:
    """Фото зникли з папки (`photos_missing`) → «На перевірку» завжди, і для колишніх схвалених."""
    with conn:
        cursor = conn.execute(
            """
            UPDATE items SET status = ?, problem_code = NULL, problem_text = NULL, updated_at = ?
            WHERE id = ? AND status = ? AND problem_code = 'photos_missing'
            """,
            (Status.REVIEW, now(), item_id, Status.PROBLEM),
        )
    return cursor.rowcount == 1


class Recheck(NamedTuple):
    code: str  # код повідомлення (див. `web.MESSAGES`)
    status: str | None  # статус Картки після перевірки; None — Картки немає чи нічого не зроблено


def recheck(conn: sqlite3.Connection, item_id: int, root: Path, channel_id: str | None) -> Recheck:
    """«Перевірити ще раз» для Картки: що саме робиться, залежить від `problem_code`.

    Спільне для екрана й бота (BOT-011). Проблеми публікації (`STICKY`) тут не рушаться — їх вирішує
    людина окремими діями. `channel_id` — канал Експорту для назв папок (як у `list_folders`)."""
    from shafa import fill  # fill імпортує review
    from shafa.folders import list_folders
    from shafa.scan import STICKY, rescan_folder

    card = load_card(conn, item_id)
    if card is None:
        return Recheck("stale", None)
    code = card.problem_code or ""
    if code == "photos_missing":
        done = photos_retry(conn, item_id)
        return Recheck("photos_retry", Status.REVIEW) if done else Recheck("stale", None)
    if code.startswith("robot_"):
        done = robot_retry(conn, item_id)
        return Recheck("robot_retry", Status.APPROVED) if done else Recheck("stale", None)
    if code == fill.STALE_RULES:
        fill.recheck(conn, [item_id])
        after = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
        return Recheck("recheck_done", after["status"] if after else None)
    if code in STICKY:
        return Recheck("sticky", card.status)
    status = rescan_folder(conn, card.folder_name, list_folders(root, channel_id))
    if status == Status.NEW:
        return Recheck("recheck_new", status)
    return Recheck("recheck_problem" if status else "stale", status)


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


POST_DELETED = "post_deleted"
POST_DELETED_TEXT = "Пост видалено з каналу — продано"
_GONE_STATUSES = (Status.NEW, Status.REVIEW, Status.APPROVED)


def archive_gone(conn: sqlite3.Connection, post_id: int) -> int:
    """CORE-018: пост зник з каналу (вона видаляє лише продане) → усі його Картки в
    `new`/`review`/`approved` у Архів з причиною `post_deleted`. Скільки Карток перенесено.

    CORE-019: і для старих постів з Експорту (`borh-…`, source `export`) — рішення Власника
    30.10: будь-який пост, що зник з каналу, = продано.
    Не чіпає опубліковані й Картки з `shafa_id`, проблеми, `publishing`.
    «Повернути» з Архіву лишається."""
    stamp = now()
    with conn:
        cursor = conn.execute(
            "UPDATE items SET status = ?, archived_at = ?, problem_code = ?, problem_text = ?, "
            "updated_at = ? WHERE post_id = ? AND shafa_id IS NULL "
            f"AND status IN ({', '.join('?' * len(_GONE_STATUSES))})",
            (Status.ARCHIVED, stamp, POST_DELETED, POST_DELETED_TEXT, stamp, post_id,
             *_GONE_STATUSES),
        )  # fmt: skip
    return cursor.rowcount


def archive_all_gone(conn: sqlite3.Connection) -> dict[int, int]:
    """CORE-018: разове (ідемпотентне) прибирання на старті — Картки постів (і бота, і старих
    з Експорту, CORE-019), які сайт
    скасував через зникнення (`site_jobs.state = 'cancelled'`) чи нічна перевірка позначила
    (`post_checks.gone_at`). «Невідомо» (`unknown_since`) — не підстава. `post_id → скільки`."""
    posts = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT i.post_id FROM items i JOIN posts p ON p.post_id = i.post_id "
            f"WHERE i.shafa_id IS NULL "
            f"AND i.status IN ({', '.join('?' * len(_GONE_STATUSES))}) "
            "AND (i.post_id IN (SELECT post_id FROM site_jobs WHERE state = 'cancelled') "
            "OR i.post_id IN (SELECT post_id FROM post_checks WHERE gone_at IS NOT NULL)) "
            "ORDER BY i.post_id",
            _GONE_STATUSES,
        )
    ]
    done = {post: archive_gone(conn, post) for post in posts}
    return {post: n for post, n in done.items() if n}


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


def _queue_key(card: Card) -> tuple:
    pos = card.queue_pos
    date = card.post_date
    return (pos is not None, pos or 0, date is None, date or "", natural_key(card.folder_name))


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
    cards.sort(key=_queue_key)  # як у черзі робота: нові за датою, старі за списком
    return cards
