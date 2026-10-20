"""Заповнення Карток ШІ-бланком: `new` → запит до Claude → `review` або `problem` (`ai_error`).

Тут — звичайні запити (по одному) і «ШІ, спробуй ще раз»; пакет — `batch.py`.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import UnidentifiedImageError

from shafa import ai, catalog, review, rules, scan
from shafa.db import Status, now
from shafa.folders import item_folder
from shafa.photos import PreparedPhoto, prepare
from shafa.robot.plan import precheck
from shafa.scan import NO_SECTION_TEXT

# Захист від випадкових витрат: більше запитів за один запуск не буває.
MAX_PER_RUN = 10


@dataclass(frozen=True)
class Planned:
    """Картка, для якої зібрано запит (ще не надісланий)."""

    item_id: int
    folder_name: str
    post_id: int
    post_text: str
    photos: list[PreparedPhoto]
    request: dict[str, Any]
    estimate: ai.Usage
    edited: str = ""  # її відредаговані назва й опис — правила читають і їх (CORE-008 B)


@dataclass(frozen=True)
class Skipped:
    folder_name: str
    text: str


@dataclass(frozen=True)
class Filled:
    folder_name: str
    usage: ai.Usage | None
    yellow: int = 0
    error: str | None = None


# Картки, що чекають результату пакета: їх не відправляємо вдруге (D3).
IN_BATCH = "SELECT item_id FROM ai_batch_items WHERE state = 'pending'"


def pick_items(conn: sqlite3.Connection, post_id: int | None, limit: int) -> list[sqlite3.Row]:
    """Картки `new` з постом (не в пакеті): одна за номером поста або перші `limit`."""
    query = (
        "SELECT id, folder_name, post_id FROM items WHERE status = ? AND post_id IS NOT NULL "
        f"AND id NOT IN ({IN_BATCH})"
    )
    params: list[Any] = [Status.NEW]
    if post_id is not None:
        query += " AND post_id = ?"
        params.append(post_id)
    query += " ORDER BY id LIMIT ?"
    params.append(min(limit, MAX_PER_RUN))
    return conn.execute(query, params).fetchall()


def edits_of(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    return {
        r["field"]: json.loads(r["value"])
        for r in conn.execute("SELECT field, value FROM edits WHERE item_id = ?", (item_id,))
    }


def _shown(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):
        return "так" if value else "ні"
    return "" if value is None else str(value)


def corrections(edits: dict[str, Any]) -> list[tuple[str, str]]:
    """Її правки для ШІ: «поле → значення» (порядок фото ШІ не цікавить)."""
    return [
        (review.SPEC[spec.name].label, _shown(edits[spec.name]))
        for spec in review.SPECS
        if spec.name in edits and spec.name != "photo_order"
    ]


def plan(
    conn: sqlite3.Connection, root: Path, row: sqlite3.Row, batch: bool = False
) -> Planned | Skipped:
    """Зібрати запит для Картки: текст поста й підготовлені фото. Нічого не надсилає.

    `batch` — для пакета: інструкція з позначкою кешу, оцінка — за ціною пакета.
    """
    text = conn.execute("SELECT text FROM posts WHERE post_id = ?", (row["post_id"],)).fetchone()
    if text is None:
        return Skipped(row["folder_name"], f"Поста {row['post_id']} немає в базі")
    names = [
        r[0]
        for r in conn.execute(
            "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (row["id"],)
        )
    ]
    photos = []
    for name in names:
        try:
            photos.append(prepare(item_folder(root, row["folder_name"]) / name))
        except (OSError, UnidentifiedImageError):
            return Skipped(row["folder_name"], f"Фото не відкривається: {name}")
    edits = edits_of(conn, row["id"])
    request = ai.build_request(text[0], photos, cache=batch, corrections=corrections(edits))
    estimate = ai.estimate(request, photos, batch=batch)
    return Planned(
        row["id"], row["folder_name"], row["post_id"], text[0], photos, request, estimate,
        rules.edit_text(edits),
    )  # fmt: skip


def save_answer(
    conn: sqlite3.Connection,
    item_id: int,
    answer: dict[str, Any],
    prompt_version: int = ai.PROMPT_VERSION,
    rebuilt: bool = False,
    needs_ai: bool = False,
) -> None:
    """Відповідь ШІ як є — нові правила коду застосовуються до неї без нового запиту (CORE-009 B)."""
    conn.execute(
        """
        INSERT INTO ai_answers (item_id, answer, prompt_version, rebuilt, needs_ai, saved_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (item_id) DO UPDATE SET answer = excluded.answer,
            prompt_version = excluded.prompt_version, rebuilt = excluded.rebuilt,
            needs_ai = excluded.needs_ai, saved_at = excluded.saved_at
        """,
        (
            item_id,
            json.dumps(answer, ensure_ascii=False),
            prompt_version,
            int(rebuilt),
            int(needs_ai),
            now(),
        ),
    )


def save_blank(
    conn: sqlite3.Connection,
    item_id: int,
    fields: list[ai.Field],
    answer: dict[str, Any] | None = None,
) -> None:
    """Бланк (поля після коду) і, якщо є, відповідь ШІ, з якої його зроблено."""
    if answer is not None:
        save_answer(conn, item_id, answer)
    conn.execute("DELETE FROM blanks WHERE item_id = ?", (item_id,))
    conn.execute(
        "UPDATE items SET rules_version = ?, refill_at = NULL WHERE id = ?",
        (rules.RULES_VERSION, item_id),
    )
    conn.executemany(
        "INSERT INTO blanks (item_id, field, value, yellow, reason) VALUES (?, ?, ?, ?, ?)",
        [
            (item_id, f.name, json.dumps(f.value, ensure_ascii=False), int(f.yellow), f.reason)
            for f in fields
        ],
    )


def log_request(
    conn: sqlite3.Connection,
    item_id: int,
    usage: ai.Usage | None,
    error: str | None,
    batch_id: int | None = None,
) -> None:
    """Запис вартості: фактичні токени й ціна з урахуванням кешу і знижки пакета."""
    usage = usage or ai.Usage(0, 0)
    conn.execute(
        """
        INSERT INTO ai_requests (item_id, model, input_tokens, output_tokens, cache_write_tokens,
                                 cache_read_tokens, cost_micro_usd, error, batch_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            ai.MODEL,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_write_tokens,
            usage.cache_read_tokens,
            usage.cost_micro_usd,
            error,
            batch_id,
            now(),
        ),
    )


def no_section(fields: list[ai.Field]) -> bool:
    """ШІ не знайшло розділу для речі (інструкція: схожий не підставляти) — у «Проблеми»."""
    return any(f.name == "section" and not f.value for f in fields)


def _kept_old_blank(conn: sqlite3.Connection, item_id: int) -> bool:
    """Картка на перезаповненні має старий бланк із розділом — помилка ШІ його не забирає."""
    row = conn.execute(
        "SELECT items.refill_at, blanks.value FROM items "
        "LEFT JOIN blanks ON blanks.item_id = items.id AND blanks.field = 'section' "
        "WHERE items.id = ?",
        (item_id,),
    ).fetchone()
    return bool(row and row["refill_at"] and row["value"] and json.loads(row["value"]))


def set_status(
    conn: sqlite3.Connection, item_id: int, error: str | None, fields: list[ai.Field] = ()
) -> bool:
    """Новий статус лише для Картки, що досі `new`. Повертає, чи змінено.

    Помилка ШІ на перезаповненні (CORE-008): Картка з готовим бланком повертається на перевірку
    зі старим бланком, а не в «Проблеми».
    """
    status, code = (Status.PROBLEM, "ai_error") if error else (Status.REVIEW, None)
    if error and _kept_old_blank(conn, item_id):
        status, code, error = Status.REVIEW, None, None
    elif not error and no_section(fields):
        status, code, error = Status.PROBLEM, "no_section", NO_SECTION_TEXT
    cursor = conn.execute(
        """
        UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ?,
                         refill_at = NULL
        WHERE id = ? AND status = ?
        """,
        (status, code, error, now(), item_id, Status.NEW),
    )
    return cursor.rowcount == 1


# --- Перезаповнити (CORE-008 A) ---

# Проблеми, які створили ШІ чи правила над його відповіддю (решта — дані й публікація).
REFILL_PROBLEMS = ("ai_error", "no_section")


# Нова інструкція ШІ могла змінити Картку (CORE-009 B: «Перезаповнити» — лише такі).
NEEDS_AI = "EXISTS (SELECT 1 FROM ai_answers WHERE item_id = items.id AND needs_ai = 1)"


def refill_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Картки, яким варто перезаповнитись: помилка ШІ, а `review` і `no_section` з бланком ШІ —
    лише ті, які нова інструкція ШІ могла змінити (CORE-009 B; зміни коду — «Перерахувати
    правилами», без ШІ). Схвалені й опубліковані — ніколи."""
    return conn.execute(
        f"""
        SELECT id, folder_name, post_id FROM items
        WHERE post_id IS NOT NULL AND purged_at IS NULL AND id NOT IN ({IN_BATCH})
          AND ((status = ? AND {NEEDS_AI})
               OR (status = ? AND (problem_code = 'ai_error'
                    OR (problem_code = 'no_section' AND {NEEDS_AI}))))
        ORDER BY id
        """,
        (Status.REVIEW, Status.PROBLEM),
    ).fetchall()


def mark_refill(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """Повернути Картки в чергу ШІ: `new` + `refill_at` (без чекання 7 днів). Бланк і правки лишаються
    до відповіді ШІ. Повертає, скільки Карток справді позначено.

    QA-001: пост, у якому з тексту видно кілька товарів чи річ без розділу (правила новіші за
    Картку), іде в «Проблеми» без ШІ — і теж рахується позначеним.
    """
    marked = 0
    with conn:
        for item_id in item_ids:
            row = conn.execute(
                "SELECT posts.text FROM items JOIN posts ON posts.post_id = items.post_id "
                "WHERE items.id = ?",
                (item_id,),
            ).fetchone()
            problem = scan.text_problem(row["text"]) if row else None
            status, code, text, refill_at = (
                (Status.PROBLEM, *problem, None) if problem else (Status.NEW, None, None, now())
            )
            cursor = conn.execute(
                f"""
                UPDATE items SET status = ?, problem_code = ?, problem_text = ?,
                                 refill_at = ?, updated_at = ?
                WHERE id = ? AND post_id IS NOT NULL AND id NOT IN ({IN_BATCH})
                  AND (status = ? OR (status = ? AND problem_code IN ('ai_error', 'no_section')))
                """,
                (status, code, text, refill_at, now(), item_id, Status.REVIEW, Status.PROBLEM),
            )
            marked += cursor.rowcount
    return marked


@dataclass(frozen=True)
class RefillPosts:
    marked: list[int]  # номери постів, Картки яких повернуто в чергу ШІ
    skipped: list[int]  # номер не знайдено або Картка не в `review`/«Проблеми» (схвалені — ніколи)


def refill_posts(conn: sqlite3.Connection, post_ids: list[int]) -> RefillPosts:
    """«Перезаповнити» лише названі пости (CORE-010: сім Карток `no_section`, не всі). Та сама
    позначка, що в `mark_refill`: `new` + `refill_at`, а ШІ-пакет (`fill --batch`, «Оновити») забере."""
    marked, skipped = [], []
    for post_id in dict.fromkeys(post_ids):
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM items WHERE post_id = ? AND purged_at IS NULL ORDER BY id",
                (post_id,),
            )
        ]
        (marked if ids and mark_refill(conn, ids) else skipped).append(post_id)
    return RefillPosts(marked, skipped)


# --- Перерахувати правилами, без ШІ (CORE-009 B) ---

# Проблеми, які ставлять правила над текстом і відповіддю ШІ: перерахунок може їх зняти чи поставити.
STALE_RULES = "stale_rules"  # робот не бере Картку, схвалену за старими правилами (ROBOT-008)
RECHECK_PROBLEMS = ("no_section", "many_items", STALE_RULES)
_AI_DOUBT = "ШІ не впевнене: "


def recheck_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Картки з бланком за старшими правилами коду: `review` і проблеми правил. Схвалені,
    опубліковані й ті, що в ШІ (`new`, пакет), — ніколи."""
    marks = ", ".join("?" * len(RECHECK_PROBLEMS))
    return conn.execute(
        f"""
        SELECT id FROM items
        WHERE post_id IS NOT NULL AND purged_at IS NULL AND id NOT IN ({IN_BATCH})
          AND (rules_version IS NULL OR rules_version < ?)
          AND (status = ? OR (status = ? AND problem_code IN ({marks})))
          AND EXISTS (SELECT 1 FROM blanks WHERE item_id = items.id)
        ORDER BY id
        """,
        (rules.RULES_VERSION, Status.REVIEW, Status.PROBLEM, *RECHECK_PROBLEMS),
    ).fetchall()


def answer_from_blank(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    """Бланк до CORE-009 (відповіді ШІ не збереглося) → відповідь ШІ, наскільки її видно з бланка:
    значення після коду (розміри вже європейські), сумніви ШІ — з причин «ШІ не впевнене: …»."""
    answer: dict[str, Any] = {"size_system": "EU", "doubts": []}
    for row in conn.execute(
        "SELECT field, value, reason FROM blanks WHERE item_id = ?", (item_id,)
    ):
        answer[row["field"]] = json.loads(row["value"]) if row["value"] is not None else None
        for part in (row["reason"] or "").split(_AI_DOUBT)[1:]:
            reason = part.strip().removesuffix(";").strip()
            answer["doubts"].append({"field": row["field"], "reason": reason})
    return answer


def _stored_answer(conn: sqlite3.Connection, item_id: int) -> tuple[dict[str, Any], int, bool]:
    row = conn.execute(
        "SELECT answer, prompt_version, rebuilt FROM ai_answers WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:  # бланк до CORE-009: інструкція — не новіша за QA-001
        return answer_from_blank(conn, item_id), 1, True
    return json.loads(row["answer"]), row["prompt_version"], bool(row["rebuilt"])


def recheck(conn: sqlite3.Connection, item_ids: list[int] | None = None) -> int:
    """Поточні правила коду — до збереженої відповіді ШІ, тексту поста і її правок (`edits` не
    чіпаються). ШІ не викликається. Заодно — чи потрібне Картці ШІ (нова інструкція). Повертає,
    скільки Карток перераховано."""
    ids = [r["id"] for r in recheck_candidates(conn)]
    if item_ids is not None:
        ids = [i for i in ids if i in set(item_ids)]
    marks = ", ".join("?" * len(RECHECK_PROBLEMS))
    done = 0
    with conn:
        for item_id in ids:
            text = conn.execute(
                "SELECT posts.text FROM items JOIN posts USING (post_id) WHERE items.id = ?",
                (item_id,),
            ).fetchone()
            if text is None:
                continue
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
                )
            ]
            answer, version, rebuilt = _stored_answer(conn, item_id)
            fields = ai.check_answer(
                answer, names, text[0], rules.edit_text(edits_of(conn, item_id))
            )
            problem = scan.text_problem(text[0])
            if problem is None and no_section(fields):
                problem = ("no_section", NO_SECTION_TEXT)
            status, code, why = (
                (Status.PROBLEM, *problem) if problem else (Status.REVIEW, None, None)
            )
            cursor = conn.execute(
                f"""
                UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ?
                WHERE id = ? AND (status = ? OR (status = ? AND problem_code IN ({marks})))
                """,
                (status, code, why, now(), item_id, Status.REVIEW, Status.PROBLEM,
                 *RECHECK_PROBLEMS),
            )  # fmt: skip
            if cursor.rowcount != 1:
                continue
            # кілька товарів у пості ШІ не виправить — «Перезаповнити» їм не пропонуємо
            needs = code != "many_items" and ai.needs_ai(version, answer, text[0])
            save_answer(conn, item_id, answer, version, rebuilt, needs)
            save_blank(conn, item_id, fields)
            done += 1
    return done


# Проблеми публікації, після яких Картка могла вже вийти на Шафу, — не чіпаємо (CORE-013).
_MAYBE_PUBLISHED = ("publish_uncertain", "possible_duplicate", "already_published")


@dataclass(frozen=True)
class SizesResult:
    fixed: list[int]  # номери постів Карток, яким повернули розмір (у `review`)
    still_empty: list[
        int
    ]  # номери постів: розділ має розміри, а відповідь ШІ їх не містить — лишили як є


def sizes_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Картки з порожнім розміром у розділі, де розмір є (CORE-013: знімок давав «Европейский»,
    `catalog.sizes` шукав «Європейський» і бланк лишався без розміру). `review`, схвалені й
    проблеми, крім тих, що могли вже вийти на Шафу; без ручної правки розміру."""
    marks = ", ".join("?" * len(_MAYBE_PUBLISHED))
    rows = conn.execute(
        f"""
        SELECT items.id, items.post_id, section.value AS section FROM items
        JOIN blanks AS sizes ON sizes.item_id = items.id AND sizes.field = 'sizes'
        JOIN blanks AS section ON section.item_id = items.id AND section.field = 'section'
        WHERE items.post_id IS NOT NULL AND items.purged_at IS NULL
          AND items.id NOT IN ({IN_BATCH})
          AND (sizes.value IS NULL OR sizes.value IN ('null', '[]'))
          AND (items.status IN (?, ?)
               OR (items.status = ? AND COALESCE(items.problem_code, '') NOT IN ({marks})))
          AND NOT EXISTS (SELECT 1 FROM edits
                          WHERE edits.item_id = items.id AND edits.field = 'sizes')
        ORDER BY items.id
        """,
        (Status.REVIEW, Status.APPROVED, Status.PROBLEM, *_MAYBE_PUBLISHED),
    ).fetchall()
    return [r for r in rows if catalog.sizes(json.loads(r["section"]) or "")]


def recheck_sizes(conn: sqlite3.Connection, dry_run: bool = False) -> SizesResult:
    """Бланк з уже збереженої відповіді ШІ — без запиту й без нового `RULES_VERSION` (CORE-013).

    Лише Картки з `sizes_candidates`. Схвалені й проблеми повертаються в `review` (автоматично
    не публікуються); `edits` не чіпаються. `dry_run` — лише порахувати."""
    rows = sizes_candidates(conn)
    fixed: list[int] = []
    still_empty: list[int] = []
    with conn:
        for row in rows:
            item_id = row["id"]
            text = conn.execute(
                "SELECT posts.text FROM items JOIN posts USING (post_id) WHERE items.id = ?",
                (item_id,),
            ).fetchone()
            if text is None:
                continue
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
                )
            ]
            answer, version, rebuilt = _stored_answer(conn, item_id)
            fields = ai.check_answer(
                answer, names, text[0], rules.edit_text(edits_of(conn, item_id))
            )
            if not next(f.value for f in fields if f.name == "sizes"):
                still_empty.append(row["post_id"])
                continue
            fixed.append(row["post_id"])
            if dry_run:
                continue
            problem = scan.text_problem(text[0])
            if problem is None and no_section(fields):
                problem = ("no_section", NO_SECTION_TEXT)
            status, code, why = (
                (Status.PROBLEM, *problem) if problem else (Status.REVIEW, None, None)
            )
            conn.execute(
                "UPDATE items SET status = ?, problem_code = ?, problem_text = ?, updated_at = ? "
                "WHERE id = ?",
                (status, code, why, now(), item_id),
            )
            needs = code != "many_items" and ai.needs_ai(version, answer, text[0])
            save_answer(conn, item_id, answer, version, rebuilt, needs)
            save_blank(conn, item_id, fields)
    return SizesResult(fixed, still_empty)


@dataclass(frozen=True)
class PrecheckResult:
    returned: list[
        tuple[int | None, str]
    ]  # (номер поста, чому робот не зможе) — Картки, що йдуть назад
    kept: int  # Схвалених, які робот опублікує


def precheck_approved(
    conn: sqlite3.Connection, root: Path, dry_run: bool = False
) -> PrecheckResult:
    """QA-002: усі «Схвалені», яких робот не опублікує (новий знімок, правка розділу), — назад
    «На перевірку» з причиною в `problem_text`. Без мережі й ШІ; `dry_run` нічого не пише."""
    returned: list[tuple[int | None, str]] = []
    kept = 0
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE status = ? AND purged_at IS NULL ORDER BY id",
            (Status.APPROVED,),
        )
    ]
    with conn:
        for item_id in ids:
            card = review.load_card(conn, item_id)
            error = precheck(card, root).error if card else None
            if error is None:
                kept += 1
                continue
            returned.append((card.post_id, error))
            if not dry_run:
                conn.execute(
                    "UPDATE items SET status = ?, problem_code = NULL, problem_text = ?, "
                    "updated_at = ? WHERE id = ? AND status = ?",
                    (
                        Status.REVIEW,
                        f"Робот не зможе опублікувати: {error}",
                        now(),
                        item_id,
                        Status.APPROVED,
                    ),
                )
    return PrecheckResult(returned, kept)


def fill(conn: sqlite3.Connection, client: Any, planned: Planned) -> Filled:
    """Надіслати запит, перевірити відповідь, зберегти бланк, вартість і новий статус."""
    try:
        answer, usage = ai.call(client, planned.request)
    except ai.AiError as error:
        with conn:
            log_request(conn, planned.item_id, error.usage, str(error))
            set_status(conn, planned.item_id, str(error))
        return Filled(planned.folder_name, error.usage, error=str(error))
    fields = ai.check_answer(
        answer, [p.file_name for p in planned.photos], planned.post_text, planned.edited
    )
    with conn:
        log_request(conn, planned.item_id, usage, None)
        if set_status(conn, planned.item_id, None, fields):  # Картку вже взяли — бланк не чіпаємо
            save_blank(conn, planned.item_id, fields, answer)
    return Filled(planned.folder_name, usage, yellow=sum(f.yellow for f in fields))


def retry_row(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """Картка `review` з постом — лише таку ШІ може заповнити заново."""
    return conn.execute(
        "SELECT id, folder_name, post_id FROM items WHERE id = ? AND status = ? "
        "AND post_id IS NOT NULL",
        (item_id, Status.REVIEW),
    ).fetchone()


def retry(conn: sqlite3.Connection, client: Any, planned: Planned) -> Filled:
    """«ШІ, спробуй ще раз»: звичайний запит без кешу (D2) для Картки `review`.

    Новий бланк замінює старий; ручні правки (`edits`) лишаються поверх нього (UI-001 D2).
    Помилка — Картка лишається на перевірці зі старим бланком.
    """
    try:
        answer, usage = ai.call(client, planned.request)
    except ai.AiError as error:
        with conn:
            log_request(conn, planned.item_id, error.usage, str(error))
        return Filled(planned.folder_name, error.usage, error=str(error))
    fields = ai.check_answer(
        answer, [p.file_name for p in planned.photos], planned.post_text, planned.edited
    )
    with conn:
        log_request(conn, planned.item_id, usage, None)
        cursor = conn.execute(
            "UPDATE items SET updated_at = ? WHERE id = ? AND status = ?",
            (now(), planned.item_id, Status.REVIEW),
        )
        if cursor.rowcount == 1:  # Картку тим часом схвалили чи відхилили — бланк не чіпаємо
            save_blank(conn, planned.item_id, fields, answer)
    return Filled(planned.folder_name, usage, yellow=sum(f.yellow for f in fields))
