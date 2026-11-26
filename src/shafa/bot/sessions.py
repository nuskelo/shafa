"""BOT-018: сесія робота — окремий блок у чаті (і Вікторії, і Власнику, однаково).

Початок (тихо, з відокремлювачем) → одне «живе» повідомлення, яке оновлюється без звуку →
наприкінці повний підсумок цієї сесії (гучно): розгортаємі блоки «Виставлені», «Проблеми»,
«Продано, в Архів», «Відкладено», «На що глянути». Історії сесій у боті немає — вона на вкладці
«Сесії» програми; бот показує лише поточну чи останню.

Модуль без мережі: тексти й `entities` (без `parse_mode`, зсуви в UTF-16, як у BOT-006) будуються
з бази й файлів теки даних; надсилає `alerts.run` через `Live` і `send`.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from shafa import hints
from shafa.bot import actions, report
from shafa.bot.telegram import TelegramError
from shafa.klubok import queue as klubok_queue
from shafa.review import POST_DELETED_TEXT
from shafa.robot import control, klubok_control, klubok_publish

SEPARATOR = "━━━━━━━━━━━━"
SITES = {"shafa": ("🛍", "Шафа"), "klubok": ("🧶", "Клубок")}
LIVE_EVERY = timedelta(seconds=20)  # живе повідомлення оновлюється не частіше
LIVE_CODES = {
    "shafa": {"running", "pausing", "paused", "stopping"},
    "klubok": {"running", "pausing", "paused", "stopping", "snapshot"},
}
MAX_UTF16 = 3800  # у повідомленні — запас до ліміту Telegram (4096)
LINE_MAX = 300  # символів на один рядок підсумку
SECTION_MAX = 120  # рядків в одному блоці; решта — «ще N у програмі»
NOTE_SKIP = ("Видалено з", "Прибрано фото")  # службові примітки робота — не для людини

Piece = str | tuple[str, str]  # шматок рядка: простий текст чи (текст, посилання)
Line = list[Piece]


@dataclass
class Message:
    text: str
    entities: list[dict] = field(default_factory=list)
    silent: bool = False
    markup: dict | None = None
    album_of: int | None = None


@dataclass
class Section:
    title: str
    lines: list[Line]


def u16(text: str) -> int:
    """Довжина, як її рахує Telegram (UTF-16: емодзі — 2)."""
    return len(text.encode("utf-16-le")) // 2


def _plain(line: Line) -> str:
    return "".join(p if isinstance(p, str) else p[0] for p in line)


class Draft:
    """Текст повідомлення з `entities`: посилання й розгортаємі блоки."""

    def __init__(self) -> None:
        self.text = ""
        self.entities: list[dict] = []

    def add(self, text: str) -> None:
        self.text += text

    def quote(self, lines: list[Line]) -> None:
        start = u16(self.text)
        quote = {"type": "expandable_blockquote", "offset": start, "length": 0}
        self.entities.append(quote)
        for number, line in enumerate(lines):
            if number:
                self.text += "\n"
            for piece in line:
                if isinstance(piece, str):
                    self.text += piece
                    continue
                text, url = piece
                self.entities.append(
                    {"type": "text_link", "offset": u16(self.text), "length": u16(text), "url": url}
                )
                self.text += text
        quote["length"] = u16(self.text) - start


def pack(head: str, sections: list[Section], silent: bool = False) -> list[Message]:
    """Шапка й розгортаємі блоки → повідомлення до `MAX_UTF16`; довгий блок ділиться
    («… (продовження)»)."""
    messages: list[Message] = []
    draft = Draft()
    draft.add(head)

    def flush() -> None:
        nonlocal draft
        messages.append(Message(draft.text, draft.entities, silent))
        draft = Draft()

    for section in sections:
        lines = section.lines[:SECTION_MAX]
        if len(section.lines) > len(lines):
            more = len(section.lines) - len(lines)
            lines.append([f"… і ще {more} — вкладка «Сесії» в програмі"])
        title = section.title
        while lines:
            sep = "\n\n" if draft.text else ""
            cost = u16(draft.text) + len(sep) + u16(title) + 1
            count = 0
            for line in lines:
                size = u16(_plain(line)) + 1
                if cost + size > MAX_UTF16:
                    break
                cost += size
                count += 1
            if count == 0 and draft.text:
                flush()
                continue
            count = max(count, 1)
            draft.add(sep)
            draft.quote([[title], *lines[:count]])
            lines = lines[count:]
            title = f"{section.title.split(' (')[0]} (продовження)"
            if lines:
                flush()
    flush()
    return messages


def _clip(text: str) -> str:
    return report.clip(report.scrub(" ".join(str(text).split())), LINE_MAX)


def _link(text: str, url: str | None, host: str) -> Piece:
    return (text, url) if url and url.startswith(host) else text


# --- Дані сесії ---


def _stamp(value: str | None, now: datetime) -> datetime | None:
    return control._stamp(value, now)


def _window(row: sqlite3.Row, moment: datetime) -> tuple[str, str]:
    return row["started_at"], row["ended_at"] or "9999"


def _sold_shafa(conn: sqlite3.Connection, row: sqlite3.Row, moment: datetime) -> list[sqlite3.Row]:
    """Картки, які пост видалила з каналу (продано) за час сесії — у Архів."""
    start, end = _window(row, moment)
    return conn.execute(
        "SELECT id, post_id, folder_name FROM items WHERE status = 'archived' "
        "AND problem_code = 'post_deleted' AND archived_at >= ? AND archived_at <= ? "
        "AND only_klubok = 0 ORDER BY post_id",
        (start, end),
    ).fetchall()


def _sold_klubok(conn: sqlite3.Connection, row: sqlite3.Row, moment: datetime) -> list[sqlite3.Row]:
    start, end = _window(row, moment)
    return conn.execute(
        "SELECT i.id, i.post_id, i.folder_name FROM klubok_ads a JOIN items i ON i.id = a.item_id "
        "WHERE a.state = 'removed' AND a.removed_at >= ? AND a.removed_at <= ? ORDER BY i.post_id",
        (start, end),
    ).fetchall()


def _problems_shafa(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, post_id, folder_name, problem_code, problem_text, shafa_id, shafa_url "
        "FROM items WHERE session_id = ? AND status = 'problem' ORDER BY id",
        (session_id,),
    ).fetchall()


def _problems_klubok(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.id, i.post_id, i.folder_name, a.problem_code, a.problem_text FROM klubok_ads a "
        "JOIN items i ON i.id = a.item_id WHERE a.session_id = ? AND a.state = 'problem' "
        "ORDER BY i.id",
        (session_id,),
    ).fetchall()


def _published_shafa(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT post_id, folder_name, shafa_url FROM items WHERE session_id = ? "
        "AND status = 'published' ORDER BY published_at, id",
        (session_id,),
    ).fetchall()


def _published_klubok(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.post_id, i.folder_name, a.url FROM klubok_ads a JOIN items i ON i.id = a.item_id "
        "WHERE a.session_id = ? AND a.state IN ('published', 'sold_asked', 'sold', 'removed') "
        "ORDER BY a.published_at, i.id",
        (session_id,),
    ).fetchall()


def counts(conn: sqlite3.Connection, row: sqlite3.Row, moment: datetime) -> tuple[int, int]:
    """(Проблем у цій сесії, продано за її час) — для живого повідомлення."""
    if row["site"] == "klubok":
        problems = len(_problems_klubok(conn, row["id"]))
        return problems, len(_sold_klubok(conn, row, moment))
    return len(_problems_shafa(conn, row["id"])), len(_sold_shafa(conn, row, moment))


def _name(row: sqlite3.Row) -> str:
    return report.scrub(f"Пост {actions.post_name(row)}")


def _problem_line(row: sqlite3.Row, site: str) -> Line:
    if site == "shafa":
        return [_clip("• " + actions.card_text(row, LINE_MAX))]
    why = klubok_queue.REASONS.get(row["problem_code"] or "") or row["problem_text"] or ""
    return [_clip(f"• {_name(row)}" + (f" — {why}" if why else ""))]


def _advice_lines(rows: list[sqlite3.Row], site: str) -> list[Line]:
    """BOT-019: «→ Назва: що робити» — по одному рядку на кожен різний код (не більше 5)."""
    out: list[Line] = []
    seen: set[str] = set()
    for row in rows:
        code = row["problem_code"] or ""
        hint = hints.problem(code, site)
        if code in seen or hint is None or not hint.do:
            continue
        seen.add(code)
        out.append([_clip(f"→ {hint.title}. Що робити: {hints.steps(hint)}")])
        if len(out) == 5:
            break
    return out


def span_text(row: sqlite3.Row, moment: datetime) -> str:
    start, end = _stamp(row["started_at"], moment), _stamp(row["ended_at"], moment)
    if start is None or end is None:
        return ""
    return control.span(max((end - start).total_seconds() / 60, 0))


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


def is_calm(row: sqlite3.Row) -> bool:
    code = row["stop_code"] or ""
    if row["site"] == "klubok":
        return code not in klubok_publish.LOUD
    return code in {"done", "limit", "stop"} or (not code and not (row["stop_reason"] or ""))


def hint_of(row: sqlite3.Row) -> hints.Hint | None:
    """BOT-019: підказка до причини зупинки сесії (той самий довідник, що на екрані)."""
    code, text = row["stop_code"] or "", row["stop_reason"] or ""
    if code in ("error", "crash") and "TargetClosed" in text:
        return None  # зрозуміліший текст — `control.CRASH_CLOSED` (вікно закрили самі)
    return hints.stop(row["site"], code)


def reason(row: sqlite3.Row) -> str:
    code, text = row["stop_code"] or "", row["stop_reason"] or ""
    if hint := hint_of(row):
        return hint.what.strip().rstrip(".")
    ours = klubok_control if row["site"] == "klubok" else control
    return str(ours.reason_of(code, text) or text).strip().rstrip(".")


def advice_of(row: sqlite3.Row) -> str:
    """«Що робити: 1) …» до зупинки; порожньо, коли робити нічого."""
    hint = hint_of(row)
    return hints.advice(hint) if hint else ""


def summary(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    moment: datetime,
    info: dict | None = None,
    owner: bool = False,
) -> list[Message]:
    """Повний підсумок завершеної сесії `row`. `info` — що в базі не лишилось: відкладені пости й
    примітки робота (журнал подій). Помилка — шапка без блоків (підсумок ніколи не блокує бота)."""
    site = row["site"]
    emoji, name = SITES[site]
    calm = is_calm(row)
    info = info or {}
    span = span_text(row, moment)
    head = f"{'✅' if calm else '⛔'} {emoji} {name} · сесія {row['id']} "
    head += "завершена" if calm else "зупинена"
    head += f" · {span}" if span else ""
    problems_n, sold_n = row["problems"] or 0, 0
    sections: list[Section] = []
    try:
        if site == "klubok":
            published = _published_klubok(conn, row["id"])
            problems = _problems_klubok(conn, row["id"])
            sold = _sold_klubok(conn, row, moment)
            host = "https://klubok.com/"
            url_of = "url"
        else:
            published = _published_shafa(conn, row["id"])
            problems = _problems_shafa(conn, row["id"])
            sold = _sold_shafa(conn, row, moment)
            host = "https://shafa.ua/"
            url_of = "shafa_url"
        sold_n = len(sold)
        if published:
            sections.append(
                Section(
                    f"Виставлені ({len(published)})",
                    [["• ", _link(_name(r), r[url_of], host)] for r in published],
                )
            )
        if problems:
            lines = [_problem_line(r, site) for r in problems]
            sections.append(
                Section(f"Проблеми ({len(problems)})", lines + _advice_lines(problems, site))
            )
        if sold:
            sections.append(
                Section(f"Продано, в Архів ({len(sold)})", [[f"• {_name(r)}"] for r in sold])
            )
        if deferred := info.get("deferred"):
            sections.append(
                Section(
                    f"Відкладено — Telegram не підтвердив, що пост є в каналі ({len(deferred)})",
                    [[f"• Пост {n}"] for n in deferred],
                )
            )
        notes = [
            n
            for n in info.get("notes") or []
            if isinstance(n, str) and not n.startswith(NOTE_SKIP) and POST_DELETED_TEXT not in n
        ]
        if notes:
            sections.append(
                Section(f"На що глянути ({len(notes)})", [[_clip("• " + n)] for n in notes])
            )
    except sqlite3.Error:
        sections = []
    facts = f"виставлено {row['published'] or 0} · Проблем {problems_n}"
    if sold_n:
        facts += f" · продано {sold_n}"
    lines = [head, facts]
    if why := reason(row):
        tail = f" (код: {row['stop_code']})" if owner and not calm and row["stop_code"] else ""
        lines.append(f"причина: {_lower(why)}{tail}")
    return pack(report.scrub("\n".join(lines)), sections, silent=not calm)


def stop_text(row: sqlite3.Row, owner: bool) -> str:
    """Гучне «⛔ …зупинився» — окреме повідомлення перед підсумком (підказки — BOT-019)."""
    site = row["site"]
    who = "Робот" if site == "shafa" else "Клубок"
    why = reason(row) or "причина невідома — подивіться екран"
    verb = "Опубліковано" if site == "shafa" else "Виставлено"
    text = f"⛔ {who} зупинився: {why}. {verb} {row['published'] or 0}, "
    text += f"у Проблемах {row['problems'] or 0}."
    if owner and row["stop_code"] and site == "klubok":
        text += f" (код: {row['stop_code']})"
    if advice := advice_of(row):  # BOT-019: що робити — тими самими словами, що й на екрані
        text += f"\n{advice}"
    return text


def start_text(site: str, data: dict[str, Any], fallback: str) -> str:
    """Початок сесії: відокремлювач + рядок «🛍 Шафа · сесія 80 почалась · у черзі 30 …»."""
    emoji, name = SITES[site]
    session = data.get("session")
    if not session:
        return f"{SEPARATOR}\n{emoji} {fallback}"
    line = f"{emoji} {name} · сесія {session} почалась"
    if site == "shafa" and data.get("queue") is not None:
        queue, old = int(data["queue"]), int(data.get("old") or 0)
        line += f" · у черзі {queue} (нових {queue - old}, старих {old})"
        if data.get("left") is not None:
            line += f" · ліміт на сьогодні ще {data['left']}"
    elif site == "klubok" and data.get("limit"):
        line += f" · виставить до {data['limit']}"
    return f"{SEPARATOR}\n{line}"


# --- Живе повідомлення ---


def live_text(conn: sqlite3.Connection, row: sqlite3.Row, moment: datetime) -> str | None:
    """Ті самі слова, що на панелі програми (`control.state` / `klubok_control.state`): прогрес,
    «Зараз: …», етапи «Перечитую ваші оголошення…» і «Telegram просить зачекати…». None — сесія
    не йде (процес помер, ще вмикається): нічого показувати."""
    site = row["site"]
    emoji, name = SITES[site]
    try:
        state = (
            klubok_control.state(conn, now=moment)
            if site == "klubok"
            else control.state(conn, now=moment)
        )
        problems, sold = counts(conn, row, moment)
    except Exception:  # noqa: BLE001 — живе повідомлення не ламає тривог
        return None
    if state.code not in LIVE_CODES[site]:
        return None
    lines = [f"{emoji} {name} · сесія {row['id']} іде"]
    if state.progress:
        lines.append(f"▸ {state.progress}")
    extra = [f"Проблем {problems}"] if problems else []
    if sold:
        extra.append(f"продано {sold}")
    if extra:
        lines.append("▸ " + " · ".join(extra))
    if state.code in ("pausing", "stopping"):
        lines.append(state.text)
    elif state.doing:
        lines.append(state.doing)
    return report.clip(report.scrub("\n".join(lines)))


def closed_text(session_id: int, site: str) -> str:
    emoji, name = SITES.get(site, SITES["shafa"])
    return f"{emoji} {name} · сесія {session_id} закінчилась — підсумок нижче ⬇️"


class Live:
    """Одне повідомлення чату, яке бот оновлює (без звуку). Відповіді: `send` → id чи None;
    `edit` → «ok» / «gone» (повідомлення зникло — слати нове) / «fail» (спробуємо згодом)."""

    def __init__(self, bot: Any, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id

    def send(self, text: str) -> int | None:
        sent = self.bot.say(self.chat_id, text, silent=True)
        return sent.get("message_id") if isinstance(sent, dict) else None

    def edit(self, message_id: int, text: str) -> str:
        try:
            self.bot.tg.edit_text(self.chat_id, message_id, text)
        except TelegramError as error:
            if "not modified" in error.description:
                return "ok"
            if error.code == 400 and (
                "not found" in error.description or "can't be edited" in error.description
            ):
                return "gone"
            self.bot.log(f"Живе повідомлення не оновлено: {error}")
            return "fail"
        return "ok"
