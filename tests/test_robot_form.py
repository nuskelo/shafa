import pytest

from shafa.db import Status
from shafa.robot import browser, guard
from shafa.robot.form import FieldMissing, dry_fill, execute
from shafa.robot.plan import Step
from test_review import add_card, status
from test_robot_plan import BLANK, TEXT


class FakeDriver:
    def __init__(self, session, missing=None):
        self.calls = []
        self.missing = missing

    def _call(self, *args):
        if self.missing and self.missing in map(str, args):
            raise FieldMissing(self.missing)
        self.calls.append(args)

    def set_photos(self, paths):
        self._call("photos", len(paths))
        return len(paths) - (self.missing == "одне фото")

    def check_own_photos(self):
        self._call("own_photos")
        return self.missing != "галочка"

    def fill(self, name, value, label):
        self._call("fill", name, label)

    def choose_category(self, parts, slug):
        self._call("category", slug, *parts)

    def pick(self, label, values):
        self._call("pick", label, *values)

    def keywords(self, words):
        self._call("keywords", *words)

    def brand(self, name):
        self._call("brand", name)
        return name != "Невідомий бренд"


class FakePage:
    def __init__(self):
        self.shots = []

    def screenshot(self, path, full_page):
        self.shots.append(path)


class FakeSession:
    def __init__(self, logged_in=True, attempts=()):
        self.page = FakePage()
        self.logged_in = logged_in
        self.attempts = list(attempts)
        self.opened = 0

    def open_form(self):
        self.opened += 1
        return self.logged_in

    def publish_attempts(self):
        return self.attempts


def approved(conn, root, folder="1342", post_id=1342):
    return add_card(
        conn, root, blank=BLANK, text=TEXT, status=Status.APPROVED, folder=folder, post_id=post_id
    )


def run(conn, root, posts, session, driver=FakeDriver, tmp=None):
    lines = []
    shots = dry_fill(conn, root, posts, session, driver, tmp, log=lines.append)
    return shots, lines


def test_execute_maps_steps_to_driver():
    driver = FakeDriver(None)
    execute(
        [
            Step("photos", "Фото", ["a", "b"]),
            Step("own_photos", "Фото зроблені самостійно"),
            Step("text", "Назва", "Кеди", "titleUk"),
            Step("category", "Розділ", ["Жіночий одяг", "Взуття"], "zhenskaya-obuv"),
            Step("condition", "Стан", "Новий"),
            Step("sizes", "Розмір", ["38", "38.5"]),
            Step("keywords", "Ключові слова", ["кеди"]),
        ],
        driver,
    )
    assert driver.calls == [
        ("photos", 2),
        ("own_photos",),
        ("fill", "titleUk", "Назва"),
        ("category", "zhenskaya-obuv", "Жіночий одяг", "Взуття"),
        ("pick", "Стан", "Новий"),
        ("pick", "Розмір", "38", "38.5"),
        ("keywords", "кеди"),
    ]


def test_dry_fill_fills_screenshots_and_publishes_nothing(conn, root, tmp_path):
    item_id = approved(conn, root)
    session = FakeSession()

    shots, lines = run(conn, root, [1342], session, tmp=tmp_path / "екрани")

    assert len(shots) == 1 and shots[0].parent == tmp_path / "екрани"
    assert session.page.shots == [str(shots[0])]
    assert status(conn, item_id) == Status.APPROVED  # нічого не змінилось
    assert "форму заповнено, НЕ опубліковано" in lines[0]


def test_missing_field_stops_run_and_moves_card_to_problems(conn, root, tmp_path):
    first = approved(conn, root)
    second = approved(conn, root, folder="1349", post_id=1349)
    session = FakeSession()

    shots, lines = run(
        conn, root, [1342, 1349], session, lambda s: FakeDriver(s, "Сезон"), tmp_path
    )

    assert shots == []
    assert session.opened == 1  # друга Картка вже не відкривалась
    assert session.page.shots[0].endswith("-1342-нема-поля.png")
    row = conn.execute(
        "SELECT status, problem_code, problem_text FROM items WHERE id = ?", (first,)
    )
    assert tuple(row.fetchone()) == (
        Status.PROBLEM,
        "robot_field_missing",
        "Робот не знайшов на Шафі поле «Сезон»",
    )
    assert status(conn, second) == Status.APPROVED
    assert "не знайдено поле «Сезон»" in lines[0]


def test_no_login_stops_without_touching_cards(conn, root, tmp_path):
    item_id = approved(conn, root)
    shots, lines = run(conn, root, [1342], FakeSession(logged_in=False), tmp=tmp_path)
    assert shots == [] and "Немає входу" in lines[0] and "--login" in lines[0]
    assert status(conn, item_id) == Status.APPROVED


def test_publish_attempt_is_an_emergency_stop(conn, root, tmp_path):
    approved(conn, root)
    with pytest.raises(RuntimeError, match="CreateProduct"):
        run(conn, root, [1342], FakeSession(attempts=["мутація CreateProduct"]), tmp=tmp_path)


@pytest.mark.parametrize("posts", [[], [1, 2, 3, 4]])
def test_at_most_three_cards(conn, root, tmp_path, posts):
    with pytest.raises(ValueError, match="від 1 до 3"):
        run(conn, root, posts, FakeSession(), tmp=tmp_path)


def test_unknown_or_unready_cards_are_skipped(conn, root, tmp_path):
    add_card(conn, root, blank=BLANK, text=TEXT, status=Status.REVIEW)
    shots, lines = run(conn, root, [1342, 999], FakeSession(), tmp=tmp_path)
    assert shots == []
    assert lines == ["Пост 1342: Картка не схвалена", "Пост 999: Картки немає"]


# --- Захист усередині Session (без справжнього браузера) ---


class FakeLocator:
    def __init__(self, text, type_attr=None):
        self.info = {"text": text, "type": type_attr}
        self.clicked = False

    def evaluate(self, script):
        return self.info

    def click(self):
        self.clicked = True


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class FakeRequest:
    def __init__(self, method, url, body=b"", content_type="application/json"):
        self.method, self.url, self.post_data_buffer = method, url, body
        self.headers = {"content-type": content_type}


class FakeRoute:
    def __init__(self):
        self.result = None

    def continue_(self):
        self.result = "continue"

    def abort(self):
        self.result = "abort"


def bare_session(allow_upload=False):
    session = browser.Session.__new__(browser.Session)
    session.allow_upload, session.blocked, session.uploads = allow_upload, [], 0
    session.page = type("P", (), {"keyboard": FakeKeyboard()})()
    return session


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)


@pytest.mark.parametrize(
    ("text", "type_attr"), [("Додати річ", "button"), ("Кросівки", "submit"), ("Подарунок", None)]
)
def test_session_refuses_forbidden_click(text, type_attr):
    locator = FakeLocator(text, type_attr)
    with pytest.raises(guard.Forbidden):
        bare_session().click(locator)
    assert not locator.clicked


def test_session_allows_ordinary_click():
    locator = FakeLocator("Кросівки", "button")
    bare_session().click(locator)
    assert locator.clicked


def test_session_enter_only_in_keywords():
    session = bare_session()
    with pytest.raises(guard.Forbidden):
        session.press("Enter")
    session.press("Enter", in_keywords=True)
    session.press("Escape")
    assert session.page.keyboard.pressed == ["Enter", "Escape"]


def test_session_routes_requests_through_guard():
    session = bare_session()
    create = FakeRequest(
        "POST", "https://shafa.ua/api/v5/graphql",
        b'{"operationName": "CreateProduct", "query": "mutation CreateProduct { createProduct { id } }"}',
    )  # fmt: skip
    route = FakeRoute()
    session._route(route, create)
    assert route.result == "abort"
    assert session.publish_attempts() == ["мутація CreateProduct"]

    route = FakeRoute()
    session._route(route, FakeRequest("GET", "https://shafa.ua/uk/new"))
    assert route.result == "continue"


def test_keyword_suggestions_are_not_publish_attempts():
    session = bare_session()
    request = FakeRequest(
        "POST", "https://shafa.ua/api/v3/graphiql",
        b'{"operationName": "ProductFormKeywordSelect", "query": "mutation ProductFormKeywordSelect { a }"}',
    )  # fmt: skip
    session._route(FakeRoute(), request)
    assert session.blocked and session.publish_attempts() == []


def test_catalog_slug_of_form_request():
    request = FakeRequest(
        "POST", "https://shafa.ua/api/v3/graphiql-batch",
        b'[{"operationName": "WEB_ProductScore", "variables": {"slug": "x"}},'
        b' {"operationName": "WEB_ProductFormCatalog", "variables": {"catalogSlug": "obuv/aksessuary"}}]',
    )  # fmt: skip
    request.post_data = request.post_data_buffer.decode()
    assert browser.catalog_slug(request) == "obuv/aksessuary"
    request.post_data = '{"operationName": "WEB_HeaderUser"}'
    assert browser.catalog_slug(request) is None


def test_brand_not_found_on_shafa_is_noted():
    driver = FakeDriver(None)
    notes = execute(
        [Step("brand", "Бренд", "Ecco"), Step("brand", "Бренд", "Невідомий бренд")], driver
    )
    assert driver.calls == [("brand", "Ecco"), ("brand", "Невідомий бренд")]
    assert notes == ["Бренду «Невідомий бренд» немає на Шафі — не заповнено (OQ-004)"]


def test_missing_checkbox_and_photo_are_notes_not_stops():
    driver = FakeDriver(None)
    driver.missing = "галочка"
    notes = execute([Step("photos", "Фото", ["a", "b"]), Step("own_photos", "Фото")], driver)
    assert notes == ["Галочки «фото зроблені самостійно» у формі немає — не ставилась"]
    driver.missing = "одне фото"
    notes = execute([Step("photos", "Фото", ["a", "b"])], driver)
    assert notes == ["Фото у формі: 1 з 2 — частина не завантажилась"]
