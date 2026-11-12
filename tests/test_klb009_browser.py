"""KLB-009: вкладки Клубку в справжньому Chromium — ноутбук і телефон, нічого не вилазить за край."""

import pytest

from shafa.db import Status
from shafa.klubok import queue
from test_klb002_queue import OTHER_PAIR, ad_row, add
from test_klb008 import klubok_card
from test_ui004 import chromium, live, lock_file, shoot  # noqa: F401


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def demo(conn, root):
    review_id = klubok_card(conn, root, 301, title="Черевики Ecco зимові 38 розміру з хутром")
    queued = add(conn, root, 7301)
    ad_row(conn, queued, queue.QUEUED)
    dup = add(conn, root, 7302, **OTHER_PAIR)
    ad_row(conn, dup, queue.PROBLEM, problem_code=queue.DUPLICATE, dup_name="Кеди Geox 41")
    published = add(conn, root, 7303, status=Status.PUBLISHED, brand="Rieker",
                    title="Туфлі Rieker 39 розміру")  # fmt: skip
    ad_row(conn, published, queue.PUBLISHED, url="https://klubok.com/item-x-5", klubok_id=5,
           published_at="2026-11-09T10:00:00+00:00")  # fmt: skip
    skipped = add(conn, root, 7304, brand="Clarks", title="Туфлі Clarks 40 розміру")
    ad_row(conn, skipped, queue.SKIPPED, problem_code="klubok_no_price")
    return review_id, dup


@pytest.mark.parametrize("width", [1400, 390])
def test_browser_klubok_tabs_and_card(conn, root, live, chromium, tmp_path, width):  # noqa: F811
    review_id, dup = demo(conn, root)
    page = chromium.new_page()
    try:
        base = f"http://127.0.0.1:{live.port}"
        paths = [("review", "/klubok/review"), ("queue", "/klubok/queue"),
                 ("decide", "/klubok/decide"), ("published", "/klubok/published"),
                 ("archive", "/klubok/archive"), ("item", f"/klubok/item/{review_id}"),
                 ("dup", f"/klubok/item/{dup}"), ("shafa", "/review")]  # fmt: skip
        for name, path in paths:
            shot = shoot(page, base, path, name, width, tmp_path)
            assert shot.stat().st_size > 5000, name
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
                name
            )
        # перемикач веде між сайтами, вкладка «Клубок» підсвічена
        page.goto(f"{base}/review")
        page.click("nav.sites >> text=Клубок")
        page.wait_for_url(f"{base}/klubok/review")
        assert page.locator("nav.sites .site.current").inner_text() == "Клубок"
        assert page.locator(".tabs .tab.current").inner_text().startswith("На перевірку")
        # «Схвалити і далі» → пара у «Схвалених» Клубку
        page.goto(f"{base}/klubok/item/{review_id}")
        page.click("button[value=approve_klubok]")
        page.wait_for_load_state("networkidle")
        state = conn.execute("SELECT state FROM klubok_ads WHERE item_id = ?", (review_id,))
        assert state.fetchone()[0] == queue.QUEUED
        page.goto(f"{base}/klubok/queue")
        assert page.locator(f"a.tilecard[href^='/klubok/item/{review_id}?']").count() == 1
    finally:
        page.close()
