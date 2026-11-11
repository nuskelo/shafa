"""KLB-002 (D10): рядок «Клубок» на Картці, рішення про дубль, блок у Проблемах."""

import pytest

from shafa import web
from shafa.db import Status
from shafa.klubok import queue
from test_klb002_queue import OTHER_PAIR, TODAY, ad_row, add, row, snapshot
from test_web import PORT, TOKEN, get, html, location, post


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def count_rows(conn):
    return conn.execute("SELECT count(*) FROM klubok_ads").fetchone()[0]


def test_line_hidden_when_off_and_no_row(conn, app, root, monkeypatch):
    monkeypatch.delenv(queue.FLAG)
    item_id = add(conn, root, 7301)
    assert "Клубок:" not in html(get(app, f"/item/{item_id}"))


def test_queued_line_with_place_and_get_writes_nothing(conn, app, root):
    add(conn, root, 7301, season=("Зима",), **OTHER_PAIR)
    item_id = add(conn, root, 7302)

    page = html(get(app, f"/item/{item_id}"))

    # KLB-003 D7: рядка ще немає — «у черзі» без номера (місце рахується лише серед `queued`)
    assert "Клубок: у черзі" in page and "місце" not in page
    assert count_rows(conn) == 0  # GET нічого не пише

    queue.sync(conn, root, TODAY)
    assert "Клубок: у черзі · місце 2" in html(get(app, f"/item/{item_id}"))


def test_skipped_line_in_plain_words(conn, app, root):
    item_id = add(conn, root, 7301, text="Черевики Ecco 38 розміру")
    page = html(get(app, f"/item/{item_id}"))
    assert "Клубок: не піде: у пості немає ціни" in page
    assert "klubok_" not in page


def test_old_post_and_review_lines(conn, app, root):
    old = add(conn, root, 7100)
    review_ = add(conn, root, 7302, status=Status.REVIEW)
    assert "Клубок: не піде: пост старіший за 7200" in html(get(app, f"/item/{old}"))
    assert "Клубок: піде після схвалення" in html(get(app, f"/item/{review_}"))


def test_published_line_links_to_klubok_only(conn, app, root, monkeypatch):
    monkeypatch.delenv(queue.FLAG)  # рядок є — показуємо навіть з вимкненим Клубком
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, item_id, queue.PUBLISHED, url="https://klubok.com/item/5",
           published_at="2026-11-09T10:00:00+00:00")  # fmt: skip
    page = html(get(app, f"/item/{item_id}"))
    assert "Клубок: виставлено 09.11.2026" in page
    assert 'href="https://klubok.com/item/5"' in page

    with conn:
        conn.execute("UPDATE klubok_ads SET url = 'javascript:alert(1)'")
    assert "javascript:" not in html(get(app, f"/item/{item_id}"))


def test_duplicate_buttons_other_pair(conn, app, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])

    page = html(get(app, f"/item/{item_id}"))  # рядка ще немає — вердикт із plan
    assert "схоже, ця пара вже є на Клубку" in page
    assert "Черевики Ecco 38 розміру" in page and 'href="https://klubok.com/item/900"' in page
    assert 'value="klubok_dup_other"' in page and 'value="klubok_dup_yes"' in page

    response = post(app, f"/item/{item_id}", action="klubok_dup_other")
    assert "msg=klubok_dup_other" in location(response)
    assert row(conn, item_id)["state"] == queue.QUEUED and row(conn, item_id)["dup_ok_at"]
    assert "Клубок: у черзі" in html(get(app, f"/item/{item_id}"))

    again = post(app, f"/item/{item_id}", action="klubok_dup_other")  # уже вирішено
    assert "msg=stale" in location(again)


def test_duplicate_yes_button(conn, app, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])
    queue.sync(conn, root, TODAY)

    post(app, f"/item/{item_id}", action="klubok_dup_yes")

    assert (row(conn, item_id)["state"], row(conn, item_id)["problem_code"]) == (
        queue.SKIPPED, queue.DUP_YES
    )  # fmt: skip
    assert "не піде: ви сказали: це дубль" in html(get(app, f"/item/{item_id}"))


def test_problems_page_has_klubok_block_and_shafa_counter_unchanged(conn, app, root):
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, item_id, queue.PROBLEM, problem_code=queue.UNCERTAIN, problem_text="x")

    page = html(get(app, "/problem"))

    assert "Клубок: треба рішення" in page
    assert f'href="/item/{item_id}?from=' in page
    assert "перевірте на Клубку, чи опубліковано" in page
    assert web.review.counts(conn)[Status.PROBLEM] == 0  # лічильник Шафи не змінився
    other = add(conn, root, 7302)
    assert "Клубок: треба рішення" not in html(get(app, "/review")) and other
