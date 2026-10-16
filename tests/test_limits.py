"""Межі «старих» і «нових» на день (CORE-005): налаштування, черга робота, панель."""

import shutil
from datetime import datetime, timedelta

import pytest

from shafa import config, options, web
from shafa.db import Status
from shafa.folders import old_root
from shafa.robot import browser, control, publish
from test_options import form
from test_robot_publish import Env, started, state
from test_web import PORT, TOKEN, get, html, post


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setenv("SHAFA_DAILY_OLD", "2")
    monkeypatch.setenv("SHAFA_DAILY_NEW", "1")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)


@pytest.fixture
def env(conn, root, tmp_path):
    """Доба робота = справжня доба: спроби цього запуску пишуться в базу справжнім часом."""
    e = Env(conn, root, tmp_path)
    e.today = datetime.now().astimezone()
    return e


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def add_new(env, n, date="2025-03-01T10:00:00"):
    from test_robot_publish import add

    return add(env, str(n), n, date)


def add_old(env, n, date="2024-03-01T10:00:00"):
    """Схвалена Картка старого поста: фото — у теці даних, як після `import-old`."""
    from test_robot_publish import add

    item = add(env, f"borh-{n}", n, date)
    target = old_root() / str(n)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(env.root / f"borh-{n}", target)
    return item


def published_order(env):
    return [r[0] for r in env.conn.execute(
        "SELECT folder_name FROM items WHERE publish_started_at IS NOT NULL "
        "ORDER BY publish_started_at, id"
    )]  # fmt: skip


# --- Налаштування ---


def test_defaults(monkeypatch):
    monkeypatch.delenv("SHAFA_DAILY_OLD")
    monkeypatch.delenv("SHAFA_DAILY_NEW")
    assert publish.group_limits() == publish.Groups(10, 0)


@pytest.mark.parametrize(
    ("old", "new", "ok"),
    [("0", "30", True), ("30", "0", True), ("15", "15", True), ("20", "11", False),
     ("31", "0", False), ("-1", "5", False), ("десять", "0", False)],
)  # fmt: skip
def test_group_limits_setting(monkeypatch, old, new, ok):
    monkeypatch.setenv("SHAFA_DAILY_OLD", old)
    monkeypatch.setenv("SHAFA_DAILY_NEW", new)
    if ok:
        assert publish.group_limits() == publish.Groups(int(old), int(new))
    else:
        with pytest.raises(publish.SetupError):
            publish.group_limits()


def test_settings_page_saves_both(app, root, monkeypatch):
    monkeypatch.delenv("SHAFA_DAILY_OLD")
    monkeypatch.delenv("SHAFA_DAILY_NEW")
    page = html(get(app, "/settings"))
    assert 'name="SHAFA_DAILY_OLD" value="10"' in page
    assert 'name="SHAFA_DAILY_NEW" value="0"' in page
    assert "Старих на день" in page and "Нових на день" in page

    post(app, "/settings", **form(root, SHAFA_DAILY_OLD="12", SHAFA_DAILY_NEW="18"))

    assert (config.setting("SHAFA_DAILY_OLD"), config.setting("SHAFA_DAILY_NEW")) == ("12", "18")


def test_settings_page_refuses_more_than_30_together(app, root, monkeypatch):
    monkeypatch.delenv("SHAFA_DAILY_OLD")
    monkeypatch.delenv("SHAFA_DAILY_NEW")
    page = html(post(app, "/settings", **form(root, SHAFA_DAILY_OLD="20", SHAFA_DAILY_NEW="11")))

    assert "старих і нових разом 31, а можна не більше 30 на день" in page
    assert config.setting("SHAFA_DAILY_OLD") is None  # нічого не записано


def test_check_sum_only_when_both_valid(root):
    _, errors, _ = options.check(form(root, SHAFA_DAILY_OLD="40", SHAFA_DAILY_NEW="5"))
    assert list(errors) == ["SHAFA_DAILY_OLD"]
    assert errors["SHAFA_DAILY_OLD"].startswith("ціле число від 0 до 30")


# --- Черга й робот ---


def test_queue_new_first_then_old_oldest_first(env):
    old_late = add_old(env, 900, "2024-05-01T10:00:00")
    new_late = add_new(env, 1500, "2025-05-01T10:00:00")
    old_early = add_old(env, 800, "2024-01-01T10:00:00")
    new_early = add_new(env, 1400, "2025-01-01T10:00:00")
    assert publish.queue(env.conn) == [new_early, new_late, old_early, old_late]


def test_run_takes_new_then_old_each_up_to_its_limit(env):
    for n in (800, 801, 802):
        add_old(env, n)
    for n in (1400, 1401):
        add_new(env, n)

    report = env.run()

    assert [post_id for post_id, _ in report.published] == [1400, 800, 801]
    assert report.reason == "на сьогодні все: старих 2 з 2 · нових 1 з 1"
    assert report.code == "limit"
    assert state(env.conn, publish.queue(env.conn)[0])[0] == Status.APPROVED


def test_new_zero_by_default_old_still_go(env, monkeypatch):
    monkeypatch.delenv("SHAFA_DAILY_NEW")
    add_new(env, 1400)
    add_old(env, 800)

    env.run()

    assert published_order(env) == ["borh-800"]


def test_earlier_attempts_today_count_per_group(env):
    started(env, add_old(env, 700), env.today - timedelta(minutes=5))
    started(env, add_new(env, 1300), env.today - timedelta(minutes=5))  # нових 1 з 1 — усе
    started(env, add_old(env, 600), env.today - timedelta(days=1))  # учора — не рахується
    add_new(env, 1400)
    add_old(env, 800)
    add_old(env, 801)

    env.run()

    assert published_order(env)[-1:] == ["borh-800"]
    assert publish.started_groups(env.conn, env.today) == publish.Groups(2, 1)


def test_uncertain_attempt_counts(env):
    """Спроба, що дійшла до кліку без певної відповіді, теж рахується — Шафа її бачила."""
    with env.conn:
        item = add_old(env, 700)
        env.conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'publish_uncertain', "
            "publish_started_at = ? WHERE id = ?",
            (datetime.now().astimezone().isoformat(timespec="seconds"), item),
        )
    assert publish.started_groups(env.conn, env.today) == publish.Groups(1, 0)


def test_both_groups_full_touches_nothing(env):
    started(env, add_new(env, 1300), env.today)
    started(env, add_old(env, 700), env.today)
    started(env, add_old(env, 701), env.today)
    waiting = add_old(env, 800)

    report = env.run()

    assert report.reason == "на сьогодні все: старих 2 з 2 · нових 1 з 1"
    assert env.session.opened == 0  # навіть форму не відкривав
    assert state(env.conn, waiting) == (Status.APPROVED, None)


def test_setting_change_applies_from_next_card(env, monkeypatch):
    for n in (800, 801, 802):
        add_old(env, n)
    env.on_submit = lambda: monkeypatch.setenv("SHAFA_DAILY_OLD", "1")  # змінила під час роботи

    env.run()

    assert published_order(env) == ["borh-800"]


def test_daily_safety_limit_still_applies(env, monkeypatch):
    monkeypatch.setenv("SHAFA_DAILY_OLD", "10")
    for n in range(800, 805):
        add_old(env, n)
    report = env.run(day_limit=2)
    assert len(report.published) == 2 and report.reason == "ліміт доби"


# --- Панель ---


def test_panel_state_counts_and_why_not(env):
    started(env, add_new(env, 1300), env.today)
    add_new(env, 1400)

    s = control.state(env.conn, env.tmp / "robot.lock", now=env.today)

    assert s.groups_text == "Сьогодні: старих 0 з 2 · нових 1 з 1"
    assert not s.can_start
    assert s.why_not == "Сьогодні вже старих 0 з 2 · нових 1 з 1 — продовжимо завтра"

    add_old(env, 800)
    s = control.state(env.conn, env.tmp / "robot.lock", now=env.today)
    assert s.can_start and not s.why_not
