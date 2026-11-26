"""KLB-015: «Спробувати ще раз» для Проблем Клубку до кліку (форма не заповнилась, запит не йшов)."""

import pytest

from shafa import events
from shafa.db import Status
from shafa.klubok import queue
from test_klb002_queue import OTHER_PAIR, ad_row, add, row
from test_klb009 import app, klubok_on, lock_file, spawner  # noqa: F401
from test_web import get, html, location, post

CODES = ("klubok_field_missing", "klubok_verify", "klubok_photos")


def test_before_click_codes_are_exactly_form_errors():
    assert queue.BEFORE_CLICK == CODES  # `klubok_uncertain`/`klubok_captcha` сюди не входять


def test_retry_form_only_before_click(conn, root, monkeypatch):
    said = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: said.append((kind, text)))
    form_error = add(conn, root, 7301)
    ad_row(conn, form_error, queue.PROBLEM, problem_code="klubok_field_missing",
           problem_text="У списку категорій немає «Детское»")  # fmt: skip
    others = {
        add(conn, root, 7302, **OTHER_PAIR): (queue.PROBLEM, queue.UNCERTAIN),
        add(conn, root, 7303, brand="Rieker", title="Туфлі Rieker 39 розміру"): (
            queue.PROBLEM, "klubok_captcha"),
        add(conn, root, 7304, brand="Clarks", title="Туфлі Clarks 40 розміру"): (
            queue.PROBLEM, queue.DUPLICATE),
        add(conn, root, 7305, brand="Lasocki", title="Туфлі Lasocki 37 розміру"): (
            queue.PUBLISHED, None),
        add(conn, root, 7306, brand="Ecco", title="Черевики Ecco 41 розміру"): (
            queue.QUEUED, None),
    }  # fmt: skip
    for item_id, (state, code) in others.items():
        ad_row(conn, item_id, state, problem_code=code)

    assert queue.retry_form(conn, form_error)
    assert row(conn, form_error)["state"] == queue.QUEUED
    assert row(conn, form_error)["problem_code"] is None
    assert said == [("klubok.retry", "Клубок: пост 7301 — помилка форми, повторюємо")]
    assert not queue.retry_form(conn, form_error)  # удруге — вже не Проблема
    for item_id, (state, code) in others.items():
        assert not queue.retry_form(conn, item_id)
        assert (row(conn, item_id)["state"], row(conn, item_id)["problem_code"]) == (state, code)


@pytest.mark.parametrize("code", CODES)
def test_all_form_codes_retry(conn, root, code):
    item_id = add(conn, root, 7301)
    ad_row(conn, item_id, queue.PROBLEM, problem_code=code, problem_text="x")
    assert queue.retry_form(conn, item_id)


def test_screen_button_and_post(conn, root, app, lock_file):  # noqa: F811
    from shafa.robot import lock

    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, item_id, queue.PROBLEM, problem_code="klubok_field_missing",
           problem_text="У списку категорій немає «Детское»")  # fmt: skip
    card = html(get(app, f"/klubok/item/{item_id}"))
    assert 'value="klubok_retry_form"' in card and "запит не йшов" in card
    decide = html(get(app, "/klubok/decide"))
    assert 'value="klubok_retry_form"' in decide  # кнопка просто в рядку «Проблем»

    with lock.hold("klubok-publish", lock_file):  # робот працює — нічого не міняємо
        busy = post(app, f"/klubok/item/{item_id}", action="klubok_retry_form")
    assert "msg=klubok_robot_busy" in location(busy)
    assert row(conn, item_id)["state"] == queue.PROBLEM

    response = post(app, f"/klubok/item/{item_id}", action="klubok_retry_form",
                    **{"from": "/klubok/decide", "stay": "list"})  # fmt: skip
    assert "msg=klubok_retry_form" in location(response)
    assert row(conn, item_id)["state"] == queue.QUEUED


def test_no_button_for_uncertain_and_post_refused(conn, root, app):  # noqa: F811
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, item_id, queue.PROBLEM, problem_code=queue.UNCERTAIN, problem_text="x")
    assert 'value="klubok_retry_form"' not in html(get(app, f"/klubok/item/{item_id}"))
    assert 'value="klubok_retry_form"' not in html(get(app, "/klubok/decide"))
    response = post(app, f"/klubok/item/{item_id}", action="klubok_retry_form")
    assert "msg=stale" in location(response)  # запит мав піти — лише через «перевірте»
    assert row(conn, item_id)["state"] == queue.PROBLEM


# --- UX-010: фото стерто — одна причина «немає фото», без «Спробувати ще раз» ---


def _erase_photos(root):
    for folder in root.iterdir():
        if folder.is_dir():
            for photo in folder.iterdir():
                photo.unlink()


def test_erased_photos_card_has_one_reason_and_no_retry(conn, root, app):  # noqa: F811
    from test_web import plain

    item_id = add(conn, root, 295, status=Status.PUBLISHED)
    ad_row(conn, item_id, queue.PROBLEM, problem_code="klubok_photos", problem_text="x")
    assert 'value="klubok_retry_form"' in html(get(app, f"/klubok/item/{item_id}"))  # фото є
    _erase_photos(root)

    page = html(get(app, f"/klubok/item/{item_id}"))
    text = plain(page)

    assert 'value="klubok_retry_form"' not in page and "Спробувати ще раз" not in page
    assert "Не піде на Клубок: немає фото" in text
    assert text.lower().count("немає фото") == 1  # одна причина, не дві
    assert "Проблема:" not in text and "не піде:" not in text  # другого рядка-причини немає
    assert "IMG_1.JPG" not in text  # без імен файлів у дужках
    decide = html(get(app, "/klubok/decide"))
    assert 'value="klubok_retry_form"' not in decide and "Відкрити Картку" in decide
    assert "Форма Клубку не заповнилась" not in page


def test_review_card_erased_photos_todo_is_just_no_photos(conn, root, app):  # noqa: F811
    from test_web import plain

    item_id = add(conn, root, 295, status=Status.APPROVED)
    _erase_photos(root)
    text = plain(html(get(app, f"/klubok/item/{item_id}")))
    assert text.count("фото вже стерто") == 0 and "немає фото" in text
    assert "IMG_1.JPG" not in text
