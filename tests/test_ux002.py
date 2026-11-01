"""UX-002: панель робота — що робить зараз, що було, що далі; пауза між товарами; підсумки сесій."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from shafa import web
from shafa.robot import control, lock, publish
from test_robot_control import NOW, session
from test_robot_publish import Env, add, other_item
from test_web import PORT, TOKEN, get, html, location, post
from test_web_robot import Process, Spawner

LOCAL = NOW  # час «зараз» у тестах панелі: 13.10.2026 12:00 UTC


@pytest.fixture(autouse=True)
def new_cards_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def state(conn, lock_file, **kw):
    return control.state(conn, lock_file, day_limit=kw.pop("day_limit", 30), now=NOW, **kw)


def two_cards(env):
    first = add(env, "1342", 1342, "2025-03-01T10:00:00")
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    other_item(env, first)
    other_item(env, second)
    return first, second


def run_probed(env, probe_sleep):
    """`Env.run` з власним `sleep`: паузу між товарами перехоплює `probe_sleep`."""
    t = [0.0]

    def sleep(seconds):
        probe_sleep(seconds)
        t[0] += seconds

    with lock.hold("publish", env.tmp / "robot.lock") as held:
        return publish.Run(
            env.conn, env.root, env.session, env.make_driver, env.load, env.tmp / "екрани", held,
            log=env.lines.append, sleep=sleep, clock=lambda: t[0],
            rand=lambda a, b: (a + b) / 2, today=lambda: env.today,
        ).start(30, 30)  # fmt: skip


# --- A13: пауза між товарами ---


def test_item_started_is_cleared_between_items(env):
    """Після товару `item_started_at` порожній — інакше «пауза» чекає вже дописаного товару."""
    two_cards(env)
    during: list = []

    def sleep(seconds):
        during.append(env.conn.execute("SELECT item_started_at FROM sessions").fetchone()[0])

    run_probed(env, sleep)
    assert during and all(value is None for value in during)


def test_pause_between_items_is_the_paused_state(env, tmp_path):
    """Пауза між товарами → «Робот на паузі», а не «чекаю, поки допишеться поточний товар»."""
    two_cards(env)
    seen: list[control.State] = []
    calls = [0]

    def sleep(seconds):
        calls[0] += 1
        if calls[0] == 1:
            publish.request(env.conn, "pause")
            seen.append(control.state(env.conn, tmp_path / "robot.lock", day_limit=30))
        elif publish.command(env.conn, 1) == "pause":
            publish.request(env.conn, None)  # «Продовжити»

    report = run_probed(env, sleep)
    assert len(report.published) == 2
    assert seen[0].code == "paused" and seen[0].wait_minutes is None
    assert "нічого не робить" in seen[0].text and "Продовжити" in seen[0].text


def test_pausing_names_the_post_and_never_freezes(conn, lock_file):
    """Пауза посеред товару: «дописую пост N, ще ≈ M хв»; довше 4 хв — «ще трохи», не «~1 хв»."""
    started = (NOW - timedelta(minutes=2)).isoformat()
    session(conn, command="pause", item_started_at=started)
    publish.write_now(1, "fill", post=6979, title="Зимові черевики", plan=3, handled=0, last=None)
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.code == "pausing"
    assert s.text == "Пауза: дописую пост 6979, ще ≈ 2 хв"
    with conn:
        conn.execute(
            "UPDATE sessions SET item_started_at = ?", ((NOW - timedelta(minutes=9)).isoformat(),)
        )
    with lock.hold("publish", lock_file):
        assert "ще трохи" in state(conn, lock_file).text


# --- A12: під час роботи ---


def test_running_shows_now_progress_and_last(env, tmp_path):
    two_cards(env)
    seen: list[control.State] = []

    def sleep(seconds):
        if not seen:
            began = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
            with env.conn:  # сесія йде вже 6 хв: один пост — 6 хв, лишився ще один
                env.conn.execute("UPDATE sessions SET started_at = ?", (began,))
            seen.append(control.state(env.conn, tmp_path / "robot.lock", day_limit=30))

    run_probed(env, sleep)
    s = seen[0]
    assert s.code == "running"
    assert s.progress == "Публікує: 1 з 2 на сьогодні · працює 6 хв · ще ≈ 6 хв"
    assert s.doing.startswith("Пауза між товарами (як людина) — наступний за ~")
    assert s.previous == "Останній: пост 1342 — опубліковано ✓"
    assert s.mini.startswith("Робот: 1 з 2")


def test_now_file_names_the_post_while_filling(env, monkeypatch):
    two_cards(env)
    stages: list[dict] = []
    original = publish.Run._fill

    def fill(self, report, card, driver, steps):
        stages.append(publish.read_now(report.session_id))
        return original(self, report, card, driver, steps)

    monkeypatch.setattr(publish.Run, "_fill", fill)
    env.run()
    assert [s["stage"] for s in stages] == ["fill", "fill"]
    assert stages[0]["post"] == 1342 and stages[0]["plan"] == 2 and stages[0]["handled"] == 0
    assert stages[1]["handled"] == 1 and stages[1]["last"] == {"post": 1342, "ok": True}
    assert publish.read_now() == {}  # сесія закінчилась — запису немає


def test_doing_line_for_a_post_being_filled(conn, lock_file):
    session(conn)
    publish.write_now(
        1, "fill", post=6979, title="Зимові черевики columbia для хлопчика розмір 38 нові",
        plan=19, handled=4, last={"post": 6393, "ok": True},
    )  # fmt: skip
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert (
        s.doing == "Зараз: пост 6979 «Зимові черевики columbia для хлопчика ро…» — заповнює форму"
    )
    assert s.previous == "Останній: пост 6393 — опубліковано ✓"
    assert s.progress.startswith("Публікує: 4 з 19 на сьогодні · працює 1 год")


def test_problem_in_previous_line(conn, lock_file):
    session(conn)
    publish.write_now(1, "between", plan=2, handled=1, last={"post": 7, "ok": False})
    with lock.hold("publish", lock_file):
        assert state(conn, lock_file).previous == "Останній: пост 7 — потрапив у Проблеми"


def test_stale_now_file_of_another_session_is_ignored(conn, lock_file):
    session(conn)
    publish.write_now(99, "fill", post=1, plan=5, handled=1)
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.progress == "" and s.doing == ""


# --- Б3: «опублікує до N» ---


def test_start_hint_counts_what_fits_today(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    for i in range(3):
        add(env, str(1342 + i), 1342 + i)
    s = state(conn, lock_file, day_limit=2)
    assert s.queue_today == 2 and s.eta_text == "Сьогодні опублікує до 2 Карток (≈ 6 хв)"
    assert state(conn, lock_file).eta_text == "Сьогодні опублікує до 3 Карток (≈ 9 хв)"


# --- A10 / A16: «останній запуск» з датою ---


def ended(conn, started, finished, **values):
    session(conn, started_at=started, ended_at=finished, **values)


def test_last_run_yesterday_has_a_date_and_today_note(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    ended(conn, "2026-10-12T15:00:00+00:00", "2026-10-12T15:28:00+00:00", published=13,
          problems=0, stop_code="limit", stop_reason="x")  # fmt: skip
    s = state(conn, lock_file)
    assert s.last == (
        "Останній запуск: вчора, 12.10 о 15:00 — опубліковано 13, денний ліміт того дня вичерпано"
    )
    assert s.idle_note == "Сьогодні робот ще не запускався — можна натискати «Старт»."
    assert "на сьогодні" not in control.LAST["limit"]


def test_last_run_today_has_duration_and_problem_counts(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    ended(conn, "2026-10-13T10:07:00+00:00", "2026-10-13T11:19:00+00:00", id=45, published=19,
          problems=1, stop_code="done", stop_reason="x")  # fmt: skip
    s = state(conn, lock_file)
    assert s.last == "Сесія 45 о 10:07 (1 год 12 хв): опубліковано 19, у Проблемах 1"
    assert (s.last_session, s.last_problems, s.idle_note) == (45, 1, "")


def test_never_started_has_only_the_note(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    s = state(conn, lock_file)
    assert s.last == "" and "ще не запускався" in s.idle_note


# --- A11: пробна перевірка з датою ---


def test_rehearsal_text_has_date_and_is_hidden_when_old():
    now = NOW
    fresh = {"at": "2026-10-12T21:00:00+00:00", "checked": 7, "good": 7, "problems": 0}
    assert control.rehearsal_text(fresh, now) == (
        "Пробна перевірка 12.10 (без публікації): 7 Карток — усе добре",
        0,
    )
    bad = {
        "at": "2026-10-13T08:00:00+00:00",
        "checked": 5,
        "good": 3,
        "problems": 2,
        "codes": "x ×2",
    }
    assert control.rehearsal_text(bad, now) == (
        "Пробна перевірка 13.10 (без публікації): 5 Карток — добре 3, у Проблемах 2 (x ×2)",
        2,
    )
    old = {"at": "2026-10-10T08:00:00+00:00", "checked": 7, "good": 7, "problems": 0}
    assert control.rehearsal_text(old, now) == ("", 0)


# --- A17/A18/A33–A36: тексти ---


def test_no_tomorrow_wording_and_group_text(conn, root, lock_file, monkeypatch):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    with conn:
        conn.execute("UPDATE items SET publish_started_at = ?", (NOW.isoformat(),))
        conn.execute("INSERT INTO items (folder_name, status, created_at, updated_at) "
                     "VALUES ('2', 'approved', 'x', 'x')")  # fmt: skip
    s = state(conn, lock_file, day_limit=1)
    assert s.why_not == (
        "На сьогодні все: робот уже почав 1 публікацію, а ліміт на добу — 1 (Налаштування). "
        "Завтра після 00:00 натисніть «Старт»."
    )
    assert "продовжимо" not in s.why_not
    assert s.groups_text.startswith("Сьогодні на Шафі: ")
    assert "з минулих постів" in s.groups_text and "з нових (бот)" in s.groups_text


def test_idle_texts(conn, lock_file):
    s = state(conn, lock_file)
    assert s.text == "Робот чекає"
    assert s.why_not == "Немає Схвалених — схваліть Картки у «На перевірку», потім «Старт»."
    assert control.LAST["slow"].endswith("через 15–30 хв")


def test_login_needed_blocks_start_until_login_window_closed(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    ended(conn, "2026-10-13T11:00:00+00:00", "2026-10-13T11:30:00+00:00", stop_code="login",
          stop_reason="x")  # fmt: skip
    s = state(conn, lock_file)
    assert s.code == "login_needed" and not s.can_start and s.buttons[:1] == ["start"]
    assert "login" in s.buttons and "спершу увійдіть" in s.why_not.lower()
    control.mark_login(NOW + timedelta(minutes=5))  # вікно входу закрили після зупинки
    s = state(conn, lock_file)
    assert s.code == "stopped" and s.can_start


def test_login_button_when_robot_never_logged_in(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    assert "login" not in state(conn, lock_file).buttons
    s = state(conn, lock_file, never_logged=True)
    assert "login" in s.buttons and s.can_start and s.code == "stopped"


# --- A14: «запускається» ---


def test_starting_ends_as_soon_as_a_session_opened_after_spawn(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    spawned = NOW - timedelta(seconds=20)
    assert state(conn, lock_file, starting=True, spawned=spawned).code == "starting"
    session(conn, started_at=(NOW - timedelta(seconds=5)).isoformat())
    s = state(conn, lock_file, starting=True, spawned=spawned)
    assert s.code == "crashed"  # сесія почалась, замка немає — процес уже помер


def test_short_session_does_not_leave_starting_for_a_minute(conn, root, lock_file):
    """A14: сесія вже почалась і закінчилась, процес ще доживає — це не «Робот запускається…»."""
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    spawned = NOW - timedelta(seconds=30)
    ended(conn, (NOW - timedelta(seconds=20)).isoformat(), (NOW - timedelta(seconds=5)).isoformat(),
          published=1, stop_code="stop", stop_reason="Стоп")  # fmt: skip
    assert state(conn, lock_file, starting=True, spawned=spawned).code == "stopped"


# --- A19/A20: упав посеред роботи ---


def test_crash_after_session_started_is_not_raw_log(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    spawned = NOW - timedelta(minutes=30)
    ended(conn, (NOW - timedelta(minutes=20)).isoformat(), (NOW - timedelta(minutes=1)).isoformat(),
          published=5, stop_code="error",
          stop_reason="збій: TimeoutError: щось дуже технічне")  # fmt: skip
    s = state(conn, lock_file, failed=True, spawned=spawned)
    assert s.code == "crashed" and "Робот не запустився" not in s.text
    assert s.text == (
        "Робот зупинився посеред роботи (опубліковано 5). Натисніть «Старт», щоб продовжити. "
        "Повториться — скажіть Власнику."
    )
    assert any("TimeoutError" in line for line in s.details) and "TimeoutError" not in s.text


def test_closed_chrome_window_has_its_own_text(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    spawned = NOW - timedelta(minutes=30)
    ended(conn, (NOW - timedelta(minutes=20)).isoformat(), (NOW - timedelta(minutes=1)).isoformat(),
          stop_code="error", stop_reason="збій: TargetClosedError: Target page has been closed")  # fmt: skip
    s = state(conn, lock_file, failed=True, spawned=spawned)
    assert s.text == control.CRASH_CLOSED and "Не закривайте це вікно" in s.text
    assert control.CRASH_CLOSED not in s.last and "опубліковано 0" in s.last  # причину не дублюємо
    assert control.reason_of("error", "збій: TargetClosedError: x") == control.CRASH_CLOSED
    assert "Картку в Проблемах" not in control.LAST["error"]


def test_failed_before_any_session_is_plain(conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    s = state(conn, lock_file, failed=True, spawned=NOW - timedelta(seconds=30))
    assert s.code == "failed" and s.text.startswith("Робот не запустився.")


# --- A21 / not ready ---


def test_balance_text_is_calm_and_names_no_threats():
    from shafa.robot import balance

    text = balance.BalanceStop(" (−193.15 грн)").text
    assert text.startswith("Шафа просить поповнити баланс (−193.15 грн).")
    assert "Робот нічого не платить" in text and "деактивац" not in text


@pytest.mark.parametrize("code", ["no_section", "section_unknown", "section_yellow", "no_sizes",
                                  "babies_size", "no_colors", "no_gender"])  # fmt: skip
def test_not_ready_codes_are_human(code):
    from shafa.robot import plan

    assert code in plan.NOT_READY and code not in plan.NOT_READY[code]
    assert "відкрийте Картку" in plan.NOT_READY[code]


# --- Екран ---


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    spawner = Spawner()
    application = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=lock_file,
    )  # fmt: skip
    application.spawner_calls = spawner.calls
    return application


def test_sessions_page_and_panel_agree_on_problems(app, conn, root, lock_file):
    """A15: сесія, де 11 Карток у Проблемах, не «Усі Схвалені опубліковано» ні там, ні там."""
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    ended(conn, "2026-10-13T08:00:00+00:00", "2026-10-13T09:00:00+00:00", id=28, published=7,
          problems=11, stop_code="done", stop_reason="черга закінчилась")  # fmt: skip
    listing = html(get(app, "/sessions"))
    assert "Усі Схвалені опубліковано" not in listing
    assert "Черга закінчилась" in listing and "У Проблемах <b>11</b> — подивіться" in listing
    assert 'class="s-why tone-warn"' in listing
    detail = html(get(app, "/session/28"))
    assert 'У Проблемах <b>11</b> — <a href="/problem">подивіться</a>' in detail
    panel = html(get(app, "/robot/panel"))
    assert "опубліковано 7, у Проблемах 11" in panel and "Відкрити Проблеми" in panel
    assert 'href="/session/28"' in panel and "Усі Схвалені опубліковано" not in panel


def test_start_refusals_say_why(app, conn, root, lock_file):
    page = post(app, "/robot", action="start", back="/review")
    assert "robot_cannot_start_none" in location(page)
    page = html(get(app, "/review?msg=robot_cannot_start_none"))
    assert "Робота не запущено: немає Схвалених Карток" in page
    for key in ("busy", "limit", "login", "setup"):
        assert web.MESSAGES[f"robot_cannot_start_{key}"].startswith("Робота не запущено: ")
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    with lock.hold("publish", lock_file):
        page = post(app, "/robot", action="start", back="/review")
    assert "robot_cannot_start_busy" in location(page)


def test_start_message_explains_the_chrome_window(app):
    text = web.MESSAGES["robot_started"]
    assert "окреме вікно Chrome" in text and "кришку ноутбука" in text and "тут, на панелі" in text


def test_panel_never_shows_raw_codes_or_tomorrow(app, conn, root, lock_file):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    page = html(get(app, "/review"))
    for raw in ("section_yellow", "продовжимо завтра", "Робота не запущено\n"):
        assert raw not in page
    assert "Сьогодні на Шафі: з минулих постів" in page
    assert "Пробно заповнити завтрашні (без публікації, ~5 хв)" in page
    assert "Перевірити завтрашні" not in page


def test_health_button_label_before_first_check(app):
    page = html(get(app, "/review"))
    if "Самоперевірка ще не проходила" in page:
        assert "<button>Перевірити</button>" in page


def test_panel_carries_tab_counts_for_live_update(app, conn, root):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    panel = html(get(app, "/robot/panel"))
    counts = re.search(r'data-counts="([^"]*)"', panel)
    assert counts and "approved=1;" in counts.group(1)
    assert "data-counts" in html(get(app, "/robot/panel?mini=1"))
    assert "updateCounts" in get(app, "/static/app.js").body.decode("utf-8")


def test_msg_is_removed_from_the_address_after_showing(app):
    script = get(app, "/static/app.js").body.decode("utf-8")
    assert 'searchParams.delete("msg")' in script and "history.replaceState" in script


def test_mini_panel_shows_progress_and_stop(app, conn, root, lock_file):
    session(conn)
    publish.write_now(1, "fill", post=5, title="x", plan=19, handled=4)
    with lock.hold("publish", lock_file):
        mini = html(get(app, "/robot/panel?mini=1&back=%2Fitem%2F5"))
    assert "Робот: 4 з 19" in mini and 'name="action" value="stop"' in mini


def test_failed_start_hides_raw_log_line_under_details(app, conn, root, tmp_path, monkeypatch):
    env = Env(conn, root, root.parent)
    add(env, "1342", 1342)
    monkeypatch.setattr(control, "log_path", lambda: tmp_path / "robot-run.log")
    (tmp_path / "robot-run.log").write_text("Traceback: щось сире\n", encoding="utf-8")
    app.process, app.spawned_at = Process(1), datetime.now().astimezone()
    page = html(get(app, "/robot/panel"))
    assert "Робот не запустився." in page
    summary_at = page.index("Детальніше")
    assert page.index("Traceback: щось сире") > summary_at  # сирий рядок — лише під «Детальніше»
