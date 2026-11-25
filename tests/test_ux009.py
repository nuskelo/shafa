"""UX-009: панель робота — чесний етап «Мої речі», оцінка часу від першої пари, фонове читання.

Усе без мережі й Chrome: підставний кабінет із `test_robot011`, підставні процеси, tmp_path.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shafa import __main__ as cli
from shafa import db, web
from shafa.robot import browser, control, listings, listings_watch, lock, login_check, publish
from test_robot011 import cabinet, listing
from test_robot_control import session
from test_robot_publish import Env, add, quiet  # noqa: F401 — autouse: без справжніх пауз
from test_ux002 import two_cards
from test_web import PORT, TOKEN, post
from test_web_robot import Spawner

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    monkeypatch.delenv("SHAFA_LISTINGS_DAYS", raising=False)
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


def state(conn, lock_file):
    return control.state(conn, lock_file, day_limit=30, now=NOW)


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


# --- 1. Оцінка часу: темп від першої Картки, не раніше ніж після 3 ---


def test_estimate_after_first_pair_ignores_the_reading_stage(conn, lock_file):
    """Сесія 80: 8 хв «Мої речі» + 2 хв перша пара. Раніше «ще ≈ 4 год 50 хв» (10 хв × 29)."""
    session(conn, started_at=stamp(10))
    publish.write_now(1, "between", plan=30, handled=1, last=None, first_at=stamp(2))
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.left_text == "1 год 25 хв"  # 29 × типові 3 хв, а не 29 × 10
    assert "4 год" not in s.progress and "працює 10 хв" in s.progress


def test_estimate_after_three_pairs_uses_pace_since_first_pair(conn, lock_file):
    """8 хв читання + 3 пари по 2 хв: темп 2 хв/пара → ще 27 × 2 = 54 хв (раніше 14/3 → ≈ 2 год)."""
    session(conn, started_at=stamp(14))
    publish.write_now(1, "between", plan=30, handled=3, last=None, first_at=stamp(6))
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.left_text == "55 хв"
    real = 27 * 2
    assert 54 <= real * 1.5 and 55 <= real * 1.5  # не більше за реальний темп ×1,5


def test_estimate_without_first_pair_time_stays_typical(conn, lock_file):
    session(conn, started_at=stamp(14))
    publish.write_now(1, "between", plan=30, handled=5, last=None)  # first_at немає
    with lock.hold("publish", lock_file):
        assert state(conn, lock_file).left_text == "1 год 15 хв"  # 25 × 3


def test_run_records_the_start_of_the_first_card(conn, root, tmp_path, monkeypatch):
    env = Env(conn, root, tmp_path)
    two_cards(env)
    seen: list[dict] = []
    original = publish.Run._fill

    def fill(self, report, card, driver, steps):
        seen.append(publish.read_now(report.session_id))
        return original(self, report, card, driver, steps)

    monkeypatch.setattr(publish.Run, "_fill", fill)
    env.run()
    first = seen[0]["first_at"]
    assert (
        datetime.fromisoformat(first).tzinfo is not None
    )  # `_working` ставить момент першої Картки
    assert seen[1]["first_at"] == first  # далі він не міняється


# --- 2. Етап «Мої речі» окремим рядком ---


def test_panel_names_the_reading_stage_with_counter_and_eta(conn, lock_file):
    session(conn)
    publish.write_now(
        1, "listings", plan=5, handled=0, last=None, read=820, expected=1650, since=stamp(4)
    )
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.doing == (
        "Зараз: Перечитую ваші оголошення на Шафі, щоб не виставити дубль: 820 з ~1650"
        " · ще ≈ 4 хв. Це раз на 7 днів."
    )
    assert s.progress.startswith("Готується: 0 з 5")  # не «Публікує», доки читає


def test_reading_text_without_previous_count_has_no_total():
    text = control.reading_text({"read": 120, "expected": 0, "since": stamp(1)}, NOW)
    assert ": прочитано 120." in text and "~" not in text and "ще ≈" not in text


def test_reading_text_says_the_period_from_settings(monkeypatch):
    monkeypatch.setenv("SHAFA_LISTINGS_DAYS", "3")
    assert control.reading_text({}, NOW).endswith("Це раз на 3 днів.")


def test_run_writes_reading_stage_with_expected_from_previous_snapshot(conn, root, tmp_path):
    env = Env(conn, root, tmp_path)
    add(env, "1342", 1342)
    listings.store(conn, [listing(1), listing(2), listing(3)])
    listings.reset(conn)  # знімок скинуто («Перечитати зараз») — старий лишив 3 рядки
    seen: list[dict] = []

    def load(session):
        listings._progress(820)  # те саме, що після прочитаної сторінки кабінету
        seen.append(publish.read_now(1))
        return [listing(1)]

    env.load = load
    env.run()
    assert seen[0]["stage"] == "listings"
    assert (seen[0]["read"], seen[0]["expected"]) == (820, 3)
    assert seen[0]["since"]


def test_load_reports_progress_after_each_page():
    seen: list[int] = []
    with listings.progress_to(seen.append):
        listings.load(cabinet(), log=lambda line: None)
    assert seen == [2, 3, 5]  # вкладки ACTIVE (2), MODERATED (+1), DECLINED (+2): наростаючим
    assert listings._progress is None  # після читання хук знято


def test_broken_progress_callback_does_not_break_reading():
    def boom(read):
        raise RuntimeError

    with listings.progress_to(boom):
        assert len(listings.load(cabinet(), log=lambda line: None)) == 5


# --- 3. Фонове читання: коли час ---


def snapshot(conn, days_ago: float, count: int = 3):
    listings.store(conn, [listing(i) for i in range(1, count + 1)], NOW - timedelta(days=days_ago))
    with conn:
        conn.execute("UPDATE my_listings_read SET read_at = ?", (stamp(days_ago * 1440),))


def test_due_soon_when_snapshot_will_be_stale_within_a_day(conn):
    snapshot(conn, 3)
    assert not listings.due_soon(conn, NOW)  # 7 днів: свіжий ще 4 доби
    snapshot(conn, 6.2)
    assert listings.due_soon(conn, NOW)  # застаріє за 0,8 доби — читаємо заздалегідь
    assert listings.stored(conn, 7, NOW) is not None  # а «Старт» його ще міг би взяти


def test_due_soon_for_empty_or_reset_snapshot(conn):
    assert listings.due_soon(conn, NOW)  # знімка ніколи не було
    snapshot(conn, 1)
    listings.reset(conn)
    assert listings.due_soon(conn, NOW)  # «Перечитати зараз»


def test_due_soon_with_one_day_setting_waits_half_a_day(conn, monkeypatch):
    monkeypatch.setenv("SHAFA_LISTINGS_DAYS", "1")
    snapshot(conn, 0.3)
    assert not listings.due_soon(conn, NOW)  # не читаємо безперервно
    snapshot(conn, 0.6)
    assert listings.due_soon(conn, NOW)


def watch(due=True, busy=False, folder=None, **kw):
    spawned, logs, clock = [], [], [NOW]
    kw.setdefault("delay", timedelta(0))
    w = listings_watch.Watch(
        command=lambda: ["--listings-refresh"], spawn=spawned.append, busy=lambda: busy,
        log=logs.append, due=lambda: due, clock=lambda: clock[0], folder=folder, **kw,
    )  # fmt: skip
    return w, spawned, logs, clock


def test_watch_starts_background_reading_when_due_and_robot_free(tmp_path):
    w, spawned, logs, _ = watch(folder=tmp_path)
    assert w.tick() == "started" and spawned == [["--listings-refresh"]]
    assert any("перечитую заздалегідь" in line for line in logs)


def test_watch_does_nothing_when_snapshot_is_fresh(tmp_path):
    w, spawned, _, _ = watch(due=False, folder=tmp_path)
    assert w.tick() is None and spawned == []


def test_watch_waits_while_robot_is_busy(tmp_path):
    w, spawned, _, _ = watch(busy=True, folder=tmp_path)
    assert w.tick() == "busy" and spawned == []


def test_watch_does_not_open_chrome_without_robot_profile_or_in_first_two_minutes(tmp_path):
    """ROBOT-019: на чистому встановленні (профілю немає) і в перші 2 хв Chrome не відкривається."""
    w, spawned, _, clock = watch(folder=tmp_path, ready=lambda: False)
    assert w.tick() is None and spawned == []
    w, spawned, _, clock = watch(folder=tmp_path, delay=listings_watch.START_DELAY)
    assert listings_watch.START_DELAY == timedelta(minutes=2)
    assert w.tick() is None
    clock[0] = NOW + timedelta(minutes=1, seconds=59)
    assert w.tick() is None and spawned == []
    clock[0] = NOW + timedelta(minutes=2)
    assert w.tick() == "started"


def test_watch_does_not_read_when_login_is_lost(tmp_path):
    login_check.store.write(False, NOW, "x", tmp_path)
    w, spawned, _, _ = watch(folder=tmp_path)
    assert w.tick() is None and spawned == []


def test_watch_retries_no_more_than_once_an_hour(tmp_path):
    w, spawned, _, clock = watch(folder=tmp_path)
    assert w.tick() == "started"
    clock[0] = NOW + timedelta(minutes=59)
    assert w.tick() is None
    clock[0] = NOW + timedelta(minutes=61)
    assert w.tick() == "started" and len(spawned) == 2


def test_watch_respects_failed_attempt_of_previous_program_run(tmp_path):
    listings.write_state(
        listings.BG_LAST, {"at": (datetime.now().astimezone()).isoformat(), "ok": False}
    )
    w, spawned, _, clock = watch(folder=tmp_path)
    clock[0] = datetime.now().astimezone() + timedelta(minutes=1)
    assert w.tick() is None and spawned == []


def test_app_watch_does_not_spawn_robot_on_clean_install(tmp_path, monkeypatch):
    """windows-package: на чистому встановленні (профілю робота немає) Chrome не відкривається."""
    from shafa import app as program

    spawned = []
    monkeypatch.setattr(control, "spawn", spawned.append)
    path = tmp_path / "база.sqlite"
    db.connect(path).close()
    w = program.listings_watch(path)
    w.born -= timedelta(hours=1)  # дві хвилини після старту вже минули
    assert not web._logged_in() and w.due()  # знімка немає, а профілю теж
    assert w.tick() is None and spawned == []


# --- 4. Процес `robot --listings-refresh` ---


class FormSession:
    def __init__(self, page, form_state=browser.FORM):
        self.page, self.form_state, self.closed = page, form_state, False

    def open_form(self):
        return SimpleNamespace(state=self.form_state, ok=self.form_state == browser.FORM)

    def close(self):
        self.closed = True


def refresh(tmp_path, make_session):
    path = tmp_path / "процес.sqlite"
    db.connect(path).close()
    lines: list[str] = []
    code = cli._listings_refresh(
        None, SimpleNamespace(db=path), lines.append, make_session=make_session
    )
    return code, path, lines


def test_listings_refresh_stores_snapshot_and_clears_state(tmp_path):
    session_ = FormSession(cabinet().page)
    code, path, lines = refresh(tmp_path, lambda: session_)
    assert code == 0 and session_.closed
    check = sqlite3.connect(path)
    assert check.execute("SELECT count(*) FROM my_listings").fetchone()[0] == 5
    assert check.execute("SELECT read_at FROM my_listings_read").fetchone()[0]
    assert listings.read_state(listings.BG_NOW) == {}  # «читаю…» з панелі зникло
    assert listings.read_state(listings.BG_LAST)["ok"] is True
    assert any("знімок оновлено" in line for line in lines)


def test_listings_refresh_with_lost_login_reads_nothing(tmp_path):
    session_ = FormSession(cabinet().page, browser.NO_LOGIN)
    code, path, _ = refresh(tmp_path, lambda: session_)
    assert code == 1 and session_.closed
    assert sqlite3.connect(path).execute("SELECT count(*) FROM my_listings").fetchone()[0] == 0
    assert listings.read_state(listings.BG_LAST)["ok"] is False
    assert listings.read_state(listings.BG_NOW) == {}


def test_listings_refresh_reading_failure_keeps_old_snapshot(tmp_path):
    session_ = FormSession(cabinet(fail=["MODERATED"]).page)
    code, path, lines = refresh(tmp_path, lambda: session_)
    assert code == 2 and any("не прочитано" in line for line in lines)
    assert sqlite3.connect(path).execute("SELECT count(*) FROM my_listings").fetchone()[0] == 0
    assert listings.read_state(listings.BG_NOW) == {}


def test_listings_refresh_chrome_failure_is_not_login_loss(tmp_path):
    def boom():
        raise RuntimeError("Chrome")

    code, _, lines = refresh(tmp_path, boom)
    assert code == 2 and any("Chrome" in line for line in lines)
    assert listings.read_state(listings.BG_NOW) == {}


# --- 5. Панель і «Старт» під час фонового читання ---


def test_panel_state_while_background_reading_shows_counter(conn, root, lock_file):
    add(Env(conn, root, root.parent), "1342", 1342)
    listings.write_state(
        listings.BG_NOW, {"read": 820, "expected": 1650, "since": stamp(4), "at": stamp(0)}
    )
    with lock.hold(listings.LOCK_MODE, lock_file):
        s = state(conn, lock_file)
    assert s.code == "reading" and s.can_start and "start" in s.buttons
    assert s.text.startswith(
        "Перечитую ваші оголошення на Шафі, щоб не виставити дубль: 820 з ~1650"
    )
    assert "rehearse" not in s.buttons  # пробний прогін потребує замка


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    spawner = Spawner()
    jobs = []
    application = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=lock_file, run=jobs.append,
    )  # fmt: skip
    application.spawner_calls, application.jobs = spawner.calls, jobs
    application.reading_poll = 0
    add(Env(conn, root, root.parent), "1342", 1342)
    login_check.store.write(True, datetime.now().astimezone())
    return application


def test_start_during_background_reading_waits_and_panel_says_so(app, conn, lock_file):
    with lock.hold(listings.LOCK_MODE, lock_file):
        page = post(app, "/robot", action="start", back="/review")
        assert "robot_waiting_reading" in page.headers["Location"]
        assert app.spawner_calls == [] and app.start_waiting and len(app.jobs) == 1
        s = app.robot_state(conn)
        assert s.code == "waiting_reading" and not s.can_start and s.buttons == []
    app.jobs[0]()  # читання закінчилось (замок знято) — сесія запускається сама
    assert len(app.spawner_calls) == 1 and "--publish" in app.spawner_calls[0]
    assert app.start_waiting is False


def test_waiting_start_does_not_launch_if_someone_else_took_the_lock(app, lock_file):
    with lock.hold(listings.LOCK_MODE, lock_file):
        post(app, "/robot", action="start", back="/review")
    with lock.hold("login", lock_file):  # після читання замок узяв інший режим
        app.jobs[0]()
    assert app.spawner_calls == [] and app.start_waiting is False


def test_start_with_free_robot_is_unchanged(app):
    post(app, "/robot", action="start", back="/review")
    assert app.jobs == [] and len(app.spawner_calls) == 1  # без читання — як було
    assert not app.start_waiting


def test_reread_message_says_background(app):
    assert "поки робот вільний" in web.MESSAGES["reread_started"]
    json.dumps(web.MESSAGES["robot_waiting_reading"], ensure_ascii=False)
    assert "robot_waiting_reading" in web.MESSAGES
