"""KLB-014: Клубок виставляє сам щодня — умови автостарту, позначка доби, налаштування, панель."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shafa import options
from shafa.klubok import auto, queue
from shafa.robot import klubok_auto, klubok_control
from shafa.robot import klubok_publish as kp
from test_klb002_queue import ad_row, add
from test_klb009 import app, lock_file, spawner  # noqa: F401  (фікстури екрана)
from test_web import post

NOW = datetime(2026, 11, 22, 15, 0).astimezone()  # неділя, 15:00 місцевого часу


def stamp(moment):
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def session(conn, site, ended, code="done", reason=None, started=None):
    with conn:
        return conn.execute(
            "INSERT INTO sessions (started_at, ended_at, stop_code, stop_reason, site) "
            "VALUES (?, ?, ?, ?, ?)",
            (stamp(started or ended - timedelta(minutes=30)), stamp(ended), code, reason, site),
        ).lastrowid


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")
    monkeypatch.delenv(auto.AUTO_SETTING, raising=False)
    monkeypatch.delenv(auto.AT_SETTING, raising=False)


@pytest.fixture
def ready(conn, root):
    """Одна схвалена пара в черзі Клубку — `state.can_start`."""
    ad_row(conn, add(conn, root, 7301), queue.QUEUED)


class Busy:
    value = None

    def __call__(self):
        return self.value


@pytest.fixture
def robot(conn, root, tmp_path, ready):
    spawned, lines, busy = [], [], Busy()
    clock = {"now": NOW}

    class Keep:  # `tick` закриває з'єднання — тест його лишає відкритим
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    a = klubok_auto.Auto(
        command=lambda left: klubok_control.start_args(tmp_path / "база.sqlite", root, left),
        spawn=spawned.append,
        busy=busy,
        log=lines.append,
        connect=Keep,
        state=lambda c, now: klubok_control.state(c, tmp_path / "robot.lock", now),
        clock=lambda: clock["now"],
        folder=tmp_path,
    )
    return a, spawned, lines, busy, clock


def test_after_calm_shafa_session_starts_with_limit_left(conn, robot):
    a, spawned, lines, _busy, _clock = robot
    assert a.tick() is None  # Шафа сьогодні ще не працювала, 21:00 ще не було
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "started"
    assert len(spawned) == 1
    args = spawned[0]
    assert args[0] == "--klubok-publish" and args[-2:] == ["--limit", "10"]
    assert "після сесії Шафи" in lines[-1]
    mark = json.loads((a.folder / auto.MARK).read_text(encoding="utf-8"))
    assert mark["day"] == "2026-11-22"


def test_limit_is_what_is_left_today(conn, robot, monkeypatch):
    a, spawned, *_ = robot
    monkeypatch.setenv(queue.LIMIT_SETTING, "5")
    with conn:
        conn.execute(
            "UPDATE klubok_ads SET publish_started_at = ?", (stamp(NOW - timedelta(hours=2)),)
        )
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "started" and spawned[0][-2:] == ["--limit", "4"]


def test_busy_robot_waits_for_next_tick(conn, robot):
    a, spawned, _lines, busy, clock = robot
    session(conn, "shafa", NOW - timedelta(minutes=5))
    busy.value = {"mode": "publish"}
    assert a.tick() == "busy" and spawned == []
    busy.value = None
    clock["now"] += timedelta(minutes=1)
    assert a.tick() == "started" and len(spawned) == 1


def test_once_a_day(conn, robot):
    a, spawned, _lines, _busy, clock = robot
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "started"
    clock["now"] += timedelta(hours=3)  # і 21:00 настало, і ще одна сесія Шафи
    session(conn, "shafa", clock["now"] - timedelta(minutes=1))
    assert a.tick() is None and len(spawned) == 1
    clock["now"] += timedelta(days=1)  # завтра — знову
    session(conn, "shafa", clock["now"] - timedelta(minutes=1))
    assert a.tick() == "started" and len(spawned) == 2


def test_limit_exhausted_or_queue_empty(conn, robot, monkeypatch):
    a, spawned, lines, *_ = robot
    session(conn, "shafa", NOW - timedelta(minutes=5))
    with conn:
        conn.execute("UPDATE klubok_ads SET state = 'skipped'")
    assert a.tick() is None and spawned == [] and "автостарт не потрібен" in lines[-1]
    with conn:
        conn.execute("UPDATE klubok_ads SET state = 'queued', publish_started_at = ?",
                     (stamp(NOW - timedelta(hours=1)),))  # fmt: skip
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")
    assert a.tick() is None and spawned == []
    monkeypatch.setenv(queue.LIMIT_SETTING, "2")  # ліміт підняли — наступний тик запускає
    assert a.tick() == "started" and spawned[0][-2:] == ["--limit", "1"]


@pytest.mark.parametrize("code", sorted(kp.LOUD))
def test_loud_klubok_stop_today_blocks(conn, robot, code):
    a, spawned, lines, *_ = robot
    session(conn, "shafa", NOW - timedelta(hours=1))
    session(conn, "klubok", NOW - timedelta(minutes=20), code=code)
    assert a.tick() is None and spawned == [] and "не буде" in lines[-1]


def test_calm_or_old_klubok_stop_does_not_block(conn, robot):
    a, spawned, *_ = robot
    session(conn, "shafa", NOW - timedelta(hours=1))
    session(conn, "klubok", NOW - timedelta(days=1), code=kp.CAPTCHA)  # вчорашня — не рахується
    session(conn, "klubok", NOW - timedelta(minutes=20), code=kp.DONE)
    assert a.tick() == "started" and len(spawned) == 1


def test_evening_without_shafa_session(conn, robot):
    a, spawned, lines, _busy, clock = robot
    clock["now"] = NOW.replace(hour=20, minute=59)
    assert a.tick() is None
    clock["now"] = NOW.replace(hour=21, minute=0)
    assert a.tick() == "started" and "за часом" in lines[-1]
    assert len(spawned) == 1


def test_evening_time_setting_and_off(conn, robot, monkeypatch):
    a, spawned, _lines, _busy, clock = robot
    monkeypatch.setenv(auto.AT_SETTING, "18:30")
    clock["now"] = NOW.replace(hour=18, minute=31)
    assert a.tick() == "started"
    spawned.clear()
    clock["now"] += timedelta(days=1)
    monkeypatch.setenv(auto.AT_SETTING, "off")  # лише після Шафи
    assert a.tick() is None
    session(conn, "shafa", clock["now"] - timedelta(minutes=1))
    assert a.tick() == "started"


def test_shafa_session_must_be_calm_and_today(conn, robot):
    a, spawned, *_ = robot
    session(conn, "shafa", NOW - timedelta(days=1))  # вчорашня
    session(conn, "shafa", NOW - timedelta(minutes=10), code="login", reason="Немає входу")
    session(conn, "klubok", NOW - timedelta(minutes=10))  # сесія Клубку — не Шафа
    assert a.tick() is None and spawned == []
    session(conn, "shafa", NOW - timedelta(minutes=5), code="limit")
    assert a.tick() == "started"


@pytest.mark.parametrize("value", ["ні", "off", "0", "no"])
def test_switched_off_never_starts(conn, robot, monkeypatch, value):
    a, spawned, _lines, _busy, clock = robot
    monkeypatch.setenv(auto.AUTO_SETTING, value)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    clock["now"] = NOW.replace(hour=23)
    assert a.tick() is None and spawned == []


def test_klubok_off_never_starts(conn, robot, monkeypatch):
    a, spawned, *_ = robot
    monkeypatch.delenv(queue.FLAG)
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() is None and spawned == []


def test_manual_start_after_auto(conn, app, spawner, robot, monkeypatch):  # noqa: F811
    a, spawned, *_ = robot
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "started"  # позначка доби є
    response = post(app, "/klubok/robot", action="start")
    assert "msg=klubok_started" in response.headers["Location"]
    assert spawner.calls and spawner.calls[0][-2:] == ["--limit", "10"]
    assert spawned[0][-2:] == spawner.calls[0][-2:]  # кнопка й автостарт — той самий набір
    with conn:  # ліміт доби вичерпано — кнопка відмовляє, як і автостарт
        conn.execute("UPDATE klubok_ads SET publish_started_at = ?", (stamp(NOW),))
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")
    before = len(spawner.calls)
    assert (
        "msg=klubok_cannot_start" in post(app, "/klubok/robot", action="start").headers["Location"]
    )
    assert len(spawner.calls) == before


def test_session_id_is_attached_to_mark(conn, robot):
    a, *_ = robot
    session(conn, "shafa", NOW - timedelta(minutes=5))
    assert a.tick() == "started"
    assert auto.read_mark(a.folder)["session"] is None
    sid = session(conn, "klubok", NOW + timedelta(minutes=1), started=NOW + timedelta(seconds=30))
    a.tick()
    assert auto.read_mark(a.folder)["session"] == sid


# --- панель і налаштування ---


def test_panel_line(conn, tmp_path, monkeypatch):
    def line():
        return auto.status(conn, NOW, tmp_path)

    assert line().line == "Автостарт: сьогодні після сесії Шафи або о 21:00"
    monkeypatch.setenv(auto.AT_SETTING, "")
    assert line().line == "Автостарт: сьогодні після сесії Шафи"
    monkeypatch.setenv(auto.AT_SETTING, "ранок")
    assert line().warn and "потрібен час" in line().line
    monkeypatch.setenv(auto.AT_SETTING, "21:00")
    session(conn, "klubok", NOW - timedelta(minutes=5), code=kp.LOGIN)
    assert line().warn and "не буде" in line().line
    auto.write_mark(NOW.replace(hour=21, minute=34), tmp_path)
    assert line().line == "Автостарт: запущено сьогодні о 21:34"
    monkeypatch.setenv(auto.AUTO_SETTING, "ні")
    assert line().line == "Автостарт: вимкнено"
    monkeypatch.delenv(queue.FLAG)
    assert line().line == ""


def test_panel_shows_line_on_screen(conn, app, ready):  # noqa: F811
    from test_web import get, html

    assert "Автостарт: сьогодні після сесії Шафи" in html(get(app, "/klubok/panel"))


def test_settings_roundtrip():
    shown = options.current()
    assert shown[auto.AUTO_SETTING] == "1" and shown[auto.AT_SETTING] == "21:00"
    form = {o.name: shown[o.name] for o in options.OPTIONS}
    form[auto.AT_SETTING] = "7:05"
    values, errors, _ = options.check(form)
    assert not errors and values[auto.AT_SETTING] == "07:05"
    form[auto.AT_SETTING] = ""
    assert options.check(form)[0][auto.AT_SETTING] == "off"
    for bad in ("25:00", "21", "21:60", "ввечері"):
        form[auto.AT_SETTING] = bad
        assert auto.AT_SETTING in options.check(form)[1]
    form[auto.AT_SETTING] = "21:00"
    del form[auto.AUTO_SETTING]  # галочку зняли
    assert options.check(form)[0][auto.AUTO_SETTING] == "0"


def test_settings_page_has_klubok_fields(app):  # noqa: F811
    from test_web import get, html

    page = html(get(app, "/settings"))
    assert "Виставляти на Клубок самому щодня" in page and "запустити Клубок о" in page
