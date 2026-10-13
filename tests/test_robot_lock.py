"""Замок «один робот» (UI-002, D1): файловий замок ОС, без перевірок PID. Працює й на Windows."""

import subprocess
import sys
import time

import pytest

from shafa.__main__ import main
from shafa.db import Status
from shafa.robot import lock, publish

HOLDER = (
    "import sys, time; from pathlib import Path; from shafa.robot import lock; "
    "held = lock.acquire('publish', Path(sys.argv[1])); print('тримаю', flush=True); "
    "time.sleep(60)"
)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def other_process(path):
    """Інший живий процес робота, що тримає замок."""
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout.readline().strip() == "тримаю"
    yield process
    process.kill()
    process.wait()


def test_second_acquire_refused_until_release(path):
    first = lock.acquire("publish", path)
    with pytest.raises(lock.Busy, match="Робот уже працює"):
        lock.acquire("login", path, wait=0)
    assert lock.busy(path)["mode"] == "publish"
    first.release()
    assert lock.busy(path) is None
    with lock.hold("login", path) as held:
        assert held.mode == "login"


def test_probe_does_not_steal_or_break_lock(path):
    with lock.hold("publish", path):
        for _ in range(3):
            assert lock.busy(path)["mode"] == "publish"
        with pytest.raises(lock.Busy):
            lock.acquire("publish", path, wait=0)


def test_lock_of_other_live_process(path, other_process):
    assert lock.busy(path)["mode"] == "publish"
    with pytest.raises(lock.Busy):
        lock.acquire("publish", path, wait=0.3)


def test_lock_released_by_os_when_process_dies(path, other_process):
    """Немає «завислого» замка: процес убито (падіння, вимкнення) — замок вільний."""
    other_process.kill()
    other_process.wait()
    held = lock.acquire("publish", path, wait=5.0)  # ОС знімає замок не миттєво (Windows)
    held.release()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Тека даних тесту; браузер у цих тестах заборонений — навіть якщо замок не спрацює."""
    import shafa.__main__ as cli

    def no_browser(*args, **kwargs):
        raise AssertionError("тест дійшов до браузера — замок не спрацював")

    monkeypatch.setattr(cli, "_with_browser", no_browser)
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    return tmp_path / "дані"


def live_robot_state(conn, root):
    """Картка посеред публікації й відкрита сесія — так виглядає база живого робота."""
    from test_robot_publish import Env, add

    item = add(Env(conn, root, root.parent))
    with conn:
        conn.execute("UPDATE items SET status = 'publishing' WHERE id = ?", (item,))
        conn.execute("INSERT INTO sessions (started_at) VALUES ('2026-10-13T08:00:00+00:00')")
    return item


@pytest.mark.parametrize(
    "args",
    [
        ["--publish", "--db", "{db}", "--root", "{root}"],
        ["--dry-fill", "--db", "{db}", "--root", "{root}", "--post", "1342"],
        ["--login"],
        ["--sync-catalog"],
    ],
)
def test_second_run_refused_and_touches_nothing(conn, root, tmp_path, data_dir, args, capsys):
    """Другий запуск при живому роботі: відмова; recover() не чіпає чужих Карток і сесії."""
    db_path = tmp_path / "база.sqlite"
    item = live_robot_state(conn, root)
    args = [a.replace("{db}", str(db_path)).replace("{root}", str(root)) for a in args]
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(data_dir / "robot.lock")],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )  # fmt: skip
    try:
        assert holder.stdout.readline().strip() == "тримаю"
        assert main(["robot", *args]) == 2
    finally:
        holder.kill()
        holder.wait()

    assert "Робот уже працює" in capsys.readouterr().out
    assert conn.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()[0] == (
        Status.PUBLISHING
    )
    assert conn.execute("SELECT ended_at FROM sessions").fetchone()[0] is None


def test_run_without_lock_refuses_before_recover(conn, root, tmp_path):
    from test_robot_publish import PubSession

    item = live_robot_state(conn, root)
    held = lock.acquire("publish", tmp_path / "robot.lock")
    held.release()
    run = publish.Run(conn, root, PubSession(), None, None, tmp_path, held, log=lambda s: None)
    with pytest.raises(lock.Busy):
        run.start(30, 30)
    login = lock.acquire("login", tmp_path / "robot.lock")
    try:
        with pytest.raises(lock.Busy):  # замок не того режиму — теж ні
            publish.Run(conn, root, PubSession(), None, None, tmp_path, login).start(30, 30)
    finally:
        login.release()
    assert conn.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()[0] == (
        Status.PUBLISHING
    )


def test_wait_retries_briefly(path):
    held = lock.acquire("publish", path)
    started = time.monotonic()
    with pytest.raises(lock.Busy):
        lock.acquire("publish", path, wait=0.5)
    assert time.monotonic() - started >= 0.5
    held.release()
