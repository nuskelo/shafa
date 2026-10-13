"""Двигун публікації (ROBOT-002) без браузера й мережі: підробні сесія, драйвер і список товарів."""

from datetime import UTC, datetime, timedelta

import pytest

from shafa.db import Status
from shafa.folders import PUBLISHED_DIR
from shafa.robot import browser, guard, publish
from shafa.robot.form import BrandMissing, FieldMissing, PhotosShort, Submitted, outcome
from shafa.robot.listings import Listing, ListingsError, find_duplicate, parse_feed
from test_robot_form import FakeDriver, FakeLocator, FakePage

OK = Submitted(sent=True, product_id=555)


class PubSession(browser.Session):
    """Справжні квитки захисту (`arm_publish`, `click_publish`), підробна сторінка."""

    def __init__(self, logged_in=(True,), attempts=()):
        self._setup(allow_upload=True, publish=True)
        self.page = FakePage()
        self.logins = list(logged_in)
        self.opened = 0
        self.extra = list(attempts)

    def open_form(self):
        self.opened += 1
        return self.logins[min(self.opened, len(self.logins)) - 1]

    def publish_attempts(self):
        return super().publish_attempts() + self.extra


class PubDriver(FakeDriver):
    """Клік «Додати річ» іде крізь справжній квиток; відповіді Шафи — з черги `results`."""

    def __init__(self, session, conn, results=(), raises=None, on_submit=None):
        super().__init__(session)
        self.session, self.conn = session, conn
        self.results, self.raises, self.on_submit = list(results), raises, on_submit
        self.submits = []  # статус Картки в базі в мить кліку

    def submit(self):
        self.submits.append(
            self.conn.execute(
                "SELECT status FROM items WHERE id = ?", (self.session._click_ticket,)
            ).fetchone()[0]
        )
        self.session.click_publish(FakeLocator("Додати річ", "submit"))
        if self.on_submit:
            self.on_submit()
        if self.raises:
            raise self.raises
        return (
            self.results.pop(0)
            if self.results
            else Submitted(True, product_id=555 + len(self.submits))
        )


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)


class Env:
    def __init__(self, conn, root, tmp_path):
        self.conn, self.root, self.tmp = conn, root, tmp_path
        self.session = PubSession()
        self.driver = None
        self.listings: list[Listing] | Exception = []
        self.sleeps: list[float] = []
        self.lines: list[str] = []
        self.results: list = []
        self.raises = None
        self.on_submit = None
        self.clock_jump = 0.0
        self.today = datetime(2026, 10, 13, 12, 0).astimezone()

    def make_driver(self, session):
        self.driver = PubDriver(session, self.conn, self.results, self.raises, self.on_submit)
        return self.driver

    def load(self, session):
        if isinstance(self.listings, Exception):
            raise self.listings
        return self.listings

    def run(self, limit=30, day_limit=30):
        t = [0.0]

        def sleep(seconds):
            self.sleeps.append(seconds)
            t[0] += seconds + self.clock_jump

        return publish.Run(
            self.conn, self.root, self.session, self.make_driver, self.load,
            self.tmp / "екрани", log=self.lines.append, sleep=sleep, clock=lambda: t[0],
            rand=lambda a, b: (a + b) / 2, today=lambda: self.today,
        ).start(limit, day_limit)  # fmt: skip


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def add(env, folder="1342", post_id=1342, date="2025-03-01T10:00:00"):
    from test_review import add_card
    from test_robot_plan import BLANK, TEXT

    return add_card(
        env.conn, env.root, folder=folder, post_id=post_id, date=date, status=Status.APPROVED,
        blank=BLANK, text=TEXT,
    )  # fmt: skip


def row(conn, item_id):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def state(conn, item_id):
    r = row(conn, item_id)
    return r["status"], r["problem_code"]


# --- Успіх ---


def test_publishes_oldest_first_records_link_and_moves_folder(env):
    newer = add(env, "1400", 1400, "2025-05-01T10:00:00")
    older = add(env, "1342", 1342, "2025-03-01T10:00:00")

    report = env.run()

    first, second = row(env.conn, older), row(env.conn, newer)
    assert (first["status"], first["shafa_id"]) == (Status.PUBLISHED, 556)
    assert first["shafa_url"] == "https://shafa.ua/uk/item/556-shafa"
    assert (second["status"], second["shafa_id"]) == (Status.PUBLISHED, 557)
    assert first["published_at"] and first["session_id"] == report.session_id
    assert env.driver.submits == [Status.PUBLISHING, Status.PUBLISHING]  # запис до кліку
    assert [p for p, _ in report.published] == [1342, 1400]
    assert (env.root / PUBLISHED_DIR / "1342" / "IMG_1.JPG").is_file()
    assert not (env.root / "1342").exists()
    assert report.reason == "черга закінчилась"
    session = env.conn.execute("SELECT * FROM sessions").fetchone()
    assert (session["published"], session["problems"], session["stop_reason"]) == (
        2, 0, "черга закінчилась"
    )  # fmt: skip
    assert session["ended_at"]
    assert env.session.creates == 0 and env.session.publish_attempts() == []


def test_human_pause_between_published_items_only(env):
    add(env, "1342", 1342)
    add(env, "1400", 1400)
    env.run()
    assert sum(env.sleeps) == pytest.approx(sum(publish.PAUSE) / 2)  # одна пауза 1–3 хв


def test_second_run_does_not_publish_again(env):
    item = add(env)
    env.run()
    report = env.run()
    assert report.reason == "черга порожня" and report.published == []
    assert row(env.conn, item)["shafa_id"] == 556


# --- Невпевненість: жодного повтору ---


@pytest.mark.parametrize(
    "result",
    [Submitted(True), Submitted(True, failure="TimeoutError: 90000ms")],
)
def test_no_certain_answer_is_uncertain_and_stops(env, result):
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.results.append(result)

    report = env.run()

    assert state(env.conn, first) == (Status.PROBLEM, "publish_uncertain")
    assert "Перевір на Шафі" in row(env.conn, first)["problem_text"]
    assert state(env.conn, second) == (Status.APPROVED, None)
    assert len(env.driver.submits) == 1  # без повтору
    assert report.reason == "немає певної відповіді Шафи"


def test_uncertain_card_never_published_again(env):
    item = add(env)
    env.results.append(Submitted(True))
    env.run()
    report = env.run()
    assert report.reason == "черга порожня"
    assert state(env.conn, item) == (Status.PROBLEM, "publish_uncertain")


def test_errors_from_shafa_are_publish_error(env):
    item = add(env)
    env.results.append(Submitted(True, errors=["price: Ціна вище ринкової"]))
    report = env.run()
    assert state(env.conn, item) == (Status.PROBLEM, "publish_error")
    assert "Ціна вище ринкової" in row(env.conn, item)["problem_text"]
    assert report.reason == "Шафа не прийняла товар"


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (Submitted(False, blocked=True), "robot_publish_blocked"),
        (Submitted(False), "robot_not_sent"),
    ],
)
def test_not_sent_is_certain_and_stops(env, result, code):
    item = add(env)
    env.results.append(result)
    env.run()
    assert state(env.conn, item) == (Status.PROBLEM, code)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("Target closed")])
def test_interrupt_during_click_is_uncertain(env, error):
    item = add(env)
    env.raises = error

    with pytest.raises(type(error)):
        env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "publish_uncertain")
    session = env.conn.execute("SELECT ended_at, stop_reason FROM sessions").fetchone()
    assert session["ended_at"] and session["stop_reason"]


def test_crash_leftover_publishing_card_recovered_as_uncertain(env):
    item = add(env)
    with env.conn:
        env.conn.execute("UPDATE items SET status = 'publishing'")
        env.conn.execute("INSERT INTO sessions (started_at) VALUES ('2026-10-13T08:00:00+00:00')")

    env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "publish_uncertain")
    first = env.conn.execute("SELECT stop_reason FROM sessions WHERE id = 1").fetchone()[0]
    assert "обрив" in first
    assert env.driver is None or env.driver.submits == []


# --- Ліміт доби ---


def started(env, item, when):
    with env.conn:
        env.conn.execute(
            "UPDATE items SET status = 'published', publish_started_at = ? WHERE id = ?",
            (when.astimezone(UTC).isoformat(timespec="seconds"), item),
        )


def test_daily_limit_counts_earlier_runs(env):
    for n in range(3):
        started(env, add(env, str(n), n), env.today - timedelta(hours=1))
    waiting = [add(env, f"14{n}", 1400 + n) for n in range(3)]

    report = env.run(day_limit=4)

    assert len(report.published) == 1 and report.reason == "ліміт доби"
    assert [state(env.conn, i)[0] for i in waiting].count(Status.APPROVED) == 2


def test_daily_limit_is_per_local_calendar_day(env):
    env.today = datetime(2026, 10, 13, 0, 30).astimezone()
    started(env, add(env, "1", 1), datetime(2026, 10, 12, 23, 50).astimezone())  # учора
    add(env, "1400", 1400)
    assert publish.started_today(env.conn, env.today) == 0
    assert len(env.run(day_limit=1).published) == 1


def test_daily_limit_exhausted_touches_nothing(env):
    started(env, add(env, "1", 1), env.today)
    item = add(env, "1400", 1400)
    report = env.run(day_limit=1)
    assert report.reason == "ліміт доби (1) вичерпано"
    assert state(env.conn, item) == (Status.APPROVED, None)
    assert env.session.opened == 0


def test_run_limit(env):
    add(env, "1342", 1342)
    add(env, "1400", 1400)
    report = env.run(limit=1)
    assert len(report.published) == 1 and report.reason == "ліміт запуску"


def test_daily_limit_setting(monkeypatch):
    assert publish.daily_limit() == 30
    monkeypatch.setenv("SHAFA_DAILY_LIMIT", "12")
    assert publish.daily_limit() == 12
    monkeypatch.setenv("SHAFA_DAILY_LIMIT", "сто")
    with pytest.raises(publish.SetupError):
        publish.daily_limit()


# --- Дублі ---


def test_same_post_in_other_folder_is_not_published_again(env):
    """1342 уже опубліковано; з'явилась папка 23948278241342 того самого поста (D3)."""
    item = add(env, "1342", 1342)
    env.run()
    assert state(env.conn, item)[0] == Status.PUBLISHED
    with env.conn:
        twin = env.conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES ('23948278241342', 1342, 'approved', 'x', 'x')"
        ).lastrowid

    report = env.run()

    assert state(env.conn, twin) == (Status.PROBLEM, "possible_duplicate")
    assert "папка 1342" in row(env.conn, twin)["problem_text"]
    assert len(env.driver.submits) == 0 and report.published == []


SAME = Listing(1, "Шкіряні черевики ecco 38 розміру в ідеальному стані", "Ecco",
               "zhenskaya-obuv/botinki", ("38",), "https://shafa.ua/uk/item/1-x")  # fmt: skip


def test_similar_listing_on_shafa_is_possible_duplicate(env):
    item = add(env)
    other = add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.listings = [SAME]
    report = env.run()
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert SAME.url in row(env.conn, item)["problem_text"]
    assert state(env.conn, other) == (Status.PROBLEM, "possible_duplicate")  # той самий товар
    assert report.published == []


@pytest.mark.parametrize(
    ("change", "duplicate"),
    [
        ({}, True),
        ({"slug": "zhenskaya-obuv/krossovki"}, False),
        ({"sizes": ("39",)}, False),
        ({"sizes": ("37", "38")}, True),
        ({"brand": "Clarks"}, False),
        ({"brand": None}, False),
        ({"name": "Жіноча сумка ecco"}, False),
        ({"name": "ШКІРЯНІ черевики Ecco, 38 розмір"}, True),
    ],
)
def test_duplicate_rule(change, duplicate):
    listing = Listing(**{**SAME.__dict__, **change})
    found = find_duplicate(
        "Шкіряні черевики ecco 38 розміру", "zhenskaya-obuv/botinki", ["38", "38.5"], "Ecco",
        [listing],
    )  # fmt: skip
    assert (found is not None) is duplicate


def test_no_brand_and_no_size_match_each_other():
    bag = Listing(2, "Шкіряна сумка", None, "sumki", (), "u")
    assert find_duplicate("Шкіряна сумка", "sumki", [], None, [bag]) == bag


def test_unreadable_listings_stop_before_any_publication(env):
    item = add(env)
    env.listings = ListingsError("Список «ACTIVE»: прочитано 100 з 1272")
    report = env.run()
    assert "дублі не перевірити" in report.reason
    assert state(env.conn, item) == (Status.APPROVED, None)


def test_parse_feed():
    response = {"data": {"viewer": {"products": {
        "edges": [{"node": {
            "id": 7, "name": "Кеди", "url": "/uk/women/kedy/7-kedy", "brand": None,
            "catalogSlug": "zhenskaya-obuv/kedy",
            "sizes": [{"name": "38"}, {"name": "38.5"}],
        }}],
        "pageInfo": {"hasNextPage": True, "total": 40},
    }}}}  # fmt: skip
    listings, more, total = parse_feed(response)
    assert listings == [
        Listing(7, "Кеди", None, "zhenskaya-obuv/kedy", ("38", "38.5"), "https://shafa.ua/uk/women/kedy/7-kedy")
    ]  # fmt: skip
    assert (more, total) == (True, 40)


# --- Зупинки ---


def test_not_logged_in_touches_nothing(env):
    item = add(env)
    env.session.logins = [False]
    report = env.run()
    assert "немає входу" in report.reason
    assert state(env.conn, item) == (Status.APPROVED, None)


def test_logged_out_mid_run_is_problem_and_stop(env):
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.session.logins = [True, False]  # перевірка на старті, далі — злетіла
    report = env.run()
    assert state(env.conn, first) == (Status.PROBLEM, "robot_logged_out")
    assert state(env.conn, second) == (Status.APPROVED, None)
    assert report.reason == "немає входу на Шафу"


@pytest.mark.parametrize(
    ("missing", "code", "reason"),
    [
        ("Сезон", "robot_field_missing", "поле не знайдено — мабуть, Шафа змінила форму"),
        ("бренд", "robot_brand_missing", "бренд не знайдено"),
        ("фото", "robot_photos", "не всі фото завантажились"),
    ],
)
def test_form_trouble_is_problem_and_stop(env, monkeypatch, missing, code, reason):
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    errors = {
        "Сезон": FieldMissing("Сезон"),
        "бренд": BrandMissing("Ecco"),
        "фото": PhotosShort("1 з 2"),
    }

    def fail(steps, driver, strict):
        assert strict
        raise errors[missing]

    monkeypatch.setattr(publish, "execute", fail)
    report = env.run()
    assert state(env.conn, first) == (Status.PROBLEM, code)
    assert state(env.conn, second) == (Status.APPROVED, None)
    assert report.reason == reason
    assert env.driver.submits == []


def test_blocked_publish_attempt_before_click_is_emergency(env):
    add(env)
    env.session.extra = ["мутація WEB_CreateProduct"]
    with pytest.raises(RuntimeError, match="до кліку"):
        env.run()


def test_stop_waits_for_current_item(env):
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.on_submit = lambda: publish.request(env.conn, "stop")

    report = env.run()

    assert state(env.conn, first)[0] == Status.PUBLISHED  # поточний товар дописано
    assert state(env.conn, second) == (Status.APPROVED, None)
    assert report.reason == "Стоп"
    assert env.conn.execute("SELECT command FROM sessions").fetchone()[0] is None


def test_pause_holds_until_resumed(env):
    add(env, "1342", 1342)
    add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.on_submit = lambda: publish.request(env.conn, "pause")
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            publish.request(env.conn, None)  # «Продовжити»

    run = publish.Run(
        env.conn, env.root, env.session, env.make_driver, env.load, env.tmp,
        log=env.lines.append, sleep=sleep, clock=lambda: 0.0, rand=lambda a, b: 0.0,
        today=lambda: env.today,
    )  # fmt: skip
    report = run.start(30, 30)
    assert len(report.published) == 2
    assert waits[:3] == [publish.TICK] * 3


def test_laptop_sleep_during_pause_stops(env):
    add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    env.clock_jump = 3600.0
    report = env.run()
    assert report.reason == "ноутбук засинав — робот зупинився"
    assert state(env.conn, second) == (Status.APPROVED, None)


def test_request_without_running_session(conn):
    assert publish.request(conn, "stop") is False


def test_card_not_ready_is_problem_and_run_continues(env):
    first = add(env, "1342", 1342)
    second = add(env, "1400", 1400, "2025-05-01T10:00:00")
    for photo in (env.root / "1342").iterdir():
        photo.unlink()
    report = env.run()
    assert state(env.conn, first) == (Status.PROBLEM, "robot_not_ready")
    assert state(env.conn, second)[0] == Status.PUBLISHED
    assert len(report.published) == 1


# --- Тека «Опубліковано» ---


def test_published_folder_name_taken_is_note(env):
    add(env)
    (env.root / PUBLISHED_DIR / "1342").mkdir(parents=True)
    report = env.run()
    assert (env.root / "1342").is_dir()
    assert any("уже є папка 1342" in n for n in report.notes)


def test_published_folders_kept_unless_enabled(env, monkeypatch):
    item = add(env)
    env.run()
    with env.conn:
        env.conn.execute("UPDATE items SET published_at = '2026-10-01T10:00:00+00:00'")
    (env.root / PUBLISHED_DIR / "чуже").mkdir()

    assert publish.purge_published(env.conn, env.root, env.today) == []
    monkeypatch.setenv("SHAFA_DELETE_PUBLISHED", "1")
    assert publish.purge_published(env.conn, env.root, env.today) == ["1342"]

    assert not (env.root / PUBLISHED_DIR / "1342").exists()
    assert (env.root / PUBLISHED_DIR / "чуже").is_dir()  # не наша Картка — не чіпаємо
    assert state(env.conn, item)[0] == Status.PUBLISHED


def test_fresh_published_folder_not_deleted(env, monkeypatch):
    add(env)
    env.run()
    monkeypatch.setenv("SHAFA_DELETE_PUBLISHED", "1")
    assert publish.purge_published(env.conn, env.root, datetime.now().astimezone()) == []


# --- Відповідь Шафи ---


def test_outcome_success_only_with_id():
    body = {"data": {"createProduct": {"createdProduct": {"id": 221913242}, "errors": []}}}
    assert outcome(True, False, body) == Submitted(True, False, 221913242, [], None)


def test_outcome_errors_and_garbage():
    body = {"data": {"createProduct": {"createdProduct": None, "errors": [
        {"field": "price", "messages": [{"code": "x", "message": "Ціна вище ринкової"}]}
    ]}}}  # fmt: skip
    assert outcome(True, False, body).errors == ["price: Ціна вище ринкової"]
    assert outcome(True, False, body).product_id is None
    top = outcome(True, False, {"errors": [{"message": "Internal"}], "data": None})
    assert (top.product_id, top.failure) == (None, "Internal")
    assert outcome(True, False, None, "TimeoutError").product_id is None
    assert (
        outcome(
            True, False, {"data": {"createProduct": {"createdProduct": {"id": "x1"}}}}
        ).product_id
        is None
    )


def test_ticket_is_per_card(env):
    """Квиток видається Картці в `publishing` один раз; захист сам відмовить удруге."""
    add(env)
    env.run()
    with pytest.raises(guard.Forbidden):
        env.session.arm_publish(next(iter(env.session.armed)), Status.PUBLISHING)


def test_late_create_request_after_uncertain_click_is_blocked(env):
    """Квиток на запит гасне після кліку: запізнілий запит (після тайм-ауту) не піде на Шафу."""
    from shafa.robot.guard import CREATE_URL
    from test_robot_form import FakeRequest, FakeRoute
    from test_robot_publish_guard import create_body

    add(env)
    env.results.append(Submitted(True, failure="TimeoutError"))
    env.run()

    route = FakeRoute()
    env.session._route(route, FakeRequest("POST", CREATE_URL, create_body()))
    assert route.result == "abort" and env.session.creates == 0


def test_strict_execute_stops_on_missing_brand_and_photos():
    from shafa.robot.form import execute
    from shafa.robot.plan import Step

    driver = FakeDriver(None)
    with pytest.raises(BrandMissing, match="Невідомий бренд"):
        execute([Step("brand", "Бренд", "Невідомий бренд")], driver, strict=True)
    driver.missing = "одне фото"
    with pytest.raises(PhotosShort, match="1 з 2"):
        execute([Step("photos", "Фото", ["a", "b"])], driver, strict=True)
