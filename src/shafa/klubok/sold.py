"""KLB-005: «Схоже, продано» — продану пару програма знімає з Клубку лише після «Так» Вікторії.

Без мережі (робот — `robot/klubok_marksold.py`). Джерело правди «продано» — нічна перевірка каналу:
`post_checks.gone_at` (`publish.known_gone`).

* (а) Виставлене програмою: `klubok_ads.state = 'published'` з `klubok_id`, пост зник, `gone_at` ще
  порожній → `sold_asked` + `gone_at`. «Так» → `sold_ok_at` (чекає робота). «Ні» → назад `published`,
  `gone_at` лишається — це й є «не питати» (`detect` бере лише `gone_at IS NULL`).
* (б) Її власне оголошення з надійно відомим постом (`klubok_listings.post_id`), `sold = 0`, пост
  зник, не прив'язане до (а). Колонок відповіді в `klubok_listings` немає (база 19, без міграції),
  тож відповідь лежить в `asked_at`: `NULL` — ще не питали (показуємо), `<час>|yes` — «Так»,
  чекає робота, `<час>|no` — «Ні», більше не показуємо.
* KLB-016: оголошення, прив'язане за фото до перепосту (`link.load_groups`, `<дані>/klubok-link.json`),
  не питаємо, поки інший пост групи живий (нічна перевірка бачила його й не зникло). Файлу немає чи
  він битий — питаємо як завжди (людина однаково підтверджує).
* Екран — по `PAGE` (30) питань: спершу без відповіді, найстаріші зникнення першими; решта — «ще N».
* Продане руками (оголошення зникло з її активних і «Обновить» — `snapshot.save`, тут `detect` —
  наздоганяє старі дані): `published`/`sold_asked` → тихо `sold`, без питання.
* Робот: `targets` → один GET «продано» на оголошення → `removed` (`removed_at`, оголошення `sold = 1`)
  чи `failed`: питання знову (`sold_ok_at` / `asked_at` порожні) з приміткою, чому не вдалось.
"""

import sqlite3
from dataclasses import dataclass

from shafa import events
from shafa.db import now

SOLD_ASKED, SOLD, REMOVED, PUBLISHED = "sold_asked", "sold", "removed", "published"
AD, LISTING = "ad", "listing"
YES, NO = "yes", "no"
SEP = "|"
LIMIT = 20  # оголошень «продано» за сесію робота
PAGE = 30  # KLB-016: питань на екрані за раз


@dataclass(frozen=True)
class Ask:
    """Рядок блоку «Схоже, продано» на екрані «Проблем» Клубку."""

    kind: str  # AD — виставлене програмою (ключ — id Картки), LISTING — її оголошення (id Клубку)
    key: int
    klubok_id: int
    name: str
    photo: str | None
    url: str | None
    post_id: int | None
    gone_at: str | None  # коли нічна перевірка не знайшла пост у каналі
    waiting: bool  # «Так» натиснуто — чекає робота
    note: str | None = None  # минула спроба зняти не вдалась — чому


@dataclass(frozen=True)
class Target:
    """Що робот має позначити проданим."""

    kind: str
    key: int
    klubok_id: int
    name: str
    post_id: int | None


def _mark(answer: str, stamp: str) -> str:
    return f"{stamp}{SEP}{answer}"


def answer_of(asked_at: str | None) -> str | None:
    """`asked_at` (б) → YES / NO / None (ще не питали)."""
    if not asked_at or SEP not in asked_at:
        return None
    tail = asked_at.rsplit(SEP, 1)[1]
    return tail if tail in (YES, NO) else None


# --- виявлення ---

_GONE_AD = (
    "SELECT k.item_id, i.post_id FROM klubok_ads k JOIN items i ON i.id = k.item_id "
    "JOIN post_checks c ON c.post_id = i.post_id "
    "WHERE k.state = 'published' AND k.klubok_id IS NOT NULL AND k.gone_at IS NULL "
    "AND c.gone_at IS NOT NULL"
)
_LISTINGS = (
    "SELECT l.id, l.name, l.photo, l.url, l.post_id, l.asked_at, c.gone_at FROM klubok_listings l "
    "JOIN post_checks c ON c.post_id = l.post_id "
    "WHERE l.sold = 0 AND l.post_id IS NOT NULL AND c.gone_at IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM klubok_ads k WHERE k.klubok_id = l.id)"
)


Groups = dict[int, list[int]]


def _groups(groups: Groups | None) -> Groups:
    if groups is not None:
        return groups
    from shafa.klubok import link

    return link.load_groups()


def _held(conn: sqlite3.Connection, groups: Groups, listing_id: int, post_id: int) -> bool:
    """KLB-016: інший пост групи перепосту ще живий — пара продається, не питаємо."""
    others = [p for p in groups.get(listing_id, ()) if p != post_id]
    if post_id not in groups.get(listing_id, ()) or not others:
        return False
    marks = ",".join("?" * len(others))
    return (
        conn.execute(
            f"SELECT 1 FROM post_checks WHERE post_id IN ({marks}) AND checked_at IS NOT NULL "
            "AND gone_at IS NULL LIMIT 1",
            others,
        ).fetchone()
        is not None
    )


def _listings(
    conn: sqlite3.Connection, groups: Groups, tail: str = "", params: tuple = ()
) -> list[sqlite3.Row]:
    """(б) з `_LISTINGS`, крім тих, чий перепост ще живий."""
    return [
        r for r in conn.execute(_LISTINGS + tail, params).fetchall()
        if not _held(conn, groups, r["id"], r["post_id"])
    ]  # fmt: skip


def manual_sold(conn: sqlite3.Connection, ids: list[int] | None = None) -> int:
    """Продане руками: виставлене програмою, чиє оголошення вже не серед її активних (`sold = 1` у
    знімку; `ids` — щойно зниклі в `snapshot.save`), — тихо `sold`. У транзакції того, хто кличе."""
    stamp = now()
    if ids is not None:
        changed = 0
        for klubok_id in ids:
            changed += conn.execute(
                "UPDATE klubok_ads SET state = ?, updated_at = ? "
                "WHERE klubok_id = ? AND state IN (?, ?)",
                (SOLD, stamp, klubok_id, PUBLISHED, SOLD_ASKED),
            ).rowcount
        return changed
    return conn.execute(
        "UPDATE klubok_ads SET state = ?, updated_at = ? WHERE state IN (?, ?) "
        "AND klubok_id IN (SELECT id FROM klubok_listings WHERE sold = 1)",
        (SOLD, stamp, PUBLISHED, SOLD_ASKED),
    ).rowcount


def detect(conn: sqlite3.Connection) -> list[int]:
    """(а) пост виставленого програмою зник → `sold_asked`. Повертає id Карток, яких щойно спитали.
    Продане руками — тихо `sold`. Повтор нічого не змінює."""
    stamp = now()
    with conn:
        manual_sold(conn)
        asked = []
        for item_id, _post_id in conn.execute(_GONE_AD).fetchall():
            changed = conn.execute(
                "UPDATE klubok_ads SET state = ?, gone_at = ?, sold_ok_at = NULL, "
                "problem_text = NULL, updated_at = ? WHERE item_id = ? AND state = ? "
                "AND gone_at IS NULL",
                (SOLD_ASKED, stamp, stamp, item_id, PUBLISHED),
            ).rowcount
            if changed:
                asked.append(item_id)
    fresh = len(asked) + sum(1 for r in _listings(conn, _groups(None)) if r["asked_at"] is None)
    if asked:
        events.emit(
            "klubok.sold_asked",
            f"Клубок: схоже, продано — {fresh} (пост зник з каналу); питання у «Проблемах» Клубку",
            data={"items": asked},
        )
    return asked


# --- екран ---


def asks(conn: sqlite3.Connection, groups: Groups | None = None) -> list[Ask]:
    """Блок «Схоже, продано»: (а) `sold_asked` і (б) її оголошення без відповіді чи з «Так».
    Порядок: спершу без відповіді, далі «чекає робота»; усередині — найстаріші зникнення першими."""
    groups = _groups(groups)
    out = []
    for r in conn.execute(
        "SELECT k.item_id, k.klubok_id, k.url, k.sold_ok_at, k.problem_text, i.post_id, "
        "c.gone_at, i.folder_name, l.name, l.photo, l.url AS listing_url FROM klubok_ads k "
        "JOIN items i ON i.id = k.item_id LEFT JOIN post_checks c ON c.post_id = i.post_id "
        "LEFT JOIN klubok_listings l ON l.id = k.klubok_id "
        "WHERE k.state = ? ORDER BY k.sold_ok_at IS NOT NULL, c.gone_at, k.item_id",
        (SOLD_ASKED,),
    ):
        ask = Ask(
            AD, r["item_id"], r["klubok_id"], r["name"] or "", r["photo"],
            r["url"] or r["listing_url"], r["post_id"], r["gone_at"],
            r["sold_ok_at"] is not None, r["problem_text"],
        )  # fmt: skip
        out.append(ask)
    for r in _listings(conn, groups, " ORDER BY c.gone_at, l.id"):
        answer = answer_of(r["asked_at"])
        if r["asked_at"] is not None and answer != YES:
            continue
        ask = Ask(
            LISTING, r["id"], r["id"], r["name"], r["photo"], r["url"], r["post_id"],
            r["gone_at"], answer == YES,
        )  # fmt: skip
        out.append(ask)
    out.sort(key=lambda a: (a.waiting, a.gone_at or "", a.kind, a.key))
    return out


def page(conn: sqlite3.Connection, size: int = PAGE) -> tuple[list[Ask], int]:
    """KLB-016: перші `size` питань і скільки їх усього (решта — «ще N» на екрані)."""
    every = asks(conn)
    return every[: max(size, 0)], len(every)


def answer(conn: sqlite3.Connection, kind: str, key: int, yes: bool) -> bool:
    """«Так, продано — зняти з Клубку» / «Ні, ще продається». False — рядок уже не той (нічого
    не змінено). «Так» — лише для ще не відповіданого; «Ні» — і поки чекає робота (передумала)."""
    stamp = now()
    with conn:
        if kind == AD:
            if yes:
                cursor = conn.execute(
                    "UPDATE klubok_ads SET sold_ok_at = ?, problem_text = NULL, updated_at = ? "
                    "WHERE item_id = ? AND state = ? AND sold_ok_at IS NULL",
                    (stamp, stamp, key, SOLD_ASKED),
                )
            else:
                cursor = conn.execute(
                    "UPDATE klubok_ads SET state = ?, sold_ok_at = NULL, problem_text = NULL, "
                    "updated_at = ? WHERE item_id = ? AND state = ?",
                    (PUBLISHED, stamp, key, SOLD_ASKED),
                )
            done = cursor.rowcount == 1
        elif kind == LISTING:
            found = _listings(conn, _groups(None), " AND l.id = ?", (key,))
            row = found[0] if found else None
            current = None if row is None else answer_of(row["asked_at"])
            shown = row is not None and (row["asked_at"] is None or current == YES)
            if not shown or (yes and current == YES):
                return False
            conn.execute(
                "UPDATE klubok_listings SET asked_at = ? WHERE id = ?",
                (_mark(YES if yes else NO, stamp), key),
            )
            done = True
        else:
            return False
    if done:
        what = "зняти з Клубку" if yes else "ще продається — більше не питаємо"
        events.emit("klubok.sold_answer", f"Клубок: «схоже, продано» — {what}",
                    data={"kind": kind, "key": key, "yes": yes})  # fmt: skip
    return done


# --- робот ---


def targets(conn: sqlite3.Connection, limit: int = LIMIT) -> list[Target]:
    """Що позначити проданим: «Так» натиснуто, оголошення ще не зняте. Старші відповіді першими."""
    out = [
        Target(AD, r["item_id"], r["klubok_id"], r["name"] or r["title"] or "", r["post_id"])
        for r in conn.execute(
            "SELECT k.item_id, k.klubok_id, i.post_id, l.name, i.folder_name AS title "
            "FROM klubok_ads k JOIN items i ON i.id = k.item_id "
            "LEFT JOIN klubok_listings l ON l.id = k.klubok_id "
            "WHERE k.state = ? AND k.sold_ok_at IS NOT NULL AND k.klubok_id IS NOT NULL "
            "ORDER BY k.sold_ok_at, k.item_id",
            (SOLD_ASKED,),
        )
    ]
    out += [
        Target(LISTING, r["id"], r["id"], r["name"], r["post_id"])
        for r in conn.execute(_LISTINGS + " ORDER BY l.asked_at, l.id")
        if answer_of(r["asked_at"]) == YES
    ]
    return out[: max(limit, 0)]


def removed(conn: sqlite3.Connection, target: Target) -> None:
    """Клубок підтвердив «продано» (`type=2`, `sold: true`)."""
    stamp = now()
    with conn:
        if target.kind == AD:
            conn.execute(
                "UPDATE klubok_ads SET state = ?, removed_at = ?, problem_text = NULL, "
                "updated_at = ? WHERE item_id = ? AND state = ?",
                (REMOVED, stamp, stamp, target.key, SOLD_ASKED),
            )
        conn.execute("UPDATE klubok_listings SET sold = 1 WHERE id = ?", (target.klubok_id,))


def failed(conn: sqlite3.Connection, target: Target, why: str) -> None:
    """Не підтвердилось: питання знову (з приміткою для (а)), повтору без людини немає."""
    stamp = now()
    with conn:
        if target.kind == AD:
            conn.execute(
                "UPDATE klubok_ads SET sold_ok_at = NULL, problem_text = ?, updated_at = ? "
                "WHERE item_id = ? AND state = ?",
                (why, stamp, target.key, SOLD_ASKED),
            )
        else:
            conn.execute(
                "UPDATE klubok_listings SET asked_at = NULL WHERE id = ? AND asked_at LIKE ?",
                (target.key, f"%{SEP}{YES}"),
            )


def pending(conn: sqlite3.Connection) -> int:
    """Скільки рядків у блоці (для лічильника «Проблем» Клубку)."""
    return len(asks(conn))
