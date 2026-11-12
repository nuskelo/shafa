"""KLB-009: вкладка «Клубок» на екрані — списки, лічильники, «Як буде на Клубку». Без HTTP.

Вкладки (адреси `/klubok/<ключ>`):

* `review` — Картки «лише Клубок» (`only_klubok = 1`) після ШІ, статус `review`;
* `queue` — рядки `klubok_ads.state = 'queued'` у порядку черги (`queue.order`), з хвилею;
* `decide` — `problem` (дубль, «перевірте, чи опубліковано», помилки форми);
* `published` — виставлені;
* `skipped` — «не піде», згруповано за причиною.

Лише читання: GET нічого не пише (рядки черги пише `queue.sync` — «Старт» чи «Перерахувати чергу»).
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from shafa import rules
from shafa.db import Status
from shafa.klubok import fields, queue
from shafa.review import Card, _matches, load_card

TABS = {
    "review": "На перевірку",
    "queue": "У черзі",
    "decide": "Треба рішення",
    "published": "Виставлено",
    "skipped": "Не піде",
}
STATES = {
    "queue": queue.QUEUED,
    "decide": queue.PROBLEM,
    "published": queue.PUBLISHED,
    "skipped": queue.SKIPPED,
}
WAVES = {1: "хвиля 1 · зима, демі, сумки", 2: "хвиля 2 · нові пости", 4: "хвиля 4 · спорт, літо"}
SEX = {fields.SEX_GIRL: "для дівчинки", fields.SEX_BOY: "для хлопчика", fields.SEX_ANY: "будь-яка"}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {key: 0 for key in TABS}
    out["review"] = conn.execute(
        "SELECT count(*) FROM items WHERE only_klubok = 1 AND status = ? AND purged_at IS NULL",
        (Status.REVIEW,),
    ).fetchone()[0]
    by_state = dict(
        conn.execute("SELECT state, count(*) FROM klubok_ads GROUP BY state").fetchall()
    )
    for key, state in STATES.items():
        out[key] = by_state.get(state, 0)
    return out


@dataclass
class Row:
    card: Card
    wave: int | None = None
    place: int | None = None
    code: str | None = None
    reason: str = ""
    published_at: str | None = None
    url: str | None = None
    dup_name: str | None = None
    dup_klubok_id: int | None = None
    klubok_id: int | None = None

    @property
    def price(self) -> int | None:
        """Ціна Клубку — з поста (не ціна Шафи +15 %)."""
        return rules.price_from_text(self.card.post_text or "") or None


def _filter(rows: list[Row], query: str) -> list[Row]:
    words = query.casefold().split()
    return [r for r in rows if _matches(r.card, words)] if words else rows


def review_cards(conn: sqlite3.Connection) -> list[Card]:
    """Картки «лише Клубок» на перевірці — у порядку черги Шафи (старі пости першими)."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE only_klubok = 1 AND status = ? AND purged_at IS NULL",
            (Status.REVIEW,),
        )
    ]
    cards = [c for c in (load_card(conn, i) for i in ids) if c is not None]
    cards.sort(key=lambda c: (c.post_date is None, c.post_date or "", c.id))
    return cards


def rows(conn: sqlite3.Connection, tab: str, query: str = "") -> list[Row]:
    """Рядки вкладки `tab` (ключ `TABS`); пошук — як у Шафи (назва, бренд, номер, текст)."""
    if tab == "review":
        return _filter(
            [Row(c, wave=queue.wave(conn, c.post_id)) for c in review_cards(conn)], query
        )
    if tab == "queue":
        out = []
        for place, (item_id, wave) in enumerate(queue.order(conn), start=1):
            card = load_card(conn, item_id)
            if card is not None:
                out.append(Row(card, wave=wave, place=place))
        return _filter(out, query)
    state = STATES[tab]
    order = "k.published_at DESC" if tab == "published" else "k.problem_code, k.updated_at DESC"
    out = []
    for r in conn.execute(
        "SELECT k.* FROM klubok_ads k JOIN items i ON i.id = k.item_id "
        f"WHERE k.state = ? ORDER BY {order}, k.item_id",
        (state,),
    ):
        card = load_card(conn, r["item_id"])
        if card is None:
            continue
        row = Row(
            card, wave=queue.wave(conn, card.post_id), code=r["problem_code"],
            reason=queue.REASONS.get(r["problem_code"] or "", r["problem_text"] or ""),
            published_at=r["published_at"], url=r["url"], dup_name=r["dup_name"],
            dup_klubok_id=r["dup_klubok_id"], klubok_id=r["klubok_id"],
        )  # fmt: skip
        out.append(row)
    return _filter(out, query)


def groups(found: list[Row]) -> list[tuple[str, list[Row]]]:
    """«Не піде»: групи за причиною (у порядку першої появи), найбільші першими."""
    out: dict[str, list[Row]] = {}
    for row in found:
        out.setdefault(queue.REASONS.get(row.code or "", row.reason or "інша причина"), []).append(
            row
        )
    return sorted(out.items(), key=lambda item: -len(item[1]))


def wave_text(wave: int | None) -> str:
    return WAVES.get(wave, f"хвиля {wave}") if wave is not None else ""


# --- «Як буде на Клубку» ---


@dataclass
class Line:
    label: str
    value: str
    warn: str = ""  # чого бракує — жовтим


@dataclass
class AdView:
    lines: list[Line] = field(default_factory=list)
    skip: str = ""  # чому не піде (простою мовою) — порожньо, якщо все гаразд
    skip_code: str = ""
    text: str = ""  # опис оголошення
    photos: list[str] = field(default_factory=list)  # назви файлів у порядку
    spare: bool = False  # запасне фото з Експорту

    @property
    def ok(self) -> bool:
        return not self.skip


def _color(card: Card) -> Line:
    colors = card.fields["colors"].value or []
    if not colors:
        return Line("Колір", "—", "колір не вказано — на Клубку буде порожньо")
    key = fields.color_key(colors)
    if key is None:
        return Line("Колір", colors[0], "такого кольору немає на Клубку — поле буде порожнє")
    return Line("Колір", "різнокольоровий" if key == fields.COLOR_MULTI else colors[0])


def ad_view(card: Card, root: Path) -> AdView:
    """Поля оголошення Клубку з Картки (`fields.ad`, без дублів і без запису); чого бракує — жовтим."""
    result = fields.ad(card, root)
    if isinstance(result, fields.KlubokAd):
        lines = [
            Line("Назва", result.name),
            Line("Ціна", f"{result.price} грн (з поста)"),
            Line("Категорія", " / ".join(result.leaf.path)),
            Line("Розміри", ", ".join(str(s) for s in result.sizes) or "не потрібні"),
            Line("Стан", "нове" if result.condition == fields.CONDITION_NEW else "вживане (Б/у)"),
        ]
        if result.sex is not None:
            lines.append(Line("Стать", SEX.get(result.sex, "")))
        lines += [_color(card), Line("Бренд", result.brand or "—")]
        spare = len(result.photos) == 1 and result.photos[0] == fields.spare_photo(card.post_id)
        return AdView(lines, text=result.text, photos=[p.name for p in result.photos], spare=spare)
    # не піде: показуємо, що відомо, і причину жовтим
    title = (card.fields["title"].value or "").strip()
    price = rules.price_from_text(card.post_text or "")
    leaf = fields.leaf_of(card)
    lines = [
        Line("Назва", title or "—", "" if title else "немає назви"),
        Line(
            "Ціна",
            f"{price} грн (з поста)" if price else "—",
            "" if price else "немає ціни в пості",
        ),
        Line(
            "Категорія",
            " / ".join(leaf.path) if leaf else card.fields["section"].value or "—",
            "" if leaf else "немає категорії Клубку для цього розділу",
        ),
        Line("Розміри", ", ".join(card.fields["sizes"].value or []) or "—"),
    ]
    reason = queue.REASONS.get(result.code, "")
    detail = result.text if result.text and result.text != reason else ""
    return AdView(
        lines, skip=f"{reason} ({detail})" if detail else reason or result.text,
        skip_code=result.code,
    )  # fmt: skip
