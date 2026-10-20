"""ROBOT-003: списки на формі Шафи — уже вибране, чужі чипи, порожній список, ціна, діагностика."""

import itertools

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver, describe_form, execute, pick_values
from shafa.robot.plan import Step, plan
from test_robot_form import FakeDriver, bare_session
from test_robot_plan import approved

MATERIALS = ["Атлас", "Бавовна", "Велюр", "Гума", "Замша", "Текстиль", "Шкіра", "Штучна шкіра"]


class ShafaList:
    """Поле-список як на Шафі: вибране — чипи, вибраного немає серед варіантів («Доступних опцій
    немає»). `broken` — після набору варіантів немає взагалі (список ще не підвантажився)."""

    listed = True

    def __init__(self, chips=(), options=MATERIALS, broken=False, sticky=()):
        self._chips = list(chips)
        self.all = list(options)
        self.broken = broken
        self.sticky = list(sticky)  # чипи, які Шафа повертає сама після закриття списку
        self.typed, self.removed, self.chosen = [], [], []

    def chips(self):
        return list(self._chips)

    def remove(self, chip):
        self.removed.append(chip)
        self._chips.remove(chip)

    def press(self, value):
        return False

    def search(self, text):
        self.typed.append(text)
        if text and self.broken:
            return ["Доступних опцій немає"]
        shown = [o for o in self.all if o not in self._chips]
        found = [o for o in shown if text.casefold() in o.casefold()]
        return found or ["Доступних опцій немає"]

    def choose(self, option):
        self.chosen.append(option)
        self._chips.append(option)

    def close(self):
        self._chips += [c for c in self.sticky if c not in self._chips]

    def describe(self):
        return "у блоці: —"


def test_already_selected_value_is_success_without_typing():
    """Живий dry-fill 878: Шафа сама поставила «Шкіра», робот набирав її і бачив порожній список."""
    widget = ShafaList(chips=["Шкіра"])
    assert pick_values(widget, "Матеріал", ["Шкіра"]) == []
    assert widget.typed == [] and widget.chosen == [] and widget.chips() == ["Шкіра"]


def test_foreign_chip_is_removed_and_own_value_chosen():
    widget = ShafaList(chips=["24"], options=["23", "24", "24,5", "25"])
    notes = pick_values(widget, "Довжина устілки", ["24,5"])
    assert widget.removed == ["24"] and widget.chips() == ["24,5"]
    assert notes == ["Довжина устілки: Шафа поставила сама «24» — прибрано"]


def test_own_chip_stays_foreign_goes():
    widget = ShafaList(chips=["Шкіра", "Гума"])
    pick_values(widget, "Матеріал", ["Шкіра", "Замша"])
    assert widget.removed == ["Гума"] and widget.chips() == ["Шкіра", "Замша"]


def test_empty_list_after_typing_reads_full_list_and_takes_exact_match():
    widget = ShafaList(broken=True)
    pick_values(widget, "Матеріал", ["шкіра"])  # без регістру, як у бренді
    assert widget.typed == ["шкіра", ""]
    assert widget.chosen == ["Шкіра"]  # не «Штучна шкіра»


def test_not_in_list_is_field_missing_with_chips_and_first_30_options():
    options = [f"Варіант {i}" for i in range(40)]
    widget = ShafaList(chips=["Варіант 3"], options=options)
    with pytest.raises(FieldMissing, match="Матеріал: Шкіра") as error:
        pick_values(widget, "Матеріал", ["Варіант 3", "Шкіра"])
    details = error.value.details
    assert details.startswith("поле «Матеріал»; вибрано: Варіант 3; у списку (39): Варіант 0,")
    assert "Варіант 30" in details and "Варіант 31" not in details and "… ще 9" in details
    assert "Доступних опцій немає" not in details


def test_shafa_putting_value_back_is_field_missing():
    widget = ShafaList(chips=["Гума"], sticky=["Гума"])
    with pytest.raises(FieldMissing, match="на формі Шкіра, Гума, треба Шкіра"):
        pick_values(widget, "Матеріал", ["Шкіра"])


class Buttons:
    listed = False

    def __init__(self, names):
        self.names, self.pressed = names, []

    def chips(self):
        raise AssertionError("у полі з кнопками чипів не читаємо")

    def press(self, value):
        if value in self.names:
            self.pressed.append(value)
            return True
        return False

    def describe(self):
        return "у блоці: 37, 38"


def test_button_fields_work_as_before():
    widget = Buttons(["Європейський", "37"])
    assert pick_values(widget, "Розмір", ["37"]) == [] and widget.pressed == ["37"]
    with pytest.raises(FieldMissing, match="Розмір: 39") as error:
        pick_values(widget, "Розмір", ["39"])
    assert error.value.details == "у блоці: 37, 38"


# --- Ціна й тексти ---


class FakeField:
    def __init__(self, value="", stubborn=False):
        self.value, self.stubborn, self.fills = value, stubborn, []

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def fill(self, value):
        self.fills.append(value)
        if not self.stubborn:
            self.value = value

    def input_value(self):
        return self.value


class FormPage:
    """Сторінка для `PageDriver`: поля за `name`, блоки списків за підписом (чипи)."""

    def __init__(self, fields=None, blocks=None, sizes=()):
        self.fields = fields or {}
        self.blocks = blocks or {}
        self.sizes = list(sizes)
        self.marks = {}

    def locator(self, selector):
        name = selector.removeprefix("[name='").removesuffix("']")
        return self.fields.get(name, FakeField())

    def evaluate(self, script, arg=None):
        if script == form.FIND_BLOCK:
            labels, mark = arg
            label = next((x for x in labels if x in self.blocks), labels[0])
            self.marks[mark] = label
            return label in self.blocks
        if script == form.CHIPS:
            return list(self.blocks.get(self.marks.get(arg), []))
        if script == form.LIST_FIELDS:
            return {"block": [], "sizes": self.sizes}
        raise AssertionError(script[:40])


def driver_for(page):
    session = bare_session()
    session.page = page
    return PageDriver(session)


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)


def test_price_set_by_shafa_is_overwritten_and_checked():
    price = FakeField("150")
    PageDriver.fill(driver_for(FormPage({"price": price})), "price", "1200", "Ціна")
    assert price.value == "1200" and price.fills[0] == ""  # спершу очищено


def test_price_that_does_not_change_is_field_missing():
    page = FormPage({"price": FakeField("150", stubborn=True)})
    with pytest.raises(FieldMissing, match="Ціна: на формі 150, треба 1200"):
        driver_for(page).fill("price", "1200", "Ціна")


def test_price_with_spaces_on_form_is_the_same_price():
    assert form.same_text("1 200", "1200", price=True)
    assert not form.same_text("150", "1200", price=True)
    assert form.same_text("Кеди  Nike\n", "Кеди Nike")


def test_verify_catches_price_changed_after_filling():
    price = FakeField()
    driver = driver_for(FormPage({"price": price}))
    driver.fill("price", "1200", "Ціна")
    driver.verify()
    price.value = "150"  # Шафа переписала пізніше
    with pytest.raises(FieldMissing, match="Ціна: на формі 150, треба 1200"):
        driver.verify()


def test_verify_catches_list_changed_after_filling():
    page = FormPage(blocks={"Матеріал": ["Шкіра"]})
    driver = driver_for(page)
    driver.lists["Матеріал"] = ["Шкіра"]
    driver.verify()
    page.blocks["Матеріал"] = ["Шкіра", "Гума"]
    with pytest.raises(FieldMissing, match="Матеріал: на формі Шкіра, Гума, треба Шкіра"):
        driver.verify()


def test_execute_verifies_at_the_end():
    class Checked(FakeDriver):
        def verify(self):
            raise FieldMissing("Ціна: на формі 150, треба 1200")

    with pytest.raises(FieldMissing, match="Ціна"):
        execute([Step("text", "Ціна", "1200", "price")], Checked(None))


def test_untouched_fields_are_logged_not_touched():
    page = FormPage(blocks={"Довжина устілки": ["24"], "Принт": []})
    notes = driver_for(page).untouched(["Довжина устілки", "Принт", "Призначення"])
    assert notes == ["на формі Шафа поставила сама: Довжина устілки: 24"]


def test_execute_passes_untouched_notes_and_list_notes():
    class Noted(FakeDriver):
        def pick(self, label, values):
            return [f"{label}: Шафа поставила сама «Гума» — прибрано"]

        def untouched(self, labels):
            return [f"на формі Шафа поставила сама: {labels[0]}: 24"]

    notes = execute(
        [
            Step("choice", "Матеріал", ["Шкіра"]),
            Step("untouched", "Поля, яких робот не заповнює", ["Довжина устілки"]),
        ],
        Noted(None),
    )
    assert notes == [
        "Матеріал: Шафа поставила сама «Гума» — прибрано",
        "на формі Шафа поставила сама: Довжина устілки: 24",
    ]


def test_plan_lists_fields_robot_does_not_fill_before_price(conn, root):
    steps, _ = plan(approved(conn, root), root)
    untouched = steps[-2]
    assert untouched.kind == "untouched" and steps[-1].target == "price"
    filled = {s.label for s in steps if s.kind == "choice"}
    assert untouched.value and not filled & set(untouched.value)


# --- Діагностика ---


def test_keyword_with_word_size_is_not_a_field_label():
    page = FormPage(sizes=["Розмір", "кросівки 37 розмір", "Таблиця розмірів"])
    assert describe_form(page, typed={"кросівки 37 розмір"}) == (
        "підписи розміру: Розмір, Таблиця розмірів"
    )
    driver = driver_for(page)
    driver.keywords = PageDriver.keywords.__get__(driver)
    driver.typed.add("кросівки 37 розмір")
    with pytest.raises(FieldMissing) as error:
        driver.pick("Розмір", ["37"])
    assert "кросівки 37 розмір" not in error.value.details


# --- Очікування автозаповнення Шафи ---


class Note:
    def __init__(self, page):
        self.page = page

    def count(self):
        return int(self.page.now >= self.page.note_at)


class ClockPage:
    def __init__(self, note_at):
        self.now, self.note_at, self.waits = 0.0, note_at, 0

    def get_by_text(self, text):
        assert text is browser.AUTOFILL_TEXT
        return Note(self)

    def wait_for_timeout(self, ms):
        self.waits += 1
        self.now += ms / 1000


def clock_session(monkeypatch, note_at, requests=()):
    session = bare_session()
    session.page = ClockPage(note_at)
    requests = list(requests)

    def monotonic():
        while requests and requests[0] <= session.page.now:
            session.last_api = requests.pop(0)  # запит до API в цю мить
        return session.page.now

    monkeypatch.setattr(browser.time, "monotonic", monotonic)
    return session


def test_waits_for_autofill_message_and_quiet_network(monkeypatch):
    session = clock_session(monkeypatch, note_at=3.0, requests=[3.5, 4.0])
    assert session.wait_autofill() is True
    assert session.page.now >= 6.0  # повідомлення на 3 с, останній запит на 4 с + 2 с тиші


def test_no_message_waits_at_most_autofill_wait(monkeypatch):
    session = clock_session(monkeypatch, note_at=999)
    assert session.wait_autofill() is False
    assert browser.AUTOFILL_WAIT <= session.page.now < browser.AUTOFILL_WAIT + browser.QUIET + 1


def test_endless_requests_do_not_hang(monkeypatch):
    session = clock_session(monkeypatch, note_at=0, requests=[i * 0.5 for i in range(1, 400)])
    session.wait_autofill()
    assert session.page.now <= browser.SETTLE_LIMIT + 1


def test_route_remembers_api_requests(monkeypatch):
    from test_robot_form import FakeRequest, FakeRoute

    session = bare_session()
    ticks = itertools.count(10)
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(ticks))
    session._route(FakeRoute(), FakeRequest("GET", "https://shafa.ua/static/a.js"))
    assert session.last_api == 0.0
    session._route(FakeRoute(), FakeRequest("POST", "https://shafa.ua/api/v3/graphiql", b"{}"))
    assert session.last_api == 10


def test_category_choice_waits_for_autofill():
    class Session:
        page = type("P", (), {"wait_for_load_state": lambda self, state, timeout=None: None})()
        waited = False

        def choose_category(self, parts, slug):
            return {}

        def wait_autofill(self):
            Session.waited = True

    PageDriver(Session()).choose_category(["Жіночий одяг"], "zhenskaya-obuv")
    assert Session.waited
