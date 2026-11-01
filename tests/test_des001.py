"""DES-001: тема світла/темна — theme.js, кнопка, CSP без вбудованих скриптів."""

import re

import pytest

from shafa import web
from test_review import add_card
from test_web import PORT, TOKEN, get, html


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


@pytest.fixture
def item_id(conn, root):
    return add_card(conn, root)


PAGES = ("/review", "/approved", "/problem", "/published", "/sessions", "/archived", "/settings")


def test_theme_js_is_served_as_javascript(app):
    response = get(app, "/static/theme.js")
    assert response.status == 200
    assert response.content_type.startswith("text/javascript")
    assert "shafa-theme" in response.body.decode("utf-8")


def test_theme_js_must_be_listed_in_static(app, monkeypatch):
    monkeypatch.delitem(web.STATIC, "theme.js")
    assert get(app, "/static/theme.js").status == 404


@pytest.mark.parametrize("path", PAGES)
def test_toggle_button_on_every_page(app, path):
    assert "data-theme-toggle" in html(get(app, path))


def test_toggle_button_on_card(app, item_id):
    page = html(get(app, f"/item/{item_id}"))
    assert page.count("data-theme-toggle") == 1
    top = page[page.index('class="top top-card"') : page.index("</header>")]
    assert top.index('id="robot"') < top.index("data-theme-toggle")


def test_theme_script_comes_right_after_stylesheet_without_defer(app):
    page = html(get(app, "/review"))
    assert (
        '<link rel="stylesheet" href="/static/style.css">\n<script src="/static/theme.js"></script>'
        in page
    )


@pytest.mark.parametrize("path", (*PAGES, "/nonexistent"))
def test_no_inline_scripts(app, item_id, path):
    response = get(app, path)
    page = response.body.decode("utf-8")
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert all(re.search(r'\bsrc="/static/[\w.-]+"', tag) for tag in scripts), scripts


def test_app_js_has_click_handler_for_toggle(app):
    js = get(app, "/static/app.js").body.decode("utf-8")
    assert "[data-theme-toggle]" in js and "shafa-theme" in js
