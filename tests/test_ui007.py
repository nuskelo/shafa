"""UI-007: «← Назад» і «Далі →» у верхньому рядку Картки — лише посилання, статус не міняють."""

import re

import pytest

from shafa import web
from shafa.db import Status
from test_review import status
from test_ui004 import build_review
from test_ui005 import fetch, hidden_value, page_of, send_form
from test_web import HOST, TOKEN


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", 8765, token=TOKEN, lock_file=lock_file
    )


def step_hrefs(page: str) -> dict[str, str | None]:
    """Адреси «Назад»/«Далі» з верху Картки; None — неактивне (`span`, без адреси)."""
    out = {}
    for label, rel in (("Назад", "prev"), ("Далі", "next")):
        link = re.search(rf'<a class="step-link" rel="{rel}" href="([^"]*)">', page)
        out[label] = link[1].replace("&amp;", "&") if link else None
    return out


def test_next_matches_approve_and_does_not_change_status(conn, root, app):
    first, second, third = build_review(conn, root)
    page = page_of(app, first)
    assert (
        step_hrefs(page)["Далі"] == hidden_value(page, "next") == f"/item/{second}?from=%2Freview"
    )
    assert status(conn, first) == Status.REVIEW
    fetch(app, step_hrefs(page)["Далі"])  # перехід — лише GET
    assert {status(conn, i) for i in (first, second, third)} == {Status.REVIEW}


def test_back_goes_to_previous_and_ends_are_disabled(conn, root, app):
    first, second, third = build_review(conn, root)
    pages = {i: page_of(app, i, f"/item/{i}?from=%2Freview") for i in (first, second, third)}
    assert step_hrefs(pages[first]) == {"Назад": None, "Далі": f"/item/{second}?from=%2Freview"}
    assert step_hrefs(pages[second]) == {
        "Назад": f"/item/{first}?from=%2Freview",
        "Далі": f"/item/{third}?from=%2Freview",
    }
    assert step_hrefs(pages[third]) == {"Назад": f"/item/{second}?from=%2Freview", "Далі": None}
    assert pages[first].count('class="step-link off" aria-disabled="true"') == 1


def test_links_keep_the_list_and_filter_and_are_ours(conn, root, app):
    ids = build_review(conn, root)
    page = page_of(app, ids[1], referer=f"http://{HOST}/review")
    for href in step_hrefs(page).values():
        assert href and web.safe_next(href) == href  # лише /item/N?from=<наш список>


def test_no_steps_outside_a_numbered_list(conn, root, app):
    (first, *_) = build_review(conn, root)
    send_form(app, page_of(app, first), first, action="approve")
    page = page_of(app, first, f"/item/{first}?from=%2Fpublished")
    assert "step-link" not in page
