"""UI-008: «На перевірку» і «Схвалені» — фільтр «Нові (бот) / Старі»."""

import pytest

from shafa import web
from shafa.db import Status
from test_review import status
from test_ui004 import ready
from test_ui005 import hidden_value, page_of, send_form
from test_web import HOST, PORT, TOKEN, get, html, location


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
                   lock_file=tmp_path / "robot.lock")  # fmt: skip


def build(conn, root, status_=Status.REVIEW):
    """Дві нові (бот) і одна стара."""
    new1 = ready(conn, root, "1342", 1342, status_)
    old = ready(conn, root, "borh-1343", 1343, status_)
    new2 = ready(conn, root, "1344", 1344, status_)
    return new1, old, new2


def links(page: str, *ids: int) -> list[bool]:
    return [f'href="/item/{i}"' in page for i in ids]


def test_kind_filters_cards(conn, root, app):
    new1, old, new2 = build(conn, root)
    assert links(html(get(app, "/review")), new1, old, new2) == [True] * 3
    assert links(html(get(app, "/review?k=new")), new1, old, new2) == [True, False, True]
    assert links(html(get(app, "/review?k=old")), new1, old, new2) == [False, True, False]
    assert links(html(get(app, "/review?k=дивне")), new1, old, new2) == [True] * 3


def test_kind_counts_and_current_pill(conn, root, app):
    build(conn, root)
    page = html(get(app, "/review?k=new"))
    assert "Усі · 3" in page and "Нові (бот) · 2" in page and "Старі · 1" in page
    assert 'class="pill on" href="/review?k=new" aria-current="true"' in page
    assert 'name="k" value="new"' in page  # пошук не губить групу


def test_kind_combines_with_f_and_q_and_counts_follow(conn, root, app):
    new1, old, new2 = build(conn, root)
    # новий жовтий: прибираємо ціну з тексту → мітка не «Готово»
    with conn:
        conn.execute("UPDATE posts SET text = 'без ціни' WHERE post_id = 1344")
    page = html(get(app, "/review?k=new&f=ok"))
    assert links(page, new1, old, new2) == [True, False, False]
    assert "Усі · 1" in page and "Нові (бот) · 1" in page and "Старі · 0" in page
    assert "Готові · 1" in page and "Треба глянути · 1" in page  # f-числа — у межах k=new
    assert "f=ok&amp;k=old" in page or "f=ok&k=old" in page  # пілюля несе решту параметрів
    # пошук
    page = html(get(app, "/review?k=new&q=1342"))
    assert links(page, new1, new2) == [True, False] and "Нові (бот) · 1" in page


def test_approved_tab_has_kind_group_without_warn(conn, root, app):
    new1, old, _ = build(conn, root, Status.APPROVED)
    page = html(get(app, "/approved?k=old"))
    assert links(page, new1, old) == [False, True]
    assert "Старі · 1" in page and "Треба глянути" not in page


def test_other_tabs_have_no_kind_group(conn, root, app):
    ready(conn, root, "1342", 1342, Status.PROBLEM)
    assert "Нові (бот)" not in html(get(app, "/problem"))


def test_new_badge_only_on_new_cards(conn, root, app):
    build(conn, root)
    page = html(get(app, "/review"))
    assert page.count("Нова · 2 фото") == 2


def test_navigation_and_approve_next_stay_inside_kind(conn, root, app):
    new1, old, new2 = build(conn, root)
    ref = f"http://{HOST}/review?k=new"
    page = page_of(app, new1, referer=ref)
    assert "1 з 2" in page
    assert hidden_value(page, "next") == f"/item/{new2}?from=%2Freview%3Fk%3Dnew"
    assert f"/item/{old}" not in page.split('rel="next"')[1][:80]
    response = send_form(app, page, new1, action="approve")
    assert location(response).startswith(f"/item/{new2}?from=%2Freview%3Fk%3Dnew")
    assert status(conn, new1) == Status.APPROVED and status(conn, old) == Status.REVIEW
    page = page_of(app, new2, f"/item/{new2}?from=%2Freview%3Fk%3Dnew")
    response = send_form(app, page, new2, action="approve")
    assert location(response) == "/review?k=new&msg=approved_done"
