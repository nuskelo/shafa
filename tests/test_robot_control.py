"""Стан робота для екрана і запуск окремим процесом (UI-002)."""

from datetime import UTC, datetime, timedelta

import pytest

from shafa.robot import control, lock
from test_robot_publish import Env, add

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def new_cards_allowed(monkeypatch):
    """Тут перевіряється робот як такий: межа «нових на день» (CORE-005) не заважає."""
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


def state(conn, lock_file, **kw):
    return control.state(conn, lock_file, day_limit=kw.pop("day_limit", 30), now=NOW, **kw)


def approved(conn, root, n=1):
    env = Env(conn, root, root.parent)
    return [add(env, str(1342 + i), 1342 + i) for i in range(n)]


def session(conn, **values):
    columns = {"started_at": "2026-10-13T11:00:00+00:00", **values}
    with conn:
        conn.execute(
            f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            tuple(columns.values()),
        )


def test_stopped_without_approved_cannot_start(conn, lock_file):
    s = state(conn, lock_file)
    assert (s.code, s.can_start) == ("stopped", False)
    assert s.why_not == "Немає Схвалених — схваліть Картки у «На перевірку», потім «Старт»."
    assert s.buttons == ["start"]  # кнопка є, але неактивна — з поясненням


def test_stopped_with_approved_can_start(conn, root, lock_file):
    approved(conn, root, 2)
    s = state(conn, lock_file)
    assert (s.code, s.can_start, s.approved, s.published_today) == ("stopped", True, 2, 0)


def test_daily_limit_exhausted_cannot_start(conn, root, lock_file):
    approved(conn, root)
    with conn:
        conn.execute("UPDATE items SET publish_started_at = ?", (NOW.isoformat(),))
        conn.execute("INSERT INTO items (folder_name, status, created_at, updated_at) "
                     "VALUES ('2', 'approved', 'x', 'x')")  # fmt: skip
    s = state(conn, lock_file, day_limit=1)
    assert not s.can_start and "На сьогодні все: 1 з 1" in s.why_not
    assert (s.published_today, s.day_limit) == (1, 1)


@pytest.mark.parametrize(
    ("command", "in_work", "code", "buttons"),
    [
        (None, None, "running", ["pause", "stop"]),
        ("pause", "2026-10-13T11:59:00+00:00", "pausing", ["resume", "stop"]),
        ("pause", None, "paused", ["resume", "stop"]),
        ("stop", "2026-10-13T11:59:00+00:00", "stopping", []),
    ],
)
def test_running_states(conn, lock_file, command, in_work, code, buttons):
    session(conn, command=command, item_started_at=in_work)
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert (s.code, s.buttons, s.can_start) == (code, buttons, False)
    if in_work:  # «чекаю, поки допишеться поточний товар (~N хв)»
        assert s.wait_minutes == 3 and "дописую поточний товар, ще ≈ 3 хв" in s.text


def test_crashed_when_session_open_but_lock_free(conn, root, lock_file):
    approved(conn, root)
    session(conn)
    s = state(conn, lock_file)
    assert (s.code, s.can_start) == ("crashed", True)  # Старт → recover → продовжить


def test_login_needed_after_logout_stop(conn, root, lock_file):
    approved(conn, root)
    session(conn, ended_at="2026-10-13T11:30:00+00:00", stop_code="login", stop_reason="x")
    s = state(conn, lock_file)
    assert s.code == "login_needed" and "login" in s.buttons
    assert s.last.endswith("робот вийшов із Шафи") and s.last.startswith("Сесія 1 о 11:00")


def test_no_form_is_not_login(conn, root, lock_file):
    approved(conn, root)
    session(conn, ended_at="2026-10-13T11:30:00+00:00", stop_code="no_form", stop_reason="x")
    s = state(conn, lock_file)
    assert s.code == "stopped" and "login" not in s.buttons and s.can_start
    assert "не показала форму" in s.last and s.last != control.LAST["login"]


def test_login_window_and_other_busy_modes(conn, lock_file):
    with lock.hold("login", lock_file):
        s = state(conn, lock_file)
        assert (s.code, s.buttons) == ("login_open", ["login_done"])
    with lock.hold("dry-fill", lock_file):
        s = state(conn, lock_file)
        assert (s.code, s.buttons) == ("busy", [])


def test_starting_and_failed(conn, root, lock_file):
    approved(conn, root)
    assert state(conn, lock_file, starting=True).code == "starting"
    assert state(conn, lock_file, failed=True).code == "failed"


def test_minutes_left_never_negative_and_overdue_is_zero():
    """Довше за звичайне — 0 («ще трохи»), а не застиглі «~1 хв» (A13)."""
    old = (NOW - timedelta(minutes=30)).isoformat()
    assert control._minutes_left(old, NOW) == 0


def test_spawn_runs_robot_as_separate_process(conn, tmp_path):
    """Справжній дочірній процес (на Windows — без вікна консолі); кирилиця в журналі."""
    db_path = tmp_path / "база.sqlite"
    log = tmp_path / "журнал.log"
    process = control.spawn(["--stop", "--db", str(db_path)], log)
    assert process.wait(timeout=60) == 1
    assert "Робот зараз не публікує" in log.read_text(encoding="utf-8")


def test_command_uses_same_python():
    import sys

    assert control.command(["--publish"])[:4] == [sys.executable, "-m", "shafa", "robot"]


def test_states_have_texts():
    assert set(control.TEXTS) >= {"stopped", "running", "paused", "login_needed", "crashed"}


def test_spawned_robot_survives_ctrl_c_of_screen(monkeypatch, tmp_path):
    """Mac: своя сесія ОС (Ctrl+C у вікні сервера не дістане); Windows: без вікна, своя група."""
    import os
    import subprocess
    import sys

    probe = "import os; print(os.getsid(0) if hasattr(os, 'getsid') else 0)"
    monkeypatch.setattr(control, "command", lambda args, program: [sys.executable, "-c", probe])
    seen = {}
    real = subprocess.Popen

    def popen(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(control.subprocess, "Popen", popen)
    log = tmp_path / "журнал.log"
    assert control.spawn([], log).wait(timeout=60) == 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        assert seen["creationflags"] & flags == flags
    else:
        assert seen.get("start_new_session") is True
        assert int(log.read_text(encoding="utf-8").strip()) != os.getsid(0)


def test_command_for_bot():
    assert control.command(["--db", "x"], "bot")[3:] == ["bot", "--db", "x"]


def test_done_with_problems_says_how_many(conn, root, lock_file):
    """CORE-007: усі Схвалені пішли в Проблеми — не «Усі Схвалені опубліковано»."""
    approved(conn, root)
    session(conn, ended_at="2026-10-13T11:30:00+00:00", stop_code="done", stop_reason="x",
            published=0, problems=3)  # fmt: skip
    assert state(conn, lock_file).last.endswith("опубліковано 0, у Проблемах 3")


def test_done_without_problems(conn, root, lock_file):
    approved(conn, root)
    session(conn, ended_at="2026-10-13T11:30:00+00:00", stop_code="done", stop_reason="x",
            published=2, problems=0)  # fmt: skip
    assert state(conn, lock_file).last.endswith("(30 хв): опубліковано 2")
