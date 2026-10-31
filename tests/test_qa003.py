"""QA-003: пробний прогін («Перевірити завтрашні») — форма без кліку «Додати річ»."""

from datetime import datetime, timedelta

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from shafa import __main__ as cli
from shafa import ai, config, review, web
from shafa import app as program
from shafa.db import Status
from shafa.robot import browser, control, guard, lock, publish, rehearse
from shafa.robot.form import BrandMissing, FieldMissing, PhotosShort
from shafa.robot.plan import PlanError, rehearsal_plan
from test_limits import add_new, add_old
from test_review import add_card
from test_robot_form import FakeDriver, FakeLocator, FakePage
from test_robot_plan import BLANK, TEXT
from test_robot_publish import Env, add, opened, row, state
from test_web import PORT, TOKEN, get, html, location, post

MISSING = FieldMissing("Сезон")
PHOTOS = PhotosShort("1 з 2")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


@pytest.fixture
def env(conn, root, tmp_path):
    """Доба робота = справжня доба: спроби публікації пишуться в базу справжнім часом."""
    e = Env(conn, root, tmp_path)
    e.today = datetime.now().astimezone()
    return e


class RSession(browser.Session):
    """Сесія пробного прогону: як у `--rehearse` — завантаження фото є, режиму публікації немає."""

    def __init__(self, logged_in=True, form_state=None, attempts=()):
        self._setup(allow_upload=True, publish=False)
        self.page = FakePage()
        self.state = form_state if form_state is not None else logged_in
        self.opened = 0
        self.extra = list(attempts)

    def open_form(self):
        self.opened += 1
        return opened(self.state)

    def publish_attempts(self):
        return super().publish_attempts() + self.extra


def cards(env, n, status=Status.APPROVED):
    items = []
    for i in range(n):
        item = add(env, str(1300 + i), 1300 + i, f"2025-01-{1 + i:02d}T10:00:00")
        items.append(item)
    if status != Status.APPROVED:
        with env.conn:
            env.conn.execute("UPDATE items SET status = ?", (status,))
    return items


def script(monkeypatch, *outcomes):
    """Що буде з кожною Карткою під час заповнення форми: "ok", виняток чи "slow" (3 тайм-аути)."""
    at = {"card": 0, "attempt": 0}

    def execute(steps, driver, strict):
        assert strict  # як у справжній публікації: бренд і фото — помилки, не нотатки
        out = outcomes[at["card"]] if at["card"] < len(outcomes) else "ok"
        if out == "slow":
            at["attempt"] += 1
            if at["attempt"] == publish.ATTEMPTS:
                at["card"], at["attempt"] = at["card"] + 1, 0
            raise PlaywrightTimeout("Timeout 30000ms exceeded.")
        at["card"] += 1
        if isinstance(out, Exception):
            raise out
        return []

    monkeypatch.setattr(rehearse, "execute", execute)


def go(env, items, session=None, tmp=None, sleeps=None):
    """Пробний прогін цих Карток; повертає (звіт, рядки журналу, сесія)."""
    session = session or RSession()
    lines, sleeps = [], [] if sleeps is None else sleeps
    folder = tmp or env.tmp / "дані-звіту"
    report = rehearse.rehearse(
        env.conn, env.root, items, session, FakeDriver, env.tmp / "екрани", log=lines.append,
        sleep=sleeps.append, rand=lambda a, b: (a + b) / 2, today=lambda: env.today,
        folder=folder,
    )  # fmt: skip
    return report, lines, session


def states(env, items):
    return [state(env.conn, i) for i in items]


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- Ядро: помилка однієї Картки не зупиняє прогін ---


def test_second_card_missing_field_others_ok_run_goes_on(env, monkeypatch):
    items = cards(env, 5)
    script(monkeypatch, "ok", MISSING, "ok", "ok", "ok")

    report, lines, session = go(env, items)

    assert states(env, items) == [
        (Status.APPROVED, None), (Status.PROBLEM, "robot_field_missing"),
        (Status.APPROVED, None), (Status.APPROVED, None), (Status.APPROVED, None),
    ]  # fmt: skip
    text = row(env.conn, items[1])["problem_text"]
    assert text.startswith("Пробний прогін: Робот не знайшов на Шафі поле «Сезон»")
    assert (report.checked, report.good, len(report.moved), report.code) == (5, 4, 1, "done")
    assert session.opened == 5  # кожна Картка — з нової форми
    assert report.summary() == (
        "Пробний прогін: перевірено 5, добре 4, у Проблемах 1 (robot_field_missing)"
    )
    assert lines[-2] == report.summary()


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (PHOTOS, "robot_photos"),
        (BrandMissing("Ecco"), "robot_brand_missing"),
        ("slow", "robot_slow"),
    ],
)
def test_each_form_error_goes_to_problems_with_prefix(env, monkeypatch, bad, code):
    items = cards(env, 2)
    script(monkeypatch, bad, "ok")
    sleeps = []

    report, _, session = go(env, items, sleeps=sleeps)

    assert states(env, items) == [(Status.PROBLEM, code), (Status.APPROVED, None)]
    assert row(env.conn, items[0])["problem_text"].startswith(rehearse.PREFIX)
    assert (report.good, len(report.moved)) == (1, 1)
    if bad == "slow":  # ті самі 3 спроби з паузою, що й у публікації (ROBOT-009)
        assert session.opened == publish.ATTEMPTS + 1
        assert sleeps.count(sum(publish.RETRY_PAUSE) / 2) == publish.ATTEMPTS - 1


def test_three_form_errors_in_a_row_stop_the_rest_untouched(env, monkeypatch):
    items = cards(env, 5)
    script(monkeypatch, MISSING, PHOTOS, MISSING, "ok", "ok")

    report, _, session = go(env, items)

    assert report.code == "streak" and "3 Картки поспіль" in report.reason
    assert session.opened == 3  # решту не відкривали
    assert states(env, items) == [
        (Status.PROBLEM, "robot_field_missing"), (Status.PROBLEM, "robot_photos"),
        (Status.PROBLEM, "robot_field_missing"), (Status.APPROVED, None), (Status.APPROVED, None),
    ]  # fmt: skip


def test_success_resets_the_streak(env, monkeypatch):
    items = cards(env, 6)
    script(monkeypatch, MISSING, MISSING, "ok", MISSING, MISSING, "ok")

    report, _, _ = go(env, items)

    assert report.code == "done" and (report.checked, report.good) == (6, 2)


def test_card_without_photos_is_reported_and_not_a_form_streak(env, monkeypatch):
    items = cards(env, 5)
    for folder in ("1300", "1301", "1302"):  # три Картки, яких не передати формі
        for photo in (env.root / folder).iterdir():
            photo.unlink()
    script(monkeypatch)

    report, _, session = go(env, items)

    assert report.code == "done" and session.opened == 2  # форми для них не відкривались
    assert [o.code for o in report.outcomes[:3]] == ["robot_not_ready"] * 3
    assert states(env, items)[:3] == [(Status.PROBLEM, "robot_not_ready")] * 3
    assert row(env.conn, items[0])["problem_text"].startswith("Пробний прогін: Немає фото")


def test_review_card_error_is_only_in_report_not_in_problems(env, monkeypatch):
    items = cards(env, 2, status=Status.REVIEW)
    script(monkeypatch, MISSING, "ok")

    report, _, _ = go(env, items)

    assert states(env, items) == [(Status.REVIEW, None), (Status.REVIEW, None)]
    assert (report.checked, report.good, len(report.moved), len(report.failed)) == (2, 1, 0, 1)
    assert report.summary().endswith("у Проблемах 0, з помилкою без схвалення 1")


def test_stale_and_already_published_cards_are_skipped_untouched(env, monkeypatch):
    stale, fresh = cards(env, 2)
    with env.conn:
        env.conn.execute("UPDATE items SET rules_version = 1 WHERE id = ?", (stale,))
    script(monkeypatch)

    report, _, session = go(env, [stale, fresh])

    assert report.checked == 1 and session.opened == 1
    assert states(env, [stale, fresh]) == [(Status.APPROVED, None)] * 2
    assert report.outcomes[0].result == "skipped"


def test_no_login_or_form_stops_card_untouched(env, monkeypatch):
    items = cards(env, 2)
    script(monkeypatch)

    report, lines, session = go(env, items, RSession(logged_in=False))
    assert (report.code, report.checked, session.opened) == ("login", 0, 1)
    assert "robot --login" in lines[0]
    assert states(env, items) == [(Status.APPROVED, None)] * 2

    report, _, _ = go(env, items, RSession(form_state=browser.NO_FORM))
    assert (report.code, report.checked) == ("no_form", 0)
    assert states(env, items) == [(Status.APPROVED, None)] * 2


def test_broken_code_is_not_hidden_report_still_written(env, monkeypatch):
    items = cards(env, 1)
    script(monkeypatch, ValueError("баг у коді"))

    with pytest.raises(ValueError):
        go(env, items)

    last = rehearse.last_summary(env.tmp / "дані-звіту")
    assert last is not None and last["stop"] == "error"
    assert states(env, items) == [(Status.APPROVED, None)]


# --- Це не публікація і не сесія ---


def test_rehearsal_is_not_a_session_and_not_in_the_day_limit(env, monkeypatch):
    items = cards(env, 3)
    script(monkeypatch)
    today = env.today

    go(env, items)

    assert count(env.conn, "sessions") == 0
    assert publish.started_today(env.conn, today) == 0
    assert env.conn.execute("SELECT COUNT(*) FROM items WHERE publish_started_at IS NOT NULL"
                            ).fetchone()[0] == 0  # fmt: skip
    assert states(env, items) == [(Status.APPROVED, None)] * 3


def test_click_on_add_item_is_impossible_in_rehearsal_session():
    session = RSession()
    assert session.publish_mode is False
    with pytest.raises(guard.Forbidden, match="поза режимом"):
        session.arm_publish(1, Status.PUBLISHING)  # квиток не видається
    with pytest.raises(guard.Forbidden, match="без квитка"):
        session.click_publish(FakeLocator("Додати річ", "submit"))


def test_blocked_publish_attempt_raises_runtime_error(env, monkeypatch):
    items = cards(env, 2)
    script(monkeypatch)

    with pytest.raises(RuntimeError, match="Заблоковано спроби публікації"):
        go(env, items, RSession(attempts=["WEB_CreateProduct"]))

    assert states(env, items) == [(Status.APPROVED, None)] * 2


def test_too_many_cards_refused(env):
    with pytest.raises(ValueError):
        go(env, [1] * (rehearse.REHEARSE_MAX + 1))
    with pytest.raises(ValueError):
        go(env, [])


# --- Звіт ---


def test_report_file_log_line_and_last_summary(env, monkeypatch):
    items = cards(env, 3)
    script(monkeypatch, "ok", MISSING, "ok")
    folder = env.tmp / "звіти"

    report, lines, _ = go(env, items, tmp=folder)
    go(env, items[:1], tmp=folder)  # другий прогін того ж дня дописується

    file = folder / f"rehearse-{env.today:%Y-%m-%d}.txt"
    body = file.read_text(encoding="utf-8")
    assert body.count("Пробний прогін: перевірено") == 2
    assert "! Пост 1301: robot_field_missing" in body and "(Картка в Проблемах)" in body
    assert lines[-1].startswith("Звіт: ") and str(file) in lines[-1]
    last = rehearse.last_summary(folder)
    assert (last["checked"], last["good"], last["problems"]) == (1, 1, 0)  # останній прогін
    assert report.summary() in body


def test_notes_go_only_to_report_not_to_card(env, monkeypatch):
    items = cards(env, 1)
    monkeypatch.setattr(rehearse, "execute", lambda steps, driver, strict: ["Фото: 1 з 2"])

    report, lines, _ = go(env, items)

    assert report.outcomes[0].notes[-1] == "Фото: 1 з 2"
    assert "Пост 1300: добре (пропусків:" in lines[0]
    assert row(env.conn, items[0])["problem_text"] is None


# --- Вибір Карток ---


def test_tomorrow_is_exactly_what_the_next_real_session_takes(env, monkeypatch):
    """Спільне ядро `publish.take`: прогноз збігається з тим, що справді опублікує `_loop`."""
    monkeypatch.setenv("SHAFA_DAILY_NEW", "3")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "2")
    new = [add_new(env, 1300 + i, f"2025-03-0{5 - i}T10:00:00") for i in range(5)]
    old = [add_old(env, 1500 + i, f"2024-03-0{i + 1}T10:00:00") for i in range(3)]

    predicted = rehearse.pick_tomorrow(env.conn, env.today, rehearse.REHEARSE_MAX)
    first_two = rehearse.pick_tomorrow(env.conn, env.today, 2)
    report = env.run()

    published = [
        env.conn.execute("SELECT id FROM items WHERE post_id = ?", (post,)).fetchone()[0]
        for post, _ in report.published
    ]
    assert predicted == published and len(published) == 5
    assert set(published) <= {*new, *old}
    assert sum(1 for i in published if i in old) == 2  # межі груп уже враховано
    assert predicted[:3] == [new[4], new[3], new[2]]  # нові — за датою, старші раніше
    assert first_two == predicted[:2]  # --count


def test_tomorrow_after_day_limit_is_the_next_days_queue(env, monkeypatch):
    monkeypatch.setenv("SHAFA_DAILY_LIMIT", "2")
    items = [add_new(env, 1300 + i, f"2025-03-0{i + 1}T10:00:00") for i in range(4)]
    env.run(limit=2, day_limit=2)  # сьогоднішні 2 вичерпано

    predicted = rehearse.pick_tomorrow(env.conn, env.today, 30)
    env.today += timedelta(days=1)
    report = env.run(limit=2, day_limit=2)

    assert predicted == items[2:]  # не порожньо й не ті, що вже опубліковані
    assert [p for p, _ in report.published] == [1302, 1303]


def test_sections_picks_one_card_per_section_approved_first(env):
    def blank(section):
        return [f for f in BLANK if f.name != "section"] + [ai.Field("section", section)]

    def card(folder, section, status=Status.APPROVED):
        return add_card(env.conn, env.root, folder=folder, post_id=int(folder), status=status,
                        blank=blank(section), text=TEXT)  # fmt: skip

    boots = "Жіночий одяг / Взуття / Черевики"
    sneakers = "Жіночий одяг / Взуття / Кросівки"
    men = "Чоловічий одяг / Взуття / Черевики"
    review_boots = card("1300", boots, Status.REVIEW)
    approved_boots = card("1301", boots)
    second_boots = card("1302", boots)
    review_sneakers = card("1303", sneakers, Status.REVIEW)
    approved_men = card("1304", men)

    picked = rehearse.pick_sections(env.conn, env.root, 30)

    assert set(picked) == {approved_boots, review_sneakers, approved_men}
    assert second_boots not in picked and review_boots not in picked
    assert picked[:2] == [approved_boots, approved_men]  # Схвалені — першими
    paths = [review.load_card(env.conn, i).path for i in picked]
    assert len(set(paths)) == 3
    assert len(rehearse.pick_sections(env.conn, env.root, 2)) == 2


def test_pick_posts_reports_missing(env):
    (item,) = cards(env, 1)
    assert rehearse.pick_posts(env.conn, [1300, 999]) == ([item], [999])


def test_rehearsal_plan_takes_review_card_but_not_one_without_section(env):
    (item,) = cards(env, 1, status=Status.REVIEW)
    card = review.load_card(env.conn, item)
    steps, _ = rehearsal_plan(card, env.root)
    assert steps[0].kind == "photos"
    with env.conn:
        env.conn.execute("DELETE FROM blanks WHERE item_id = ? AND field = 'section'", (item,))
    with pytest.raises(PlanError, match="без розділу"):
        rehearsal_plan(review.load_card(env.conn, item), env.root)


# --- Кнопка в програмі ---


class Spawner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return Process()


class Process:
    def poll(self):
        return None


@pytest.fixture
def spawner():
    return Spawner()


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def app(conn, tmp_path, root, spawner, lock_file):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=lock_file,
    )  # fmt: skip


def approved(conn, root, folder="1342", post_id=1342):
    return add_card(conn, root, folder=folder, post_id=post_id, status=Status.APPROVED)


def test_button_starts_rehearsal_in_background(conn, root, app, spawner):
    approved(conn, root)
    assert "Пробно заповнити завтрашні" in html(get(app, "/review"))

    assert location(post(app, "/robot", action="rehearse", back="/review")).endswith(
        "msg=rehearse_started"
    )

    (args,) = spawner.calls
    assert args[0] == "--rehearse" and args[-2:] == ["--count", "5"]
    assert "--publish" not in args and "--fake-shafa" not in args
    assert "Пробна перевірка запускається" in html(get(app, "/review?msg=rehearse_started"))


def test_button_refused_while_publishing_nothing_changes(conn, root, app, spawner, lock_file):
    item = approved(conn, root)
    before = (count(conn, "sessions"), publish.started_today(conn, datetime.now().astimezone()))

    with lock.hold("publish", lock_file):  # йде публікація (інший процес)
        assert location(post(app, "/robot", action="rehearse", back="/review")).endswith(
            "msg=rehearse_busy"
        )
        page = html(get(app, "/review"))
        assert "Пробно заповнити завтрашні" not in page and "Робот публікує" in page
    assert "перевірка після неї" in html(get(app, "/review?msg=rehearse_busy"))

    assert spawner.calls == []
    assert (
        count(conn, "sessions"),
        publish.started_today(conn, datetime.now().astimezone()),
    ) == before
    assert state(conn, item) == (Status.APPROVED, None)


def test_panel_while_rehearsing_and_without_approved(conn, root, app, spawner, lock_file):
    assert location(post(app, "/robot", action="rehearse", back="/review")).endswith(
        "msg=rehearse_cannot"
    )  # Схвалених немає
    approved(conn, root)
    with lock.hold("rehearse", lock_file):
        page = html(get(app, "/review"))
        assert "Йде пробна перевірка" in page
        assert 'value="start"' not in page and "Пробно заповнити завтрашні" not in page
        assert location(post(app, "/robot", action="start", back="/review")).endswith(
            "msg=robot_cannot_start_busy"
        )
    assert spawner.calls == []


def test_button_not_available_in_fake_mode(conn, root, tmp_path, spawner, lock_file):
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "t", PORT, token=TOKEN,
                  spawner=spawner, lock_file=lock_file, fake_shafa=True)  # fmt: skip
    approved(conn, root)
    assert location(post(app, "/robot", action="rehearse", back="/review")).endswith(
        "msg=rehearse_fake"
    )
    assert spawner.calls == []


def test_bad_count_setting_refuses_button(conn, root, app, spawner, monkeypatch):
    approved(conn, root)
    monkeypatch.setenv("SHAFA_REHEARSE_COUNT", "99")
    assert location(post(app, "/robot", action="rehearse", back="/review")).endswith(
        "msg=rehearse_setup"
    )
    monkeypatch.setenv("SHAFA_REHEARSE_COUNT", "7")
    post(app, "/robot", action="rehearse", back="/review")
    assert spawner.calls[0][-2:] == ["--count", "7"]


def test_summary_and_problems_link_after_run(conn, root, app, env):
    approved(conn, root)
    report = rehearse.RehearsalReport(
        [rehearse.Outcome(1, 1, "ok"),
         rehearse.Outcome(2, 2, "error", "robot_field_missing", "x", moved=True)],
        "перевірено все", "done",
    )  # fmt: skip
    assert "Пробна перевірка" not in html(get(app, "/review"))

    rehearse.save(report, datetime.now().astimezone())

    page = html(get(app, "/review"))
    assert "(без публікації): 2 Картки — добре 1, у Проблемах 1 (robot_field_missing)" in page
    assert 'href="/problem"' in page


# --- Консоль ---


def parse(*extra, tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args(
        ["robot", "--db", str(tmp_path / "база.sqlite"), "--root", str(tmp_path), *extra]
    )
    return parser, args


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        (["--rehearse", "--sections", "--post", "1"], "--sections і --post разом не можна"),
        (["--rehearse", "--count", "0"], "--count від 1 до 30"),
        (["--rehearse", "--count", "31"], "--count від 1 до 30"),
        (["--rehearse", "--fake-shafa"], "--fake-shafa лише разом із --publish"),
        (["--dry-fill", "--post", "1", "--count", "3"], "лише з --rehearse"),
        (["--dry-fill", "--post", "1", "--sections"], "лише з --rehearse"),
    ],
)
def test_cli_refuses_wrong_combinations(tmp_path, conn, capsys, extra, why):
    parser, args = parse(*extra, tmp_path=tmp_path)
    with pytest.raises(SystemExit):
        cli.run_robot(parser, args)
    assert why in capsys.readouterr().err


def test_cli_rehearse_takes_its_own_lock_mode_and_default_count(tmp_path, conn, monkeypatch):
    seen = {}

    def fake(args, held, root, limit, day_limit):
        seen.update(mode=held.mode, count=args.count, session_limit=(limit, day_limit))
        return 0

    monkeypatch.setattr(cli, "_with_browser", fake)
    parser, args = parse("--rehearse", tmp_path=tmp_path)

    assert cli.run_robot(parser, args) == 0
    assert seen == {"mode": "rehearse", "count": 5, "session_limit": (0, 0)}
    monkeypatch.setenv("SHAFA_REHEARSE_COUNT", "9")
    parser, args = parse("--rehearse", tmp_path=tmp_path)
    cli.run_robot(parser, args)
    assert seen["count"] == 9


def test_cli_rehearse_refused_while_publishing(tmp_path, conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_with_browser", lambda *a: pytest.fail("браузер не мав стартувати"))
    parser, args = parse("--rehearse", tmp_path=tmp_path)
    with lock.hold("publish"):
        assert cli.run_robot(parser, args) == 2
    assert capsys.readouterr().out.strip()


def test_cli_empty_selection_opens_no_browser(tmp_path, conn, monkeypatch):
    _, args = parse("--rehearse", tmp_path=tmp_path)
    lines = []
    assert cli._rehearse_cards(args, tmp_path, lines.append) == []
    assert lines == ["Пробний прогін: немає Карток для перевірки"]


# --- Нічний запуск ---


class Night:
    def __init__(self, tmp_path, busy=False):
        self.now = datetime(2026, 10, 22, 2, 0).astimezone()
        self.calls, self.lines, self.is_busy = [], [], busy
        self.nightly = rehearse.Nightly(
            command=lambda n: ["--rehearse", "--count", str(n)],
            spawn=self.calls.append,
            busy=lambda: self.is_busy,
            log=self.lines.append,
            clock=lambda: self.now,
            folder=tmp_path,
        )

    def at(self, hour, minute=0, second=0, days=0):
        day = self.now.replace(hour=hour, minute=minute, second=second) + timedelta(days=days)
        self.now = day
        return self.nightly.tick()


def test_nightly_runs_once_at_the_time_and_again_next_day(tmp_path, monkeypatch):
    monkeypatch.setenv(rehearse.AT_SETTING, "03:00")
    night = Night(tmp_path)

    assert night.at(2, 59) is None
    assert night.at(3, 0, 10) == "started"
    assert night.at(3, 0, 40) is None and night.at(12) is None
    assert night.calls == [["--rehearse", "--count", "5"]]
    assert night.at(3, 0, 5, days=1) == "started"  # наступної доби — знову
    assert len(night.calls) == 2


def test_nightly_off_by_default_never_runs(tmp_path):
    night = Night(tmp_path)
    for hour in (2, 3, 4, 12):
        assert night.at(hour) is None
    assert night.calls == [] and night.lines == []


def test_nightly_not_repeated_after_app_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(rehearse.AT_SETTING, "03:00")
    first = Night(tmp_path)
    first.at(2, 59)
    assert first.at(3, 0, 5) == "started"
    second = Night(tmp_path)  # програму перезапустили о 03:02
    second.now = first.now
    assert second.at(3, 2) is None and second.calls == []


def test_nightly_after_sleep_is_skipped_with_log_line_no_catching_up(tmp_path, monkeypatch):
    monkeypatch.setenv(rehearse.AT_SETTING, "03:00")
    night = Night(tmp_path)
    night.at(2, 0)  # програма жива, потім ноутбук заснув
    assert night.at(8, 0) == "slept"
    assert night.calls == [] and "спав" in night.lines[0] and "03:00" in night.lines[0]
    assert night.at(8, 0, 30) is None and night.calls == []  # без доганяння


def test_nightly_app_opened_late_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv(rehearse.AT_SETTING, "03:00")
    night = Night(tmp_path)
    assert night.at(9, 0) is None  # першу перевірку зроблено вже після години
    assert night.calls == [] and night.lines == []


def test_nightly_busy_robot_skips_with_log_line(tmp_path, monkeypatch):
    monkeypatch.setenv(rehearse.AT_SETTING, "03:00")
    night = Night(tmp_path, busy=True)
    night.at(2, 59)
    assert night.at(3, 0, 10) == "busy"
    assert night.calls == [] and "робот зайнятий" in night.lines[0]
    night.is_busy = False
    assert night.at(3, 1) is None and night.calls == []  # не доганяє


def test_nightly_uses_count_setting_and_reports_wrong_values_once(tmp_path, monkeypatch):
    monkeypatch.setenv(rehearse.AT_SETTING, "03:00")
    monkeypatch.setenv(rehearse.COUNT_SETTING, "12")
    night = Night(tmp_path)
    night.at(2, 59)
    night.at(3, 0, 5)
    assert night.calls == [["--rehearse", "--count", "12"]]

    bad = Night(tmp_path / "інша")
    monkeypatch.setenv(rehearse.AT_SETTING, "3 години")
    bad.at(3)
    bad.at(4)
    assert bad.calls == [] and len(bad.lines) == 1 and "ГГ:ХХ" in bad.lines[0]


@pytest.mark.parametrize(
    ("raw", "ok"), [("03:00", True), ("3:05", True), ("23:59", True), ("24:00", False),
                    ("03:60", False), ("", False), ("ніч", False)]
)  # fmt: skip
def test_parse_at(raw, ok):
    assert (rehearse.parse_at(raw) is not None) is ok


def test_app_wires_nightly_to_the_rehearse_command(tmp_path, root, monkeypatch):
    monkeypatch.setenv("SHAFA_ROOT_DIR", str(root))
    night = program.nightly(tmp_path / "б.sqlite")
    assert night.command(7) == [
        "--rehearse",
        "--db",
        str(tmp_path / "б.sqlite"),
        "--root",
        str(root),
        "--count",
        "7",
    ]
    assert night.spawn is control.spawn and night.log is control.log_line


def test_log_line_writes_timestamped_line_to_robot_log():
    control.log_line("Пробний прогін о 03:00 запущено")
    text = control.log_path().read_text(encoding="utf-8")
    assert text.rstrip().endswith("Пробний прогін о 03:00 запущено") and text[:2] == "20"
    assert config.data_dir() in control.log_path().parents
