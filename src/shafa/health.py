"""Самоперевірка при запуску (UI-003): «Все гаразд» або «що не так і що зробити» простими словами.

Десять перевірок у фіксованому порядку (база, Головна папка, диск, ключ ШІ, бюджет ШІ, бот, сайт,
вхід робота, довідники, пробний прогін). Усе, що ходить у мережу чи на диск, — через `Deps`:
тести без мережі. Жодна перевірка не кидає виняток назовні, мережева триває ≤ 10 с, а ні
браузера, ні живої Шафи тут немає (вхід робота — з профілю й журналу сесій). У тексти й у
`health-last.json` секрети не потрапляють: перед записом з них вирізаються значення ключів.
"""

import contextlib
import json
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shafa import batch, config, places, site
from shafa.robot import browser, lock, rehearse

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
LAST_NAME = "health-last.json"
NET_LIMIT = 10.0  # секунд на кожну мережеву перевірку
GB = 1024**3
DISK_WARN, DISK_FAIL = 2 * GB, GB // 2
CATALOG_DAYS = 30
# Опитування Telegram — до 25 с, але «Опублікувати всі» (кілька фото й пауза 3 с на чернетку)
# тримає цикл бота довше: 10 хв без пульсу — справді не відповідає.
BOT_STALE = 600
BOT_START = 60  # секунд після запуску, поки бот ще не встиг записати перший пульс
ASK_OWNER = "Напишіть Власнику"


@dataclass(frozen=True)
class Check:
    """Результат однієї перевірки. `text` і `fix` — для людини, без секретів."""

    name: str
    status: str
    text: str
    fix: str = ""


@dataclass
class Deps:
    """Усе зовнішнє. `ai_models`, `bot_rights`, `site_get` кидають виняток, якщо щось не так."""

    ai_models: Callable[[str], None]
    bot_rights: Callable[[str, str | None], str]
    site_get: Callable[[str], None]
    now: Callable[[], datetime] = lambda: datetime.now().astimezone()
    disk_usage: Callable[[Path], Any] = shutil.disk_usage
    catalog_path: Path | None = None


def _ai_models(key: str) -> None:
    import anthropic

    anthropic.Anthropic(api_key=key, max_retries=0, timeout=NET_LIMIT).models.list(limit=1)


def _bot_rights(token: str, channel: str | None) -> str:
    from shafa.bot import run
    from shafa.bot.telegram import Telegram

    tg = Telegram(token)
    try:
        return run.check_rights(tg, run.channel_id(channel))[0]
    finally:
        tg.close()


def _site_get(key: str) -> None:
    import httpx2

    client = httpx2.Client(timeout=NET_LIMIT)
    try:
        site.Site(key, client).subcategories(NET_LIMIT)  # лише читання
    finally:
        client.close()


def real_deps() -> Deps:
    return Deps(_ai_models, _bot_rights, _site_get)


def _limited(fn: Callable[..., Any], *args: Any) -> Any:
    """Виклик не довше `NET_LIMIT`: завис — TimeoutError (потік-демон догорить сам)."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn(*args)
        except Exception as error:  # noqa: BLE001 — віддаємо тому, хто викликав
            box["error"] = error

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(NET_LIMIT)
    if worker.is_alive():
        raise TimeoutError
    if "error" in box:
        raise box["error"]
    return box.get("value")


@dataclass
class _Ctx:
    data_dir: Path
    root: Path
    deps: Deps
    conn: sqlite3.Connection | None = None
    db_error: str = ""
    secrets: list[str] = field(default_factory=list)


# --- Перевірки ---

DB, FOLDER, DISK, AI_KEY, AI_BUDGET, BOT, SITE, LOGIN, CATALOG, REHEARSAL = (
    "База",
    "Головна папка",
    "Місце на диску",
    "Ключ ШІ",
    "Бюджет ШІ на сьогодні",
    "Бот",
    "Сайт",
    "Вхід на Шафу",
    "Довідники Шафи",
    "Пробний прогін",
)


def _database(ctx: _Ctx) -> Check:
    path = ctx.data_dir / "shafa.sqlite"
    if not path.is_file():
        ctx.db_error = "бази ще немає"
        return Check(DB, FAIL, "Бази даних не знайдено", "Запустіть програму з ярлика ще раз")
    try:
        # лише читання: перевірка нічого не створює й не міняє в базі
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error:
        ctx.db_error = "база не відкривається"
        return Check(DB, FAIL, "База даних не відкривається", f"{ASK_OWNER}: потрібне відновлення")
    if verdict != "ok":
        conn.close()
        ctx.db_error = "база пошкоджена"
        return Check(DB, FAIL, "База даних пошкоджена", f"{ASK_OWNER}: потрібне відновлення")
    ctx.conn = conn
    return Check(DB, OK, "База відкривається, пошкоджень немає")


def _folder(ctx: _Ctx) -> Check:
    root = ctx.root
    if not root.is_dir():
        return Check(
            FOLDER,
            FAIL,
            "Головної папки з фото немає на місці",
            "Поверніть папку або вкажіть нову в «Налаштуваннях»",
        )
    try:  # справжній запис: права Windows через os.access не видно
        with tempfile.TemporaryFile(dir=root):
            pass
    except OSError:
        return Check(
            FOLDER,
            FAIL,
            "У Головну папку не можна записувати",
            "Закрийте програми, що тримають папку, або виберіть іншу в «Налаштуваннях»",
        )
    if places.in_onedrive(root):
        return Check(
            FOLDER,
            WARN,
            "Головна папка лежить у OneDrive",
            "Перенесіть її з OneDrive: він може забирати фото в хмару",
        )
    return Check(FOLDER, OK, "Головна папка на місці, запис працює")


def _disk(ctx: _Ctx) -> Check:
    free = ctx.deps.disk_usage(ctx.data_dir).free
    text = f"Вільно {free / GB:.1f} ГБ"
    if free < DISK_FAIL:
        return Check(DISK, FAIL, text, "Звільніть місце на диску, інакше програма зупиниться")
    if free < DISK_WARN:
        return Check(DISK, WARN, text, "Місця стає мало: видаліть непотрібне з диска")
    return Check(DISK, OK, text)


def _ai_key(ctx: _Ctx) -> Check:
    import anthropic

    from shafa import ai

    key = config.setting(config.API_KEY)
    if key is None:
        return Check(AI_KEY, FAIL, "Немає ключа ШІ", f"{ASK_OWNER}: потрібно вписати ключ ШІ")
    ctx.secrets.append(key)
    try:
        _limited(ctx.deps.ai_models, key)
    except anthropic.AnthropicError as error:
        return Check(AI_KEY, FAIL, f"Ключ ШІ не працює: {ai.error_text(error)}", ASK_OWNER)
    except TimeoutError:
        return Check(AI_KEY, FAIL, "ШІ не відповів за 10 секунд", "Перевірте інтернет і повторіть")
    return Check(AI_KEY, OK, "Ключ ШІ працює")


def _ai_budget(ctx: _Ctx) -> Check:
    if ctx.conn is None:
        return Check(AI_BUDGET, SKIP, f"Не перевірено: {ctx.db_error}")
    try:
        limits = batch.limits()
    except batch.SetupError as error:
        return Check(AI_BUDGET, WARN, f"Налаштування ліміту ШІ неправильне: {error}", ASK_OWNER)
    left = batch.budget_left(ctx.conn, ctx.deps.now(), limits)
    if left <= 0:
        return Check(AI_BUDGET, WARN, "Ліміт ШІ на сьогодні вичерпано", "ШІ продовжить завтра")
    return Check(AI_BUDGET, OK, f"На сьогодні лишилось ${left / 1_000_000:.2f}")


def _age(ctx: _Ctx, stamp: str | None) -> float | None:
    """Скільки секунд минуло від позначки часу; немає чи зіпсована — None."""
    try:
        return (ctx.deps.now() - datetime.fromisoformat(stamp)).total_seconds()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _bot_beat(ctx: _Ctx, name: str) -> Check:
    """Права вже перевірено; чи живий сам процес бота — за замком і пульсом у базі."""
    from shafa.bot import run

    row = None
    if ctx.conn is not None:
        row = ctx.conn.execute("SELECT heartbeat_at, error FROM bot_status WHERE id = 1").fetchone()
    held = lock.busy(ctx.data_dir / run.LOCK_NAME)
    if held is None:
        return Check(
            BOT, WARN, f"Права є, але бот @{name} не запущений", "Натисніть «Запустити бота»"
        )
    age = _age(ctx, row["heartbeat_at"] if row else None)
    if age is None or age > BOT_STALE:
        started = _age(ctx, held.get("since"))
        if started is not None and started < BOT_START:  # щойно запустився: пульсу ще немає
            return Check(BOT, OK, f"Бот @{name} запускається, права в каналі є")
        return Check(
            BOT,
            WARN,
            f"Бот @{name} запущений, але не відповідає",
            "Закрийте програму («Вийти») і відкрийте знову",
        )
    if row["error"]:
        return Check(BOT, WARN, f"Бот @{name} працює, але: {row['error']}", ASK_OWNER)
    return Check(BOT, OK, f"Бот @{name} працює, права в каналі є")


def _bot(ctx: _Ctx) -> Check:
    from shafa.bot import run

    token = config.setting(run.TOKEN)
    if token is None:
        return Check(BOT, FAIL, "Немає токена бота", f"{ASK_OWNER}: потрібно вписати токен бота")
    ctx.secrets.append(token)
    try:
        name = _limited(ctx.deps.bot_rights, token, config.setting(run.CHANNEL))
    except run.SetupError as error:
        return Check(BOT, FAIL, str(error), "Виправте в Telegram або напишіть Власнику")
    except TimeoutError:
        return Check(BOT, FAIL, "Telegram не відповів за 10 секунд", "Перевірте інтернет")
    return _bot_beat(ctx, name)


def _site(ctx: _Ctx) -> Check:
    if not site.enabled():
        return Check(SITE, SKIP, "Відправка на сайт вимкнена")
    key = site.key()
    if key is None:
        return Check(SITE, FAIL, "Немає ключа сайту", f"{ASK_OWNER}: потрібно вписати ключ сайту")
    ctx.secrets.append(key)
    try:
        _limited(ctx.deps.site_get, key)
    except site.SiteError as error:
        fix = (
            "Ключ сайту не підходить — напишіть Власнику"
            if error.status in (401, 403, 404)
            else "Сайт тимчасово недоступний; перевірте інтернет і повторіть"
        )
        return Check(SITE, FAIL, f"Сайт: {error}", fix)
    except TimeoutError:
        return Check(SITE, FAIL, "Сайт не відповів за 10 секунд", "Перевірте інтернет і повторіть")
    return Check(SITE, OK, "Сайт відповідає, ключ приймає")


def _login(ctx: _Ctx) -> Check:
    profile = ctx.data_dir / browser.profile_dir().name
    if not profile.is_dir() or not any(profile.iterdir()):
        return Check(
            LOGIN, WARN, "Робот ще не входив на Шафу", "Натисніть «Увійти на Шафу» на панелі"
        )
    if ctx.conn is not None:
        last = ctx.conn.execute(
            "SELECT stop_code FROM sessions WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is not None and last["stop_code"] == "login":
            return Check(
                LOGIN,
                FAIL,
                "Робот вийшов із Шафи",
                "Натисніть «Увійти на Шафу», увійдіть як завжди й закрийте вікно",
            )
    return Check(LOGIN, OK, "Профіль робота є, остання сесія не просила входу")


def _catalog(ctx: _Ctx) -> Check:
    from shafa import catalog

    path = ctx.deps.catalog_path or catalog.SNAPSHOT
    try:
        checked = date.fromisoformat(str(json.loads(path.read_text(encoding="utf-8"))["checked"]))
    except (OSError, ValueError, KeyError, TypeError):
        return Check(CATALOG, WARN, "Не вдалося прочитати дату довідників", ASK_OWNER)
    days = (ctx.deps.now().date() - checked).days
    if days > CATALOG_DAYS:
        return Check(
            CATALOG,
            WARN,
            f"Довідники Шафи звірено {days} днів тому",
            f"{ASK_OWNER}: потрібно оновити довідники",
        )
    return Check(CATALOG, OK, f"Довідники Шафи звірено {checked:%d.%m.%Y}")


def _rehearsal(ctx: _Ctx) -> Check:
    last = rehearse.last_summary(ctx.data_dir)
    if last is None:
        return Check(REHEARSAL, SKIP, "Пробних прогонів ще не було")
    problems, stop = last.get("problems") or 0, last.get("stop") or "done"
    if problems or stop != "done":
        return Check(
            REHEARSAL,
            WARN,
            f"Останній пробний прогін: {last.get('summary') or 'є проблеми'}",
            "Подивіться вкладку «Проблеми»",
        )
    return Check(REHEARSAL, OK, f"Останній пробний прогін: {last.get('summary') or 'без проблем'}")


CHECKS: tuple[tuple[str, Callable[[_Ctx], Check]], ...] = (
    (DB, _database),
    (FOLDER, _folder),
    (DISK, _disk),
    (AI_KEY, _ai_key),
    (AI_BUDGET, _ai_budget),
    (BOT, _bot),
    (SITE, _site),
    (LOGIN, _login),
    (CATALOG, _catalog),
    (REHEARSAL, _rehearsal),
)


def _scrub(check: Check, secrets: list[str]) -> Check:
    text, fix = check.text, check.fix
    for secret in secrets:
        text, fix = text.replace(secret, "***"), fix.replace(secret, "***")
    return Check(check.name, check.status, text, fix)


def run_all(data_dir: Path, root: Path, *, deps: Deps | None = None) -> list[Check]:
    """Усі перевірки в порядку екрана; результат — у `<дані>/health-last.json`."""
    deps = deps or real_deps()
    ctx = _Ctx(data_dir, root, deps)
    checks = []
    try:
        for name, check in CHECKS:
            try:
                result = check(ctx)
            except Exception:  # noqa: BLE001 — перевірка не валить решту й екран
                result = Check(name, FAIL, "Не вдалося виконати перевірку", ASK_OWNER)
            checks.append(_scrub(result, ctx.secrets))
    finally:
        if ctx.conn is not None:
            ctx.conn.close()
    with contextlib.suppress(Exception):  # дзеркало для людини; не вдалось — не біда
        save(checks, deps.now(), data_dir)
    return checks


def save(checks: list[Check], at: datetime, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LAST_NAME
    temp = folder / (LAST_NAME + ".tmp")
    payload = {"at": at.isoformat(timespec="seconds"), "checks": [asdict(c) for c in checks]}
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return path


def has_failures(checks: list[Check]) -> bool:
    return any(c.status == FAIL for c in checks)


MARKS = {OK: "ok  ", WARN: "УВАГА", FAIL: "ПОМИЛКА", SKIP: "—   "}


def render(checks: list[Check]) -> str:
    """Той самий звіт для консолі: рядок на перевірку, «що зробити» — з нового рядка."""
    lines = []
    for c in checks:
        lines.append(f"[{MARKS[c.status]}] {c.name}: {c.text}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"        що зробити: {c.fix}")
    return "\n".join(lines)
