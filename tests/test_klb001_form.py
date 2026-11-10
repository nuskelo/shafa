"""KLB-001: драйвер форми Клубку без браузера — вибір бренду, звірка, dry-fill, база лише для читання."""

import sqlite3
from typing import ClassVar

import pytest

from shafa import db, review
from shafa.db import Status
from shafa.klubok import catalog, fields
from shafa.robot import browser
from shafa.robot import klubok_form as form
from test_klb001_fields import make


def ad_for(tmp_path, **kw):
    values = {
        "item_id": 1,
        "name": "Черевики Ecco",
        "text": "Черевики Ecco.\n\nОпис.",
        "price": 1200,
        "leaf": catalog.leaf(1000637),
        "sex": None,
        "condition": 0,
        "sizes": (38, 39),
        "color": 7,
        "brand": "Ecco",
        "photos": (tmp_path / "a.jpg", tmp_path / "b.jpg"),
    }
    return fields.KlubokAd(**(values | kw))


def state_for(ad, **changes):
    item = {
        "item_name": ad.name,
        "text": ad.text,
        "price": ad.price,
        "sex": "" if ad.sex is None else ad.sex,
        "condition": ad.condition,
        "size": list(ad.sizes),
        "color": "" if ad.color is None else ad.color,
        "brand_name": (ad.brand or "") + " (Кирилиця)",
    }
    state = {
        "userItem": item,
        "categoryId": str(ad.leaf.id),
        "images": [p.name for p in ad.photos],
    }
    for key, value in changes.items():
        (item if key in item else state)[key] = value
    return state


# --- вибір підказки бренду ---


def test_pick_brand_prefers_cyrillic_suffix():
    options = ["Ecco Golf", "Ecco (Кирилиця)", "Eccolo"]
    assert form.pick_brand("Ecco", options) == "Ecco (Кирилиця)"
    assert form.pick_brand("ecco", options) == "Ecco (Кирилиця)"  # без урахування регістру


def test_pick_brand_first_that_starts_with_brand():
    assert form.pick_brand("Ecco", ["Ecco Golf", "Eccolo"]) == "Ecco Golf"


def test_pick_brand_none_when_no_option_starts_with_it():
    assert (
        form.pick_brand("Ecco", ["Nike (Кирилиця)", "MiO Ecco"]) is None
    )  # «починається», не «містить»
    assert form.pick_brand("Ecco", []) is None


# --- звірка formData ---


def test_verify_ok(tmp_path):
    ad = ad_for(tmp_path)
    assert form.verify_problems(ad, state_for(ad)) == []


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"item_name": "Інше"}, "назва"),
        ({"price": 1380}, "ціна"),
        ({"condition": 1}, "стан"),
        ({"size": [38]}, "розміри"),
        ({"color": 3}, "колір"),
        ({"brand_name": "Nike"}, "бренд"),
        ({"categoryId": "1000633"}, "категорія"),
        ({"images": ["b.jpg", "a.jpg"]}, "порядок фото"),
        ({"text": "Інший опис"}, "опис"),
        ({"sex": 1}, "стать"),  # у дорослого листка стать має лишитись порожньою
    ],
)
def test_verify_names_the_field(tmp_path, change, field):
    ad = ad_for(tmp_path)
    assert form.verify_problems(ad, state_for(ad, **change)) == [field]


def test_verify_sex_for_kids(tmp_path):
    ad = ad_for(tmp_path, leaf=catalog.leaf(1000041), sex=0, sizes=(30,))
    assert form.verify_problems(ad, state_for(ad)) == []
    assert form.verify_problems(ad, state_for(ad, sex=2)) == ["стать"]
    assert form.verify_problems(ad, state_for(ad, sex="")) == ["стать"]


def test_verify_text_ignores_crlf(tmp_path):
    ad = ad_for(tmp_path)
    assert form.verify_problems(ad, state_for(ad, text=ad.text.replace("\n", "\r\n"))) == []


def test_verify_free_text_brand_is_ok(tmp_path):
    ad = ad_for(tmp_path)
    assert form.verify_problems(ad, state_for(ad, brand_name="Ecco")) == []


def test_plan_lines_show_what_will_be_sent(tmp_path):
    text = "\n".join(form.plan_lines(ad_for(tmp_path)))
    assert "Категорія: Женское / Обувь / Зимняя обувь / Зимние ботинки (1000637)" in text
    assert "Ціна: 1200" in text
    assert "Розміри: 38, 39" in text
    assert "у дорослих немає" in text
    assert str(tmp_path) not in text  # шляхів до фото в консоль не виводимо


# --- база лише для читання ---


def test_open_readonly_cannot_write(conn, tmp_path):
    path = tmp_path / "база.sqlite"
    conn.commit()
    ro = form.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("INSERT INTO posts VALUES (1, 'x', 'x', 'export', 'x')")
    finally:
        ro.close()


def test_open_readonly_refuses_old_schema_without_migrating(tmp_path):
    path = tmp_path / "стара.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE t (a)")
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    raw.commit()
    raw.close()
    with pytest.raises(form.KlubokError) as error:
        form.open_readonly(path)
    assert error.value.code == "klubok_db"
    assert str(db.SCHEMA_VERSION - 1) in error.value.text
    after = sqlite3.connect(path)
    try:
        assert after.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
        assert [r[0] for r in after.execute("SELECT name FROM sqlite_master")] == ["t"]
    finally:
        after.close()


def test_open_readonly_missing_file_is_clear_error(tmp_path):
    with pytest.raises(form.KlubokError) as error:
        form.open_readonly(tmp_path / "немає.sqlite")
    assert error.value.code == "klubok_db"


# --- dry_fill: логіка без браузера ---


class FakeDriver:
    instances: ClassVar[list] = []

    def __init__(self, session):
        self.session, self.notes, self.calls = session, ["бренд"], []
        FakeDriver.instances.append(self)

    def fill(self, ad):
        self.calls.append(("fill", ad.name))

    def verify(self, ad):
        self.calls.append(("verify", ad.name))
        if self.session.fail:
            raise form.KlubokError("klubok_verify", "Форма Клубку не збігається з планом: ціна")


class FakePage:
    def __init__(self):
        self.shots = []

    def screenshot(self, path, full_page):
        self.shots.append(path)


class FakeSession:
    def __init__(self, state=browser.FORM, fail=False):
        self.state, self.fail, self.page, self.opens = state, fail, FakePage(), 0

    def open_form(self):
        self.opens += 1
        return browser.Opened(self.state, "http://127.0.0.1/item/add")


@pytest.fixture
def ro_card(conn, root, tmp_path):
    make(conn, root, status=Status.APPROVED)
    conn.commit()
    ro = form.open_readonly(tmp_path / "база.sqlite")
    yield ro
    ro.close()


def run(ro, root, tmp_path, session, posts=(1342,)):
    log = []
    FakeDriver.instances.clear()
    shots = form.dry_fill(
        ro, root, list(posts), session, FakeDriver, tmp_path / "screens", log.append
    )
    return shots, log


def test_dry_fill_fills_verifies_and_screenshots(ro_card, root, tmp_path):
    session = FakeSession()
    shots, log = run(ro_card, root, tmp_path, session)
    assert [c[0] for c in FakeDriver.instances[0].calls] == ["fill", "verify"]
    assert session.page.shots == [str(s) for s in shots] and len(shots) == 1
    assert shots[0].parent == tmp_path / "screens"
    assert any("НЕ опубліковано" in line for line in log)
    assert any("Категорія: Женское / Обувь / Зимняя обувь" in line for line in log)
    assert any("бренд" in line for line in log)  # нотатки драйвера


def test_dry_fill_card_missing(ro_card, root, tmp_path):
    session = FakeSession()
    shots, log = run(ro_card, root, tmp_path, session, posts=(9999,))
    assert shots == [] and session.opens == 0
    assert log == ["Пост 9999: Картки немає"]


def test_dry_fill_skip_does_not_open_form(conn, root, tmp_path):
    make(conn, root, section="Аксесуари / Ремені та пояси / Ремені", sizes=())
    conn.commit()
    ro = form.open_readonly(tmp_path / "база.sqlite")
    try:
        session = FakeSession()
        shots, log = run(ro, root, tmp_path, session)
    finally:
        ro.close()
    assert shots == [] and session.opens == 0
    assert "klubok_no_category" in log[0]


def test_dry_fill_verify_error_is_reported_and_no_screenshot(ro_card, root, tmp_path):
    session = FakeSession(fail=True)
    shots, log = run(ro_card, root, tmp_path, session)
    assert shots == [] and session.page.shots == []
    assert any("klubok_verify" in line and "ціна" in line for line in log)


def test_dry_fill_no_login_stops(ro_card, root, tmp_path):
    session = FakeSession(state=browser.NO_LOGIN)
    shots, log = run(ro_card, root, tmp_path, session)
    assert shots == [] and FakeDriver.instances == []
    assert any("немає входу" in line for line in log)


def test_dry_fill_card_limit(ro_card, root, tmp_path):
    with pytest.raises(ValueError, match="від 1 до 3"):
        form.dry_fill(ro_card, root, [1, 2, 3, 4], FakeSession(), FakeDriver, tmp_path)
    with pytest.raises(ValueError):
        form.dry_fill(ro_card, root, [], FakeSession(), FakeDriver, tmp_path)


def test_dry_fill_never_writes_to_db(conn, root, tmp_path):
    item_id = make(conn, root).id
    conn.commit()
    path = tmp_path / "база.sqlite"
    before = path.read_bytes()
    ro = form.open_readonly(path)
    try:
        run(ro, root, tmp_path, FakeSession())
    finally:
        ro.close()
    assert path.read_bytes() == before
    assert review.load_card(conn, item_id).status == Status.APPROVED


# --- CLI ---


def test_cli_live_klubok_is_refused(capsys):
    from shafa.__main__ import main

    with pytest.raises(SystemExit):
        main(["robot", "--klubok-dry-fill", "--db", "x", "--post", "1"])
    assert "Живий Клубок — з KLB-003" in capsys.readouterr().err


def test_cli_fake_klubok_needs_klubok_mode(capsys):
    from shafa.__main__ import main

    with pytest.raises(SystemExit):
        main(["robot", "--dry-fill", "--fake-klubok", "--db", "x", "--post", "1"])
    assert "--fake-klubok лише разом із --klubok-dry-fill" in capsys.readouterr().err


def test_cli_old_schema_stops_before_browser(root, tmp_path, capsys):
    from shafa.__main__ import main

    path = tmp_path / "стара.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    raw.commit()
    raw.close()
    code = main(
        ["robot", "--klubok-dry-fill", "--fake-klubok", "--db", str(path), "--root", str(root),
         "--post", "1"]
    )  # fmt: skip
    assert code == 2
    assert "спершу відкрийте базу програмою" in capsys.readouterr().out


def test_cli_post_limit(root, tmp_path, capsys):
    from shafa.__main__ import main

    with pytest.raises(SystemExit):
        main(
            ["robot", "--klubok-dry-fill", "--fake-klubok", "--db", "x", "--root", str(root)]
            + [a for n in (1, 2, 3, 4) for a in ("--post", str(n))]
        )
    assert "--post від 1 до 3" in capsys.readouterr().err
