"""KLB-009: вкладка «Клубок» на екрані — списки, лічильники, «Як буде на Клубку». Без HTTP.

KLB-010: ті самі слова й порядок, що в Шафи (адреси `/klubok/<ключ>` лишились):

* `review` «На перевірку» — Картки «лише Клубок» (`only_klubok = 1`) після ШІ, статус `review`;
* `queue` «Схвалені» — рядки `klubok_ads.state = 'queued'` у порядку черги (`queue.order`);
* `decide` «Проблеми» — `problem` (дубль, «перевірте, чи опубліковано», помилки форми);
* `published` «Опубліковані» — виставлені;
* у «Ще»: `archive` «Архів» — `skipped` (не піде) і Картки «лише Клубок» в Архіві («Не
  виставляти», «не для Клубку»), згруповано за причиною; «Сесії» — сесії робота Клубку.

Лише читання: GET нічого не пише (рядки черги пише `queue.sync` — у фоні, `klubok.refresh`).
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
    "queue": "Схвалені",
    "decide": "Проблеми",
    "published": "Опубліковані",
}
MORE = {"sessions": "Сесії", "archive": "Архів"}  # у «Ще», як у Шафи
TITLES = TABS | MORE
STATES = {
    "queue": queue.QUEUED,
    "decide": queue.PROBLEM,
    "published": queue.PUBLISHED,
}
WAVE_FILTER = ("review", "queue")  # вкладки з фільтром «Хвиля 1 / 2 / 4»
NEW_POST = "новий пост"  # хвиля 2 — пости бота, рядка `klubok_waves` немає
SEX = {fields.SEX_GIRL: "для дівчинки", fields.SEX_BOY: "для хлопчика", fields.SEX_ANY: "будь-яка"}
LISTING_PHOTO = "https://img.klubok.com/"  # фото її оголошення — лише з цього хоста


def _archived_klubok(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, problem_text, updated_at FROM items WHERE only_klubok = 1 AND status = ? "
        "AND purged_at IS NULL ORDER BY updated_at DESC, id",
        (Status.ARCHIVED,),
    ).fetchall()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {key: 0 for key in TITLES}
    out["review"] = conn.execute(
        "SELECT count(*) FROM items WHERE only_klubok = 1 AND status = ? AND purged_at IS NULL",
        (Status.REVIEW,),
    ).fetchone()[0]
    by_state = dict(
        conn.execute("SELECT state, count(*) FROM klubok_ads GROUP BY state").fetchall()
    )
    for key, state in STATES.items():
        out[key] = by_state.get(state, 0)
    out["archive"] = by_state.get(queue.SKIPPED, 0) + len(_archived_klubok(conn))
    out["sessions"] = conn.execute(
        "SELECT count(*) FROM sessions WHERE site = 'klubok'"
    ).fetchone()[0]
    return out


@dataclass
class Listing:
    """Схоже оголошення на Клубку (для рядка «Проблеми»): назва, ціна, фото, посилання."""

    name: str
    price: int | None = None
    photo: str | None = None
    url: str | None = None


@dataclass
class Row:
    card: Card
    wave: int | None = None
    kind: str | None = None  # тип цієї пари (`klubok_waves.kind`), не всієї хвилі
    place: int | None = None
    code: str | None = None
    reason: str = ""
    published_at: str | None = None
    url: str | None = None
    dup_name: str | None = None
    dup_klubok_id: int | None = None
    klubok_id: int | None = None
    listing: Listing | None = None

    @property
    def price(self) -> int | None:
        """Ціна Клубку: «лише Клубок» — поле Картки (з поста, можна правити), інакше з поста."""
        return fields.klubok_price(self.card)


def _filter(rows: list[Row], query: str, wave: int | None = None) -> list[Row]:
    words = query.casefold().split()
    rows = [r for r in rows if _matches(r.card, words)] if words else rows
    return [r for r in rows if r.wave == wave] if wave is not None else rows


def waves_of(conn: sqlite3.Connection) -> dict[int, tuple[int, str]]:
    """Пост → (хвиля, тип пари) з `klubok_waves`; постів бота там немає (хвиля 2)."""
    return {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT post_id, wave, kind FROM klubok_waves")
    }


def _wave(known: dict[int, tuple[int, str]], card: Card) -> tuple[int | None, str | None]:
    if card.post_id is None:
        return None, None
    return known.get(card.post_id, (queue.WAVE_DEFAULT, NEW_POST))


def card_wave(conn: sqlite3.Connection, card: Card) -> tuple[int | None, str | None]:
    """Хвиля й тип пари однієї Картки (для Картки — без читання всієї таблиці)."""
    if card.post_id is None:
        return None, None
    row = conn.execute(
        "SELECT wave, kind FROM klubok_waves WHERE post_id = ?", (card.post_id,)
    ).fetchone()
    return (row[0], row[1]) if row else (queue.WAVE_DEFAULT, NEW_POST)


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


def _listing(conn: sqlite3.Connection, klubok_id: int | None, name: str | None) -> Listing | None:
    """Схоже оголошення: її (`klubok_listings`) чи виставлене програмою (`klubok_ads`)."""
    if klubok_id is None:
        return Listing(name) if name else None
    row = conn.execute(
        "SELECT name, price, photo, url FROM klubok_listings WHERE id = ?", (klubok_id,)
    ).fetchone()
    if row is None:
        return Listing(name or "") if name else None
    photo = row["photo"] if (row["photo"] or "").startswith(LISTING_PHOTO) else None
    return Listing(row["name"] or name or "", row["price"], photo, row["url"])


def rows(conn: sqlite3.Connection, tab: str, query: str = "", wave: int | None = None) -> list[Row]:
    """Рядки вкладки `tab` (ключ `TABS` чи «archive»); пошук — як у Шафи (назва, бренд, номер,
    текст); `wave` — лише ця хвиля."""
    known = waves_of(conn)
    if tab == "review":
        out = [Row(c, *_wave(known, c)) for c in review_cards(conn)]
        return _filter(out, query, wave)
    if tab == "queue":
        out = []
        for place, (item_id, _wave_n) in enumerate(queue.order(conn), start=1):
            card = load_card(conn, item_id)
            if card is not None:
                out.append(Row(card, *_wave(known, card), place=place))
        return _filter(out, query, wave)
    if tab == "archive":
        return _filter(_archive(conn, known), query)
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
            card, *_wave(known, card), code=r["problem_code"],
            reason=queue.REASONS.get(r["problem_code"] or "", r["problem_text"] or ""),
            published_at=r["published_at"], url=r["url"], dup_name=r["dup_name"],
            dup_klubok_id=r["dup_klubok_id"], klubok_id=r["klubok_id"],
        )  # fmt: skip
        if r["problem_code"] == queue.DUPLICATE:
            row.listing = _listing(conn, r["dup_klubok_id"], r["dup_name"])
        out.append(row)
    return _filter(out, query, wave)


def _archive(conn: sqlite3.Connection, known: dict[int, tuple[int, str]]) -> list[Row]:
    """«Архів» Клубку: не піде (`skipped`) і Картки «лише Клубок» в Архіві — з причиною."""
    out = []
    for r in conn.execute(
        "SELECT k.* FROM klubok_ads k WHERE k.state = ? ORDER BY k.problem_code, k.item_id",
        (queue.SKIPPED,),
    ):
        card = load_card(conn, r["item_id"])
        if card is not None:
            reason = queue.REASONS.get(r["problem_code"] or "", r["problem_text"] or "")
            out.append(Row(card, *_wave(known, card), code=r["problem_code"], reason=reason))
    for r in _archived_klubok(conn):
        card = load_card(conn, r["id"])
        if card is not None:
            reason = r["problem_text"] or "Не виставляти"
            out.append(Row(card, *_wave(known, card), code=card.problem_code, reason=reason))
    return out


def groups(found: list[Row]) -> list[tuple[str, list[Row]]]:
    """«Архів»: групи за причиною (у порядку першої появи), найбільші першими."""
    out: dict[str, list[Row]] = {}
    for row in found:
        out.setdefault(row.reason or "інша причина", []).append(row)
    return sorted(out.items(), key=lambda item: -len(item[1]))


def wave_text(wave: int | None, kind: str | None = None) -> str:
    """«Хвиля 1 · зима» — хвиля й тип цієї пари (не перелік усієї хвилі)."""
    if wave is None:
        return ""
    return f"Хвиля {wave}" + (f" · {kind}" if kind else "")


def wave_counts(found: list[Row]) -> dict[int, int]:
    out: dict[int, int] = {}
    for row in found:
        if row.wave is not None:
            out[row.wave] = out.get(row.wave, 0) + 1
    return dict(sorted(out.items()))


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
            Line("Ціна", f"{result.price} грн" + ("" if card.only_klubok else " (з поста)")),
            Line("Категорія", " / ".join(result.leaf.path)),
            Line("Розміри", ", ".join(str(s) for s in result.sizes) or "не потрібні"),
            Line("Стан", "нове" if result.condition == fields.CONDITION_NEW else "вживане (Б/у)"),
        ]
        if result.sex is not None:
            lines.append(Line("Стать", SEX.get(result.sex, "")))
        lines += [_color(card), Line("Бренд", result.brand or "—")]
        spare = (  # N4: запасне фото — лише в еталона Шафи; у «лише Клубок» це її власне фото
            not card.only_klubok
            and len(result.photos) == 1
            and result.photos[0] == fields.spare_photo(card.post_id)
        )
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
