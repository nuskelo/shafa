"""KLB-002: черга Клубку — хто піде, у якому порядку, скільки на день, що не піде і чому.

Живого Клубку тут немає (KLB-003): лише рішення й стани в `klubok_ads`.

* `plan` — вердикт кожній Картці-кандидату, нічого не пише; `sync` — записує вердикти, але
  чіпає лише рядки `queued`/`skipped` (крім її «Так, дубль»), решту станів — ніколи.
* Порядок: зимове → інше → лише літнє; усередині — старіші пости першими.
* `take` — що можна сьогодні (ліміт доби, свіжий знімок її оголошень, дублі перед видачею).
* `start` / `published` / `problem` / `recover` — переходи для робота KLB-003, кожен в одній
  транзакції й з умовою на поточний стан.
* `needs_photos` — фото Картки ще знадобляться Клубку: стирати їх не можна (D7).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shafa import config, variants
from shafa.db import Status, now
from shafa.klubok import dup, fields
from shafa.klubok.dup import Dup
from shafa.review import Card, load_card
from shafa.robot import publish

FLAG = "SHAFA_KLUBOK"
LIMIT_SETTING = "SHAFA_KLUBOK_DAILY_LIMIT"
LIMIT_DEFAULT = 10
LIMIT_RANGE = (1, 30)
FROM_SETTING = "SHAFA_KLUBOK_FROM_POST"
FROM_DEFAULT = 7200
FROM_RANGE = (1, 1_000_000)
SNAPSHOT_MAX = timedelta(hours=24)

QUEUED, PUBLISHING, PUBLISHED, PROBLEM = "queued", "publishing", "published", "problem"
SOLD_ASKED, SOLD, REMOVED, SKIPPED = "sold_asked", "sold", "removed", "skipped"
OPEN = (QUEUED, SKIPPED)  # лише ці рядки програма перераховує сама
ON_KLUBOK = (PUBLISHING, PUBLISHED, SOLD_ASKED, SOLD, REMOVED)  # оголошення є (чи могло з'явитись)
PHOTOS_DONE = (PUBLISHED, SKIPPED, SOLD_ASKED, SOLD, REMOVED)  # фото Клубку більше не потрібні

POST_GONE, POST_DONE = "klubok_post_gone", "klubok_post_done"
DUPLICATE, DUP_YES, UNCERTAIN = "klubok_duplicate", "klubok_dup_yes", "klubok_uncertain"
OFF, SNAPSHOT_OLD, LIMIT_REACHED = "klubok_off", "klubok_snapshot_old", "klubok_limit"
EMPTY = "klubok_queue_empty"

# Причини простою мовою — для екрана й `klubok-queue` (англійських кодів людина не бачить).
REASONS = {
    "klubok_no_category": "для цього розділу немає категорії на Клубку",
    "klubok_size": "розміру немає у списку Клубку",
    "klubok_no_price": "у пості немає ціни",
    "klubok_title_long": "назва довша за 100 знаків",
    "klubok_no_title": "у Картки немає назви",
    "klubok_no_photos": "фото вже стерто (або папка порожня)",
    POST_GONE: "пост видалено з каналу — пару продано",
    POST_DONE: "цей пост уже виставлено на Клубку з іншої Картки",
    DUPLICATE: "схоже, ця пара вже є на Клубку",
    DUP_YES: "ви сказали: це дубль — не виставляти",
    UNCERTAIN: "перевірте на Клубку, чи опубліковано",
    OFF: "Клубок вимкнено в налаштуваннях",
    SNAPSHOT_OLD: "список оголошень на Клубку не перечитано за останню добу",
    LIMIT_REACHED: "ліміт публікацій на Клубку на сьогодні вичерпано",
    EMPTY: "у черзі Клубку нічого немає",
}


class SetupError(Exception):
    """Налаштування Клубку з помилкою."""


def _int_setting(name: str, default: int, bounds: tuple[int, int]) -> int:
    raw = config.setting(name)
    if raw is None:
        return default
    low, high = bounds
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise SetupError(f"{name} — ціле число від {low} до {high}")
    return int(raw)


def enabled() -> bool:
    return (config.setting(FLAG) or "").lower() in ("1", "так", "yes", "true")


def daily_limit() -> int:
    return _int_setting(LIMIT_SETTING, LIMIT_DEFAULT, LIMIT_RANGE)


def from_post() -> int:
    return _int_setting(FROM_SETTING, FROM_DEFAULT, FROM_RANGE)


@dataclass(frozen=True)
class Verdict:
    item_id: int
    post_id: int | None
    state: str  # queued / skipped / problem (дубль)
    code: str | None = None
    text: str | None = None
    ad: fields.KlubokAd | None = None
    dup: Dup | None = None  # (не `dup.Dup`: на 3.12 поле `dup` уже None)
    rank: int = 1  # 0 — зимове, 1 — інше, 2 — лише літнє

    @property
    def reason(self) -> str:
        return REASONS.get(self.code or "", self.text or "")


def _candidates(conn: sqlite3.Connection, item_id: int | None = None) -> list[sqlite3.Row]:
    """D3: схвалені й опубліковані на Шафі, пост новіший за межу, рядка Клубку немає або він
    `queued`/`skipped` (її «Так, дубль» — ні). Порядок — старіші пости першими."""
    sql = (
        "SELECT i.id, i.post_id FROM items i LEFT JOIN klubok_ads k ON k.item_id = i.id "
        "LEFT JOIN posts p ON p.post_id = i.post_id "
        "WHERE i.status IN (?, ?) AND i.purged_at IS NULL AND i.post_id > ? "
        "AND (k.item_id IS NULL OR (k.state IN (?, ?) AND COALESCE(k.problem_code, '') != ?))"
    )
    args: list = [Status.APPROVED, Status.PUBLISHED, from_post(), *OPEN, DUP_YES]
    if item_id is not None:
        sql += " AND i.id = ?"
        args.append(item_id)
    return conn.execute(sql + " ORDER BY p.date IS NULL, p.date, i.id", args).fetchall()


def post_done(conn: sqlite3.Connection, card: Card) -> bool:
    """D6.1: інша Картка того самого поста (не сестра-розмір) вже на Клубку чи могла туди потрапити."""
    if card.post_id is None:
        return False
    sisters = {r["id"] for r in variants.sisters(conn, card.folder_name, card.post_id)}
    rows = conn.execute(
        "SELECT k.item_id FROM klubok_ads k JOIN items i ON i.id = k.item_id "
        f"WHERE i.post_id = ? AND k.item_id != ? AND (k.state IN ({', '.join('?' * len(ON_KLUBOK))}) "
        "OR (k.state = ? AND k.problem_code = ?))",
        (card.post_id, card.id, *ON_KLUBOK, PROBLEM, UNCERTAIN),
    )
    return any(r[0] not in sisters for r in rows)


def _dup_ok(conn: sqlite3.Connection, item_id: int) -> bool:
    row = conn.execute("SELECT dup_ok_at FROM klubok_ads WHERE item_id = ?", (item_id,)).fetchone()
    return row is not None and row[0] is not None


def rank(card: Card) -> int:
    """Місце групи в черзі: 0 — зимове, 1 — інше, 2 — лише літнє."""
    path = fields.card_path(card)
    if not path:
        return 1
    return 0 if fields.winter(card, path) else 2 if fields.summer_only(card, path) else 1


def judge(conn: sqlite3.Connection, root: Path, card: Card) -> Verdict:
    """Вердикт однієї Картки-кандидата (нічого не пише)."""
    base = {"item_id": card.id, "post_id": card.post_id, "rank": rank(card)}
    if card.post_id is not None and publish.known_gone(conn, card.post_id):
        return Verdict(**base, state=SKIPPED, code=POST_GONE)
    if post_done(conn, card):
        return Verdict(**base, state=SKIPPED, code=POST_DONE)
    result = fields.ad(card, root)
    if isinstance(result, fields.Skip):
        return Verdict(**base, state=SKIPPED, code=result.code, text=result.text)
    if not _dup_ok(conn, card.id):
        found = dup.find(conn, card)
        if found is not None:
            return Verdict(**base, state=PROBLEM, code=DUPLICATE, ad=result, dup=found)
    return Verdict(**base, state=QUEUED, ad=result)


def plan(conn: sqlite3.Connection, root: Path, today: datetime | None = None) -> list[Verdict]:
    """Вердикти всім кандидатам у порядку черги (`today` — для однаковості з `take`)."""
    verdicts = []
    for row in _candidates(conn):
        card = load_card(conn, row["id"])
        if card is not None:
            verdicts.append(judge(conn, root, card))
    return sorted(verdicts, key=lambda v: v.rank)  # стабільне: усередині — порядок `_candidates`


def plan_one(conn: sqlite3.Connection, root: Path, item_id: int) -> Verdict | None:
    """Вердикт однієї Картки для екрана (GET нічого не пише); не кандидат — None."""
    if not _candidates(conn, item_id):
        return None
    card = load_card(conn, item_id)
    return judge(conn, root, card) if card else None


def _write(conn: sqlite3.Connection, v: Verdict, stamp: str) -> None:
    dup_id, dup_name = (v.dup.klubok_id, v.dup.name) if v.dup else (None, None)
    text = None if v.state == QUEUED else (v.text or REASONS.get(v.code or ""))
    queued_at = stamp if v.state == QUEUED else None
    conn.execute(
        "INSERT INTO klubok_ads (item_id, state, problem_code, problem_text, dup_klubok_id, "
        "dup_name, queued_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (item_id) DO UPDATE SET state = excluded.state, "
        "problem_code = excluded.problem_code, problem_text = excluded.problem_text, "
        "dup_klubok_id = excluded.dup_klubok_id, dup_name = excluded.dup_name, "
        "queued_at = CASE WHEN excluded.state = 'queued' "
        "THEN COALESCE(klubok_ads.queued_at, excluded.queued_at) END, "
        "updated_at = excluded.updated_at "
        "WHERE klubok_ads.state IN ('queued', 'skipped') "
        "AND COALESCE(klubok_ads.problem_code, '') != 'klubok_dup_yes' "
        "AND (klubok_ads.state, COALESCE(klubok_ads.problem_code, ''), "
        "COALESCE(klubok_ads.problem_text, '')) IS NOT "
        "(excluded.state, COALESCE(excluded.problem_code, ''), COALESCE(excluded.problem_text, ''))",
        (v.item_id, v.state, v.code, text, dup_id, dup_name, queued_at, stamp, stamp),
    )


def sync_one(conn: sqlite3.Connection, root: Path, item_id: int) -> None:
    """Записати вердикт однієї Картки (екран: кнопка під рядком «Клубок», рядка ще немає)."""
    verdict = plan_one(conn, root, item_id)
    if verdict is not None:
        with conn:
            _write(conn, verdict, now())


def forget(conn: sqlite3.Connection, item_id: int) -> None:
    """Картку знято зі схвалених: її місце в черзі зникає (у транзакції того, хто кличе)."""
    conn.execute("DELETE FROM klubok_ads WHERE item_id = ? AND state = ?", (item_id, QUEUED))


def sync(conn: sqlite3.Connection, root: Path, today: datetime | None = None) -> list[Verdict]:
    """Записати вердикти `plan`. Рядки `queued`/`skipped` Карток, що вже не кандидати, — геть."""
    verdicts = plan(conn, root, today)
    stamp = now()
    with conn:
        conn.execute(
            "DELETE FROM klubok_ads WHERE state IN (?, ?) AND COALESCE(problem_code, '') != ? "
            "AND item_id NOT IN (SELECT i.id FROM items i WHERE i.status IN (?, ?) "
            "AND i.purged_at IS NULL AND i.post_id > ?)",
            (*OPEN, DUP_YES, Status.APPROVED, Status.PUBLISHED, from_post()),
        )
        for verdict in verdicts:
            _write(conn, verdict, stamp)
    return verdicts


# --- ліміт доби й видача (D4) ---


def started_today(conn: sqlite3.Connection, today: datetime) -> int:
    """Скільки публікацій Клубку почато за місцеву добу — тим самим способом, що й у Шафи."""
    return len(
        publish.started_on(
            conn, "SELECT item_id, publish_started_at FROM klubok_ads WHERE publish_started_at >= ?",
            today,
        )
    )  # fmt: skip


def snapshot_fresh(conn: sqlite3.Connection, today: datetime) -> bool:
    """Знімок її оголошень є й не старший за добу (інакше дублі не перевірити)."""
    row = conn.execute("SELECT read_at FROM klubok_listings_read WHERE id = 1").fetchone()
    if row is None:
        return False
    read = datetime.fromisoformat(row[0])
    if read.tzinfo is None:
        read = read.replace(tzinfo=UTC)
    return timedelta(0) <= today - read <= SNAPSHOT_MAX


@dataclass(frozen=True)
class Taken:
    cards: list[Verdict]
    code: str | None = None  # чому порожньо: OFF / SNAPSHOT_OLD / LIMIT_REACHED / EMPTY

    @property
    def reason(self) -> str:
        return REASONS.get(self.code or "", "")


def take(
    conn: sqlite3.Connection, root: Path, today: datetime, post_id: int | None = None
) -> Taken:
    """Картки, які можна виставити сьогодні, по порядку черги. Перед видачею кожна ще раз
    перевіряється на дубль (KLB-003 кличе знову після кожної публікації). `post_id` — лише Картки
    цього поста (фільтр до обрізання за лімітом; ліміт доби діє як завжди)."""
    if not enabled():
        return Taken([], OFF)
    if not snapshot_fresh(conn, today):
        return Taken([], SNAPSHOT_OLD)
    left = daily_limit() - started_today(conn, today)
    if left <= 0:
        return Taken([], LIMIT_REACHED)
    out: list[Verdict] = []
    for verdict in sync(conn, root, today):
        if verdict.state != QUEUED or (post_id is not None and verdict.post_id != post_id):
            continue
        card = load_card(conn, verdict.item_id)
        fresh = judge(conn, root, card) if card else None
        if fresh is None or fresh.state != QUEUED:
            if fresh is not None:
                with conn:
                    _write(conn, fresh, now())
            continue
        out.append(fresh)
        if len(out) >= left:
            break
    return Taken(out, None if out else EMPTY)


def place(conn: sqlite3.Connection, root: Path, item_id: int) -> int | None:
    """D7: місце Картки серед рядків `queued` — у порядку `plan` (зимове → інше → лише літнє, далі
    дата поста й id), але без `dup.find`, без `fields.ad` і без запису: екран відкриває Картку
    швидко й при тисячах її оголошень. Рядка `queued` немає — None («у черзі» без номера).
    `root` — для однаковості з `plan` (фото тут не потрібні)."""
    del root
    rows = conn.execute(
        "SELECT k.item_id FROM klubok_ads k JOIN items i ON i.id = k.item_id "
        "LEFT JOIN posts p ON p.post_id = i.post_id WHERE k.state = ? "
        "ORDER BY p.date IS NULL, p.date, i.id",
        (QUEUED,),
    ).fetchall()
    ids = [r[0] for r in rows]
    if item_id not in ids:
        return None
    ranks = {i: rank(card) if (card := load_card(conn, i)) else 1 for i in ids}
    ordered = sorted(ids, key=lambda i: ranks[i])  # стабільне, як у `plan`
    return ordered.index(item_id) + 1


# --- стани для KLB-003 (D5) ---


def _move(conn: sqlite3.Connection, item_id: int, source: str, sets: str, args: tuple) -> bool:
    with conn:
        cursor = conn.execute(
            f"UPDATE klubok_ads SET {sets}, updated_at = ? WHERE item_id = ? AND state = ?",
            (*args, now(), item_id, source),
        )
    return cursor.rowcount == 1


def start(conn: sqlite3.Connection, item_id: int, session_id: int) -> bool:
    return _move(
        conn, item_id, QUEUED, "state = ?, publish_started_at = ?, session_id = ?",
        (PUBLISHING, now(), session_id),
    )  # fmt: skip


def published(conn: sqlite3.Connection, item_id: int, klubok_id: int, url: str | None) -> bool:
    """`url` None — посилання допише наступний знімок (KLB-003 DRIFT-1)."""
    return _move(
        conn, item_id, PUBLISHING, "state = ?, klubok_id = ?, url = ?, published_at = ?",
        (PUBLISHED, klubok_id, url, now()),
    )  # fmt: skip


def release(conn: sqlite3.Connection, item_id: int) -> bool:
    """`publishing` → `queued`: запит публікації точно не пішов (guard його не бачив).
    `publish_started_at` лишається — спроба в ліміті доби (повторний старт того ж дня
    перезапише час, тож дві спроби однієї Картки за добу рахуються як одна; похибка ≤ 1)."""
    return _move(conn, item_id, PUBLISHING, "state = ?", (QUEUED,))


def problem(conn: sqlite3.Connection, item_id: int, code: str, text: str) -> bool:
    """`queued`/`publishing` → `problem` (повтору немає: Проблему вирішує людина)."""
    with conn:
        cursor = conn.execute(
            "UPDATE klubok_ads SET state = ?, problem_code = ?, problem_text = ?, updated_at = ? "
            "WHERE item_id = ? AND state IN (?, ?)",
            (PROBLEM, code, text, now(), item_id, QUEUED, PUBLISHING),
        )
    return cursor.rowcount == 1


def recover(conn: sqlite3.Connection) -> list[int]:
    """Після обриву: що лишилось у `publishing` — у Проблеми «перевірте, чи опубліковано»."""
    ids = [
        r[0] for r in conn.execute("SELECT item_id FROM klubok_ads WHERE state = ?", (PUBLISHING,))
    ]
    for item_id in ids:
        problem(conn, item_id, UNCERTAIN, "Перевірте на Клубку, чи опубліковано")
    return ids


# --- рішення людини про дубль (D6.3) ---


def dup_other(conn: sqlite3.Connection, item_id: int) -> bool:
    """«Інша пара — виставити»: у чергу, дублі для Картки більше не перевіряються."""
    stamp = now()
    with conn:
        cursor = conn.execute(
            "UPDATE klubok_ads SET state = ?, problem_code = NULL, problem_text = NULL, "
            "dup_ok_at = ?, queued_at = ?, updated_at = ? "
            "WHERE item_id = ? AND state = ? AND problem_code = ?",
            (QUEUED, stamp, stamp, stamp, item_id, PROBLEM, DUPLICATE),
        )
    return cursor.rowcount == 1


def dup_yes(conn: sqlite3.Connection, item_id: int) -> bool:
    """«Так, дубль — не виставляти». Її оголошення без поста — тепер знає свій пост (KLB-005)."""
    stamp = now()
    with conn:
        row = conn.execute(
            "SELECT k.dup_klubok_id, i.post_id FROM klubok_ads k JOIN items i ON i.id = k.item_id "
            "WHERE k.item_id = ? AND k.state = ? AND k.problem_code = ?",
            (item_id, PROBLEM, DUPLICATE),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE klubok_ads SET state = ?, problem_code = ?, problem_text = ?, updated_at = ? "
            "WHERE item_id = ?",
            (SKIPPED, DUP_YES, REASONS[DUP_YES], stamp, item_id),
        )
        if row[0] is not None and row[1] is not None:
            conn.execute(
                "UPDATE klubok_listings SET post_id = ? WHERE id = ? AND post_id IS NULL",
                (row[1], row[0]),
            )
    return True


# --- фото (D7) ---


def needs_photos(conn: sqlite3.Connection, item_id: int) -> bool:
    """Фото Картки ще знадобляться Клубку. Не залежить від `SHAFA_KLUBOK` (рішення Ітерації 1):
    кандидат захищений, поки його рядок не `published`/`skipped`/`sold*`/`removed`."""
    row = conn.execute("SELECT state FROM klubok_ads WHERE item_id = ?", (item_id,)).fetchone()
    if row is not None:
        return row[0] not in PHOTOS_DONE
    try:
        limit = from_post()
    except SetupError:
        limit = FROM_DEFAULT  # налаштування з помилкою — краще зберегти фото
    return (
        conn.execute(
            "SELECT 1 FROM items WHERE id = ? AND status IN (?, ?) AND purged_at IS NULL "
            "AND post_id > ?",
            (item_id, Status.APPROVED, Status.PUBLISHED, limit),
        ).fetchone()
        is not None
    )


def post_needs_photos(conn: sqlite3.Connection, post_id: int | None) -> bool:
    """Чи потрібні Клубку фото хоч однієї Картки поста (сестри-розміри ділять теку)."""
    if post_id is None:
        return False
    ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE post_id = ?", (post_id,))]
    return any(needs_photos(conn, item_id) for item_id in ids)
