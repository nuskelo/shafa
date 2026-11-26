"""QA-005 «день Вікторії»: сценарії, яких ще не покривав жоден тест."""

import ast
import threading
import time
from pathlib import Path

import shafa
from shafa import app, config, db
from shafa.robot import lock
from test_web import post
from test_web_robot import Process, card


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
