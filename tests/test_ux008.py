"""UX-008: дрібні правки з повторного аудиту (R-A12, A15, A19, B11–B15, B17, B19–B22)."""

from pathlib import Path

import pytest

from shafa import batch, events, health, review, web
from shafa.db import Status
from test_review import card, form_of
from test_ui004 import ready
from test_ux004 import problem
from test_web import PORT, TOKEN, get, html, location, post


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def test_finished_batch_counts_what_ai_filled_not_current_status(conn, root):  # R-A12
    item = ready(conn, root)
    with conn:
        conn.execute(
            "INSERT INTO ai_batches (id, status, created_at, ended_at) "
            "VALUES (1, 'ended', 'x', '2026-11-01T10:00:00+00:00')"
        )
        conn.execute("INSERT INTO ai_batch_items VALUES (1, ?, 0, 'done')", (item,))
        conn.execute("UPDATE items SET status = 'problem' WHERE id = ?", (item,))
    done = batch.last_finished(conn)
    assert (done.ok, done.problems) == (1, 0)  # пізніша зміна статусу підсумок не міняє


def test_cards_accusative_and_returned_text():  # R-B19
    assert events.cards_acc(1) == "1 Картку" and events.cards_acc(5) == "5 Карток"
    assert "1 Картку — вона знову" in batch.collect_problem_text(batch.Collected(returned=1))
    assert "3 Картки — вони знову" in batch.collect_problem_text(batch.Collected(returned=3))


def test_settings_file_input_has_own_label(app):  # R-A15
    page = html(get(app, "/settings"))
    assert 'for="export-file"' in page and "Вибрати файл result.json" in page


def test_health_rows_do_not_repeat_name():  # R-A19
    # рядок самоперевірки на панелі — «<назва>: <текст>», тож у тексті назви вже немає
    src = Path(health.__file__).read_text(encoding="utf-8")
    assert '"Бот не підключений"' not in src and '"Сайт не підключений"' not in src


def test_empty_bot_list_says_what_is_in_old(conn, root, app):  # R-B11
    ready(conn, root, "borh-1343", 1343)
    page = html(get(app, "/review?k=new"))
    assert "З бота на перевірці нічого немає. Є 1 Картка зі старих постів." in page
    ready(conn, root, "1342", 1342)
    ready(conn, root, "1344", 1344)
    page = html(get(app, "/review?k=old"))  # стара є — повідомлення нема
    assert "нічого немає. Є" not in page


def test_empty_old_list_says_what_is_in_bot(conn, root, app):
    ready(conn, root, "1342", 1342)
    page = html(get(app, "/review?k=old"))
    assert "Зі старих постів на перевірці нічого немає. Є 1 Картка з бота." in page


def test_search_without_result_offers_reset(conn, root, app):  # R-B12
    ready(conn, root)
    page = html(get(app, "/review?q=нема-такого"))
    assert "Нічого не знайдено." in page and "На перевірці ще 1 Картка —" in page
    assert "«Скинути» пошук" in page


def test_review_texts_use_polite_form():  # R-B13
    texts = [q for _, q in review.GENDER_PAIRS]
    assert all(t.startswith("Перевірте") for t in texts)


def test_teen_choice_names_both_values(conn, root, app):  # R-B14
    c = card(conn, ready(conn, root))
    review.save(conn, c, form_of(c, section="Дитячий одяг / Для дівчаток / Взуття / Кросівки"))
    page = html(get(app, f"/item/{c.id}"))
    assert "так — для підлітків" in page and "ні — для дітей" in page


def test_restore_after_reject_opens_card(conn, root, app):  # R-B15
    item = ready(conn, root)
    post(app, f"/item/{item}", action="reject", **{"from": "/review"})
    response = post(app, f"/item/{item}", action="restore", **{"from": "/review"})
    assert location(response).startswith(f"/item/{item}?")
    assert "msg=restored" in location(response)


def test_published_intro(app):  # R-B17
    page = html(get(app, "/published"))
    assert "Ці Картки вже є на Шафі — опубліковані роботом або вами вручну." in page


def test_problem_labels(conn, root, app):  # R-B20, R-B21
    problem(conn, root, "no_photos")
    problem(conn, root, "robot_photos", folder="1343", post_id=1343)
    problem(conn, root, "possible_duplicate", folder="1344", post_id=1344)
    page = html(get(app, "/problem"))
    assert "Нема фото в папці" in page and "Фото не завантажились" in page
    assert "інша пара — «Це інший товар», та сама — «В архів»" in page


def test_stale_rules_recheck_that_ends_in_problem_is_not_reported_as_done(
    conn, root, app, monkeypatch
):  # R-B22
    item = problem(conn, root, "stale_rules")
    monkeypatch.setattr(
        review, "recheck", lambda *a, **k: review.Recheck("recheck_done", Status.PROBLEM)
    )
    response = post(app, f"/item/{item}", action="recheck")
    assert location(response) == "/problem?msg=recheck_rules_problem"
    assert "проблема лишилась" in html(get(app, location(response)))
