"""QA-005 «день Вікторії»: сценарії, яких ще не покривав жоден тест."""

import ast
import threading
import time
from pathlib import Path

import pytest

import shafa
from shafa import app, config, db
from shafa.db import Status
from shafa.robot import lock, publish
from test_robot_publish import Env, add, new_cards_allowed, quiet, state  # noqa: F401 — autouse
from test_web import post
from test_web_robot import Process, card


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


class SlowSpawner:
    """Запуск процесу, що триває помітний час (як справжній `Popen` на Windows)."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        time.sleep(0.3)
        self.calls.append(args)
        return Process()


# --- 4. Подвійний клік «Старт» ---


def test_double_click_on_start_spawns_one_robot(conn, root, tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")
    from shafa import web
    from test_web import PORT, TOKEN

    card(conn, root)
    spawner = SlowSpawner()
    app_ = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=tmp_path / "robot.lock",
    )  # fmt: skip
    answers = []
    threads = [
        threading.Thread(
            target=lambda: answers.append(post(app_, "/robot", action="start", back="/approved"))
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(spawner.calls) == 1, "два «Старт» одночасно запустили двох роботів"
    assert len(answers) == 2 and all("msg=robot_" in a.headers["Location"] for a in answers)


# --- 6. «Вийти» посеред сесії ---


def test_quit_stops_klubok_session_too(tmp_path):
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    conn = db.connect(app.db_path())
    with conn:
        conn.execute("INSERT INTO sessions (started_at, site) VALUES ('x', 'klubok')")
    with lock.hold("klubok-publish"):
        done = app.quit_all(kill=lambda pid, sig: None)
    assert done == ["робот Клубку допише поточну пару і зупиниться"]
    assert conn.execute("SELECT command FROM sessions").fetchone()[0] == "stop"
    conn.close()


# --- 23. Windows: жодних чорних вікон під `pythonw` ---

CONSOLE_OK = {"install.py", "wizard.py"}  # їх запускають із консолі (`Встановити.cmd`, `setup`)


def test_every_child_process_hides_its_console_window():
    """`pythonw` не має консолі: кожен `subprocess.run/Popen` без `creationflags` блимає вікном."""
    bad = []
    for path in Path(shafa.__file__).parent.rglob("*.py"):
        if path.name in CONSOLE_OK:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
                continue
            if node.func.attr not in {"run", "Popen", "call", "check_call", "check_output"}:
                continue
            hidden = any(k.arg == "creationflags" for k in node.keywords) or any(
                k.arg is None
                for k in node.keywords  # `**options` / `**hidden_window()`
            )
            if not hidden:
                bad.append(f"{path.name}:{node.lineno}")
    assert bad == []


# --- 5. Пауза / Стоп на етапах: «Мої речі», форма ---
# Модуль робота слухає команди між етапами й між парами: «Стоп» під час 5–10-хвилинного читання
# «Мої речі» спрацьовує, щойно воно скінчилось, — ще до першої Картки.


def test_stop_during_listings_reading_ends_before_the_first_card(env):
    item = add(env, "1342", 1342)
    env.load = lambda session: publish.request(env.conn, "stop") and []
    report = env.run()
    assert report.reason == "Стоп" and report.published == []
    assert state(env.conn, item) == (Status.APPROVED, None)  # Картку не чіпали
    assert env.driver.submits == []


def test_pause_during_listings_reading_holds_before_the_first_card_until_resumed(env):
    item = add(env, "1342", 1342)
    env.load = lambda session: publish.request(env.conn, "pause") and []
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            publish.request(env.conn, None)  # «Продовжити»

    with lock.hold("publish", env.tmp / "robot.lock") as held:
        run = publish.Run(
            env.conn, env.root, env.session, env.make_driver, env.load, env.tmp, held,
            log=env.lines.append, sleep=sleep, clock=lambda: 0.0, rand=lambda a, b: 0.0,
            today=lambda: env.today,
        )  # fmt: skip
        report = run.start(30, 30)
    assert waits[:3] == [publish.TICK] * 3 and len(report.published) == 1
    assert state(env.conn, item)[0] == Status.PUBLISHED


def test_stop_during_the_form_finishes_this_card_and_leaves_the_next_untouched(env):
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.on_submit = lambda: publish.request(env.conn, "stop")  # «Стоп» натиснуто, поки йде Картка
    report = env.run()
    assert report.reason == "Стоп" and [p for p, _ in report.published] == [1342]
    assert state(env.conn, first)[0] == Status.PUBLISHED
    assert state(env.conn, second) == (Status.APPROVED, None)


# --- 9. Сесія через північ ---


def test_session_over_midnight_stops_at_its_limit_and_next_day_starts_fresh(env):
    from datetime import datetime, timedelta

    env.today = datetime.now().astimezone()
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    other_item(env, second)
    env.on_submit = lambda: setattr(env, "today", env.today + timedelta(days=1))  # північ

    report = env.run(day_limit=1)  # ліміт доби — 1, сесія його не перевищує і після півночі
    assert [p for p, _ in report.published] == [1342] and "ліміт" in report.reason
    assert state(env.conn, second) == (Status.APPROVED, None)

    env.on_submit = None
    assert publish.started_today(env.conn, env.today) == 0  # новий день — лічильник з нуля
    again = env.run(day_limit=1)
    assert [p for p, _ in again.published] == [1400] and first


def other_item(env, item_id):
    with env.conn:
        env.conn.execute("UPDATE items SET duplicate_ok_at = 'x' WHERE id = ?", (item_id,))
