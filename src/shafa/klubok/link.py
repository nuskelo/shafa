"""KLB-016: її старі оголошення на Клубку ↔ пости каналу за фото (без браузера).

Її оголошення (`klubok_listings`, `sold = 0`) — мініатюра `/m/` (кеш `<дані>/klubok-photos/<id>.jpg`,
`photo_dup.Photos`: лише `img.klubok.com`, паузи, 429/403 — стоп). Фото постів — Експорт Каналу
(1 на пост) і всі фото Карток (папки, `borh/<N>`, `klubok-old/<N>`). Відбиток і поріг — `photo_dup`.

Правило B (рішення Архітектора 23.11):
* кандидати — пости з відстанню ≤ T1; d = T1 (4) — лише коли текст поста не суперечить назві
  оголошення (бренд латиницею й розмір, якщо відомі з обох боків), d ≤ 3 — без тексту;
* група = найближчий пост + кандидати, чиї фото й між собою ≤ T1 **і** з тим самим текстом (бренд
  спільний чи обидва без бренду, розмір ±0,5 чи невідомий) — перепост тієї самої пари;
* кандидат поза групою з відривом < 3 від найближчого → «неоднозначно», нічого не пишемо;
* прив'язка — найновіший пост групи (найбільший номер); пишемо `post_id` лише в NULL.

Група перепосту — `<дані>/klubok-link.json` (`{"<id оголошення>": [пости, від найновішого]}`, лише
групи з ≥ 2 постів, що містять `post_id` оголошення), переписується цілком кожним записом (тимчасовий
файл → заміна). `sold.py` читає її: інший пост групи живий — «Схоже, продано» не питаємо.
TD: група — у таблицю при наступній міграції.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import UnidentifiedImageError

from shafa import config, events
from shafa.export import Post
from shafa.folders import photo_folder
from shafa.klubok import fields, photo_dup, waves

FILE = "klubok-link.json"
GAP = 3  # відрив найближчого від чужого кандидата (бітів)
TEXT_HEAD = 300  # розмір шукаємо в першому рядку й перших символах поста
LINES_MAX = 60
SHOW = 10
TD_PER_LINE = 20
TD_LINES = 10

MATCH, AMBIGUOUS, NONE = "match", "ambiguous", "none"


# --- група перепосту (файл у теці даних) ---


def groups_path(data_dir: Path | None = None) -> Path:
    return (data_dir or config.data_dir()) / FILE


def load_groups(data_dir: Path | None = None) -> dict[int, list[int]]:
    """Групи перепостів; файлу немає чи він битий — порожньо (тоді `sold` питає як завжди)."""
    try:
        raw = json.loads(groups_path(data_dir).read_text(encoding="utf-8"))
        return {int(k): [int(p) for p in v] for k, v in raw.items() if isinstance(v, list)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_groups(groups: dict[int, list[int]], data_dir: Path | None = None) -> None:
    """Переписати файл цілком: тимчасовий файл → заміна (читач ніколи не бачить половини)."""
    path = groups_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    data = {str(k): v for k, v in sorted(groups.items())}
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


# --- текст ---


@dataclass(frozen=True)
class Sig:
    brand: frozenset[str]
    sizes: frozenset[float]


def post_sig(text: str) -> Sig:
    """Бренд — латиниця першого рядка; розмір — з першого рядка й початку поста."""
    line = waves.first_line(text)
    return Sig(frozenset(waves.latin(line)), frozenset(waves.sizes(f"{line} {text[:TEXT_HEAD]}")))


def _fits(a: frozenset[float], b: frozenset[float]) -> bool:
    return any(abs(x - y) <= waves.SIZE_SPREAD for x in a for y in b)


def same_text(a: Sig, b: Sig) -> bool:
    """Два пости — та сама пара за текстом (для групи перепосту)."""
    brand = bool(a.brand & b.brand) if a.brand and b.brand else not a.brand and not b.brand
    size = not a.sizes or not b.sizes or _fits(a.sizes, b.sizes)
    return brand and size


def listing_sizes(name: str, sizes_json: str | None) -> frozenset[float]:
    found = set(waves.sizes(name))
    try:
        found |= {float(str(x).replace(",", ".")) for x in json.loads(sizes_json or "[]")}
    except (ValueError, TypeError):
        pass
    return frozenset(found)


def contradicts(name: str, sizes: frozenset[float], post_text: str) -> bool:
    """Текст поста суперечить назві оголошення: бренд і розмір — лише якщо відомі з обох боків."""
    ours, theirs = waves.latin(post_text), waves.latin(name)
    if ours and theirs and not ours & theirs:
        return True
    post_sizes = post_sig(post_text).sizes
    return bool(sizes and post_sizes and not _fits(sizes, post_sizes))


# --- рішення для одного оголошення ---


@dataclass(frozen=True)
class Verdict:
    kind: str  # MATCH / AMBIGUOUS / NONE
    post_id: int | None = None  # найновіший пост групи
    group: tuple[int, ...] = ()  # пости групи, від найновішого
    distance: int | None = None  # найменша відстань (до найближчого поста)
    close: tuple[int, ...] = ()  # кандидати ≤ T1 (для звіту неоднозначних)
    by_text: int = 0  # кандидатів d = T1, відкинутих текстом


class Posts:
    """Відбитки фото постів і їхні тексти; відстані пост↔пост кешуються."""

    def __init__(self, hashes: dict[int, list[int]], texts: dict[int, str]) -> None:
        self.hashes = {p: h for p, h in hashes.items() if h}
        self.texts = texts
        self._pair: dict[tuple[int, int], int] = {}
        self._sig: dict[int, Sig] = {}

    def text(self, post_id: int) -> str:
        return self.texts.get(post_id, "")

    def sig(self, post_id: int) -> Sig:
        if post_id not in self._sig:
            self._sig[post_id] = post_sig(self.text(post_id))
        return self._sig[post_id]

    def between(self, a: int, b: int) -> int:
        key = (min(a, b), max(a, b))
        if key not in self._pair:
            self._pair[key] = min(
                photo_dup.distance(x, y) for x in self.hashes[a] for y in self.hashes[b]
            )
        return self._pair[key]

    def distances(self, theirs: int) -> dict[int, int]:
        return {
            p: min(photo_dup.distance(h, theirs) for h in hashes)
            for p, hashes in self.hashes.items()
        }


def judge(theirs: int, posts: Posts, name: str, sizes: frozenset[float]) -> Verdict:
    """Правило B (див. модуль)."""
    dist = posts.distances(theirs)
    best_d = min(dist.values(), default=None)
    near = {p: d for p, d in dist.items() if d <= photo_dup.T1}
    close = {
        p: d for p, d in near.items()
        if d < photo_dup.T1 or not contradicts(name, sizes, posts.text(p))
    }  # fmt: skip
    by_text = len(near) - len(close)
    if not close:
        return Verdict(NONE, distance=best_d, by_text=by_text)
    best = min(close, key=lambda p: (close[p], -p))
    group = [
        p for p in close
        if p == best or (
            same_text(posts.sig(p), posts.sig(best)) and posts.between(p, best) <= photo_dup.T1
        )
    ]  # fmt: skip
    others = [d for p, d in close.items() if p not in group]
    ranked = tuple(sorted(close, key=lambda p: (close[p], -p)))
    if others and min(others) - close[best] < GAP:
        return Verdict(AMBIGUOUS, distance=close[best], close=ranked, by_text=by_text)
    ordered = tuple(sorted(group, reverse=True))
    return Verdict(MATCH, ordered[0], ordered, close[best], ranked, by_text)


# --- фото постів ---


def post_photos(
    conn: sqlite3.Connection, posts: dict[int, Post], export_dir: Path, root: Path
) -> dict[int, list[Path]]:
    """Пост → файли фото: Експорт (1) + усі фото Карток цього поста + запасне з Експорту."""
    out: dict[int, list[Path]] = {}
    for post_id, post in posts.items():
        if post.photo:
            out.setdefault(post_id, []).append(export_dir / post.photo)
    for item_id, folder_name, post_id in conn.execute(
        "SELECT id, folder_name, post_id FROM items WHERE post_id IS NOT NULL ORDER BY id"
    ).fetchall():
        folder = photo_folder(root, folder_name)
        files = [
            folder / r[0]
            for r in conn.execute(
                "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
            )
        ]
        files += fields.spare_photos(post_id)
        known = out.setdefault(post_id, [])
        known += [f for f in files if f not in known]
    return out


def hash_photos(photos: dict[int, list[Path]]) -> tuple[dict[int, list[int]], int]:
    """Відбитки; файлу немає чи він не читається — пропуск. → (пост → відбитки, скільки пропущено)."""
    out: dict[int, list[int]] = {}
    missed = 0
    for post_id, paths in photos.items():
        for path in paths:
            try:
                out.setdefault(post_id, []).append(photo_dup.ours_hash(path))
            except (OSError, UnidentifiedImageError, ValueError):
                missed += 1
    return out, missed


def post_texts(conn: sqlite3.Connection, posts: dict[int, Post]) -> dict[int, str]:
    """Текст поста: з бази (останній), інакше з Експорту."""
    texts = {p: post.text for p, post in posts.items()}
    texts |= {r[0]: r[1] for r in conn.execute("SELECT post_id, text FROM posts") if r[1]}
    return texts


# --- прохід ---


@dataclass
class Linked:
    listing_id: int
    name: str
    known: int | None  # `post_id`, що вже був
    verdict: Verdict


@dataclass
class Report:
    applied: bool
    export_posts: int = 0
    photo_posts: int = 0
    hashes: int = 0
    missed_files: int = 0
    listings: int = 0
    no_photo: int = 0
    stopped: str | None = None
    rows: list[Linked] = field(default_factory=list)
    written: list[int] = field(default_factory=list)  # оголошення, яким щойно записано `post_id`
    groups: dict[int, list[int]] = field(default_factory=dict)
    gone: set[int] = field(default_factory=set)
    checked: set[int] = field(default_factory=set)
    asks: list[int] = field(default_factory=list)  # нові питання «Схоже, продано» (оголошення)
    td077: list[int] | None = None
    td077_total: int = 0
    td077_error: str | None = None

    def of(self, kind: str) -> list[Linked]:
        return [r for r in self.rows if r.verdict.kind == kind]

    def lines(self) -> list[str]:
        mode = "записано" if self.applied else "пробний прогін — нічого не записано"
        out = [
            f"Клубок ↔ пости за фото ({mode})",
            (
                f"Фото постів: {self.photo_posts} постів ({self.hashes} відбитків; Експорт — "
                f"{self.export_posts} постів) · файлів не прочитано: {self.missed_files}"
            ),
        ]
        if self.stopped:
            out.append(f"Зупинено: {self.stopped}. Нічого не записано — повторіть пізніше "
                       "(завантажені мініатюри збережено).")  # fmt: skip
        known = sum(1 for r in self.rows if r.known)
        out.append(
            f"Її активних оголошень: {self.listings} (з постом уже: {known}) · "
            f"без мініатюри: {self.no_photo}"
        )
        matched = self.of(MATCH)
        reposts = [r for r in matched if len(r.verdict.group) > 1]
        fresh = [r for r in matched if r.listing_id in self.written]
        gone = [r for r in fresh if r.verdict.post_id in self.gone]
        unknown = [r for r in fresh if r.verdict.post_id not in self.checked]
        out += [
            (
                f"Зіставлено: {len(matched)} (один пост: {len(matched) - len(reposts)} · "
                f"перепост: {len(reposts)}) · нових прив'язок: {len(fresh)}"
            ),
            (
                f"  нові: пост зник — {len(gone)} (з них «Схоже, продано»: {len(self.asks)}) · "
                f"живий — {len(fresh) - len(gone) - len(unknown)} · "
                f"нічна перевірка не бачила — {len(unknown)}"
            ),
            f"Неоднозначно (пост не пишемо): {len(self.of(AMBIGUOUS))}",
            f"Без збігу: {len(self.of(NONE))}",
        ]
        spread = Counter(r.verdict.distance for r in self.rows if r.verdict.kind == MATCH)
        by_text = sum(r.verdict.by_text for r in self.rows)
        out.append(
            "Відстань у збігах: "
            + " · ".join(f"{d}: {spread[d]}" for d in range(photo_dup.T1 + 1))
            + f" · кандидатів d = {photo_dup.T1}, відкинутих текстом: {by_text}"
        )
        differ = [r for r in self.rows if r.known and r.known != r.verdict.post_id]
        if differ:
            out.append(f"Уже відомий пост ≠ за фото (не чіпаємо): {len(differ)}")
            out += [f"  {r.listing_id}: у базі {r.known}, за фото {_said(r.verdict)}"
                    for r in differ[:SHOW]]  # fmt: skip
        if reposts:
            out.append(f"Перепости (перші {min(len(reposts), SHOW)}):")
            out += [f"  {r.listing_id} → {r.verdict.post_id} (група {_join(r.verdict.group)})"
                    for r in reposts[:SHOW]]  # fmt: skip
        ambiguous = self.of(AMBIGUOUS)
        if ambiguous:
            out.append(f"Неоднозначні (перші {min(len(ambiguous), SHOW)}):")
            out += [f"  {r.listing_id}: пости {_join(r.verdict.close[:5])} · «{r.name[:40]}»"
                    for r in ambiguous[:SHOW]]  # fmt: skip
        out += self._td077()
        return out[:LINES_MAX]

    def _td077(self) -> list[str]:
        if self.td077_error:
            return [f"TD-077: не пораховано — {self.td077_error}"]
        if self.td077 is None:
            return []
        out = [
            (
                f"TD-077: хвиля 1 «уже на Клубку» за текстом — {self.td077_total}, "
                f"без фото-збігу — {len(self.td077)}:"
            )
        ]
        chunks = [self.td077[i : i + TD_PER_LINE] for i in range(0, len(self.td077), TD_PER_LINE)]
        out += [f"  {_join(c)}" for c in chunks[:TD_LINES]]
        rest = len(self.td077) - TD_PER_LINE * TD_LINES
        if rest > 0:
            out.append(f"  … ще {rest}")
        return out


def _join(ids) -> str:
    return ", ".join(str(i) for i in ids)


def _said(verdict: Verdict) -> str:
    if verdict.kind == MATCH:
        return str(verdict.post_id)
    return "неоднозначно" if verdict.kind == AMBIGUOUS else "немає"


def _hash_listing(photos: photo_dup.Photos, listing_id: int, url: str | None) -> int | None:
    try:
        return photo_dup.theirs_hash(photos.small(listing_id, url))
    except photo_dup.FetchFailed:
        return None
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def run(
    conn: sqlite3.Connection,
    posts: dict[int, Post],
    export_dir: Path,
    root: Path,
    photos: photo_dup.Photos,
    *,
    write: bool,
    today: datetime,
    data_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Report:
    """Зіставити її активні оголошення з постами. `write` — записати `post_id` (лише в NULL), групи
    й подію; інакше база (копія в пам'яті) теж оновлюється — для підрахунку «Схоже, продано», —
    але файл груп і подія не пишуться. 429/403 від Клубку — нічого не записано (`stopped`)."""
    from shafa.klubok import sold

    say = progress or (lambda _text: None)
    report = Report(applied=write, export_posts=sum(1 for p in posts.values() if p.photo))
    say("Відбитки фото постів…")
    hashes, report.missed_files = hash_photos(post_photos(conn, posts, export_dir, root))
    index = Posts(hashes, post_texts(conn, posts))
    report.photo_posts = len(index.hashes)
    report.hashes = sum(len(h) for h in index.hashes.values())
    listings = conn.execute(
        "SELECT id, name, sizes, photo, post_id FROM klubok_listings WHERE sold = 0 ORDER BY id"
    ).fetchall()
    report.listings = len(listings)
    say(f"Мініатюри її оголошень: {len(listings)}…")
    for listing in listings:
        try:
            theirs = _hash_listing(photos, listing["id"], listing["photo"])
        except photo_dup.FetchStop as error:
            report.stopped = str(error)
            return report
        if theirs is None:
            report.no_photo += 1
            continue
        sizes = listing_sizes(listing["name"], listing["sizes"])
        verdict = judge(theirs, index, listing["name"], sizes)
        report.rows.append(Linked(listing["id"], listing["name"], listing["post_id"], verdict))
    with conn:
        for row in report.of(MATCH):
            if row.known is None:
                changed = conn.execute(
                    "UPDATE klubok_listings SET post_id = ? WHERE id = ? AND post_id IS NULL",
                    (row.verdict.post_id, row.listing_id),
                ).rowcount
                if changed:
                    report.written.append(row.listing_id)
            final = row.known or row.verdict.post_id
            if len(row.verdict.group) > 1 and final in row.verdict.group:
                report.groups[row.listing_id] = list(row.verdict.group)
    report.gone = {
        r[0] for r in conn.execute("SELECT post_id FROM post_checks WHERE gone_at IS NOT NULL")
    }
    report.checked = {r[0] for r in conn.execute("SELECT post_id FROM post_checks")}
    written = set(report.written)
    report.asks = [
        a.key for a in sold.asks(conn, groups=report.groups) if a.kind == sold.LISTING
        and a.key in written
    ]  # fmt: skip
    _td077(conn, posts, export_dir, root, today, report)
    if write:
        save_groups(report.groups, data_dir)
        events.emit(
            "klubok.linked",
            (
                f"Клубок: її оголошення ↔ пости за фото — прив'язано {len(report.written)}, "
                f"неоднозначно {len(report.of(AMBIGUOUS))}, без збігу {len(report.of(NONE))}"
            ),
            data={
                "linked": len(report.written),
                "matched": len(report.of(MATCH)),
                "ambiguous": len(report.of(AMBIGUOUS)),
                "none": len(report.of(NONE)),
                "no_photo": report.no_photo,
                "asks": len(report.asks),
                "reposts": {str(k): v for k, v in report.groups.items()},
            },
            data_dir=data_dir,
        )
    return report


def _td077(
    conn: sqlite3.Connection,
    posts: dict[int, Post],
    export_dir: Path,
    root: Path,
    today: datetime,
    report: Report,
) -> None:
    """Пости хвилі 1, які `klubok-import` визнав «уже на Клубку» за текстом, а за фото жодне її
    оголошення не зіставилось (лише звіт; імпорт — окремий тікет)."""
    from shafa.klubok import importer

    try:
        found = importer.run(conn, posts, export_dir, root, 1, apply=False, today=today)
    except importer.ImportFailed as error:
        report.td077_error = str(error)
        return
    linked = {p for r in report.of(MATCH) for p in r.verdict.group}
    linked |= {r[0] for r in conn.execute(
        "SELECT post_id FROM klubok_listings WHERE post_id IS NOT NULL"
    )}  # fmt: skip
    on = found.of(importer.ON_KLUBOK)
    report.td077_total = len(on)
    report.td077 = sorted(d.post_id for d in on if d.post_id not in linked)
