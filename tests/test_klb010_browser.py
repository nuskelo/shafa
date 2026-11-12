"""KLB-010: Клубок = Шафа в справжньому Chromium — шапка в один рядок, назви вкладок, перший екран
Картки, без горизонтальної прокрутки на телефоні."""

import re

import pytest

from shafa.db import Status
from shafa.klubok import queue
from test_klb002_queue import OTHER_PAIR, ad_row, add
from test_klb008 import klubok_card
from test_ui004 import chromium, live, lock_file  # noqa: F401

SHAFA_TABS = ["На перевірку", "Схвалені", "Проблеми", "Опубліковані"]
TOP = """() => {
  const top = document.querySelector('header.top .top-in');
  const items = [...top.children].filter(e => e.getBoundingClientRect().height > 0);
  const rows = new Set(items.map(e => Math.round(e.getBoundingClientRect().top / 20)));
  return {rows: rows.size, height: Math.round(top.getBoundingClientRect().height)};
}"""


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def demo(conn, root):
    for n in range(4):  # «Ще не заповнені ШІ» й великі лічильники розпирали шапку Шафи
        add(conn, root, 7400 + n, status=Status.NEW)
    review_id = klubok_card(conn, root, 301, title="Черевики Ecco зимові 38 розміру з хутром")
    queued = add(conn, root, 7301)
    ad_row(conn, queued, queue.QUEUED)
    dup = add(conn, root, 7302, **OTHER_PAIR)
    ad_row(conn, dup, queue.PROBLEM, problem_code=queue.DUPLICATE, dup_name="Кеди Geox 41")
    return review_id


def tab_names(page):
    texts = page.locator("nav.tabs a.tab").all_inner_texts()
    return [re.sub(r"\s*\d+\s*$", "", t).strip() for t in texts]


@pytest.mark.parametrize("width", [1440, 1024])
def test_header_is_one_row_on_both_sites(conn, root, live, chromium, width):  # noqa: F811
    demo(conn, root)
    live.on_quit = list  # «Вийти» — як у програми з ярлика
    page = chromium.new_page(viewport={"width": width, "height": 900})
    try:
        base = f"http://127.0.0.1:{live.port}"
        for path in ("/review", "/klubok/review", "/klubok/decide"):
            page.goto(base + path)
            top = page.evaluate(TOP)
            assert top["rows"] == 1 and top["height"] < 80, (path, top)
            assert page.locator("form.quit button").is_visible()
    finally:
        page.close()


def test_klubok_tab_names_are_shafa_names(conn, root, live, chromium):  # noqa: F811
    demo(conn, root)
    page = chromium.new_page(viewport={"width": 1440, "height": 900})
    try:
        base = f"http://127.0.0.1:{live.port}"
        page.goto(base + "/review")
        shafa = tab_names(page)
        page.goto(base + "/klubok/review")
        assert tab_names(page) == shafa == SHAFA_TABS
        page.click("details.more > summary")
        menu = page.locator("details.more .menu a").all_inner_texts()
        assert [m.split("\n")[0].split(" ")[0] for m in menu] == ["Сесії", "Архів", "Налаштування"]
        # лічильник Схвалених — одне число, без «+N»
        assert "+" not in page.locator(".sp-stats").inner_text()
    finally:
        page.close()


def test_klubok_card_photo_and_fields_on_first_screen(conn, root, live, chromium):  # noqa: F811
    review_id = demo(conn, root)
    page = chromium.new_page(viewport={"width": 1440, "height": 900})
    try:
        page.goto(f"http://127.0.0.1:{live.port}/klubok/item/{review_id}")
        photo = page.locator(".gallery").bounding_box()
        field = page.locator(".right .field").first.bounding_box()
        assert photo["y"] < 300 and field["y"] + 60 < 900, (photo, field)
        assert page.locator(".kad").is_visible()  # «Як буде на Клубку» — над полями
        assert (
            page.locator("button[value=approve_klubok]")
            .inner_text()
            .strip()
            .endswith("Схвалити і далі")
        )
    finally:
        page.close()


@pytest.mark.parametrize("path", ["/klubok/review", "/klubok/queue", "/klubok/decide",
                                  "/klubok/archive", "/klubok/sessions", "item", "/nope"])  # fmt: skip
def test_phone_has_no_horizontal_scroll_and_card_top_is_two_rows(conn, root, live, chromium, path):  # noqa: F811
    review_id = demo(conn, root)
    page = chromium.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
    try:
        url = f"/klubok/item/{review_id}" if path == "item" else path
        page.goto(f"http://127.0.0.1:{live.port}{url}")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        if path == "item":
            assert page.evaluate(TOP)["height"] < 120  # F8: 2 рядки (було 3)
            assert page.locator(".kad").get_attribute("open") is None  # F5: згорнуто
    finally:
        page.close()


def test_broken_photo_stub_does_not_cover_the_card(conn, root, live, chromium):  # noqa: F811
    """N6: зламане фото → «немає фото» (без зламаної картинки), а кнопки Картки клікаються."""
    item_id = add(conn, root, 7501, status=Status.REVIEW)
    (root / "7501" / "IMG_2.JPG").write_bytes(b"")  # порожній файл — мініатюра не відкриється
    with conn:
        conn.execute("INSERT INTO photos VALUES (?, 2, 'IMG_2.JPG')", (item_id,))
    page = chromium.new_page(viewport={"width": 1400, "height": 900})
    try:
        page.goto(f"http://127.0.0.1:{live.port}/item/{item_id}")
        page.wait_for_load_state("networkidle")
        assert page.locator(".noshot-inline").count() == 1
        assert page.evaluate("[...document.images].every(i => !i.src || i.naturalWidth > 0)")
        chip = page.locator(".right button.chip").first  # як «мавпа»: «Новий» у полі «Стан»
        chip.scroll_into_view_if_needed()
        box = chip.bounding_box()
        hit = page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.closest('button')?.textContent",
            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
        )
        assert hit == chip.text_content()
        chip.click(timeout=3000)
    finally:
        page.close()


def test_long_unbroken_title_does_not_scroll_card_sideways(conn, root, live, chromium):  # noqa: F811
    """Знахідка мавпи: назва з 400 літер без пробілів розпирала Картку на 390 (обидва сайти)."""
    shafa = add(conn, root, 7601, status=Status.REVIEW, title="а" * 400)
    only = klubok_card(conn, root, 302, title="б" * 400)
    page = chromium.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
    try:
        for path in (f"/item/{shafa}", f"/klubok/item/{only}"):
            page.goto(f"http://127.0.0.1:{live.port}{path}")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    finally:
        page.close()
