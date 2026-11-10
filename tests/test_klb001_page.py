"""KLB-001 у справжньому Chromium на підставному Клубку (без браузера Playwright — пропуск).

Живого klubok.com тут немає: сервер — `klubok_fake` на 127.0.0.1, guard — справжній `klubok_guard`.
"""

import pytest

from shafa.klubok import catalog
from shafa.klubok.fields import COLOR_MULTI, KlubokAd
from shafa.robot import klubok_fake as fake
from shafa.robot import klubok_form as form
from shafa.robot.klubok_guard import Forbidden


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import Error, sync_playwright

    playwright = sync_playwright().start()
    try:
        browser_ = playwright.chromium.launch()
    except Error:
        playwright.stop()
        pytest.skip("немає браузера Playwright")
    yield browser_
    browser_.close()
    playwright.stop()


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(form, "FIELD_WAIT", 1.0)
    monkeypatch.setattr(form, "BRAND_WAIT", 1.5)


@pytest.fixture
def server():
    srv = fake.Server(delay=(0.02, 0.4), seed=1)
    yield srv
    srv.close()


def session_for(chromium, server, scenario="ok"):
    return fake.FakeSession(None, server.base, scenario, context=chromium.new_context())


@pytest.fixture
def session(chromium, server):
    s = session_for(chromium, server)
    yield s
    s.close()


def photos(tmp_path, count):
    names = [f"IMG_{i}.jpg" for i in (3, 1, 2, 9, 8, 7, 6, 5, 4)][:count]  # порядок Картки
    for name in names:
        (tmp_path / name).write_bytes(b"\xff\xd8\xff\xe0jpeg")
    return tuple(tmp_path / n for n in names)


def make_ad(tmp_path, leaf_id=1000637, **kw):
    values = {
        "item_id": 1,
        "name": "Шкіряні черевики Ecco 38 розміру в ідеальному стані",
        "text": "Шкіряні черевики Ecco 38 розміру.\n\nНа хутрі.",
        "price": 1200,
        "leaf": catalog.leaf(leaf_id),
        "sex": None,
        "condition": 0,
        "sizes": (38, 39),
        "color": 7,
        "brand": "Ecco",
        "photos": photos(tmp_path, 3),
    }
    return KlubokAd(**(values | kw))


def opened(session):
    result = session.open_form()
    assert result.ok, result.state
    return form.KlubokDriver(session)


def test_fill_boots_and_verify(session, tmp_path):
    ad = make_ad(tmp_path)
    driver = opened(session)
    driver.fill(ad)
    driver.verify(ad)  # не кидає

    state = driver.state()
    assert state["categoryId"] == "1000637"
    assert state["userItem"]["size"] == [38, 39]
    assert state["userItem"]["brand_name"] == "Ecco (Кирилиця)"  # підказка, а не набраний текст
    assert state["images"] == [p.name for p in ad.photos]
    assert "вибрано підказку «Ecco (Кирилиця)»" in driver.notes[0]
    assert session.uploads == 3


def test_four_level_and_three_level_category(session, tmp_path):
    driver = opened(session)
    driver.choose_category(catalog.leaf(1000976))  # Мужское / Обувь / Туфли — лист 3-го рівня
    assert driver.state()["categoryId"] == "1000976"
    driver.choose_category(catalog.leaf(1000633))  # 4-й рівень: optgroup «Демисезонная обувь»
    assert driver.state()["categoryId"] == "1000633"
    driver.choose_category(catalog.leaf(1000637))  # той самий тип у іншій групі
    assert driver.state()["categoryId"] == "1000637"


def test_nine_photos_keep_card_order(chromium, tmp_path):
    srv = fake.Server(delay=(0.05, 0.9), seed=7)  # довші затримки: порядок відповідей розсипається
    s = session_for(chromium, srv)
    try:
        ad = make_ad(tmp_path, photos=photos(tmp_path, 9))
        driver = opened(s)
        driver.photos(list(ad.photos))
        assert driver.state()["images"] == [p.name for p in ad.photos]
        assert srv.uploaded == 9
    finally:
        s.close()
        srv.close()


def test_all_at_once_scrambles_order(chromium, tmp_path):
    """Контроль: підставний Клубок справді розсипає порядок, якщо вибрати всі файли одразу."""
    srv = fake.Server(delay=(0.05, 0.9), seed=7)
    s = session_for(chromium, srv)
    try:
        ad = make_ad(tmp_path, photos=photos(tmp_path, 9))
        opened(s)
        s.page.locator("input[type=file]").set_input_files([str(p) for p in ad.photos])
        s.page.wait_for_function("formData.images.length === 9", timeout=15_000)
        got = s.page.evaluate("formData.images.map(i => i.title)")
        assert sorted(got) == sorted(p.name for p in ad.photos)
        assert got != [p.name for p in ad.photos]
    finally:
        s.close()
        srv.close()


def test_sex_field_only_in_kids(session, tmp_path):
    driver = opened(session)
    kids = make_ad(tmp_path, 1000041, sex=0, sizes=(30,), color=COLOR_MULTI)
    driver.fill(kids)
    driver.verify(kids)
    assert driver.state()["userItem"]["sex"] == 0

    adult = make_ad(tmp_path, 1000637, sex=1)  # у дорослих поля статі на формі немає
    session.open_form()
    with pytest.raises(form.KlubokError) as error:
        form.KlubokDriver(session).fill(adult)
    assert error.value.code == "klubok_field_missing"
    assert "стать" in error.value.text


def test_bag_has_no_size_block(session, tmp_path):
    ad = make_ad(tmp_path, 1000288, sizes=(), color=None, brand=None)
    driver = opened(session)
    driver.fill(ad)
    driver.verify(ad)
    assert session.page.locator("#filter4").is_hidden()
    assert driver.state()["userItem"]["color"] == ""


def test_brand_without_suggestions_stays_free_text(chromium, server, tmp_path):
    s = session_for(chromium, server, "brand_none")
    try:
        ad = make_ad(tmp_path)
        driver = opened(s)
        driver.fill(ad)
        driver.verify(ad)
        assert driver.state()["userItem"]["brand_name"] == "Ecco"
        assert "підказок немає" in driver.notes[0]
    finally:
        s.close()


def test_login_scenario(chromium, server):
    s = session_for(chromium, server, "login")
    try:
        assert s.open_form().state == "login"
    finally:
        s.close()


def test_verify_catches_price_and_photo_order(session, tmp_path):
    ad = make_ad(tmp_path)
    driver = opened(session)
    driver.fill(ad)
    session.page.evaluate("formData.userItem.price = 999")
    with pytest.raises(form.KlubokError, match="ціна") as error:
        driver.verify(ad)
    assert error.value.code == "klubok_verify"
    session.page.evaluate("formData.userItem.price = 1200; formData.images.reverse()")
    with pytest.raises(form.KlubokError, match="порядок фото"):
        driver.verify(ad)


def test_submit_button_click_is_forbidden(session, server, tmp_path):
    opened(session)
    with pytest.raises(Forbidden):
        session.click(session.page.locator("#btn-submit"), "тест")
    assert server.submits == 0


@pytest.mark.parametrize("href", ["/renew/all", "/users/198135?pg=marksold&id=1"])
def test_trap_links_click_is_forbidden(session, server, href):
    opened(session)
    with pytest.raises(Forbidden):
        session.click(session.page.locator(f'a[href="{href}"]'), "тест")
    assert server.traps == []


def test_navigation_to_trap_never_reaches_server(session, server):
    opened(session)
    from playwright.sync_api import Error

    for path in ("/renew/all", "/users/198135?pg=renew-used&id=1", "/payment/1"):
        with pytest.raises(Error):
            session.page.goto(session.base + path)
    assert server.traps == []
    assert all("небезпечна" in why for url, why in session.blocked)
    assert len(session.blocked) == 3


def test_page_cannot_post_add_edit_used_or_leave_the_fake(session, server):
    opened(session)
    sent = session.page.evaluate(
        """async () => {
          const out = [];
          for (const [url, opt] of [
            ['/api/add-edit-used', {method: 'POST', body: '{}'}],
            ['https://example.com/x', {method: 'POST', body: 'x'}],
            ['https://example.com/x', {}],
          ]) {
            try { await fetch(url, opt); out.push('went'); } catch (e) { out.push('blocked'); }
          }
          return out;
        }"""
    )
    # GET чужого хосту дозволений guard-ом, але сесія пускає лише підставний Клубок
    assert sent == ["blocked", "blocked", "blocked"]
    assert server.submits == 0
    assert session.outside() == ["https://example.com/x", "https://example.com/x"]


def test_enter_key_is_forbidden(session):
    opened(session)
    with pytest.raises(Forbidden):
        session.press("Enter")


def test_fake_submit_flow_exists_but_is_only_reachable_by_server_scenario(server):
    """Сервер знає `error429` (для KLB-003), але робот KLB-001 до нього не доходить."""
    import json
    import urllib.request

    request = urllib.request.Request(
        server.base + "/api/add-edit-used?s=error429", data=b"{}", method="POST"
    )
    with urllib.request.urlopen(request) as response:
        assert json.loads(response.read())["status"] == "Error"
