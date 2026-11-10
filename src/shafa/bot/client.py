"""Клієнт на компі (OPS-004): бот живе на сервері, комп лише забирає його чергу і робить решту.

- `SHAFA_BOT_SERVER` (хост для `ssh`, напр. `ubuntu@1.2.3.4`) є → програма **ніколи** не запускає
  бота і не опитує Telegram (`enabled`; перевіряють `run.poll`, `web.App.start_bot`, `app.quit_all`).
- Обмін — системний `ssh` з окремим ключем (`SHAFA_BOT_SERVER_KEY` — шлях, типово
  `<дані>/bot-server-key`; відбиток сервера — `<дані>/bot-server-known_hosts`): «give N» → zip
  черги після N → застосувати по `seq` → «ack N». Обрив будь-де — повтор без шкоди: кожен запис
  ідемпотентний, `server_sync.last_seq` росте лише після застосування, тека — через тимчасову назву.
- Звіт, тривоги (з кнопками), сайт і `presence` — фоновий потік `Client` лише відправкою
  (`SendOnly` не вміє `getUpdates`); команди Власника з черги (`owner_cmds`) виконуються тут.
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from shafa import changes, config, db, events, review, site
from shafa.bot import outbox
from shafa.bot.telegram import Telegram
from shafa.db import now
from shafa.folders import PHOTO_SUFFIXES

SERVER = "SHAFA_BOT_SERVER"
KEY = "SHAFA_BOT_SERVER_KEY"
KEY_NAME = "bot-server-key"
HOSTS_NAME = "bot-server-known_hosts"
SERVER_BOT_NAME = "bot-server.json"  # пульс бота на сервері з останнього обміну — для панелі
LOCK_NAME = "sync.lock"
LOG_NAME = "bot-sync.log"  # журнал обміну в теці даних
EVERY = timedelta(minutes=10)  # обмін: при старті й далі так часто
TICK = 30  # секунд між звітом/тривогами/командами
LOST = timedelta(hours=1)  # без вдалого обміну довше — жовте на панелі й у самоперевірці
SERVER_BOT_STALE = timedelta(minutes=15)  # пульс бота на сервері старший — бот там не відповідає
TIMEOUT = 300  # секунд на один виклик ssh (zip до 64 МБ)
ROUNDS = 20  # віддач за один обмін (по 50 записів)
TEMP = ".приймаю-"  # тека пишеться під прихованою назвою (`folders` пропускає «.»)


class SyncError(Exception):
    """Обмін не вдався. Текст — для людини й журналу, без секретів."""


def server() -> str | None:
    return config.setting(SERVER)


def enabled() -> bool:
    """Бот на сервері: комп не опитує Telegram і не запускає бота — ніколи."""
    return server() is not None


# --- Транспорт: системний ssh ---


@dataclass
class Ssh:
    host: str
    key: Path
    known_hosts: Path
    program: list[str] = field(default_factory=lambda: [shutil.which("ssh") or "ssh"])
    timeout: float = TIMEOUT

    def args(self, command: str) -> list[str]:
        return [
            *self.program,
            "-i", str(self.key),
            "-o", "BatchMode=yes",  # без паролів і питань — вікна немає
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=15",
            "-T", self.host, command,
        ]  # fmt: skip

    def run(self, command: str) -> bytes:
        options: dict[str, Any] = {}
        if os.name == "nt":  # pragma: no cover — перевіряється в windows-tests
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            done = subprocess.run(
                self.args(command), capture_output=True, stdin=subprocess.DEVNULL,
                timeout=self.timeout, check=False, **options,
            )  # fmt: skip
        except FileNotFoundError:
            raise SyncError("на комп'ютері немає програми ssh") from None
        except subprocess.TimeoutExpired:
            raise SyncError(f"сервер не відповів за {int(self.timeout)} с") from None
        if done.returncode != 0:
            lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
            why = lines[-1][:200] if lines else "без пояснення"
            raise SyncError(f"ssh: код {done.returncode}: {why}")
        return done.stdout

    def give(self, after: int) -> bytes:
        return self.run(f"give {after}")

    def ack(self, upto: int) -> None:
        answer = self.run(f"ack {upto}").decode("utf-8", "replace").strip()
        if answer != f"ok {upto}":
            raise SyncError(f"сервер не підтвердив {upto}: {answer[:100]!r}")


def transport(data_dir: Path | None = None) -> Ssh:
    host = server()
    if host is None:
        raise SyncError(f"Немає {SERVER}")
    data_dir = data_dir or config.data_dir()
    raw = config.setting(KEY)
    key = Path(raw).expanduser() if raw else data_dir / KEY_NAME
    return Ssh(host, key, data_dir / HOSTS_NAME)


# --- Застосування черги ---


def last_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT last_seq FROM server_sync WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def _set_last(conn: sqlite3.Connection, seq: int) -> None:
    """Лише всередині транзакції того, хто кличе."""
    conn.execute(
        "INSERT INTO server_sync (id, last_seq) VALUES (1, ?) "
        "ON CONFLICT (id) DO UPDATE SET last_seq = max(server_sync.last_seq, excluded.last_seq)",
        (seq,),
    )


def _status(conn: sqlite3.Connection, error: str | None) -> None:
    stamp = now() if error is None else None
    with conn:
        conn.execute(
            "INSERT INTO server_sync (id, last_seq, ok_at, error) VALUES (1, 0, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET error = excluded.error, "
            "ok_at = coalesce(excluded.ok_at, server_sync.ok_at)",
            (stamp, error),
        )


@dataclass
class Applied:
    last: int  # останній застосований `seq` (= попередній, якщо нового не було)
    posts: list[int] = field(default_factory=list)
    edits: list[int] = field(default_factory=list)
    commands: int = 0
    more: bool = False
    bot: dict | None = None  # пульс бота на сервері


def _local_date(unix: int) -> str:
    return datetime.fromtimestamp(unix, UTC).astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _photos(zf: zipfile.ZipFile, seq: int, files: Any) -> dict[str, bytes]:
    """Фото запису з zip, перевірені за sha256. Будь-яка невідповідність — нічого не пишемо."""
    if not isinstance(files, list):
        raise SyncError(f"запис {seq}: список фото пошкоджено")
    out: dict[str, bytes] = {}
    for item in files:
        name = item.get("name") if isinstance(item, dict) else None
        if (
            not isinstance(name, str) or not name or name.startswith(".") or "/" in name
            or "\\" in name or Path(name).suffix.lower() not in PHOTO_SUFFIXES or name in out
        ):  # fmt: skip
            raise SyncError(f"запис {seq}: неприпустима назва фото {name!r}")
        try:
            data = zf.read(f"{outbox.PHOTOS}/{seq}/{name}")
        except KeyError:
            raise SyncError(f"запис {seq}: у zip немає фото {name}") from None
        if _sha(data) != item.get("sha256"):
            raise SyncError(f"запис {seq}: фото {name} пошкоджено (sha256)")
        out[name] = data
    return out


def _same(folder: Path, photos: dict[str, bytes]) -> bool:
    """Тека вже містить рівно ці фото (повтор після обриву)."""
    found = {p.name: p for p in outbox.photo_files(folder)}
    if set(found) != set(photos):
        return False
    return all(_sha(found[n].read_bytes()) == _sha(d) for n, d in photos.items())


def _names(post_id: int) -> Any:
    yield str(post_id)
    yield f"{post_id}-бот"
    n = 2
    while True:
        yield f"{post_id}-бот-{n}"
        n += 1


def place_folder(root: Path, post_id: int, seq: int, photos: dict[str, bytes]) -> Path:
    """Фото поста → `<Головна>/<номер>`; зайнято чужими фото — `<номер>-бот`, `…-бот-2` (як бот).
    Та сама тека з тими самими фото — повтор, нічого не пишемо. Пишемо в приховану тимчасову теку і
    лише тоді перейменовуємо: обрив посередині не лишає напівтеки під справжньою назвою."""
    for name in _names(post_id):
        path = root / name
        if path.is_symlink():
            continue
        if path.exists():
            if path.is_dir() and _same(path, photos):
                return path
            continue
        temp = root / f"{TEMP}{seq}-{name}"
        if temp.is_dir() and not temp.is_symlink():
            shutil.rmtree(temp)  # наш власний залишок після обриву
        temp.mkdir(parents=True)
        for photo, data in photos.items():
            (temp / photo).write_bytes(data)
        os.replace(temp, path)
        return path
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class _Ctx:
    conn: sqlite3.Connection
    root: Path
    zf: zipfile.ZipFile
    data_dir: Path
    same_channel: bool
    log: Callable[[str], None]


def _post(ctx: _Ctx, seq: int, rec: dict, out: Applied) -> None:
    post_id, at, data = rec.get("post_id"), rec.get("at"), rec.get("data") or {}
    text = data.get("text")
    if not isinstance(post_id, int) or not isinstance(at, int) or not isinstance(text, str):
        raise SyncError(f"запис {seq}: пост без номера, часу чи тексту")
    photos = _photos(ctx.zf, seq, rec.get("files", []))
    folder = place_folder(ctx.root, post_id, seq, photos) if photos else None
    with ctx.conn:
        ctx.conn.execute(
            "INSERT INTO posts (post_id, date, text, source, added_at) "
            "VALUES (?, ?, ?, 'bot', ?) ON CONFLICT (post_id) DO NOTHING",
            (post_id, _local_date(at), text, now()),
        )
    if folder is None:
        ctx.log(f"Пост {post_id} з сервера — без фото (папки немає)")
    else:
        posted_at = datetime.fromtimestamp(at, UTC).astimezone().isoformat(timespec="seconds")
        try:
            if site.enqueue(ctx.conn, post_id, folder.name, posted_at, text):
                ctx.log(f"Пост {post_id} — у черзі на сайт")
        except Exception as error:  # noqa: BLE001 — як у бота: сайт не ламає пост
            ctx.log(f"Пост {post_id} не став у чергу сайту: {type(error).__name__}")
        if folder.name != str(post_id):
            ctx.log(f"Пост {post_id}: тека «{post_id}» уже була — фото в «{folder.name}»")
    with ctx.conn:
        _set_last(ctx.conn, seq)
    out.posts.append(post_id)


def _edit(ctx: _Ctx, seq: int, rec: dict, out: Applied) -> None:
    """Те саме, що бот без сервера робить на `edited_channel_post`: сайт і Картки (CORE-015)."""
    post_id, text = rec.get("post_id"), (rec.get("data") or {}).get("text")
    if not isinstance(post_id, int) or not isinstance(text, str):
        raise SyncError(f"запис {seq}: правка без номера чи тексту")
    try:
        if site.edited(ctx.conn, post_id, text):
            ctx.log(f"Пост {post_id} змінено в каналі — на сайт піде новий текст")
        change = changes.text_changed(ctx.conn, post_id, text, allow_old=ctx.same_channel)
        for number, what in change.published:
            ctx.log(f"Пост {number} змінено після публікації на Шафі: {what}")
            changes.record_published(ctx.data_dir, number, what)
    except Exception as error:  # noqa: BLE001 — як у бота: одна правка чергу не зупиняє
        ctx.log(f"Пост {post_id}: зміну тексту не застосовано ({type(error).__name__})")
    with ctx.conn:
        _set_last(ctx.conn, seq)
    out.edits.append(post_id)


def _gone(ctx: _Ctx, seq: int, rec: dict, out: Applied) -> None:
    post_id = rec.get("post_id")
    if isinstance(post_id, int) and (n := review.archive_gone(ctx.conn, post_id)):
        events.emit(
            "post.gone", f"{post_id} видалено з каналу — {events.cards(n)} прибрано в Архів"
        )
    with ctx.conn:
        _set_last(ctx.conn, seq)


def _known(ctx: _Ctx, seq: int, rec: dict, out: Applied) -> None:
    with ctx.conn:  # пост уже є і на компі — робити нічого
        _set_last(ctx.conn, seq)


def _owner_cmd(ctx: _Ctx, seq: int, rec: dict, out: Applied) -> None:
    data = rec.get("data") or {}
    cmd, at = data.get("cmd"), rec.get("at")
    if not isinstance(cmd, str) or not isinstance(at, int):
        raise SyncError(f"запис {seq}: команда без назви чи часу")
    with ctx.conn:  # команда й номер — разом: повтор не дублює, обрив не губить
        ctx.conn.execute(
            "INSERT OR IGNORE INTO owner_cmds (seq, cmd, data, at, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (seq, cmd, json.dumps(data, ensure_ascii=False), at, now()),
        )
        _set_last(ctx.conn, seq)
    out.commands += 1


APPLY = {"post": _post, "edit": _edit, "gone": _gone, "known": _known, "owner_cmd": _owner_cmd}


def apply(
    conn: sqlite3.Connection,
    root: Path,
    blob: bytes,
    *,
    data_dir: Path,
    same_channel: bool,
    log: Callable[[str], None] = print,
) -> Applied:
    """Zip з `outbox.give` → у базу й Головну, по зростанню `seq`; уже застосоване — пропуск.
    Помилка запису — стоп на ньому (наступні не застосовуються, номер не росте)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        index = json.loads(zf.read(outbox.INDEX).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise SyncError("сервер віддав пошкоджений архів") from None
    if not isinstance(index, dict) or index.get("format") != outbox.FORMAT:
        raise SyncError("формат черги сервера інший — оновіть програму")
    records = index.get("records")
    if not isinstance(records, list) or not all(
        isinstance(r, dict) and isinstance(r.get("seq"), int) for r in records
    ):
        raise SyncError("сервер віддав пошкоджений список записів")
    ctx = _Ctx(conn, root, zf, data_dir, same_channel, log)
    out = Applied(last_seq(conn), more=bool(index.get("more")), bot=index.get("bot"))
    for rec in sorted(records, key=lambda r: r["seq"]):
        seq = rec["seq"]
        if seq <= out.last:
            continue
        step = APPLY.get(rec.get("kind"))  # type: ignore[arg-type]
        if step is None:
            raise SyncError(f"запис {seq}: невідомий вид {rec.get('kind')!r} — оновіть програму")
        step(ctx, seq, rec, out)
        out.last = seq
    return out


# --- Обмін ---


def _save_bot(data_dir: Path, bot: Any) -> None:
    if not isinstance(bot, dict):
        return
    try:
        state = {"heartbeat_at": bot.get("heartbeat_at"), "error": bot.get("error"), "at": now()}
        (data_dir / SERVER_BOT_NAME).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def server_bot(data_dir: Path) -> dict | None:
    try:
        data = json.loads((data_dir / SERVER_BOT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def exchange(
    conn: sqlite3.Connection,
    root: Path,
    ssh: Ssh,
    *,
    data_dir: Path,
    same_channel: bool,
    log: Callable[[str], None] = print,
) -> Applied:
    """Один обмін: забрати все нове (кілька віддач, якщо `more`), застосувати, підтвердити.
    Помилка — у `server_sync.error` і `SyncError` далі; застосоване лишається."""
    start = last_seq(conn)
    total = Applied(start)
    row = conn.execute("SELECT error FROM server_sync WHERE id = 1").fetchone()
    unsure = bool(row and row[0])  # минулий обмін упав — підтвердження могло не дійти
    try:
        for _ in range(ROUNDS):
            after = last_seq(conn)
            applied = apply(
                conn, root, ssh.give(after), data_dir=data_dir, same_channel=same_channel, log=log
            )
            _save_bot(data_dir, applied.bot)
            total.posts += applied.posts
            total.edits += applied.edits
            total.commands += applied.commands
            total.last, total.more, total.bot = applied.last, applied.more, applied.bot
            if applied.last > after or (unsure and applied.last > 0):
                ssh.ack(applied.last)
                unsure = False
            if not applied.more or applied.last == after:
                break
    except SyncError as error:
        _status(conn, str(error))
        raise
    _status(conn, None)
    if total.last > start:
        log(
            f"Обмін із сервером: записів до {total.last} — постів {len(total.posts)}, "
            f"правок {len(total.edits)}, команд {total.commands}"
        )
    return total


@dataclass
class Link:
    """Стан зв'язку для панелі й самоперевірки."""

    ok_at: datetime | None
    error: str | None

    def age(self, moment: datetime) -> timedelta | None:
        return None if self.ok_at is None else moment - self.ok_at

    def lost(self, moment: datetime) -> bool:
        age = self.age(moment)
        return age is None or age > LOST


def link(conn: sqlite3.Connection) -> Link:
    row = conn.execute("SELECT ok_at, error FROM server_sync WHERE id = 1").fetchone()
    if row is None:
        return Link(None, None)
    try:
        ok_at = datetime.fromisoformat(row["ok_at"]) if row["ok_at"] else None
    except ValueError:
        ok_at = None
    return Link(ok_at, row["error"])


def ago(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 1:
        return "щойно"
    if minutes < 120:
        return f"{minutes} хв тому"
    hours = minutes // 60
    return f"{hours} год тому" if hours < 48 else f"{hours // 24} дн тому"


def server_bot_stale(data_dir: Path, moment: datetime) -> bool:
    """Обмін іде, а сам бот на сервері давно не писав пульс."""
    beat = (server_bot(data_dir) or {}).get("heartbeat_at")
    try:
        return beat is not None and moment - datetime.fromisoformat(beat) > SERVER_BOT_STALE
    except (TypeError, ValueError):
        return False


# --- Відправка без опитування: звіт, тривоги, команди Власника ---


class SendOnly(Telegram):
    """Клієнт Telegram на компі: надсилати можна, опитувати — ніколи (два опитувачі = 409)."""

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        raise RuntimeError("Бот на сервері: комп не опитує Telegram (OPS-004)")


def sender(conn: sqlite3.Connection, root: Path, tg: Telegram, log: Callable[[str], None]) -> Any:
    """`Bot` лише для відправки: його звіт, тривоги й кнопки — без `poll`."""
    from shafa.bot import run
    from shafa.bot.core import Bot

    class Sender(Bot):
        def _answer(self, callback_id: str, text: str) -> None:
            pass  # на кнопку вже відповів сервер («Прийнято…»); старий id Telegram не прийме

    return Sender(conn, tg, run.settings(root), log=log)


def _button_query(data: dict) -> dict:
    chat = data.get("chat_id")
    return {
        "id": "",
        "data": data.get("data"),
        "from": {"id": chat},
        "message": {
            "chat": {"id": chat, "type": "private"},
            "message_id": data.get("message_id"),
            "text": data.get("text") or "",
            "reply_markup": {"inline_keyboard": data.get("keyboard") or []},
        },
    }


def run_commands(conn: sqlite3.Connection, bot: Any, log: Callable[[str], None] = print) -> int:
    """`/zvit`, `/detali` і кнопки тривог, які Власник надіслав серверу. Кожна — один раз;
    виконана позначається `done_at`. Кнопка сама перевіряє стан Картки («Уже вирішено»)."""
    from shafa.bot import actions
    from shafa.bot.core import DETAIL_COMMAND, REPORT_COMMAND

    owner = bot.settings.owner_chat
    done = 0
    for row in conn.execute(
        "SELECT * FROM owner_cmds WHERE done_at IS NULL ORDER BY seq"
    ).fetchall():
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = {}
        chat, cmd = data.get("chat_id"), row["cmd"]
        result = "виконано"
        try:
            if owner is None or chat != owner:
                result = "не від Власника — пропущено"
            elif cmd == REPORT_COMMAND.lstrip("/"):
                bot._report_now(chat)
            elif cmd == DETAIL_COMMAND.lstrip("/"):
                bot._detail(chat)
            elif cmd == "button":
                actions.handle(bot, _button_query(data))
            else:
                result = f"невідома команда {cmd!r}"
        except Exception as error:  # noqa: BLE001 — одна команда не зупиняє інші
            result = f"помилка: {type(error).__name__}"
            log(f"Команда Власника {row['seq']}: {type(error).__name__}: {str(error)[:200]}")
        with conn:
            conn.execute(
                "UPDATE owner_cmds SET done_at = ?, result = ? WHERE seq = ?",
                (now(), result, row["seq"]),
            )
        done += 1
    return done


# --- Фоновий потік у програмі ---


class Client:
    """Потік у `app`/`ui`: обмін при старті й кожні `EVERY`; щопівхвилини — команди Власника,
    звіт і тривоги; сайт — своїм потоком, як у бота. Замок `sync.lock` — один на комп."""

    def __init__(
        self,
        db_path: Path,
        root: Path,
        *,
        ssh: Ssh | None = None,
        telegram: Callable[[str], Telegram] = SendOnly,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        log: Callable[[str], None] = print,
        start_site: bool = True,
    ) -> None:
        self.db_path, self.root, self.ssh, self.telegram = db_path, root, ssh, telegram
        self.clock, self.log, self.start_site = clock, log, start_site

    def run(self, stop: threading.Event) -> None:
        from shafa.bot import run as bot_run
        from shafa.robot import lock

        data_dir = config.data_dir()
        try:
            held = lock.acquire("sync", data_dir / LOCK_NAME)
        except lock.Busy:
            self.log("Обмін із сервером уже йде в іншій програмі")
            return
        conn = db.connect(self.db_path)
        tg = bot = site_stop = None
        try:
            token = config.setting(bot_run.TOKEN)
            if token is not None:
                tg = self.telegram(token)
                try:
                    bot = sender(conn, self.root, tg, self.log)
                    same = bot.settings.export_channel_id == bot.settings.channel_id
                    if self.start_site:
                        site_stop = bot_run.start_site(self.db_path, self.root, token, bot.settings)
                except bot_run.SetupError as error:
                    self.log(f"Бот на сервері: звіт і тривоги вимкнено — {error}")
                    bot, same = None, False
            else:
                self.log(f"Немає {bot_run.TOKEN}: звіт, тривоги й сайт з компа не надсилаються")
                same = False
            ssh = self.ssh or transport(data_dir)
            next_sync = self.clock()
            while not stop.is_set():
                moment = self.clock()
                if moment >= next_sync:
                    next_sync = moment + EVERY
                    try:
                        exchange(
                            conn, self.root, ssh, data_dir=data_dir, same_channel=same, log=self.log
                        )
                    except SyncError as error:
                        self.log(f"Обмін із сервером: {error}")
                    except Exception as error:  # noqa: BLE001 — потік не вмирає
                        _status(conn, f"{type(error).__name__}")
                        self.log(f"Обмін із сервером: {type(error).__name__}: {str(error)[:200]}")
                if bot is not None:
                    self._send(conn, bot, moment)
                stop.wait(TICK)
        finally:
            if site_stop is not None:
                site_stop.set()
            conn.close()
            if tg is not None:
                tg.close()
            held.release()

    def _send(self, conn: sqlite3.Connection, bot: Any, moment: datetime) -> None:
        try:
            run_commands(conn, bot, self.log)
            bot._daily_report(moment)
            bot._alerts(moment)
        except Exception as error:  # noqa: BLE001
            self.log(f"Звіт/тривоги: {type(error).__name__}: {str(error)[:200]}")


def start(db_path: Path, root: Path, **kw: Any) -> threading.Event | None:
    """Потік клієнта, якщо бот на сервері; повертає подію «зупинись»."""
    if not enabled():
        return None
    stop = threading.Event()
    client = Client(db_path, root, **kw)
    threading.Thread(target=client.run, args=(stop,), daemon=True, name="bot-server-sync").start()
    return stop
