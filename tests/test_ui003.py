"""UI-003: самоперевірка при запуску — десять перевірок, блок на панелі, CLI, жодних секретів."""

import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from shafa import __main__ as cli
from shafa import app as program
from shafa import config, db, health, web
from shafa.bot import run as bot_run
from shafa.health import FAIL, OK, SKIP, WARN, Check, Deps
from shafa.robot import lock
from shafa.site import SiteError
from test_web import PORT, TOKEN, get, html, location, post

GB = 1024**3
SECRET = "sk-test-SECRET"
BOT_TOKEN = "123456:BOT-SECRET-TOKEN"
SITE_KEY = "site-SECRET-key"
# «Зараз» для перевірок — на годину попереду справжнього: замок бота записує справжній час, і
# «щойно запустився» не заважає тестам про застарілий пульс.
NOW = datetime.now().astimezone() + timedelta(hours=1)
NAMES = [
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
]


def ok_deps(catalog, **over) -> Deps:
    values = {
        "ai_models": lambda key: None,
        "bot_rights": lambda token, channel: "shafa_bot",
        "site_get": lambda key: None,
        "now": lambda: NOW,
        "disk_usage": lambda path: SimpleNamespace(free=10 * GB),
        "catalog_path": catalog,
    }
    values.update(over)
    return Deps(**values)


class Setup:
    def __init__(self, data, root, catalog):
        self.data, self.root, self.catalog = data, root, catalog

    def deps(self, **over) -> Deps:
        return ok_deps(self.catalog, **over)

    def run(self, **over) -> dict[str, Check]:
        return {c.name: c for c in health.run_all(self.data, self.root, deps=self.deps(**over))}

    def sql(self, query, *params):
        conn = db.connect(self.data / "shafa.sqlite")
        try:
            with conn:
                conn.execute(query, params)
        finally:
            conn.close()

    def heartbeat(self, seconds_ago, error=None):
        stamp = (NOW - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
        self.sql("DELETE FROM bot_status")
        self.sql(
            "INSERT INTO bot_status (id, started_at, heartbeat_at, error) VALUES (1, ?, ?, ?)",
            stamp,
            stamp,
            error,
        )


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """Усе гаразд: база, папка, ключі, бот (замок узято, пульс свіжий), профіль робота, довідники."""
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    db.connect(data / "shafa.sqlite").close()
    root = tmp_path / "Робочий стіл" / "Шафа"
    root.mkdir(parents=True)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps({"checked": str((NOW - timedelta(days=5)).date())}), encoding="utf-8"
    )
    (data / "robot-profile" / "Default").mkdir(parents=True)
    (data / "robot-profile" / "Default" / "Cookies").write_bytes(b"x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_BOT_CHANNEL_ID", "2394827824")
    setup = Setup(data, root, catalog)
    setup.heartbeat(30)
    held = lock.acquire("bot", data / bot_run.LOCK_NAME)
    yield setup
    held.release()


def by_name(checks):
    return {c.name: c for c in checks}


# --- Загальне ---


def test_everything_fine_in_order_and_saved(ready):
    checks = health.run_all(ready.data, ready.root, deps=ready.deps())
    assert [c.name for c in checks] == NAMES
    statuses = {c.name: c.status for c in checks}
    assert statuses["Сайт"] == SKIP and statuses["Пробний прогін"] == SKIP
    assert [c.status for c in checks if c.name not in ("Сайт", "Пробний прогін")] == [OK] * 8
    saved = json.loads((ready.data / health.LAST_NAME).read_text(encoding="utf-8"))
    assert saved["at"] and [c["name"] for c in saved["checks"]] == NAMES
    assert not health.has_failures(checks)


# --- 1. База ---


def test_database_missing_is_fail_and_not_created(ready):
    (ready.data / "shafa.sqlite").unlink()
    result = ready.run()
    assert result["База"].status == FAIL
    assert result["Бюджет ШІ на сьогодні"].status == SKIP  # без бази — не вгадуємо
    assert not (ready.data / "shafa.sqlite").exists()  # перевірка нічого не створює


def test_database_corrupt_is_fail(ready):
    (ready.data / "shafa.sqlite").write_bytes("це не база даних".encode() * 400)
    result = ready.run()
    assert result["База"].status == FAIL and "не відкривається" in result["База"].text
    assert result["Бюджет ШІ на сьогодні"].status == SKIP


def test_quick_check_failure_is_fail(ready, monkeypatch):
    class Bad:
        def execute(self, *a):
            return SimpleNamespace(fetchone=lambda: ["*** row 1 missing from index"])

        def close(self):
            pass

    monkeypatch.setattr(health.sqlite3, "connect", lambda *a, **k: Bad())
    assert ready.run()["База"].text == "База даних пошкоджена"


# --- 2. Головна папка ---


def test_folder_missing_and_readonly_and_onedrive(ready, tmp_path, monkeypatch):
    ready.root.rmdir()
    assert ready.run()["Головна папка"].status == FAIL
    ready.root.mkdir()

    def denied(*a, **k):
        raise PermissionError

    monkeypatch.setattr(health.tempfile, "TemporaryFile", denied)
    assert "не можна записувати" in ready.run()["Головна папка"].text
    monkeypatch.undo()

    cloud = tmp_path / "OneDrive" / "Шафа"
    cloud.mkdir(parents=True)
    ready.root = cloud
    check = ready.run()["Головна папка"]
    assert check.status == WARN and "OneDrive" in check.text


# --- 3. Диск ---


@pytest.mark.parametrize(
    ("free", "status"),
    [(2 * GB, OK), (2 * GB - 1, WARN), (GB, WARN), (GB // 2, WARN), (GB // 2 - 1, FAIL)],
)
def test_disk_thresholds(ready, free, status):
    deps = {"disk_usage": lambda path: SimpleNamespace(free=free)}
    assert ready.run(**deps)["Місце на диску"].status == status


# --- 4. Ключ ШІ ---


def test_ai_key_missing_broken_timeout_and_ok(ready, monkeypatch):
    request = httpx2.Request("GET", "https://example.test")
    broken = anthropic.APIConnectionError(request=request)

    def refuse(key):
        raise broken

    assert ready.run()["Ключ ШІ"].status == OK
    check = ready.run(ai_models=refuse)["Ключ ШІ"]
    assert check.status == FAIL and "Немає з'єднання" in check.text

    monkeypatch.setattr(health, "NET_LIMIT", 0.05)
    gate = threading.Event()
    check = ready.run(ai_models=lambda key: gate.wait(2))["Ключ ШІ"]
    gate.set()
    assert check.status == FAIL and "не відповів" in check.text

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert ready.run()["Ключ ШІ"].status == FAIL


# --- 5. Бюджет ШІ ---


def test_ai_budget_ok_and_exhausted(ready, monkeypatch):
    check = ready.run()["Бюджет ШІ на сьогодні"]
    assert check.status == OK and "$3.00" in check.text
    monkeypatch.setattr(health.batch, "budget_left", lambda *a: 0)
    check = ready.run()["Бюджет ШІ на сьогодні"]
    assert check.status == WARN and check.fix == "ШІ продовжить завтра"


def test_ai_budget_bad_setting_is_warn(ready, monkeypatch):
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "багато")
    assert ready.run()["Бюджет ШІ на сьогодні"].status == WARN


# --- 6. Бот ---


def test_bot_token_missing_and_rights_error(ready, monkeypatch):
    def no_rights(token, channel):
        raise bot_run.SetupError("Бот не адміністратор каналу з правом публікувати повідомлення")

    check = ready.run(bot_rights=no_rights)["Бот"]
    assert check.status == FAIL and "не адміністратор" in check.text
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert "токена" in ready.run()["Бот"].text


def test_bot_process_states(ready):
    assert ready.run()["Бот"].status == OK
    ready.heartbeat(health.BOT_STALE + 60)
    check = ready.run()["Бот"]
    assert check.status == WARN and "не відповідає" in check.text
    ready.sql("DELETE FROM bot_status")
    assert ready.run()["Бот"].status == WARN  # пульсу немає зовсім
    ready.heartbeat(30, error="Telegram не відповів вчасно (getUpdates)")
    check = ready.run()["Бот"]
    assert check.status == WARN and "Telegram не відповів" in check.text


def test_bot_just_started_has_no_pulse_yet(ready):
    ready.heartbeat(86400)  # пульс від учорашнього запуску
    now = datetime.now().astimezone()  # замок бота записано щойно
    assert ready.run(now=lambda: now)["Бот"].status == OK


def test_bot_not_running(ready, tmp_path):
    other = tmp_path / "other-data"  # окрема тека даних: замка бота там ніхто не тримає
    other.mkdir()
    db.connect(other / "shafa.sqlite").close()
    check = by_name(health.run_all(other, ready.root, deps=ready.deps()))["Бот"]
    assert check.status == WARN and "не запущений" in check.text


# --- 7. Сайт ---


def test_site_skip_missing_key_errors_ok(ready, monkeypatch):
    assert ready.run()["Сайт"].status == SKIP
    monkeypatch.setenv("SHAFA_SITE_SEND", "так")
    assert "ключа" in ready.run()["Сайт"].text
    monkeypatch.setenv("SITE_INTEGRATION_KEY", SITE_KEY)
    assert ready.run()["Сайт"].status == OK

    def down(key):
        raise SiteError("сайт відповів 502", 502)

    def refused(key):
        raise SiteError("сайт відповів 401", 401)

    assert ready.run(site_get=down)["Сайт"].status == FAIL
    check = ready.run(site_get=refused)["Сайт"]
    assert check.status == FAIL and "Ключ сайту не підходить" in check.fix


def test_site_timeout_is_fail(ready, monkeypatch):
    monkeypatch.setenv("SHAFA_SITE_SEND", "так")
    monkeypatch.setenv("SITE_INTEGRATION_KEY", SITE_KEY)
    monkeypatch.setattr(health, "NET_LIMIT", 0.05)
    gate = threading.Event()
    check = ready.run(site_get=lambda key: gate.wait(2))["Сайт"]
    gate.set()
    assert check.status == FAIL and "не відповів" in check.text


# --- 8. Вхід на Шафу ---


def test_login_states(ready):
    assert ready.run()["Вхід на Шафу"].status == OK
    ready.sql(
        "INSERT INTO sessions (started_at, ended_at, stop_code) VALUES (?, ?, 'login')",
        "2026-10-20T10:00:00+00:00",
        "2026-10-20T10:01:00+00:00",
    )
    check = ready.run()["Вхід на Шафу"]
    assert check.status == FAIL and "Увійти на Шафу" in check.fix
    ready.sql(
        "INSERT INTO sessions (started_at, ended_at, stop_code) VALUES (?, ?, 'done')",
        "2026-10-20T11:00:00+00:00",
        "2026-10-20T11:01:00+00:00",
    )
    assert ready.run()["Вхід на Шафу"].status == OK  # пізніша сесія пройшла
    for path in sorted((ready.data / "robot-profile").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    assert ready.run()["Вхід на Шафу"].status == WARN  # профілю ще немає — невідомо


def test_login_check_never_opens_a_browser(ready, monkeypatch):
    from shafa.robot import browser

    def boom(*a, **k):
        raise AssertionError("браузер у перевірці не відкривається")

    monkeypatch.setattr(browser, "launch", boom)
    monkeypatch.setattr(browser.Session, "open_form", boom)
    assert ready.run()["Вхід на Шафу"].status == OK


# --- 9. Довідники ---


@pytest.mark.parametrize(("days", "status"), [(0, OK), (30, OK), (31, WARN)])
def test_catalog_age(ready, days, status):
    stamp = str((NOW - timedelta(days=days)).date())
    ready.catalog.write_text(json.dumps({"checked": stamp}), encoding="utf-8")
    assert ready.run()["Довідники Шафи"].status == status


def test_catalog_unreadable_is_warn(ready):
    ready.catalog.write_text("не json", encoding="utf-8")
    assert ready.run()["Довідники Шафи"].status == WARN


# --- 10. Пробний прогін ---


def write_rehearsal(ready, **fields):
    last = {"at": NOW.isoformat(), "checked": 5, "good": 5, "problems": 0, "stop": "done"}
    last.update(summary="Перевірено 5: усе добре", **fields)
    (ready.data / "rehearse-last.json").write_text(json.dumps(last), encoding="utf-8")


def test_rehearsal_states(ready):
    assert ready.run()["Пробний прогін"].status == SKIP
    write_rehearsal(ready)
    assert ready.run()["Пробний прогін"].status == OK
    write_rehearsal(ready, problems=2)
    assert ready.run()["Пробний прогін"].status == WARN
    write_rehearsal(ready, stop="login")
    assert ready.run()["Пробний прогін"].status == WARN


# --- Помилки й секрети ---


def boom(*args):
    raise RuntimeError(f"обрив: {SECRET} {BOT_TOKEN} {SITE_KEY}")


def test_exceptions_in_dependencies_do_not_escape_and_secrets_stay_out(ready, monkeypatch):
    monkeypatch.setenv("SHAFA_SITE_SEND", "так")
    monkeypatch.setenv("SITE_INTEGRATION_KEY", SITE_KEY)
    deps = ready.deps(
        ai_models=boom, bot_rights=boom, site_get=boom, disk_usage=boom, now=lambda: NOW
    )
    checks = health.run_all(ready.data, ready.root, deps=deps)
    assert [c.name for c in checks] == NAMES
    statuses = by_name(checks)
    for name in ("Місце на диску", "Ключ ШІ", "Бот", "Сайт"):
        assert statuses[name].status == FAIL, name
    everything = (
        health.render(checks)
        + (ready.data / health.LAST_NAME).read_text(encoding="utf-8")
        + json.dumps([c.__dict__ for c in checks], ensure_ascii=False)
    )
    for secret in (SECRET, BOT_TOKEN, SITE_KEY):
        assert secret not in everything


def test_secret_inside_a_safe_error_text_is_scrubbed(ready):
    def leaking(token, channel):
        raise bot_run.SetupError(f"Не вдалося перевірити бота: {token}")

    checks = health.run_all(ready.data, ready.root, deps=ready.deps(bot_rights=leaking))
    assert BOT_TOKEN not in health.render(checks)
    assert BOT_TOKEN not in (ready.data / health.LAST_NAME).read_text(encoding="utf-8")
    assert "***" in by_name(checks)["Бот"].text


def test_any_check_raising_gives_fail_not_exception(ready, monkeypatch):
    def broken(ctx):
        raise ValueError(SECRET)

    monkeypatch.setattr(health, "CHECKS", tuple((n, broken) for n, _ in health.CHECKS))
    checks = health.run_all(ready.data, ready.root, deps=ready.deps())
    assert len(checks) == 10 and all(c.status == FAIL for c in checks)
    assert SECRET not in health.render(checks)


# --- Панель ---


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def panel_app(conn, tmp_path, root, checks, calls=None):
    def check():
        if calls is not None:
            calls.append(1)
        return checks

    clock = Clock()
    app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        health_check=check, run=lambda job: job(), clock=clock,
    )  # fmt: skip
    return app, clock


GOOD = [Check(name, OK, "добре") for name in NAMES]
BAD = [
    Check("Ключ ШІ", FAIL, "Ключ ШІ не працює: Ключ Claude API не прийнято (401)", "Напишіть Власнику"),
    Check("Бот", WARN, "Права є, але бот @x не запущений", "Натисніть «Запустити бота»"),
    Check("База", OK, "добре"),
]  # fmt: skip


def test_panel_all_fine_is_one_green_line(conn, tmp_path, root):
    app, _ = panel_app(conn, tmp_path, root, GOOD)
    assert "Самоперевірка ще не проходила" in html(get(app, "/review"))
    assert location(post(app, "/health", back="/review")).endswith("msg=health_started")
    page = html(get(app, "/review"))
    assert "Все гаразд" in page and "перевірено" in page
    assert "Є що виправити" not in page and "health-rows" not in page


def test_panel_problems_show_what_and_how_red_first(conn, tmp_path, root):
    app, _ = panel_app(conn, tmp_path, root, list(reversed(BAD)))
    post(app, "/health", back="/review")
    page = html(get(app, "/review"))
    assert "Є що виправити" in page and "Все гаразд" not in page
    assert page.index("Ключ ШІ не працює") < page.index("не запущений")  # червоні вище жовтих
    assert "Що зробити: Напишіть Власнику" in page and "Що зробити: Натисніть" in page
    assert "решта (1)" in page and "health-fail" in page and "health-warn" in page


def test_panel_says_checking_while_running(conn, tmp_path, root):
    jobs = []
    app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        health_check=lambda: GOOD, run=jobs.append, clock=Clock(),
    )  # fmt: skip
    assert location(post(app, "/health", back="/review")).endswith("msg=health_started")
    page = html(get(app, "/review"))
    assert "Перевіряю…" in page and "Перевірити ще раз" not in page
    assert location(post(app, "/health", back="/review")).endswith("msg=health_running")
    jobs[0]()  # фон закінчив
    assert "Все гаразд" in html(get(app, "/review"))


def test_recheck_not_more_often_than_every_30_seconds(conn, tmp_path, root):
    calls = []
    app, clock = panel_app(conn, tmp_path, root, GOOD, calls)
    assert location(post(app, "/health", back="/review")).endswith("msg=health_started")
    clock.now += health_every() - 1
    assert location(post(app, "/health", back="/review")).endswith("msg=health_wait")
    clock.now += 2
    assert location(post(app, "/health", back="/review")).endswith("msg=health_started")
    assert len(calls) == 2


def health_every():
    return web.HEALTH_EVERY


def test_recheck_button_needs_token(conn, tmp_path, root):
    app, _ = panel_app(conn, tmp_path, root, GOOD)
    assert post(app, "/health", token=None, back="/review").status == 403
    assert "Перевірити ще раз" in html(get(app, "/review"))


def test_failing_check_job_does_not_break_the_panel(conn, tmp_path, root, capsys):
    def explode():
        raise RuntimeError("збій")

    app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        health_check=explode, run=lambda job: job(), clock=Clock(),
    )  # fmt: skip
    post(app, "/health", back="/review")
    assert "Самоперевірка ще не проходила" in html(get(app, "/review"))  # і не «Перевіряю…»


def test_panel_keeps_csp(conn, tmp_path, root):
    app, _ = panel_app(conn, tmp_path, root, BAD)
    post(app, "/health", back="/review")
    response = get(app, "/robot/panel")
    assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    assert b"<script" not in response.body


# --- Запуск при старті ---


def test_start_runs_health_in_background(monkeypatch, tmp_path):
    monkeypatch.setattr(program, "HEALTH_DELAY", 0)
    started, release = threading.Event(), threading.Event()

    def slow(self):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(web.App, "_real_health", slow)
    before = time.monotonic()
    assert program.start(lambda url: None, port=0, serve=lambda server: None) == 0
    assert time.monotonic() - before < 4  # екран не чекав на перевірку
    assert started.wait(5)
    release.set()


# --- CLI ---


def test_cli_prints_ten_lines_and_exit_code(ready, monkeypatch, capsys):
    monkeypatch.setenv("SHAFA_ROOT_DIR", str(ready.root))
    monkeypatch.setattr(health, "real_deps", lambda: ready.deps())
    assert cli.main(["health"]) == 0
    out = capsys.readouterr().out
    assert len([line for line in out.splitlines() if line.startswith("[")]) == 10
    assert SECRET not in out and BOT_TOKEN not in out

    monkeypatch.setattr(
        health, "real_deps", lambda: ready.deps(disk_usage=lambda p: SimpleNamespace(free=1))
    )
    assert cli.main(["health"]) == 1  # є «помилка»
    assert "що зробити" in capsys.readouterr().out


def test_cli_json(ready, monkeypatch, capsys):
    monkeypatch.setenv("SHAFA_ROOT_DIR", str(ready.root))
    monkeypatch.setattr(health, "real_deps", lambda: ready.deps(ai_models=boom))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    assert cli.main(["health", "--json"]) == 1
    out = capsys.readouterr().out
    data = json.loads(out)
    assert [c["name"] for c in data] == NAMES
    assert SECRET not in out
