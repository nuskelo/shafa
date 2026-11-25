"""KLB-008 (D4): `klubok-import` — старі пости (до бота) з Експорту в чергу Клубку хвилями.

Пост Експорту з номером ≤ `SHAFA_KLUBOK_FROM_POST` і типом цієї хвилі (`waves.kind`, `wave_of`):
* є рядок `klubok_waves` — уже імпортовано, нічого (повтор не дублює);
* є Картка «лише Клубок» — лише рядок хвилі, якщо його немає;
* є Картка Шафи (не архів) — **еталон**: рядок хвилі, нової Картки немає; черга Клубку візьме її,
  коли вона схвалена/опублікована на Шафі. Фото еталона немає — одне фото з Експорту в
  `<дані>/klubok-old/<N>/1.jpg` (запасне, `fields.spare_photos`). Картка Шафи не змінюється;
* лише Картки Шафи в архіві (продано, відкладено) — не імпортуємо;
* Картки немає — KLB-017 (TD-077): «вже на Клубку» лише за фото (`klubok-link`, KLB-016): пост —
  `post_id` її активного оголошення (`klubok_listings`, `sold = 0`) чи в групі перепосту активного
  оголошення (`link.load_groups`); не імпортуємо. Текстовий `waves.Matcher` — лише для звіту («за
  текстом схоже: …» поруч з «імпортуємо»). `klubok-link` ще не робили (файлу груп немає чи жодного
  `post_id` у знімку) — відмова (`ImportFailed`), інакше імпортували б усе підряд.
  Ітерація 1 (рішення Архітектора 24.11): «вже на Клубку» ще й за **суворим текстом** (`Strict`) —
  у неї на Клубку буває каталожне фото чи інший кадр.
  Ітерація 2: пост «схоже за текстом» (старий `Matcher` «є» чи `Strict`) без фото-прив'язки —
  ШІ-зір (`dup.Judge`, Клубок): фото поста з Експорту + велике фото її оголошення (`photo_dup.Photos`,
  кеш). `yes` → «вже на Клубку»; `no` → імпорт; `unsure` / не запитано (бюджет, ключ, мережа, фото) →
  суворий текст. `--dry-run` ШІ не питає (лише «піде на ШІ: N, ≈ $»). Перепости серед імпортованих
  (фото ≤ `photo_dup.T1` і `link.same_text`) — імпортуємо лише найновіший пост групи. Інакше нова Картка `klubok-<N>` (`only_klubok = 1`, `new` → ШІ) з одним фото й текстом поста в
  `<дані>/klubok-old/<N>/`. Пост без фото чи з проблемою тексту (ціна, кілька товарів) — не
  імпортуємо, рядок у звіті.
`apply=False` (`--dry-run`) — нічого не пише ні в базу, ні на диск.
"""

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from shafa import dup as shafa_dup
from shafa.borh import copy_photo
from shafa.db import Status, now
from shafa.export import Post
from shafa.folders import (
    SPARE_PHOTO,
    Folder,
    klubok_folder_name,
    klubok_spare,
    list_photos,
    photo_folder,
)
from shafa.klubok import dup, link, photo_dup, queue, waves
from shafa.klubok.photo_dup import FetchFailed, FetchStop
from shafa.robot.listings import Listing
from shafa.scan import find_problem, save_posts

TEXT_NAME = "текст.txt"
LINES_MAX = 60
SHOW = 15  # прикладів «раніше вважались на Клубку за текстом» (KLB-017)
SHOW_OTHER = 3  # прикладів «вже на Клубку» / «не імпортовано» у звіті

PRICE_GAP = 0.10  # суворий текст: ціна поста vs її ціна
_BAGS = frozenset(name for name, _ in dup.BAG_TYPES)  # без розміру — лише сумки/гаманці
_GO = re.compile(r"[ІіIi]дуть\s+на\s*(\d{1,2}(?:[,.]5)?)(?:\s*[-–/]\s*(\d{1,2}(?:[,.]5)?))?")
_PHRASE_END = re.compile(r"[.!]\s+(?=[\dА-ЯІЇЄҐ])")  # «стані. 38 розмір» — так, «Dr. Martens» — ні

NEW, ETALON, ETALON_WAIT, ON_KLUBOK = "new", "etalon", "etalon_wait", "on_klubok"
DONE, KLUBOK_CARD, ARCHIVED, NO_PHOTO, TEXT = "done", "klubok_card", "archived", "no_photo", "text"
REPOST = "repost"  # перепост пари, яку імпортуємо з новішого поста
LATER = "later"  # ШІ не запитано — нічого не вирішуємо, пост чекає наступного запуску
AI_REQUEST_USD = 0.0086  # ≈ запит ШІ-зору: прогін KLB-017 на копії 25.11 — 173 запити, 1,49 $
NOT_ASKED = "none"  # ШІ не запитано (бюджет, ключ, мережа, 429, немає фото) → `LATER`


class ImportFailed(Exception):
    """Імпорт неможливий (знімок Клубку старий, налаштування з помилкою)."""


@dataclass
class Decision:
    post_id: int
    kind: str
    action: str
    note: str = ""  # «вже на Клубку»: назва оголошення; проблема тексту
    spare: bool = False  # еталону потрібне запасне фото
    by_text: bool = False  # «вже на Клубку» за суворим текстом, не за фото
    text_match: str = ""  # KLB-017: старий `Matcher` «є» (до KLB-017 — «вже на Клубку»)
    first: str = ""  # перша фраза поста — для звіту
    strict_hit: bool = False  # Б: суворий текст каже «на Клубку» (А — ні)
    cand_id: int | None = None  # її оголошення для ШІ-зору (суворий текст, інакше старий `Matcher`)
    cand_name: str = ""
    ai: str = ""  # yes / no / unsure / none (не запитано); "" — ШІ не для цього поста
    ai_why: str = ""

    @property
    def side(self) -> str:
        return "Б" if self.strict_hit else "А"


@dataclass
class Report:
    wave: int
    limit: int
    applied: bool
    snapshot: str
    total: int = 0  # постів до межі
    other_wave: int = 0
    skipped_kinds: Counter = field(default_factory=Counter)  # одяг, інше
    decisions: list[Decision] = field(default_factory=list)
    ai_requests: int = 0
    ai_cost_micro: int = 0  # фактично за цей прогін (`ai_requests`)
    ai_stopped: str = ""  # чому ШІ-зір зупинено посеред проходу (бюджет, ключ, Клубок 429)

    def of(self, action: str) -> list[Decision]:
        return [d for d in self.decisions if d.action == action]

    def strict(self) -> list[Decision]:
        return [d for d in self.of(ON_KLUBOK) if d.by_text]

    def candidates(self) -> list[Decision]:
        """Пости «схоже за текстом» без фото-прив'язки — для ШІ-зору."""
        return [d for d in self.decisions if d.cand_id is not None]

    def _ai_lines(self) -> list[str]:
        cands = self.candidates()
        if not cands:
            return []
        b = sum(d.strict_hit for d in cands)
        head = (
            f"Схоже за текстом без фото-прив'язки: {len(cands)} "
            f"(Б — суворий текст {b}, А — {len(cands) - b})"
        )
        if not any(d.ai for d in cands):
            n = len(cands)
            return [
                (
                    f"{head} → піде на ШІ: {n}, ≈ {n * AI_REQUEST_USD:.2f} $ "
                    "(тут ШІ не питали — прогноз: Б → на Клубку, А → імпорт)"
                )
            ]
        out = [f"{head} · запитів ШІ: {self.ai_requests} ({self.ai_cost_micro / 1e6:.2f} $)"]
        if self.ai_stopped:
            out.append(f"  ШІ-зір зупинено: {self.ai_stopped} — решта чекає наступного запуску")
        for side in ("Б", "А"):
            rows = [d for d in cands if d.side == side]
            if rows:
                said = Counter(d.ai or NOT_ASKED for d in rows)
                on = sum(d.action == ON_KLUBOK for d in rows)
                later = sum(d.action == LATER for d in rows)
                out.append(
                    f"  {side}: так {said[shafa_dup.YES]} · ні {said[shafa_dup.NO]} · не впевнений "
                    f"{said[shafa_dup.UNSURE]} · не запитано {said[NOT_ASKED]} → на Клубку {on}, "
                    f"імпорт {len(rows) - on - later}, повтор {later}"
                )
        return out

    def lines(self) -> list[str]:
        mode = "записано" if self.applied else "пробний прогін — нічого не записано"
        kinds = Counter(d.kind for d in self.decisions)
        new = self.of(NEW)
        out = [
            f"Клубок, хвиля {self.wave} ({mode}) · пости до {self.limit} включно",
            f"Знімок Клубку: {self.snapshot}",
            (
                f"Постів у Експорті до межі: {self.total} · цієї хвилі: {len(self.decisions)} "
                f"({_counts(kinds)}) · інша хвиля: {self.other_wave} · "
                f"не беремо: {sum(self.skipped_kinds.values())} ({_counts(self.skipped_kinds)})"
            ),
            f"Нових Карток «лише Клубок»: {len(new)} ({_counts(Counter(d.kind for d in new))})",
            (
                f"Еталон Шафи (схвалена / опублікована): {len(self.of(ETALON))} · "
                f"з них потрібне запасне фото: {sum(d.spare for d in self.of(ETALON))}"
            ),
            f"Еталон Шафи ще не схвалений (чекає Шафи): {len(self.of(ETALON_WAIT))}",
            (
                f"Уже на Клубку (не імпортуємо): {len(self.of(ON_KLUBOK))} · за фото "
                f"{len(self.of(ON_KLUBOK)) - len(self.strict())} · за текстом / ШІ "
                f"{len(self.strict())}"
            ),
            f"Перепост — імпортуємо лише найновіший пост: {len(self.of(REPOST))}",
            f"Повтор — ШІ не запитано, чекає наступного запуску: {len(self.of(LATER))}",
            f"Архів Шафи — продано/відкладено (не імпортуємо): {len(self.of(ARCHIVED))}",
            f"Без фото в Експорті (не імпортуємо): {len(self.of(NO_PHOTO))}",
            f"Проблема тексту (не імпортуємо): {len(self.of(TEXT))}",
            (
                f"Уже в хвилі (повтор): {len(self.of(DONE))} · Картка «лише Клубок» без хвилі: "
                f"{len(self.of(KLUBOK_CARD))}"
            ),
        ]
        out += self._ai_lines()
        similar = Counter(d.note for d in new if d.note)
        if similar:
            out.append(f"  серед нових: {_counts(similar)}")
        was = [d for d in self.decisions if d.text_match and d.action not in (ON_KLUBOK, LATER)]
        if was:
            by = Counter(d.action for d in was)
            out.append(
                f"Раніше вважались на Клубку за текстом, тепер імпортуємо: {len(was)} (нова Картка "
                f"{by[NEW]}, перепост {by[REPOST]}, без фото {by[NO_PHOTO]}, проблема тексту "
                f"{by[TEXT]}):"
            )
            out += [
                f"  {d.post_id}: {d.first[:60]} · за текстом схоже: {d.text_match}"[:140]
                for d in was[:SHOW]
            ]
        photo = [d for d in self.of(ON_KLUBOK) if not d.by_text]
        for title, items in (
            ("Уже на Клубку за текстом / ШІ", self.strict()),
            ("Перепост", self.of(REPOST)),
            ("Уже на Клубку за фото", photo),
            ("Без фото", self.of(NO_PHOTO)),
            ("Проблема тексту", self.of(TEXT)),
        ):
            if items:
                out.append(f"{title} (перші {min(len(items), SHOW_OTHER)}):")
                out += [f"  {d.post_id}: {d.note}"[:110] for d in items[:SHOW_OTHER]]
        return out[:LINES_MAX]


def _counts(counter: Counter) -> str:
    return ", ".join(f"{k} {n}" for k, n in counter.most_common()) or "—"


def _snapshot(conn: sqlite3.Connection, today: datetime) -> tuple[str, bool]:
    row = conn.execute("SELECT read_at, count FROM klubok_listings_read WHERE id = 1").fetchone()
    if row is None:
        return "немає — спершу robot --klubok-snapshot", False
    fresh = queue.snapshot_fresh(conn, today)
    text = f"прочитано {row['read_at']}, оголошень {row['count']}"
    return text + ("" if fresh else " — старший за добу"), fresh


def linked_posts(conn: sqlite3.Connection, data_dir: Path | None = None) -> dict[int, str]:
    """KLB-017: пости, прив'язані за фото до її активних оголошень → «<id> «назва»».

    `post_id` активного оголошення та всі пости його групи перепосту (`link.load_groups`).
    `klubok-link` ще не робили (файлу груп немає чи жодного `post_id`) — `ImportFailed`.
    """
    active = {
        r["id"]: (r["name"], r["post_id"])
        for r in conn.execute("SELECT id, name, post_id FROM klubok_listings WHERE sold = 0")
    }
    if not link.groups_path(data_dir).is_file() or not any(p for _, p in active.values()):
        raise ImportFailed(
            "Її оголошення ще не зіставлені з постами за фото — спершу klubok-link "
            "(без нього «вже на Клубку» не визначити, імпортувались би всі пости підряд)"
        )
    found: dict[int, str] = {}
    for listing_id, (name, post_id) in active.items():
        if post_id is not None:
            found.setdefault(post_id, f"{listing_id} «{name}»")
    for listing_id, group in link.load_groups(data_dir).items():
        if listing_id in active:
            for post_id in group:
                found.setdefault(post_id, f"{listing_id} «{active[listing_id][0]}» (перепост)")
    return found


def post_sizes(text: str) -> frozenset[float]:
    """Розмір поста: той, на який «ідуть» («Вказаний 37, ідуть на 36» → 36; «36-36,5» — обидва),
    інакше названий «N розмір» у першому рядку й на початку тексту."""
    go = _GO.search(text)
    if go:
        return frozenset(float(g.replace(",", ".")) for g in go.groups() if g)
    return frozenset(waves.sizes(waves.first_line(text) + " " + text[:300]))


def _near(a: int | None, b: int | None) -> bool:
    return bool(a and b and abs(a - b) <= PRICE_GAP * max(a, b))


@dataclass(frozen=True)
class _Ad:
    id: int
    name: str
    price: int | None
    sizes: frozenset[float]
    words: frozenset[str]
    subtype: str | None


class Strict:
    """Ітерація 1: «вже на Клубку» за суворим текстом — її активне оголошення, де разом:
    спільне слово бренду латиницею; розмір рівно той самий (без розміру з обох боків — лише
    сумки/гаманці того самого підтипу); ціна ±10 %; той самий підтип (`dup.subtype`), коли відомий
    з обох боків."""

    def __init__(self, rows) -> None:
        self._index: dict[str, list[_Ad]] = {}
        for listing_id, name, price, sizes_json in rows:
            sizes = set(dup.title_sizes(name))
            try:
                sizes |= {str(x) for x in json.loads(sizes_json or "[]")}
            except ValueError:
                pass
            ad = _Ad(
                listing_id,
                name,
                price,
                frozenset(float(x.replace(",", ".")) for x in sizes),
                frozenset(waves.latin(name)),
                dup.subtype(name),
            )
            for word in ad.words:
                self._index.setdefault(word, []).append(ad)

    def match(self, text: str) -> _Ad | None:
        line = waves.first_line(text)
        words = waves.latin(line)
        sizes, price, kind = post_sizes(text), waves.price(text), dup.subtype(line)
        seen = {ad.id: ad for w in sorted(words) for ad in self._index.get(w, ())}
        for ad in sorted(seen.values(), key=lambda a: (-len(words & a.words), a.id)):
            if not _near(price, ad.price):
                continue
            if kind and ad.subtype and kind != ad.subtype:
                continue
            if sizes or ad.sizes:
                if sizes != ad.sizes:
                    continue
            elif not (kind and kind == ad.subtype and kind in _BAGS):
                continue
            return ad
        return None


def _strict(conn: sqlite3.Connection) -> Strict:
    return Strict(
        tuple(r)
        for r in conn.execute("SELECT id, name, price, sizes FROM klubok_listings WHERE sold = 0")
    )


def _matcher(conn: sqlite3.Connection) -> waves.Matcher:
    return waves.Matcher(
        (r[0], r[1], r[2])
        for r in conn.execute("SELECT id, name, price FROM klubok_listings WHERE sold = 0")
    )


def _has_photos(root: Path, conn: sqlite3.Connection, item_id: int, name: str) -> bool:
    files = [
        r[0] for r in conn.execute("SELECT file_name FROM photos WHERE item_id = ?", (item_id,))
    ]
    folder = photo_folder(root, name)
    return bool(files) and all((folder / f).is_file() for f in files)


def decide(
    conn: sqlite3.Connection,
    post: Post,
    kind: str,
    root: Path,
    export_dir: Path,
    matcher: waves.Matcher,
    linked: dict[int, str],
    strict: Strict | None = None,
) -> Decision:
    """Що робити з постом цієї хвилі (нічого не пише); `linked` — `linked_posts`.
    Кандидат ШІ-зору повертається шляхом імпорту — підсумок робить `settle` (після ШІ)."""
    post_id = post.post_id
    if conn.execute("SELECT 1 FROM klubok_waves WHERE post_id = ?", (post_id,)).fetchone():
        return Decision(post_id, kind, DONE)
    cards = conn.execute(
        "SELECT id, folder_name, status, only_klubok FROM items WHERE post_id = ? "
        "AND purged_at IS NULL ORDER BY id",
        (post_id,),
    ).fetchall()
    if any(c["only_klubok"] for c in cards):
        return Decision(post_id, kind, KLUBOK_CARD)
    shafa = [c for c in cards if c["status"] != Status.ARCHIVED]
    if shafa:
        ready = [c for c in shafa if c["status"] in (Status.APPROVED, Status.PUBLISHED)]
        spare = not any(_has_photos(root, conn, c["id"], c["folder_name"]) for c in shafa)
        return Decision(post_id, kind, ETALON if ready else ETALON_WAIT, spare=spare)
    if (
        cards
        or conn.execute(
            "SELECT 1 FROM items WHERE post_id = ? AND purged_at IS NOT NULL", (post_id,)
        ).fetchone()
    ):
        return Decision(post_id, kind, ARCHIVED)
    if post_id in linked:
        return Decision(post_id, kind, ON_KLUBOK, linked[post_id])
    same = strict.match(post.text) if strict is not None else None
    found = matcher.match(post.text)  # старий текст: лише кандидат для ШІ й рядок звіту
    on = found.verdict == waves.ON and found.listing is not None
    text_match = f"{found.listing.id} «{found.listing.name}»" if on else ""
    base = {
        "text_match": text_match,
        "first": _PHRASE_END.split(waves.first_line(post.text), maxsplit=1)[0],
        "strict_hit": same is not None,
        "cand_id": same.id if same else (found.listing.id if on else None),
        "cand_name": same.name if same else (found.listing.name if on else ""),
    }
    source = export_dir / post.photo if post.photo else None
    if source is None or not source.is_file():
        return Decision(post_id, kind, NO_PHOTO, base["first"], **base)
    name = klubok_folder_name(post_id)
    problem = find_problem(
        Folder(name, post_id, (SPARE_PHOTO,)), {post_id: post.text}, {post_id: [name]}
    )
    if problem:
        return Decision(post_id, kind, TEXT, problem[1], **base)
    note = {waves.SIMILAR: "схоже", waves.NO_LATIN: "без бренду латиницею"}.get(found.verdict, "")
    if text_match:
        note = "за текстом схоже"
    return Decision(post_id, kind, NEW, note, **base)


def settle(d: Decision) -> Decision:
    """Кандидат ШІ-зору (ітерація 3): ШІ «так» чи «не впевнений» → «вже на Клубку» (не впевнений —
    здебільшого її каталожне фото тієї ж моделі); «ні» → шлях імпорту (нова / без фото / текст);
    не запитано → `LATER` (нічого не пишемо, наступний запуск спитає знову). Без ШІ (`--dry-run`,
    `ai == ""`) — прогноз за суворим текстом."""
    if d.cand_id is None or d.action == ON_KLUBOK:
        return d
    if d.ai == NOT_ASKED:
        return replace(d, action=LATER, note=f"ШІ не запитано: {d.ai_why}")
    if d.ai in (shafa_dup.YES, shafa_dup.UNSURE) or (not d.ai and d.strict_hit):
        how = f"ШІ: {d.ai_why}" if d.ai else "суворий текст"
        return replace(
            d, action=ON_KLUBOK, by_text=True, note=f"{d.cand_id} «{d.cand_name}» ({how})"
        )
    return d


@dataclass
class _Value:
    value: object


@dataclass
class PostCard:
    """Пост без Картки для `dup.Judge`: те, що суддя читає з Картки (назва, ціна, розміри)."""

    post_id: int
    post_text: str
    fields: dict[str, _Value]
    id: int | None = None  # запит ШІ — без Картки (`ai_requests.item_id` NULL)
    only_klubok: bool = True


def post_card(post: Post) -> PostCard:
    text = post.text
    sizes = sorted(post_sizes(text))
    return PostCard(
        post.post_id,
        text,
        {
            "title": _Value(_PHRASE_END.split(waves.first_line(text), maxsplit=1)[0]),
            "price": _Value(waves.price(text)),
            "sizes": _Value([f"{x:g}".replace(".", ",") for x in sizes]),
            "colors": _Value(None),
        },
    )


def _their_listing(conn: sqlite3.Connection, listing_id: int) -> tuple[Listing, str | None]:
    row = conn.execute(
        "SELECT name, brand, price, sizes, url, photo FROM klubok_listings WHERE id = ?",
        (listing_id,),
    ).fetchone()
    sizes = tuple(json.loads(row["sizes"] or "[]")) or tuple(dup.title_sizes(row["name"]))
    listing = Listing(
        listing_id, row["name"], row["brand"], "", sizes, row["url"] or "", price=row["price"]
    )
    return listing, row["photo"]


def ask_ai(
    conn: sqlite3.Connection,
    report: Report,
    posts: dict[int, Post],
    export_dir: Path,
    judge,
    photos: photo_dup.Photos,
) -> None:
    """ШІ-зір для кожного кандидата (`Report.candidates`); підсумок — `settle`."""
    for d in report.decisions:
        if d.cand_id is None:
            continue
        verdict = _look(conn, d, posts[d.post_id], export_dir, judge, photos, report)
        if verdict.asked:
            report.ai_requests += 1
        d.ai = verdict.same if verdict.asked else NOT_ASKED
        d.ai_why = verdict.why


def _look(conn, d, post, export_dir, judge, photos, report) -> shafa_dup.Verdict:
    if report.ai_stopped:
        return shafa_dup.unasked(report.ai_stopped)
    ours = export_dir / post.photo if post.photo else None
    if ours is None or not ours.is_file():
        return shafa_dup.unasked("немає фото поста")
    listing, url = _their_listing(conn, d.cand_id)
    try:
        theirs = photos.large(d.cand_id, url)
    except FetchStop as error:
        report.ai_stopped = str(error)
        return shafa_dup.unasked(str(error))
    except FetchFailed as error:
        return shafa_dup.unasked(str(error))
    verdict = judge(post_card(post), listing, ours, theirs)
    if verdict.retry:  # бюджет, ключ, мережа ШІ — до кінця проходу не питаємо
        report.ai_stopped = f"ШІ: {verdict.why}"
    return verdict


def mark_reposts(
    conn: sqlite3.Connection, report: Report, posts: dict[int, Post], export_dir: Path
) -> None:
    """Перепости серед імпортованих: та сама пара в кількох постах без Картки (фото ≤ T1 і
    `link.same_text`) — імпортуємо найновіший; уже імпортований раніше пост групи (Картка «лише
    Клубок») — його й лишаємо, повтор нічого не дублює."""
    imported = {
        r[0]
        for r in conn.execute(
            "SELECT post_id FROM items WHERE only_klubok = 1 AND post_id IS NOT NULL"
        )
    }
    pool = [d for d in report.decisions if d.action == NEW or d.post_id in imported]
    hashes: dict[int, int] = {}
    for d in pool:
        post = posts[d.post_id]
        try:
            hashes[d.post_id] = photo_dup.ours_hash(export_dir / str(post.photo))
        except (OSError, ValueError):
            continue
    ids = sorted(hashes)
    sigs = {n: link.post_sig(posts[n].text) for n in ids}
    parent = {n: n for n in ids}

    def top(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if photo_dup.distance(hashes[a], hashes[b]) <= photo_dup.T1 and link.same_text(
                sigs[a], sigs[b]
            ):
                parent[top(a)] = top(b)
    groups: dict[int, list[int]] = {}
    for n in ids:
        groups.setdefault(top(n), []).append(n)
    keep: dict[int, int] = {}
    for members in groups.values():
        done = [n for n in members if n in imported]
        winner = max(done) if done else max(members)
        keep.update({n: winner for n in members})
    for i, d in enumerate(report.decisions):
        winner = keep.get(d.post_id)
        if d.action == NEW and winner is not None and winner != d.post_id:
            report.decisions[i] = replace(d, action=REPOST, note=f"перепост {winner}")


def _write_folder(post: Post, export_dir: Path) -> bool:
    """Фото й текст поста → `<дані>/klubok-old/<N>/`. Фото не відкривається — False."""
    target = klubok_spare(post.post_id)
    if not copy_photo(export_dir / str(post.photo), target / SPARE_PHOTO):
        return False
    (target / TEXT_NAME).write_text(post.text, encoding="utf-8")
    return True


def _apply(
    conn: sqlite3.Connection, d: Decision, post: Post, export_dir: Path, wave: int
) -> Decision:
    stamp = now()
    if d.action in (ETALON, ETALON_WAIT, KLUBOK_CARD):
        if d.spare and post.photo and not _write_folder(post, export_dir):
            d.spare = False  # фото Експорту не відкрилось — черга скаже «немає фото»
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO klubok_waves (post_id, wave, kind, added_at) "
                "VALUES (?, ?, ?, ?)",
                (d.post_id, wave, d.kind, stamp),
            )
        return d
    if d.action != NEW:
        return d
    if not _write_folder(post, export_dir):
        return replace(d, action=NO_PHOTO, note="фото не відкривається")
    name = klubok_folder_name(d.post_id)
    with conn:
        save_posts(conn, {d.post_id: post})
        item_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at, only_klubok) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (name, d.post_id, Status.NEW, stamp, stamp),
        ).lastrowid
        conn.executemany(
            "INSERT INTO photos (item_id, position, file_name) VALUES (?, ?, ?)",
            [(item_id, i, n) for i, n in enumerate(list_photos(klubok_spare(d.post_id)), 1)],
        )
        conn.execute(
            "INSERT OR IGNORE INTO klubok_waves (post_id, wave, kind, added_at) VALUES (?, ?, ?, ?)",
            (d.post_id, wave, d.kind, stamp),
        )
    return d


def run(
    conn: sqlite3.Connection,
    posts: dict[int, Post],
    export_dir: Path,
    root: Path,
    wave: int,
    *,
    apply: bool,
    today: datetime,
    data_dir: Path | None = None,
    judge=None,
    photos: photo_dup.Photos | None = None,
) -> Report:
    """Пости хвилі `wave` (1 чи 4) з Експорту → рішення; `apply` — записати (див. модуль)."""
    if wave not in (1, 4):
        raise ImportFailed(f"хвиля {wave}: імпорт лише для 1 і 4")
    try:
        limit = queue.from_post()
    except queue.SetupError as error:
        raise ImportFailed(f"Налаштування Клубку з помилкою: {error}") from None
    snapshot, fresh = _snapshot(conn, today)
    if apply and not fresh:
        raise ImportFailed(f"Знімок її оголошень на Клубку не свіжий ({snapshot}) — спершу "
                           "robot --klubok-snapshot")  # fmt: skip
    linked = linked_posts(conn, data_dir)
    report = Report(wave, limit, apply, snapshot)
    matcher, strict = _matcher(conn), _strict(conn)
    for post_id in sorted(posts):
        post = posts[post_id]
        if post_id > limit:
            continue
        report.total += 1
        kind = waves.kind(post.text)
        target = waves.wave_of(kind)
        if target is None:
            report.skipped_kinds[kind] += 1
            continue
        if target != wave:
            report.other_wave += 1
            continue
        report.decisions.append(decide(conn, post, kind, root, export_dir, matcher, linked, strict))
    if judge is not None and photos is not None:
        last = conn.execute("SELECT coalesce(max(id), 0) FROM ai_requests").fetchone()[0]
        ask_ai(conn, report, posts, export_dir, judge, photos)
        report.ai_cost_micro = conn.execute(
            "SELECT coalesce(sum(cost_micro_usd), 0) FROM ai_requests WHERE id > ?", (last,)
        ).fetchone()[0]
    report.decisions = [settle(d) for d in report.decisions]
    mark_reposts(conn, report, posts, export_dir)
    if apply:
        report.decisions = [
            _apply(conn, d, posts[d.post_id], export_dir, wave) for d in report.decisions
        ]
    return report
