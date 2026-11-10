"""KLB-002 (D2): відкрита сесія Клубку не видна Шафі й не закривається нею."""

from datetime import date, datetime, timedelta

import pytest

from shafa import health, web
from shafa.bot import alerts, report
from shafa.db import now
from shafa.robot import control, publish
from test_web import PORT, TOKEN, get, html

NOW = datetime.now().astimezone()


def _session(conn, site, ended=False, code=None):
    stamp = (NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
    with conn:
        return conn.execute(
            "INSERT INTO sessions (started_at, ended_at, stop_code, site) VALUES (?, ?, ?, ?)",
            (stamp, now() if ended else None, code, site),
        ).lastrowid


@pytest.fixture
def klubok_open(conn):
    return _session(conn, "klubok")


def _row(conn, session_id):
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def test_recover_of_shafa_leaves_klubok_session_open(conn, klubok_open):
    shafa_open = _session(conn, "shafa")

    publish.recover(conn)

    assert _row(conn, shafa_open)["stop_code"] == "crash"
    assert _row(conn, klubok_open)["ended_at"] is None
    assert _row(conn, klubok_open)["stop_code"] is None


def test_pause_and_stop_of_shafa_do_not_reach_klubok(conn, klubok_open):
    assert publish.request(conn, "stop") is False  # відкритої сесії Шафи немає
    assert _row(conn, klubok_open)["command"] is None
    assert publish.command(conn, klubok_open) is None

    shafa_open = _session(conn, "shafa")
    assert publish.request(conn, "pause") is True
    assert _row(conn, shafa_open)["command"] == "pause"
    assert _row(conn, klubok_open)["command"] is None


def test_panel_does_not_see_klubok_session(conn, tmp_path, klubok_open):
    shafa_done = _session(conn, "shafa", ended=True, code="done")
    _session(conn, "klubok", ended=True, code="login")

    state = control.state(conn, tmp_path / "robot.lock", day_limit=30, now=NOW)

    assert state.code != "crashed"  # відкрита сесія без живого робота — це про Шафу, не про Клубок
    assert state.last_session == shafa_done
    assert state.session_published is None


def test_health_login_check_reads_only_shafa(conn, tmp_path):
    profile = tmp_path / "robot-profile"
    profile.mkdir()
    (profile / "x").write_text("1", encoding="utf-8")
    _session(conn, "shafa", ended=True, code="done")
    _session(conn, "klubok", ended=True, code="login")  # Клубок не увійшов — Шафі байдуже
    deps = health.Deps(lambda key: None, lambda token, chat: "", lambda url: None)
    ctx = health._Ctx(tmp_path, tmp_path, deps, conn=conn)

    assert health._login(ctx).status == health.OK


def test_bot_alerts_and_report_skip_klubok(conn, tmp_path):
    _session(conn, "klubok", ended=True, code="error")
    assert alerts._last_session(conn) == 0
    assert alerts._session_events(conn, {"session_id": 0}) == []
    shafa = _session(conn, "shafa", ended=True, code="done")
    assert alerts._last_session(conn) == shafa

    text = report.build(conn, tmp_path, date.today())
    assert "сесій 1" in text


def test_sessions_tab_shows_only_shafa(conn, tmp_path, root, klubok_open):
    shafa = _session(conn, "shafa", ended=True, code="done")
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)

    body = html(get(app, "/sessions"))

    assert f'href="/session/{shafa}"' in body
    assert f'href="/session/{klubok_open}"' not in body
    assert get(app, f"/session/{klubok_open}").status == 404
