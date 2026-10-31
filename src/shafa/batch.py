"""ШІ пакетом (Batch API: усе вдвічі дешевше) з кешем інструкції на годину (D1).

Черга — Картки `new` з постом, старшим за 7 днів (рішення 11.4: що продасться в Telegram, за те ШІ
не платимо), які не чекають результату іншого пакета (D3). Старі (`borh-…`, CORE-005) — без чекання,
одразу за чергою `queue_pos` (CORE-007, рішення Власника 17.10): 7 днів — лише для нових. Дата поста — місцевий час без поясу, як
в Експорті; порівнюється з місцевим часом ноутбука (D4, точність до доби).

Гроші (D5): до відправки — оцінка найгіршого випадку (кеш не спрацював) + уже витрачене сьогодні за
фактом + оцінка ще не забраних пакетів ≤ ліміт доби. Карток у пакеті — не більше ліміту.

Результати: бланк → `review`; помилка запиту → `problem` `ai_error`; `expired`/`canceled` → Картка
знову в черзі, без оплати; результати, не забрані за 29 днів, → `problem` `ai_error` (D6).

Пакет створюється без повторів SDK: повтор після обриву міг би створити другий, теж платний пакет.
Відповідь на створення не прийшла → пакет «невідомий» (Картки не відправляються вдруге); при зборі
його шукаємо серед пакетів API за `custom_id`, а не знайдений за добу вважаємо нествореним.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anthropic

from shafa import ai, config, events, fill, rules, variants
from shafa.db import Status, now
from shafa.folders import OLD_PREFIX, is_old
from shafa.robot import publish

WAIT_DAYS = 7
BATCH_LIMIT = 50
DAILY_USD = 3
AHEAD_DAYS = 3  # на скільки днів публікації наперед ШІ заповнює старі Картки
WAIT_SETTING = "SHAFA_AI_WAIT_DAYS"
BATCH_SETTING = "SHAFA_AI_BATCH_LIMIT"
MONEY_SETTING = "SHAFA_AI_DAILY_USD"

RESULTS_KEPT = timedelta(days=29)  # стільки API зберігає результати пакета
BATCH_MAX_AGE = timedelta(hours=25)  # пакет обробляється не довше 24 год, далі — `expired`
ADOPT_WINDOW = timedelta(minutes=10)  # годинники ноутбука й API можуть трохи розходитись

LOST_TEXT = "Результати ШІ не забрано за 29 днів"
_CUSTOM_ID = re.compile(r"b(\d+)-i(\d+)")


class SetupError(Exception):
    """Неправильне налаштування. Текст — для людини."""


@dataclass(frozen=True)
class Limits:
    wait_days: int = WAIT_DAYS
    batch_limit: int = BATCH_LIMIT
    daily_micro_usd: int = DAILY_USD * 1_000_000
    old_per_day: int = (
        10  # «Старих на день» (робот): ШІ готує старі не більше ніж на AHEAD_DAYS днів
    )


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    raw = config.setting(name)
    if raw is None:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise SetupError(f"{name} — ціле число від {low} до {high}")
    return int(raw)


def limits() -> Limits:
    """Налаштування: днів очікування, Карток у пакеті, доларів на добу."""
    money = DAILY_USD * 1_000_000
    raw = config.setting(MONEY_SETTING)
    if raw is not None:
        try:
            value = Decimal(raw.replace(",", "."))
        except InvalidOperation:
            value = Decimal(-1)
        if not value.is_finite() or not 0 < value <= 100:
            raise SetupError(f"{MONEY_SETTING} — сума в доларах від 0 до 100, наприклад 3")
        money = int(value * 1_000_000)
    try:
        old_per_day = publish.group_limits().old
    except publish.SetupError as error:
        raise SetupError(str(error)) from None
    return Limits(
        _int_setting(WAIT_SETTING, WAIT_DAYS, 0, 60),
        _int_setting(BATCH_SETTING, BATCH_LIMIT, 1, 500),
        money,
        old_per_day,
    )


def custom_id(batch_id: int, item_id: int) -> str:
    """`custom_id` запиту: лише `[a-zA-Z0-9_-]`, до 64 символів (docs: batch-processing)."""
    return f"b{batch_id}-i{item_id}"


# --- Черга ---


def post_is_old(date: str, today: datetime, wait_days: int) -> bool:
    """Пост старший за `wait_days` днів. Дата Експорту — місцевий час без поясу (D4)."""
    try:
        posted = datetime.fromisoformat(date)
    except ValueError:
        return False
    if posted.tzinfo is not None:  # дата з поясом (наприклад, від бота) — у місцевий час
        posted = posted.astimezone(today.tzinfo).replace(tzinfo=None)
    return posted <= today.replace(tzinfo=None) - timedelta(days=wait_days)


@dataclass(frozen=True)
class Queue:
    ready: list[sqlite3.Row]  # можна відправляти, найстаріші пости першими
    young: int  # новий пост ще молодший за `wait_days` (старі не чекають)
    pending: int  # чекають результату пакета


def queue(conn: sqlite3.Connection, today: datetime, wait_days: int) -> Queue:
    rows = conn.execute(
        f"""
        SELECT items.id, items.folder_name, items.post_id, items.refill_at, posts.date
        FROM items JOIN posts USING (post_id)
        WHERE items.status = ? AND items.id NOT IN ({fill.IN_BATCH})
        ORDER BY items.refill_at IS NULL, items.folder_name LIKE '{OLD_PREFIX}%',
                 items.queue_pos IS NULL, items.queue_pos,
                 posts.date, items.id
        """,
        (Status.NEW,),
    ).fetchall()
    ready = [
        r
        for r in rows
        if r["refill_at"]  # перезаповнення: її Картка вже пройшла чекання (CORE-008)
        or r["folder_name"].startswith(OLD_PREFIX)
        or post_is_old(r["date"], today, wait_days)
    ]
    ready = variants.one_per_post(conn, ready)  # сестри-розміри — один запит на пост (CORE-014)
    pending = conn.execute(
        "SELECT count(DISTINCT item_id) FROM ai_batch_items WHERE state = 'pending'"
    ).fetchone()[0]
    return Queue(ready, len(rows) - len(ready), pending)


def old_ahead(conn: sqlite3.Connection) -> int:
    """Старі Картки з готовим бланком, ще не опубліковані (чекають перевірки чи публікації)."""
    return conn.execute(
        "SELECT count(*) FROM items WHERE folder_name LIKE ? AND status IN (?, ?) "
        "AND purged_at IS NULL",
        (f"{OLD_PREFIX}%", Status.REVIEW, Status.APPROVED),
    ).fetchone()[0]


def old_in_flight(conn: sqlite3.Connection) -> int:
    """Старі Картки, що чекають результату пакета: бланк буде, тож рахуються наперед."""
    return conn.execute(
        "SELECT count(DISTINCT i.id) FROM items i JOIN ai_batch_items b ON b.item_id = i.id "
        "WHERE b.state = 'pending' AND i.folder_name LIKE ?",
        (f"{OLD_PREFIX}%",),
    ).fetchone()[0]


def old_room(conn: sqlite3.Connection, lim: Limits) -> int:
    """Скільки старих Карток ШІ ще може заповнити наперед (CORE-006): `AHEAD_DAYS` днів роботи."""
    return AHEAD_DAYS * lim.old_per_day - old_ahead(conn) - old_in_flight(conn)


def held_back(conn: sqlite3.Connection, ready: list[sqlite3.Row], lim: Limits) -> int:
    """Старі готові Картки, які `prepare` не візьме, поки не звільниться місце «наперед»."""
    old = sum(1 for r in ready if is_old(r["folder_name"]) and not r["refill_at"])
    return max(0, old - max(old_room(conn, lim), 0))


def refill_estimate(
    conn: sqlite3.Connection, root: Path, item_ids: list[int] | None = None
) -> tuple[int, int, int]:
    """«Перезаповнити»: скільки Карток піде і скільки це коштує (мікродолари, ціна пакета).

    Третє — скільки Карток не вийде підготувати (фото не відкривається). Нічого не надсилає.
    """
    rows = [r for r in fill.refill_candidates(conn) if item_ids is None or r["id"] in set(item_ids)]
    rows = variants.one_per_post(conn, rows)
    count = cost = skipped = 0
    for row in rows:
        planned = fill.plan(conn, root, row, batch=True)
        if isinstance(planned, fill.Skipped):
            skipped += 1
            continue
        count += 1
        cost += planned.estimate.cost_micro_usd
    return count, cost, skipped


# --- Гроші ---


def _local_day(stamp: str, today: datetime):
    try:
        return datetime.fromisoformat(stamp).astimezone(today.tzinfo).date()
    except ValueError:
        return None


def spent_today(conn: sqlite3.Connection, today: datetime) -> int:
    """Мікродолари: фактична вартість запитів за місцеву добу `today` + оцінка ще не забраних
    пакетів (вони вже відправлені, заплатимо за них потім)."""
    since = (today - timedelta(days=2)).astimezone(UTC).isoformat(timespec="seconds")
    day = today.date()
    actual = sum(
        cost
        for stamp, cost in conn.execute(
            "SELECT created_at, cost_micro_usd FROM ai_requests WHERE created_at >= ?", (since,)
        )
        if _local_day(stamp, today) == day
    )
    waiting = conn.execute(
        "SELECT coalesce(sum(estimate_micro_usd), 0) FROM ai_batch_items WHERE state = 'pending'"
    ).fetchone()[0]
    return actual + waiting


def budget_left(conn: sqlite3.Connection, today: datetime, lim: Limits) -> int:
    return lim.daily_micro_usd - spent_today(conn, today)


# --- Відправка ---


@dataclass
class Plan:
    """Що піде в пакет (ще нічого не надіслано)."""

    planned: list[fill.Planned] = field(default_factory=list)
    skipped: list[fill.Skipped] = field(default_factory=list)
    left: int = 0  # готових Карток лишилось у черзі (ліміт пакета чи грошей)
    held: int = 0  # з них старих, що чекають місця «наперед» (`AHEAD_DAYS`), а не наступного пакета
    over_budget: bool = False  # зупинились через ліміт грошей
    young: int = 0
    pending: int = 0

    @property
    def estimate(self) -> int:
        return sum(p.estimate.cost_micro_usd for p in self.planned)


def prepare(
    conn: sqlite3.Connection,
    root: Path,
    today: datetime,
    lim: Limits,
    limit: int | None = None,
) -> Plan:
    """Зібрати запити пакета в межах лімітів Карток і грошей. Нічого не надсилає."""
    q = queue(conn, today, lim.wait_days)
    cap = min(lim.batch_limit, limit or lim.batch_limit)
    budget = budget_left(conn, today, lim)
    plan = Plan(young=q.young, pending=q.pending)
    # старі — лише наперед (CORE-006): бланки на ≤ AHEAD_DAYS днів публікації, щоб не платити за всі
    room = old_room(conn, lim)
    for row in q.ready:
        if len(plan.planned) >= cap:
            break
        if is_old(row["folder_name"]) and not row["refill_at"]:
            if room <= 0:
                continue
            room -= 1
        result = fill.plan(conn, root, row, batch=True)
        if isinstance(result, fill.Skipped):
            plan.skipped.append(result)
            continue
        if result.estimate.cost_micro_usd > budget:
            plan.over_budget = True
            break
        budget -= result.estimate.cost_micro_usd
        plan.planned.append(result)
    plan.left = len(q.ready) - len(plan.planned) - len(plan.skipped)
    plan.held = held_back(conn, q.ready, lim)
    return plan


@dataclass
class Sent:
    plan: Plan
    batch_id: int | None = None
    error: str | None = None  # пакет точно не створено
    uncertain: bool = False  # відповіді не було: пакет міг створитися

    @property
    def count(self) -> int:
        return len(self.plan.planned) if self.batch_id and not self.error else 0


def _definitely_not_created(error: Exception) -> bool:
    """API відповів помилкою 4xx — пакета немає. 5xx чи обрив — невідомо."""
    return isinstance(error, anthropic.APIStatusError) and error.status_code < 500


def send(
    conn: sqlite3.Connection,
    client: Any,
    root: Path,
    today: datetime,
    lim: Limits,
    limit: int | None = None,
) -> Sent:
    """Відправити пакет. Картки записуються в пакет ДО запиту — вдруге не підуть (D3)."""
    plan = prepare(conn, root, today, lim, limit)
    with conn:  # фото не відкривається — у «Проблеми», щоб не стояла в черзі щоразу
        for skipped in plan.skipped:
            conn.execute(
                "UPDATE items SET status = ?, problem_code = 'ai_error', problem_text = ?, "
                "updated_at = ?, refill_at = NULL WHERE folder_name = ? AND status = ?",
                (Status.PROBLEM, skipped.text, now(), skipped.folder_name, Status.NEW),
            )
    if not plan.planned:
        if plan.over_budget:  # гучно: нові Картки стоять, поки не вичерпається доба
            events.emit(
                "ai.stop",
                f"ШІ не взявся за нові Картки: ліміт витрат на добу ({dollars(lim.daily_micro_usd)}) "
                "вичерпано — решта завтра",
            )
        return Sent(plan)
    with conn:
        batch_id = conn.execute(
            "INSERT INTO ai_batches (status, created_at) VALUES ('sending', ?)", (now(),)
        ).lastrowid
        conn.executemany(
            "INSERT INTO ai_batch_items VALUES (?, ?, ?, 'pending')",
            [(batch_id, p.item_id, p.estimate.cost_micro_usd) for p in plan.planned],
        )
    requests = [
        {"custom_id": custom_id(batch_id, p.item_id), "params": p.request} for p in plan.planned
    ]
    try:
        created = client.with_options(max_retries=0).messages.batches.create(requests=requests)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
        text = ai.error_text(error)
        with conn:
            if _definitely_not_created(error):
                _fail(conn, batch_id, text)
                events.emit("ai.stop", f"ШІ не взяв Картки: {text}. Вони лишились у черзі")
                return Sent(plan, batch_id, error=text)
            conn.execute(
                "UPDATE ai_batches SET status = 'uncertain', note = ? WHERE id = ?",
                (text, batch_id),
            )
        events.emit(
            "ai.stop",
            f"ШІ не відповів на пакет ({text}): перевірю його пізніше, вдруге не відправляю",
        )
        return Sent(plan, batch_id, uncertain=True)
    with conn:
        conn.execute(
            "UPDATE ai_batches SET anthropic_id = ?, status = 'in_progress' WHERE id = ?",
            (created.id, batch_id),
        )
    if len(plan.planned) > 1:  # одну Картку (пост з бота) не оголошуємо: лише результат
        events.emit(
            "ai.start",
            f"ШІ почав аналіз: {events.cards(len(plan.planned))} (пакет, ≈ {cents(plan.estimate)}, "
            "результат зазвичай до години)",
        )
    return Sent(plan, batch_id)


def _fail(conn: sqlite3.Connection, batch_id: int, note: str) -> None:
    """Пакета немає — його Картки знову в черзі."""
    conn.execute(
        "UPDATE ai_batch_items SET state = 'returned' WHERE batch_id = ? AND state = 'pending'",
        (batch_id,),
    )
    conn.execute(
        "UPDATE ai_batches SET status = 'failed', note = ?, ended_at = ? WHERE id = ?",
        (note, now(), batch_id),
    )


# --- Збір результатів ---


@dataclass
class Collected:
    filled: int = 0
    errors: int = 0
    returned: int = 0
    lost: int = 0
    waiting: int = 0  # пакетів ще в роботі
    note: str | None = None  # не вдалося спитати API
    tally: fill.Tally = field(default_factory=fill.Tally)  # для журналу подій (BOT-012)


def _folder(conn: sqlite3.Connection, item_id: int) -> str:
    row = conn.execute("SELECT folder_name FROM items WHERE id = ?", (item_id,)).fetchone()
    return row[0] if row else str(item_id)


def _announce(conn: sqlite3.Connection, report: Collected) -> None:
    """BOT-012: пакет(и) оброблено — підсумок в журнал подій (одна Картка — один короткий рядок)."""
    tally = report.tally
    if tally.total:
        events.emit("ai.done", fill.tally_text(conn, tally))
    elif tally.returned:
        events.emit(
            "ai.done", f"ШІ: пакет не обробив {events.cards(tally.returned)} — вони знову в черзі"
        )


def _card_inputs(conn: sqlite3.Connection, item_id: int) -> tuple[list[str], str, str, str]:
    row = conn.execute(
        "SELECT items.folder_name, posts.text FROM items LEFT JOIN posts USING (post_id) "
        "WHERE items.id = ?",
        (item_id,),
    ).fetchone()
    return (
        fill.photo_names(conn, item_id),
        row["folder_name"] if row else "",
        (row["text"] if row else None) or "",
        rules.edit_text(fill.edits_of(conn, item_id)),
    )


def _errored_text(result: Any) -> str:
    kind = getattr(getattr(getattr(result, "error", None), "error", None), "type", None)
    return f"Помилка Claude API в пакеті ({kind})" if kind else "Помилка Claude API в пакеті"


def apply_result(conn: sqlite3.Connection, batch_id: int, line: Any, report: Collected) -> None:
    """Один рядок результатів пакета → бланк, проблема або повернення в чергу. Повторний збір
    того самого рядка нічого не міняє (стан уже не `pending`)."""
    match = _CUSTOM_ID.fullmatch(line.custom_id or "")
    if match is None or int(match[1]) != batch_id:
        return
    item_id = int(match[2])
    with conn:
        row = conn.execute(
            "SELECT state FROM ai_batch_items WHERE batch_id = ? AND item_id = ?",
            (batch_id, item_id),
        ).fetchone()
        if row is None or row[0] != "pending":
            return
        result = line.result
        if result.type in ("expired", "canceled"):  # не оброблено й не оплачено
            state = "returned"
            report.returned += 1
            report.tally.returned += 1
        elif result.type == "succeeded":
            try:
                answer, usage = ai.parse(result.message, batch=True)
            except ai.AiError as error:
                fill.log_request(conn, item_id, error.usage, str(error), batch_id)
                fill.set_status(conn, item_id, str(error))
                state = "error"
                report.errors += 1
                report.tally.card(_folder(conn, item_id), problem=True)
            else:
                names, folder, text, edited = _card_inputs(conn, item_id)
                fields = variants.check_answer(answer, names, folder, text, edited)
                fill.log_request(conn, item_id, usage, None, batch_id)
                if fill.set_status(
                    conn, item_id, None, fields
                ):  # Картку тим часом змінили — не чіпаємо
                    fill.save_blank(conn, item_id, fields, answer)
                    variants.spread(conn, item_id, answer)  # сестрам — та сама відповідь
                state = "done"
                report.filled += 1
                report.tally.card(
                    folder, yellow=any(f.yellow for f in fields),
                    problem=fill.no_section(fields), cost=usage.cost_micro_usd,
                )  # fmt: skip
        else:  # errored: не оплачується
            text = _errored_text(result)
            fill.log_request(conn, item_id, None, text, batch_id)
            fill.set_status(conn, item_id, text)
            state = "error"
            report.errors += 1
            report.tally.card(_folder(conn, item_id), problem=True)
        conn.execute(
            "UPDATE ai_batch_items SET state = ? WHERE batch_id = ? AND item_id = ?",
            (state, batch_id, item_id),
        )


def _finish(conn: sqlite3.Connection, batch_id: int, report: Collected) -> None:
    """Пакет оброблено: Картки без результату — знову в черзі."""
    with conn:
        cursor = conn.execute(
            "UPDATE ai_batch_items SET state = 'returned' WHERE batch_id = ? AND state = 'pending'",
            (batch_id,),
        )
        report.returned += cursor.rowcount
        report.tally.returned += cursor.rowcount
        conn.execute(
            "UPDATE ai_batches SET status = 'ended', ended_at = ? WHERE id = ?", (now(), batch_id)
        )


def _lose(conn: sqlite3.Connection, batch_id: int, report: Collected) -> None:
    """Результати вже видалено з API (29 днів) — Картки в «Проблеми» (D6)."""
    with conn:
        items = [
            r[0]
            for r in conn.execute(
                "SELECT item_id FROM ai_batch_items WHERE batch_id = ? AND state = 'pending'",
                (batch_id,),
            )
        ]
        for item_id in items:
            fill.set_status(conn, item_id, LOST_TEXT)
        conn.execute(
            "UPDATE ai_batch_items SET state = 'error' WHERE batch_id = ? AND state = 'pending'",
            (batch_id,),
        )
        conn.execute(
            "UPDATE ai_batches SET status = 'ended', note = ?, ended_at = ? WHERE id = ?",
            (LOST_TEXT, now(), batch_id),
        )
    report.lost += len(items)
    for item_id in items:
        report.tally.card(_folder(conn, item_id), problem=True)


def _adopt(conn: sqlite3.Connection, client: Any, today: datetime, report: Collected) -> None:
    """Пакети без відповіді на створення: знайти серед пакетів API за `custom_id` першого
    результату; не знайдений за добу — не створювався, його Картки знову в черзі."""
    rows = conn.execute(
        "SELECT id, created_at FROM ai_batches WHERE status IN ('sending', 'uncertain')"
    ).fetchall()
    if not rows:
        return
    unknown = {r["id"] for r in rows}
    oldest = min(datetime.fromisoformat(r["created_at"]) for r in rows) - ADOPT_WINDOW
    known = {
        r[0]
        for r in conn.execute("SELECT anthropic_id FROM ai_batches WHERE anthropic_id NOT NULL")
    }
    for info in client.messages.batches.list(limit=100):  # найновіші першими
        if info.created_at < oldest:
            break
        if info.id in known:
            continue
        if info.processing_status != "ended":
            report.waiting += 1
            continue
        first = next(iter(client.messages.batches.results(info.id)), None)
        match = _CUSTOM_ID.fullmatch(first.custom_id if first else "")
        if match and int(match[1]) in unknown:
            with conn:
                conn.execute(
                    "UPDATE ai_batches SET anthropic_id = ?, status = 'in_progress' WHERE id = ?",
                    (info.id, int(match[1])),
                )
            unknown.discard(int(match[1]))
    with conn:
        for row in rows:
            if (
                row["id"] in unknown
                and today - datetime.fromisoformat(row["created_at"]) > BATCH_MAX_AGE
            ):
                _fail(conn, row["id"], "Пакет не створено")


def collect(conn: sqlite3.Connection, client: Any, today: datetime) -> Collected:
    """Забрати результати готових пакетів. Помилка зв'язку — спробуємо наступного разу."""
    report = Collected()
    try:
        _adopt(conn, client, today, report)
        batches = conn.execute(
            "SELECT id, anthropic_id, created_at FROM ai_batches WHERE status = 'in_progress' "
            "ORDER BY id"
        ).fetchall()
        for batch in batches:
            if today - datetime.fromisoformat(batch["created_at"]) > RESULTS_KEPT:
                _lose(conn, batch["id"], report)
                continue
            info = client.messages.batches.retrieve(batch["anthropic_id"])
            if info.processing_status != "ended":
                report.waiting += 1
                continue
            for line in client.messages.batches.results(batch["anthropic_id"]):
                apply_result(conn, batch["id"], line, report)
            _finish(conn, batch["id"], report)
    except anthropic.AnthropicError as error:
        report.note = ai.error_text(error)
    _announce(conn, report)
    return report


def open_batches(conn: sqlite3.Connection) -> int:
    """Скільки пакетів ще чекають збору (зокрема невідомих)."""
    return conn.execute(
        "SELECT count(*) FROM ai_batches WHERE status IN ('sending', 'uncertain', 'in_progress')"
    ).fetchone()[0]


# --- Що показати на панелі (стан береться з бази, а не з пам'яті програми) ---


@dataclass(frozen=True)
class Active:
    """Пакети, що ще чекають збору."""

    cards: int  # Карток, що ШІ зараз заповнює
    since: str  # коли відправлено найдавніший (UTC, ISO)
    estimate: int  # оцінка вартості, мікродолари (реально — не більше)


def active(conn: sqlite3.Connection) -> Active | None:
    row = conn.execute(
        """
        SELECT count(DISTINCT i.item_id), coalesce(sum(i.estimate_micro_usd), 0), min(b.created_at)
        FROM ai_batches b LEFT JOIN ai_batch_items i ON i.batch_id = b.id AND i.state = 'pending'
        WHERE b.status IN ('sending', 'uncertain', 'in_progress')
        """
    ).fetchone()
    return Active(row[0], row[2], row[1]) if row[2] else None


@dataclass(frozen=True)
class Finished:
    """Підсумок останнього забраного пакета."""

    at: str  # UTC, ISO
    ok: int  # Карток, що пішли в «На перевірку»
    problems: int  # Карток, що пішли в «Проблеми»
    returned: int  # не оброблено — знову в черзі


def last_finished(conn: sqlite3.Connection) -> Finished | None:
    batch = conn.execute(
        "SELECT id, ended_at FROM ai_batches b WHERE status = 'ended' AND ended_at IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM ai_batch_items WHERE batch_id = b.id AND state != 'pending') "
        "ORDER BY ended_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if batch is None:
        return None
    ok = problems = returned = 0
    for state, status in conn.execute(
        "SELECT b.state, i.status FROM ai_batch_items b JOIN items i ON i.id = b.item_id "
        "WHERE b.batch_id = ?",
        (batch["id"],),
    ):
        if state == "returned":
            returned += 1
        elif state == "error" or status == Status.PROBLEM:
            problems += 1
        else:
            ok += 1
    return Finished(batch["ended_at"], ok, problems, returned)


# --- Тексти для людини ---


def cents(micro_usd: int) -> str:
    return f"{micro_usd / 10_000:.2f}".replace(".", ",") + " ¢"


def dollars(micro_usd: int) -> str:
    return f"{micro_usd / 1_000_000:.2f}".replace(".", ",") + " $"


def approx_usd(micro_usd: int) -> str:
    """Для панелі: «≈ 1,36 $» (центи Вікторії нічого не кажуть)."""
    return f"≈ {micro_usd / 1_000_000:.2f}".replace(".", ",") + " $"


def days_word(count: int) -> str:
    tail = count % 100
    if tail // 10 != 1 and tail % 10 == 1:
        return f"{count} день"
    if tail // 10 != 1 and tail % 10 in (2, 3, 4):
        return f"{count} дні"
    return f"{count} днів"


HELD_TEXT = "{n} чекають своєї черги: ШІ готує Картки лише на {days} роботи робота наперед"
UNCERTAIN_TEXT = "ШІ не відповів вчасно — перевірю сам пізніше, нічого робити не треба"


def held_text(held: int) -> str:
    return HELD_TEXT.format(n=held, days=days_word(AHEAD_DAYS))


def sent_text(sent: Sent, lim: Limits, *, panel: bool = False) -> str:
    """Підсумок відправки. `panel` — для екрана: що пішло й коли, там уже видно з бази."""
    plan = sent.plan
    if sent.error:
        return f"Пакет не відправлено: {sent.error}. Спробуйте пізніше"
    if sent.uncertain:
        return UNCERTAIN_TEXT
    parts = []
    if sent.count and not panel:
        parts.append(
            f"Відправлено на ШІ {sent.count} товарів, результати зазвичай до години "
            f"({approx_usd(plan.estimate)} або менше)"
        )
    elif not sent.count and not plan.over_budget:
        parts.append("Немає нових товарів для ШІ")
    if plan.skipped:
        parts.append(f"у «Проблеми» (фото не відкривається): {len(plan.skipped)}")
    if plan.over_budget:
        parts.append(f"ліміт витрат на ШІ за добу ({dollars(lim.daily_micro_usd)}) — решта завтра")
    else:
        if plan.left - plan.held > 0:
            parts.append(f"ще {plan.left - plan.held} — наступним пакетом")
        if plan.held:
            parts.append(held_text(plan.held))
    return "; ".join(parts)


def collected_text(report: Collected) -> str | None:
    parts = []
    if report.filled:
        parts.append(f"ШІ заповнив бланків: {report.filled}")
    if report.errors or report.lost:
        parts.append(f"у «Проблеми»: {report.errors + report.lost}")
    if report.returned:
        parts.append(f"повернуто в чергу: {report.returned}")
    if report.note:
        parts.append(f"результати не забрано: {report.note}")
    return "; ".join(parts) or None


def collect_problem_text(report: Collected) -> str | None:
    """Для панелі: лише те, що не склалося (підсумок успіху панель бере з бази)."""
    if report.note:
        return f"Не вдалося перевірити, чи готово ({report.note}) — спробую ще раз за кілька хвилин"
    if report.returned:
        return f"ШІ не встиг обробити {report.returned} Карток — вони знову в черзі"
    return None
