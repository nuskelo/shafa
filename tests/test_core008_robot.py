"""CORE-008 C, D, F: сторінка реклами після публікації, діагностика поля розміру, колір дублів."""

from html.parser import HTMLParser

import pytest

from conftest import FIXTURES
from shafa.db import Status
from shafa.robot import browser, form, guard, publish
from shafa.robot.form import FieldMissing, PageDriver, describe_form
from shafa.robot.listings import Listing, find_duplicate
from test_robot_publish import OK, add, env, row, state  # noqa: F401 — фікстура env


@pytest.fixture(autouse=True)
def new_cards_allowed(monkeypatch):
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")
    monkeypatch.setattr(browser, "pause", lambda *a: None)


# --- C. Сторінка після «Додати річ» ---


class Clickables(HTMLParser):
    """Усе клікабельне на збереженій сторінці: текст і aria-label кнопок і посилань."""

    def __init__(self):
        super().__init__()
        self.items: list[tuple[str, str]] = []  # (тег, текст)
        self._tag, self._label, self._text = None, "", ""

    def handle_starttag(self, tag, attrs):
        if tag in ("button", "a"):
            self._tag, self._text = tag, ""
            self._label = dict(attrs).get("aria-label", "")

    def handle_data(self, data):
        if self._tag:
            self._text += data

    def handle_endtag(self, tag):
        if tag == self._tag:
            self.items.append((tag, f"{self._text} {self._label}".strip()))
            self._tag = None


def clickables():
    parser = Clickables()
    parser.feed((FIXTURES.parent / "fixtures" / "shafa-after-publish.html").read_text("utf-8"))
    return parser.items


def test_nothing_paid_on_the_ad_page_can_be_clicked():
    paid = [
        t for _, t in clickables() if any(w in t for w in ("Оплат", "Підняти", "Купити", "Реклам"))
    ]
    assert len(paid) == 4
    for text in paid:
        assert not guard.click_allowed(text, None), text


def test_ad_page_free_exits_are_not_publish_clicks():
    free = [t for _, t in clickables() if t.startswith(("Пізніше", "Додати ще"))]
    for text in free:
        assert not guard.publish_click_allowed(text, "button")


class AdPage:
    """Сторінка після публікації: навігація «висить», перехід на форму працює."""

    url = "https://shafa.ua/uk/promo/after-create?ref=x"

    def __init__(self, goto_error=None):
        self.visited, self.goto_error, self.shots = [], goto_error, []

    def goto(self, url, **kwargs):
        self.visited.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error

    def screenshot(self, path, full_page, timeout=None):
        self.shots.append(path)


def test_leave_result_page_goes_to_the_new_item_form_without_clicks():
    session = browser.Session.__new__(browser.Session)
    session.page = AdPage()

    session.leave_result_page()

    ((url, kwargs),) = session.page.visited
    assert url == browser.NEW_URL and kwargs["wait_until"] == "commit"
    assert kwargs["timeout"] == browser.LEAVE_WAIT * 1000


def test_leave_result_page_never_fails_the_run():
    session = browser.Session.__new__(browser.Session)
    session.page = AdPage(goto_error=TimeoutError("ad page is slow"))

    session.leave_result_page()  # без винятку


def test_published_card_leaves_ad_page_and_nothing_else_happens(env):  # noqa: F811
    item = add(env)
    env.session.page = AdPage()
    env.results.append(OK)

    env.run()

    assert state(env.conn, item) == (Status.PUBLISHED, None)
    assert [u for u, _ in env.session.page.visited] == [browser.NEW_URL]


def test_uncertain_after_click_logs_url_and_screenshot_and_asks_to_check(env):  # noqa: F811
    item = add(env)
    env.session.page = AdPage()
    env.results.append(form.Submitted(True, failure="TimeoutError: 60000ms"))

    env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "publish_uncertain")
    assert row(env.conn, item)["problem_text"].startswith("Перевірте на Шафі, чи опубліковано")
    log = next(line for line in env.lines if "немає певної відповіді" in line)
    assert env.session.page.url in log and "60 с" in log and ".png" in log
    assert env.session.page.visited == []  # тут нічого не тиснемо і нікуди не йдемо


def test_submit_timeout_is_sixty_seconds():
    assert form.SUBMIT_TIMEOUT == 60


class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Expect:
    """`page.expect_response`: як у Playwright — на виході без винятку чекає відповідь."""

    def __init__(self, response):
        self.response, self.waited = response, False
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.waited = True
            self.value = self.response
        return False


class SubmitPage:
    def __init__(self, response):
        self.expect = Expect(response)

    def locator(self, *args, **kwargs):
        return self

    def filter(self, **kwargs):
        return self

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def expect_response(self, predicate, timeout):
        self.timeout = timeout
        return self.expect


class ClickSession:
    def __init__(self, error=None):
        self.error, self.creates, self.blocked = error, 0, []

    def click_publish(self, button):
        self.creates += 1
        if self.error:
            raise self.error


def make_driver(response, error=None):
    driver = PageDriver.__new__(PageDriver)
    driver.page = SubmitPage(response)
    driver.session = ClickSession(error)
    return driver


CREATED = Response({"data": {"createProduct": {"createdProduct": {"id": "777"}}}})


def test_click_stuck_on_navigation_still_reads_created_product():
    # після кліку Шафа веде на сторінку реклами; клік завершується помилкою, а товар уже створено
    result = make_driver(CREATED, TimeoutError("waiting for scheduled navigations")).submit()

    assert result.product_id == 777 and result.sent


def test_forbidden_click_is_not_swallowed_and_does_not_wait_for_a_response():
    driver = make_driver(CREATED, guard.Forbidden("не кнопка публікації"))

    result = driver.submit()

    assert result.product_id is None and "Forbidden" in result.failure
    assert not driver.page.expect.waited  # відмови захисту ніхто не чекає 60 с


def test_response_timeout_without_answer_is_uncertain_not_created():
    class Never(Response):
        def json(self):
            raise TimeoutError("no response in 60s")

    result = make_driver(Never(None)).submit()

    assert result.product_id is None and "TimeoutError" in result.failure


# --- C. Повтор лише після пошуку за назвою у «Мої речі» ---


def uncertain_card(env, title="Уги UGG замшеві 40 розміру"):  # noqa: F811
    item = add(env)
    with env.conn:
        env.conn.execute("INSERT INTO edits VALUES (?, 'title', ?, 'x')", (item, f'"{title}"'))
        env.conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'publish_uncertain' WHERE id = ?",
            (item,),
        )
    return item


def snapshot(env, name, url="https://shafa.ua/uk/item/4242-x"):  # noqa: F811
    with env.conn:
        env.conn.execute(
            "INSERT INTO my_listings VALUES (4242, ?, 'UGG', 'x', '[]', ?, '[]', 1000, NULL, 'x')",
            (name, url),
        )


def test_retry_refused_when_snapshot_has_same_name(env):  # noqa: F811
    item = uncertain_card(env)
    snapshot(env, "  Уги  UGG замшеві 40 РОЗМІРУ ")

    assert publish.not_on_shafa(env.conn, item) == "listing_found"

    assert state(env.conn, item) == (Status.PROBLEM, "publish_uncertain")  # у чергу не повернено
    assert "https://shafa.ua/uk/item/4242-x" in row(env.conn, item)["problem_text"]


def test_retry_allowed_when_snapshot_has_no_such_name(env):  # noqa: F811
    item = uncertain_card(env)
    snapshot(env, "Зовсім інший товар")

    assert publish.not_on_shafa(env.conn, item) == "not_on_shafa"
    assert state(env.conn, item) == (Status.APPROVED, None)


# --- D. Немає поля Розмір: Європейський ---


class DiagPage:
    def __init__(self, found=None, error=None):
        self.found, self.error = found, error

    def evaluate(self, script, arg=None):
        if self.error:
            raise self.error
        return self.found


def test_describe_form_lists_block_buttons_and_size_labels():
    page = DiagPage({"block": ["Український", "US"], "sizes": ["Розмір", "Таблиця розмірів"]})

    text = describe_form(page, "b1")

    assert "у блоці: Український, US" in text
    assert "підписи розміру: Розмір, Таблиця розмірів" in text


def test_describe_form_says_so_when_nothing_about_size_found():
    assert "нічого зі словом «розмір»" in describe_form(DiagPage({"block": [], "sizes": []}))


def test_describe_form_never_hides_the_real_failure():
    assert describe_form(DiagPage(error=RuntimeError("page closed"))) == ""


def test_field_missing_keeps_label_and_details():
    error = FieldMissing("Розмір: Європейський", "у блоці: Український")

    assert str(error) == "Розмір: Європейський" and error.details == "у блоці: Український"
    assert FieldMissing("Колір").details == ""


class MissingDriver:
    """Драйвер, що не знаходить поле розміру й каже, що є на формі."""

    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        def call(*args, **kwargs):
            if name == "pick":
                raise FieldMissing("Розмір: Європейський", "у блоці: Український, US")
            return True if name in ("check_own_photos", "brand") else len(args[0]) if args else 0

        return call


def test_missing_size_field_logs_what_is_on_the_form_and_does_not_guess(env):  # noqa: F811
    item = add(env)
    env.make_driver = lambda session: MissingDriver(session)

    report = env.run()

    assert state(env.conn, item) == (Status.PROBLEM, "robot_field_missing")
    assert report.code == "done"  # ROBOT-010: Картка в Проблемах, сесія не зупиняється
    assert any(
        "Розмір: Європейський" in line and "на формі — у блоці: Український, US" in line
        for line in env.lines
    )
    assert any(p.endswith("-нема-поля.png") for p in env.session.page.shots)


# --- F. Колір не порівнюється (TD-034) ---

SLIPPERS = Listing(
    5, "Уги UGG замшеві 40 розміру", "UGG", "zhenskaya-obuv/uggi", ("40",),
    "https://shafa.ua/uk/item/5-x", colors=("Чорний",), price=1300,
)  # fmt: skip
SLUGS = frozenset({"zhenskaya-obuv/uggi"})


@pytest.mark.parametrize("theirs", [(), ("Чорний",), ("Коричневий",)])
@pytest.mark.parametrize("ours", [[], ["Чорний"], ["Білий"]])
def test_color_neither_makes_nor_breaks_a_duplicate(ours, theirs):
    listing = Listing(**{**SLIPPERS.__dict__, "colors": theirs})

    found = find_duplicate(
        "Уги UGG замшеві 40 розміру", SLUGS, ["40"], "UGG", [listing], ours, 1300
    )

    assert found is listing


def test_price_still_decides():
    listing = Listing(**{**SLIPPERS.__dict__, "price": 500})

    assert (
        find_duplicate("Уги UGG замшеві 40 розміру", SLUGS, ["40"], "UGG", [listing], [], 1300)
        is None
    )


# --- E. Запуск на Mac із власною текою даних ---


def test_data_dir_expands_tilde(monkeypatch):
    monkeypatch.setenv("SHAFA_DATA_DIR", "~/shafa-viktoria")

    from shafa import config

    assert config.data_dir() == config.Path.home() / "shafa-viktoria"


def test_everything_lives_in_the_chosen_data_dir(monkeypatch, tmp_path):
    """База, профіль Chrome робота, замки, журнали й `.env` — лише в `SHAFA_DATA_DIR`."""
    from shafa import app, config
    from shafa.robot import control

    data = tmp_path / "shafa-viktoria"
    monkeypatch.setenv("SHAFA_DATA_DIR", str(data))
    monkeypatch.setattr(config, "ENV_FILE", None)  # як у встановленій програмі

    assert app.db_path() == data / "shafa.sqlite"
    assert browser.profile_dir() == data / "robot-profile"
    assert control.log_path().parent == data
    assert config.env_file().parent == data


def test_robot_and_bot_children_inherit_the_data_dir(monkeypatch, tmp_path):
    import subprocess

    from shafa.robot import control

    seen = []

    class Popen:
        def __init__(self, command, **kwargs):
            seen.append((command, kwargs["env"]))

    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path))

    control.spawn([], tmp_path / "r.log")
    control.spawn([], tmp_path / "b.log", program="bot")

    assert [c[3] for c, _ in seen] == ["robot", "bot"]
    assert all(e["SHAFA_DATA_DIR"] == str(tmp_path) for _, e in seen)


def test_tests_never_see_the_real_data_dir_or_env(monkeypatch):
    from shafa import config

    monkeypatch.delenv("SHAFA_DATA_DIR")

    with pytest.raises(config.RealPathInTests):
        config.data_dir()
