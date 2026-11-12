"""KLB-008 (D4): `klubok-import` — старі пости (до бота) з Експорту в чергу Клубку хвилями.

Пост Експорту з номером ≤ `SHAFA_KLUBOK_FROM_POST` і типом цієї хвилі (`waves.kind`, `wave_of`):
* є рядок `klubok_waves` — уже імпортовано, нічого (повтор не дублює);
* є Картка «лише Клубок» — лише рядок хвилі, якщо його немає;
* є Картка Шафи (не архів) — **еталон**: рядок хвилі, нової Картки немає; черга Клубку візьме її,
  коли вона схвалена/опублікована на Шафі. Фото еталона немає — одне фото з Експорту в
  `<дані>/klubok-old/<N>/1.jpg` (запасне, `fields.spare_photo`). Картка Шафи не змінюється;
* лише Картки Шафи в архіві (продано, відкладено) — не імпортуємо;
* Картки немає — грубе зіставлення з її оголошеннями (`waves.Matcher`): «є» — не імпортуємо;
  інакше нова Картка `klubok-<N>` (`only_klubok = 1`, `new` → ШІ) з одним фото й текстом поста в
  `<дані>/klubok-old/<N>/`. Пост без фото чи з проблемою тексту (ціна, кілька товарів) — не
  імпортуємо, рядок у звіті.
`apply=False` (`--dry-run`) — нічого не пише ні в базу, ні на диск.
"""

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
from shafa.klubok import queue, waves
from shafa.scan import find_problem, save_posts

TEXT_NAME = "текст.txt"
LINES_MAX = 60
SHOW = 15  # рядків «вже на Клубку» / «не імпортовано» у звіті

NEW, ETALON, ETALON_WAIT, ON_KLUBOK = "new", "etalon", "etalon_wait", "on_klubok"
DONE, KLUBOK_CARD, ARCHIVED, NO_PHOTO, TEXT = "done", "klubok_card", "archived", "no_photo", "text"


class ImportFailed(Exception):
    """Імпорт неможливий (знімок Клубку старий, налаштування з помилкою)."""


@dataclass
class Decision:
    post_id: int
    kind: str
    action: str
    note: str = ""  # «вже на Клубку»: назва оголошення; проблема тексту
    spare: bool = False  # еталону потрібне запасне фото


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

    def of(self, action: str) -> list[Decision]:
        return [d for d in self.decisions if d.action == action]

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
            f"Уже на Клубку за текстом (не імпортуємо): {len(self.of(ON_KLUBOK))}",
            f"Архів Шафи — продано/відкладено (не імпортуємо): {len(self.of(ARCHIVED))}",
            f"Без фото в Експорті (не імпортуємо): {len(self.of(NO_PHOTO))}",
            f"Проблема тексту (не імпортуємо): {len(self.of(TEXT))}",
            (
                f"Уже в хвилі (повтор): {len(self.of(DONE))} · Картка «лише Клубок» без хвилі: "
                f"{len(self.of(KLUBOK_CARD))}"
            ),
        ]
        similar = Counter(d.note for d in new if d.note)
        if similar:
            out.append(f"  серед нових: {_counts(similar)}")
        for title, action in (
            ("Уже на Клубку", ON_KLUBOK),
            ("Без фото", NO_PHOTO),
            ("Проблема тексту", TEXT),
        ):
            items = self.of(action)
            if items:
                out.append(f"{title} (перші {min(len(items), SHOW)}):")
                out += [f"  {d.post_id}: {d.note}"[:110] for d in items[:SHOW]]
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
) -> Decision:
    """Що робити з постом цієї хвилі (нічого не пише)."""
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
    found = matcher.match(post.text)
    if found.verdict == waves.ON and found.listing is not None:
        return Decision(post_id, kind, ON_KLUBOK, f"{found.listing.id} «{found.listing.name}»")
    source = export_dir / post.photo if post.photo else None
    if source is None or not source.is_file():
        return Decision(post_id, kind, NO_PHOTO, waves.first_line(post.text))
    name = klubok_folder_name(post_id)
    problem = find_problem(
        Folder(name, post_id, (SPARE_PHOTO,)), {post_id: post.text}, {post_id: [name]}
    )
    if problem:
        return Decision(post_id, kind, TEXT, problem[1])
    note = {waves.SIMILAR: "схоже", waves.NO_LATIN: "без бренду латиницею"}.get(found.verdict, "")
    return Decision(post_id, kind, NEW, note)


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
        return Decision(d.post_id, d.kind, NO_PHOTO, "фото не відкривається")
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
    report = Report(wave, limit, apply, snapshot)
    matcher = _matcher(conn)
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
        decision = decide(conn, post, kind, root, export_dir, matcher)
        if apply:
            decision = _apply(conn, decision, post, export_dir, wave)
        report.decisions.append(decision)
    return report
