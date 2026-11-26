"""Тривоги Власнику в Telegram одразу (BOT-010): зупинка робота, нові Проблеми, збій самоперевірки,
бюджет ШІ. Звіт о 21:00 (`report`) лишається як є.

BOT-018: сесії — окремі блоки (`sessions.py`: початок, живе повідомлення, повний підсумок), тривоги
йдуть і Вікторії, і Власнику однаково (`SHAFA_ALERTS_TO`), стан «що надіслано» — окремо на чат.

BOT-012: ще й журнал подій (`events.py`) — усе, що відбувається у фоні (ШІ, робот, довідники).
Гучні події йдуть одразу, тихі — однією збіркою раз на 5 хв (без звуку). `/detali` перемикає
«Детально» ↔ «Лише важливе» (без журналу, як до BOT-012).

Бот сам читає базу й файли теки даних; робот, програма й `health` нічого нового не пишуть. Що вже
надіслано — файл `alerts-last.json` у теці даних. Перший запуск лише запам'ятовує поточний стан і
нічого не шле за минуле. `run` нічого не знає про Telegram: надсилання — функція `send`, яка
каже, чи дійшло; не дійшло — подія лишається до наступного циклу (без дублів).
"""

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from shafa import batch, config, events, hints
from shafa.bot import actions, report, sessions
from shafa.db import Status
from shafa.klubok import queue as klubok_queue
from shafa.klubok import renew as klubok_renew

SETTING = "SHAFA_ALERTS"  # не задано — тривоги є; «off» (чи «ні», «no», «0», «false») — вимкнено
OFF = {"off", "ні", "no", "0", "false"}
TO_SETTING = (
    "SHAFA_ALERTS_TO"  # BOT-018: `both` (типово) — Вікторії й Власнику; `owner` — лише Власнику
)
LAST_NAME = "alerts-last.json"  # стан чату Власника; Вікторії — `alerts-last-seller.json`
EVERY = timedelta(seconds=20)  # як часто бот дивиться в базу (живе повідомлення — не частіше 20 с)
SAME = timedelta(hours=1)  # однакове повідомлення (той самий ключ) — не частіше
HEALTH_REPEAT = timedelta(hours=6)  # той самий FAIL самоперевірки — не частіше
PER_HOUR = 20  # гучних повідомлень на годину, включно з «ще N подій»
QUIET_EVERY = timedelta(minutes=5)  # тихі події збираються в одне повідомлення
QUIET_LINES = 15  # …або коли набралось стільки рядків
QUIET_PER_HOUR = 12  # збірок на годину; понад це — «ще N подій»
QUIET_LINE = 300  # символів на один рядок збірки
QUIET_HEAD = "🕓 Фонові події"
FRESH = timedelta(minutes=10)  # перший запуск: старіше за це в журналі не шлемо
BUDGET_SHARE = 80  # % добового ліміту ШІ
LIST_MAX = 10  # проблем в одному повідомленні
LINE_MAX = actions.LINE_MAX  # символів на одну проблему
START_KINDS = {"robot.start": "shafa", "klubok.start": "klubok"}  # початок блоку сесії
END_KINDS = ("robot.end", "robot.stop", "klubok.end", "klubok.stop")  # дані для підсумку
INFO_KEEP = 20  # сесій, чиї «відкладено» й примітки зберігаємо до підсумку


@dataclass
class Event:
    key: str
    text: str | None  # None — нічого не слати, лише запам'ятати (commit)
    commit: Callable[[dict], None]  # що запам'ятати, коли повідомлення дійшло
    markup: dict | None = None  # кнопки під повідомленням (BOT-011)
    album_of: int | None = None  # Картка, чиї фото (+ схожого оголошення) йдуть перед повідомленням
    quiet: bool = False  # подія журналу без звуку: іде в збірку (BOT-012)
    silent: bool = False  # BOT-018: одразу, але без звуку (початок сесії)
    messages: list[sessions.Message] | None = None  # BOT-018: кілька повідомлень (підсумок сесії)


def enabled() -> bool:
    if SETTING in os.environ:
        raw = os.environ[SETTING]
    else:
        raw = config.read_env_file(config.env_file()).get(SETTING, "")
    return raw.strip().lower() not in OFF


def to_seller() -> bool:
    """BOT-018: чи йдуть тривоги й Вікторії (типово так); `SHAFA_ALERTS_TO=owner` — лише Власнику."""
    if TO_SETTING in os.environ:
        raw = os.environ[TO_SETTING]
    else:
        raw = config.read_env_file(config.env_file()).get(TO_SETTING, "")
    return raw.strip().lower() != "owner"


# --- Стан ---


def _defaults() -> dict:
    """Поля BOT-012; у файлі, записаному раніше, їх ще немає."""
    return {
        "events": None,  # позиція в журналі подій: {"inode", "offset"}
        "detail": True,  # «Детально» (усе) чи «Лише важливе»
        "info": {},  # BOT-018: id сесії → {deferred, notes} з журналу — для підсумку
        "partial": {},  # BOT-018: ключ події → скільки її повідомлень уже дійшло
        "pending": [],  # тихі рядки, що чекають збірки
        "pending_since": "",
        "batches": [],  # коли надіслано збірки за останню годину
        "dropped": 0,  # подій понад ліміт збірок, про які ще не казали
        "notice_at": "",
        "renew_due": "",  # KLB-004: про який прострочений термін «Обновить все» вже сказали
    }


def _file(slot: str = "") -> str:
    return f"alerts-last-{slot}.json" if slot else LAST_NAME


def load(data_dir: Path, slot: str = "") -> dict | None:
    """None — файла немає чи він зіпсований: тоді це «перший запуск». `slot` — чат (BOT-018)."""
    try:
        state = json.loads((data_dir / _file(slot)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or "session_id" not in state:
        return None
    for name, value in _defaults().items():
        state.setdefault(name, value)
    return state


def save(data_dir: Path, state: dict, slot: str = "") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    temp = data_dir / (_file(slot) + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.replace(data_dir / _file(slot))


def forget(data_dir: Path) -> None:
    """Тривоги вимкнено: стан застаріває, тож після ввімкнення — знову «перший запуск»."""
    for slot in ("", "seller"):
        try:
            (data_dir / _file(slot)).unlink(missing_ok=True)
        except OSError:
            pass


def _stamp(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return None


def _problems_now(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["id"]): row["problem_code"] or ""
        for row in conn.execute(
            "SELECT id, problem_code FROM items WHERE status = ? AND only_klubok = 0",
            (Status.PROBLEM,),
        )
    }  # fmt: skip  (KLB-008: Проблеми Карток «лише Клубок» — не тривога Шафи)


def _last_session(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM sessions WHERE ended_at IS NOT NULL"
    ).fetchone()[0]  # BOT-018: Шафа й Клубок — один ряд номерів


def _health_file(data_dir: Path) -> tuple[datetime, list[dict]] | None:
    try:
        data = json.loads((data_dir / report.HEALTH_NAME).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(data["at"]).astimezone()
        checks = [c for c in data["checks"] if isinstance(c, dict)]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return at, checks


def _fails(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c.get("status") == "fail" and c.get("name")]


def _budget_high(conn: sqlite3.Connection, day: date) -> tuple[int, int] | None:
    """(витрачено, ліміт) у мікродоларах, коли витрачено ≥ 80 % добового ліміту."""
    try:
        limit = batch.limits().daily_micro_usd
    except Exception:  # noqa: BLE001 — неправильне налаштування тривог не ламає
        return None
    spent = report._spent(conn, day)
    return (spent, limit) if limit > 0 and spent * 100 >= limit * BUDGET_SHARE else None


def snapshot(conn: sqlite3.Connection, data_dir: Path, moment: datetime) -> dict:
    """Перший запуск: поточний стан як «вже відоме», без жодного повідомлення."""
    state: dict = {
        "session_id": _last_session(conn),
        "problems": _problems_now(conn),
        "health_at": "",
        "health_sent": {},
        "budget_day": "",
        "sent": {},
        "hour": [],
        "live": None,  # BOT-018: {site, session, message_id, text, at} живого повідомлення
        **_defaults(),
    }
    if found := _health_file(data_dir):
        at, checks = found
        state["health_at"] = _stamp(at)
        state["health_sent"] = {str(c["name"]): _stamp(moment) for c in _fails(checks)}
    if _budget_high(conn, moment.astimezone().date()):
        state["budget_day"] = moment.astimezone().date().isoformat()
    return state


# --- Події ---


def _sentence(text: object) -> str:
    return str(text or "").strip().rstrip(".")


def _session_events(
    conn: sqlite3.Connection, state: dict, moment: datetime, owner: bool
) -> list[Event]:
    """BOT-018: кінець сесії Шафи чи Клубку — повний підсумок (гучно); зупинка не з вини черги —
    ще й окреме гучне «⛔ …» перед ним (підсумок тоді без звуку)."""
    out: list[Event] = []
    rows = conn.execute(
        "SELECT * FROM sessions WHERE id > ? AND ended_at IS NOT NULL "
        "AND site IN ('shafa', 'klubok') ORDER BY id",
        (state["session_id"],),
    ).fetchall()
    for row in rows:
        info = state["info"].get(str(row["id"]))
        messages: list[sessions.Message] = []
        if not sessions.is_calm(row):
            keyboard = (
                actions.session_markup(row["id"]) if owner and row["site"] == "shafa" else None
            )
            messages.append(sessions.Message(sessions.stop_text(row, owner), markup=keyboard))
        messages += sessions.summary(conn, row, moment, info, owner)
        out.append(
            Event(f"session:{row['id']}", messages[0].text, _ended(row["id"]), messages=messages)
        )
    return out


def _ended(session_id: int) -> Callable[[dict], None]:
    def commit(state: dict) -> None:
        state["session_id"] = session_id
        state["info"].pop(str(session_id), None)

    return commit


def _set(name: str, value: object) -> Callable[[dict], None]:
    def commit(state: dict) -> None:
        state[name] = value

    return commit


def _problem_events(conn: sqlite3.Connection, state: dict, owner: bool) -> list[Event]:
    seen: dict = state["problems"]
    now = _problems_now(conn)
    fresh = [i for i, code in now.items() if seen.get(i) != code]
    if not fresh:
        if now != seen:  # частина Проблем розв'язана — «новою» вона буде, якщо повернеться
            state["problems"] = now
        return []
    rows = conn.execute(
        "SELECT id, post_id, folder_name, problem_code, problem_text, shafa_id, shafa_url "
        f"FROM items WHERE id IN ({','.join('?' * len(fresh))}) ORDER BY id",
        [int(i) for i in fresh],
    ).fetchall()
    lines = [actions.card_text(row, LINE_MAX) for row in rows[:LIST_MAX]]
    more = len(rows) - len(lines)
    if len(rows) == 1:
        text = "⚠️ " + lines[0]
    else:
        text = f"⚠️ Нові Проблеми ({len(rows)}):\n" + "\n".join("• " + line for line in lines)
        text += f"\n…і ще {more}" if more else ""
    text += actions.advice_text(rows)  # BOT-019: що робити — з того самого довідника, що й екран
    if owner and len(rows) > actions.MULTI_MAX:
        text += f"\nКнопки — для перших {actions.MULTI_MAX}, решту — у програмі."
    album_of = None
    if len(rows) == 1 and rows[0]["problem_code"] == "possible_duplicate":
        album_of = rows[0]["id"]
    event = Event(
        "problems:" + ",".join(fresh),
        text,
        _set("problems", now),
        actions.markup(rows) if owner else None,  # кнопки рішень — лише Власнику (`actions`)
        album_of,
    )
    return [event]


def _health_events(data_dir: Path, state: dict, moment: datetime) -> list[Event]:
    found = _health_file(data_dir)
    if found is None:
        return []
    at, checks = found
    known = _parse(state["health_at"])
    if known is not None and at <= known:
        return []
    sent: dict = state["health_sent"]
    due = []
    for check in _fails(checks):
        last = _parse(sent.get(str(check["name"])))
        if last is None or moment - last >= HEALTH_REPEAT:
            due.append(check)
    if not due:
        state["health_at"] = _stamp(at)
        return []
    lines = []
    for check in due:
        line = f"🔴 {check['name']}: {_sentence(check.get('text'))}."
        if fix := _sentence(check.get("fix")):
            line += f" {fix}."
        lines.append(line)

    def commit(state: dict) -> None:
        state["health_at"] = _stamp(at)
        state["health_sent"].update({str(c["name"]): _stamp(moment) for c in due})

    return [
        Event("health:" + ",".join(sorted(str(c["name"]) for c in due)), "\n".join(lines), commit)
    ]


def _budget_events(conn: sqlite3.Connection, state: dict, moment: datetime) -> list[Event]:
    day = moment.astimezone().date()
    if state["budget_day"] == day.isoformat() or not (high := _budget_high(conn, day)):
        return []
    spent, limit = high
    text = (
        f"💸 ШІ: сьогодні витрачено ${spent / 1_000_000:.2f} із ${limit / 1_000_000:.2f} "
        f"({spent * 100 // limit}%)."
    )
    text += "\n" + hints.advice(hints.EVENTS["ai_budget"])  # BOT-019
    return [Event(f"budget:{day}", text, _set("budget_day", day.isoformat()))]


def _renew_events(
    conn: sqlite3.Connection, data_dir: Path, state: dict, moment: datetime
) -> list[Event]:
    """KLB-004: «Обновить все» на Клубку прострочено > 12 год — один раз на термін."""
    if not klubok_queue.enabled():
        return []
    try:
        found = klubok_renew.status(conn, moment.astimezone(), data_dir)
    except Exception:  # noqa: BLE001 — тривоги не ламаються через Клубок
        return []
    if found.since is None or moment - found.since <= klubok_renew.ALERT_AFTER:
        return []
    key = found.since.isoformat()
    if state.get("renew_due") == key:
        return []
    text = f"🔄 Клубок: {found.line}. Подивіться панель Клубку на екрані."
    return [Event(f"renew:{key}", text, _set("renew_due", key))]


def _position(record: events.Record) -> Callable[[dict], None]:
    return _set("events", {"inode": record.inode, "offset": record.end})


def _stash_info(state: dict, record: events.Record) -> None:
    """Кінець сесії в журналі: що не лишається в базі (відкладені, примітки) — для підсумку."""
    session = record.data.get("session")
    if not session:
        return
    state["info"][str(session)] = {
        "deferred": [n for n in record.data.get("deferred") or [] if isinstance(n, int)],
        "notes": [n for n in record.data.get("notes") or [] if isinstance(n, str)],
    }
    for stale in list(state["info"])[:-INFO_KEEP]:
        del state["info"][stale]


def _journal_events(data_dir: Path, state: dict, moment: datetime) -> list[Event]:
    """BOT-012: нові рядки журналу подій. «Лише важливе» і види з `skip` — лише посуваємо позицію."""
    records, fresh = events.read_new(data_dir, state["events"])
    out = []
    for record in records:
        key = f"journal:{record.inode}:{record.end}"
        commit = _position(record)
        kind = events.info(record.kind)
        if record.kind in END_KINDS:
            _stash_info(state, record)
        old = fresh and (record.at is None or moment - record.at > FRESH)
        if not record.kind or old or kind.skip or not state["detail"]:
            out.append(Event(key, None, commit))
            continue
        if site := START_KINDS.get(record.kind):  # початок сесії — блок із відокремлювачем
            text = sessions.start_text(site, record.data, record.text)
            out.append(Event(key, text, commit, silent=True))
            continue
        line = f"{kind.emoji} {record.text}"
        loud = record.loud or kind.loud
        hint = hints.EVENTS.get(str(record.data.get("hint") or kind.hint))
        if loud and hint and hint.do:  # BOT-019: гучна подія — одразу з порадою «Що робити»
            line += f"\n{hints.advice(hint)}"
        if not loud:
            stamp = record.at.strftime("%H:%M") if record.at else ""
            line = f"{stamp} {line}"
        out.append(Event(key, line, commit, quiet=not loud))
    return out


def collect(
    conn: sqlite3.Connection,
    data_dir: Path,
    state: dict,
    moment: datetime,
    owner: bool = True,
) -> list[Event]:
    """Що нового від останнього разу. Може тихо оновити `state` (розв'язані Проблеми, самоперевірка
    без помилок); усе, про що треба сказати, — подіями."""
    journal = _journal_events(
        data_dir, state, moment
    )  # першим: кінець сесії з нього гасить ✅ нижче
    return [
        *journal,
        *_session_events(conn, state, moment, owner),
        *_problem_events(conn, state, owner),
        *_health_events(data_dir, state, moment),
        *_budget_events(conn, state, moment),
        *_renew_events(conn, data_dir, state, moment),
    ]


# --- Надсилання ---


def _hold(state: dict, line: str, moment: datetime) -> None:
    if not state["pending"]:
        state["pending_since"] = _stamp(moment)
    state["pending"].append(report.clip(line, QUIET_LINE))


def _flush(state: dict, moment: datetime, send: Callable[..., bool]) -> int:
    """Тихі рядки — одним повідомленням без звуку: раз на 5 хв чи коли набралось 15 рядків. Збірок
    не більше 12 на годину, понад це — один раз на годину «ще N подій». Повертає, скільки надіслано."""
    pending: list = state["pending"]
    since = _parse(state["pending_since"]) or moment
    if not pending or (len(pending) < QUIET_LINES and moment - since < QUIET_EVERY):
        return 0
    if len(state["batches"]) >= QUIET_PER_HOUR:
        state["dropped"] += len(pending)
        pending.clear()
        notice = _parse(state["notice_at"])
        if notice and moment - notice < SAME:
            return 0
        if send(f"ℹ️ Ще {state['dropped']} подій — подивись /zvit.", silent=True):
            state["dropped"], state["notice_at"] = 0, _stamp(moment)
            return 1
        return 0
    chunk = pending[:QUIET_LINES]
    text = QUIET_HEAD + "\n\n" + "\n\n".join(chunk)  # BOT-018: шапка й порожній рядок між подіями
    if not send(report.clip(report.scrub(text)), silent=True):
        return 0
    del pending[: len(chunk)]
    state["pending_since"] = _stamp(moment) if pending else ""
    state["batches"].append(_stamp(moment))
    return 1


def toggle(conn: sqlite3.Connection, data_dir: Path, moment: datetime) -> bool:
    """`/detali`: «Детально» ↔ «Лише важливе». Повертає новий стан («Детально» — True). Лише чат
    Власника: Вікторія перемикача не має."""
    state = load(data_dir) or snapshot(conn, data_dir, moment)
    state["detail"] = not state["detail"]
    if not state["detail"]:  # що чекало збірки — вже не потрібне
        state["pending"], state["pending_since"] = [], ""
    save(data_dir, state)
    return state["detail"]


def _deliver(send: Callable[..., bool], message: sessions.Message) -> bool:
    args: list = [message.text]
    if message.markup or message.album_of:
        args += [message.markup, message.album_of]
    kwargs: dict = {}
    if message.silent:
        kwargs["silent"] = True
    if message.entities:
        kwargs["entities"] = message.entities
    return send(*args, **kwargs)


def _send_event(state: dict, event: Event, send: Callable[..., bool], owner: bool) -> bool:
    """Усі повідомлення події по черзі. Обрив посередині — наступного циклу з того ж місця (те, що
    дійшло, вдруге не шлемо)."""
    messages = event.messages
    if messages is None:
        text = report.clip(report.scrub(event.text or ""))
        messages = [
            sessions.Message(
                text, silent=event.silent, markup=event.markup, album_of=event.album_of
            )
        ]
    done = int(state["partial"].get(event.key, 0))
    for number in range(done, len(messages)):
        message = messages[number]
        if not owner:  # кнопки рішень — лише Власнику
            message = sessions.Message(message.text, message.entities, message.silent, None,
                                       message.album_of)  # fmt: skip
        if not _deliver(send, message):
            state["partial"][event.key] = number
            return False
    state["partial"].pop(event.key, None)
    return True


def _live_step(
    conn: sqlite3.Connection, state: dict, moment: datetime, live: sessions.Live | None
) -> None:
    """BOT-018: одне повідомлення про сесію, яка йде зараз; оновлюється без звуку не частіше за
    `LIVE_EVERY`. Сесія закінчилась — воно позначається «підсумок нижче» і забувається."""
    if live is None or not state["detail"]:
        return
    current = state.get("live")
    row = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL AND site IN ('shafa', 'klubok') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if current and (row is None or row["id"] != current["session"]):
        live.edit(current["message_id"], sessions.closed_text(current["session"], current["site"]))
        state["live"] = current = None  # найкраще, що можна: правка не вдалась — не зациклюємось
    if row is None:
        return
    text = sessions.live_text(conn, row, moment)
    if text is None:
        return
    if current is None:
        if (message_id := live.send(text)) is not None:
            state["live"] = {
                "site": row["site"], "session": row["id"], "message_id": message_id,
                "text": text, "at": _stamp(moment),
            }  # fmt: skip
        return
    last = _parse(current["at"])
    if text == current["text"] or (last is not None and moment - last < sessions.LIVE_EVERY):
        return
    match live.edit(current["message_id"], text):
        case "ok":
            current["text"], current["at"] = text, _stamp(moment)
        case "gone":  # повідомлення зникло — наступного циклу надішлемо нове
            state["live"] = None


def run(
    conn: sqlite3.Connection,
    data_dir: Path,
    moment: datetime,
    send: Callable[..., bool],
    *,
    live: sessions.Live | None = None,
    owner: bool = True,
    slot: str = "",
) -> int:
    """Один цикл для одного чату (`slot`: "" — Власник, "seller" — Вікторія; стан кожного свій, щоб
    збій одного не дублював іншому). `send(text[, кнопки, Картка для фото][, silent=, entities=])`
    каже, чи дійшло. `live` — живе повідомлення сесії. Повертає, скільки повідомлень надіслано."""
    state = load(data_dir, slot)
    if state is None:
        save(data_dir, snapshot(conn, data_dir, moment), slot)
        return 0
    before = json.dumps(state, sort_keys=True)
    if "live" not in state:  # стан до BOT-018: минулі сесії Клубку не переповідаємо
        state["live"] = None
        state["session_id"] = _last_session(conn)
    hour_ago = moment - SAME
    state["hour"] = [s for s in state["hour"] if (t := _parse(s)) and t > hour_ago]
    state["batches"] = [s for s in state["batches"] if (t := _parse(s)) and t > hour_ago]
    state["sent"] = {k: s for k, s in state["sent"].items() if (t := _parse(s)) and t > hour_ago}
    sent_count = overflow = 0
    for event in collect(conn, data_dir, state, moment, owner):
        if event.text is None:  # нічого слати: лише запам'ятати
            event.commit(state)
        elif event.quiet:
            _hold(state, event.text, moment)
            event.commit(state)
        elif event.key in state["sent"]:  # те саме щойно було: лише позначаємо опрацьованим
            event.commit(state)
        elif len(state["hour"]) >= PER_HOUR - 1:
            overflow += 1
            event.commit(state)
        else:
            if not _send_event(state, event, send, owner):
                break  # Telegram не відповів: решта — наступним циклом
            event.commit(state)
            state["sent"][event.key] = _stamp(moment)
            state["hour"].append(_stamp(moment))
            sent_count += 1
    if overflow and send(f"ℹ️ Ще {overflow} подій — подивись /zvit."):
        state["hour"].append(_stamp(moment))
        sent_count += 1
    sent_count += _flush(state, moment, send)
    _live_step(conn, state, moment, live)
    if json.dumps(state, sort_keys=True) != before:
        save(data_dir, state, slot)
    return sent_count
