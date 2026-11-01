"""UX-007: повторний аудит — суперечності «текст ≠ стан» і пастки.

Дизайн не міняли: перевіряємо тексти й поведінку. Усе — у tmp_path, без мережі й справжніх даних.
"""

from datetime import timedelta

import pytest

from shafa import web
from shafa.robot import control, lock, publish
from test_robot_control import NOW, session
from test_web import PORT, TOKEN, get, html, location, plain, post


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    return web.App(
        tmp_path / "база.sqlite",
        root,
        tmp_path / "thumbs",
        PORT,
        token=TOKEN,
        lock_file=lock_file,
        on_quit=list,
    )


def state(conn, lock_file, **kw):
    return control.state(conn, lock_file, day_limit=kw.pop("day_limit", 30), now=NOW, **kw)


# --- R-A1: Пауза/Стоп у перші секунди ---


def test_lock_without_session_is_starting_without_pause_and_stop(conn, lock_file):
    """Замок узято, а сесії ще нема (Chrome відкривається) — «запускається», кнопок Пауза/Стоп нема."""
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.code == "starting"
    assert s.text == "Робот запускається — відкривається вікно Chrome (до хвилини)"
    assert s.buttons == []


def test_pause_button_appears_only_when_session_is_open(conn, lock_file):
    session(conn)
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.code == "running" and s.buttons == ["pause", "stop"]


# --- R-A2: пауза без «працює» і «ще ≈» ---


def test_paused_shows_published_count_without_working_and_estimate(conn, lock_file):
    session(conn, command="pause")
    publish.write_now(1, "pause", plan=9, handled=1, last=None)
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert s.code == "paused"
    assert s.progress == "Опубліковано 1 з 9 на сьогодні · на паузі"
    assert "працює" not in s.progress and "≈" not in s.progress + s.mini
    assert s.mini == "Робот: 1 з 9 · на паузі"


def test_pause_time_is_not_counted_in_the_estimate(conn, lock_file, tmp_path, monkeypatch):
    """Після «Продовжити» оцінка «ще ≈» не росте від часу, який робот простояв на паузі."""
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path / "дані"))
    session(conn, started_at=(NOW - timedelta(minutes=40)).isoformat())
    publish.write_now(1, "between", plan=3, handled=1, last=None)
    control.mark_pause(1, True, NOW - timedelta(minutes=30))
    control.mark_pause(1, False, NOW - timedelta(minutes=10))  # на паузі було 20 хв з 40
    with lock.hold("publish", lock_file):
        s = state(conn, lock_file)
    assert "працює 20 хв" in s.progress
    assert "ще ≈ 40 хв" in s.progress  # 2 пости × 20 хв, а не 2 × 40


# --- R-A3: сторінка сесії показує й Картки, що пішли в Проблеми ---


def test_session_page_lists_cards_that_went_to_problems(conn, root, app, tmp_path):
    from shafa.db import Status
    from test_ui004 import ready

    item = ready(conn, root, status=Status.APPROVED)
    session(conn, ended_at=NOW.isoformat(), problems=1, stop_code="done")
    run = publish.Run.__new__(publish.Run)  # лише `_problem`: решта публікації тут не потрібна
    run.conn, run.log = conn, lambda text: None
    report = publish.Report(1)
    card = review_card(conn, item)
    run._problem(report, card, Status.APPROVED, "possible_duplicate", "Схожа пара вже є")
    assert conn.execute("SELECT session_id FROM items WHERE id = ?", (item,)).fetchone()[0] == 1
    page = html(get(app, "/session/1"))
    assert f"/item/{item}" in page
    assert "Опублікованих у цій сесії немає. Картки, що пішли в Проблеми, — нижче." in page
    assert "не брався за жодну Картку" not in page


def test_session_with_problems_but_no_record_points_to_problems(conn, app):
    """Стара сесія (до UX-007): Проблеми були, а Карток із session_id немає — не «жодну Картку»."""
    session(conn, ended_at=NOW.isoformat(), problems=2, stop_code="done")
    page = html(get(app, "/session/1"))
    assert "не брався за жодну Картку" not in page
    assert "Опублікованих у цій сесії немає." in page and 'href="/problem"' in page


def test_empty_session_still_says_nothing_was_taken(conn, app):
    session(conn, ended_at=NOW.isoformat(), stop_code="done")
    assert "У цій сесії робот не брався за жодну Картку." in html(get(app, "/session/1"))


def review_card(conn, item):
    from shafa import review

    return review.load_card(conn, item)


# --- R-A4: одне число — різні назви ---


def test_header_and_tile_use_different_names_for_waiting_cards(conn, root, tmp_path):
    from test_web_ai import GOOD, TODAY, Client, Clock, message, new_card

    client = Client(message(GOOD))
    ai_app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        ai_client=lambda: client, run=lambda job: job(), clock=Clock(), today=lambda: TODAY,
    )  # fmt: skip
    new_card(conn, root)
    new_card(conn, root, folder="1349", post_id=1349, date="2026-10-12T10:00:00")
    page = html(get(ai_app, "/review"))
    assert "Ще не заповнені ШІ: 2" in page
    assert "Готові йти на ШІ 1" in plain(page)
    assert "Чекають на ШІ" not in page


# --- R-B1: «Скасувати схвалення» → «Схвалити і далі» ---


def test_approve_after_unapprove_from_approved_goes_back_to_the_list(conn, root, app):
    from shafa import review
    from shafa.db import Status
    from test_ui004 import ready

    first = ready(conn, root, folder="1342", post_id=1342)
    second = ready(conn, root, folder="1343", post_id=1343)
    for item in (first, second):
        review.act(conn, item, "approve", root)
    assert review.act(conn, first, "unapprove") == "unapproved"
    response = post(
        app, f"/item/{first}", action="approve", **{"from": "/approved"},
        next=f"/item/{second}?from=%2Fapproved",
    )  # fmt: skip
    target = location(response)
    assert target.startswith("/approved?") and f"msg=approved_back&ok={first}" in target
    page = html(get(app, target))
    assert "Картка знову в «Схвалених»" in page and "Це наступна Картка" not in page
    assert conn.execute("SELECT status FROM items WHERE id = ?", (first,)).fetchone()[0] == (
        Status.APPROVED
    )


def test_approve_from_review_still_goes_to_next_card(conn, root, app):
    from test_ui004 import ready

    first = ready(conn, root, folder="1342", post_id=1342)
    second = ready(conn, root, folder="1343", post_id=1343)
    response = post(
        app, f"/item/{first}", action="approve", **{"from": "/review"},
        next=f"/item/{second}?from=%2Freview",
    )  # fmt: skip
    assert f"msg=approved_next&ok={first}" in location(response)


# --- R-B2: «В архів» Картки, що вже на Шафі, — лише після питання ---


def test_archiving_a_card_that_is_on_shafa_asks_first(conn, root, app):
    from shafa.db import Status
    from test_ux004 import problem

    item = problem(conn, root, "shafa_declined", "Шафа відхилила оголошення")
    with conn:
        conn.execute(
            "UPDATE items SET shafa_id = 12345678, shafa_url = 'https://shafa.ua/x' WHERE id = ?",
            (item,),
        )
    page = html(post(app, f"/item/{item}", action="to_archive", **{"from": "/problem"}))
    assert "Картку не можна буде повернути — оголошення вже є на Шафі. Точно в архів?" in page
    assert conn.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()[0] == (
        Status.PROBLEM
    )
    done = post(app, f"/item/{item}", action="to_archive", sure="1", **{"from": "/problem"})
    assert location(done).startswith("/problem?")
    assert conn.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()[0] == (
        Status.ARCHIVED
    )


# --- СЕРЕДНІ ---


def _status(conn, item):
    return conn.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()[0]


def test_archive_reason_texts_match_why_the_card_is_there(conn, root, app):
    """R-B3: причина в Архіві не суперечить тому, що сталось."""
    from test_ux004 import archived

    expected = {
        "possible_duplicate": "ви вирішили: схожа пара вже є на Шафі",
        "size_gone": "розмір прибрали з поста — мабуть, продано",
        "shafa_declined": "Шафа відхилила оголошення",
        "robot_field_missing": "ви вирішили не публікувати після помилки робота",
    }
    for i, (code, text) in enumerate(expected.items()):
        item = archived(conn, root, code, "x", folder=str(1342 + i), post_id=1342 + i)
        assert text in html(get(app, f"/item/{item}"))
    assert "Картка не піде на Шафу" not in "".join(
        html(get(app, f"/item/{i}")) for i in range(1, 5)
    )


def test_kid_doubt_asks_adult_or_kid_and_has_no_gender_buttons(conn, root, app):
    """R-B4: сумнів «дитяча чи доросла» — не питання «жіночі чи чоловічі»."""
    from shafa import ai
    from test_review import add_card
    from test_robot_plan import BOOTS, TEXT
    from test_ui005 import blank_with, page_of

    item = add_card(
        conn, root, folder="1350", post_id=1350, text=TEXT,
        blank=blank_with(ai.Field("section", BOOTS, True, ai.MAYBE_KID)),
    )  # fmt: skip
    page = page_of(app, item)
    assert "Доросла чи дитяча пара?" in page
    assert "Дитячий одяг / Для дівчаток (хлопчиків) / Взуття" in page
    assert "gender:women" not in page and "gender:men" not in page
    assert "Це жіночі" not in page and "Це чоловічі" not in page


def test_problems_have_a_way_out_for_every_code():
    """R-B5: «В архів» є в усіх Проблем про папку, текст і ШІ; для no_text ще «Опублікувала вручну»."""
    for code in ("no_text", "no_price", "bad_folder_name", "duplicate_post", "post_not_found",
                 "no_photos", "too_many_photos", "folder_missing", "ai_error", "stale_rules"):  # fmt: skip
        assert web.problem_actions(code, "1342").archive, code
    assert web.problem_actions("no_text", "1342").manual


def test_archiving_a_no_text_problem_works_on_the_server(conn, root, app):
    from test_ux004 import problem

    item = problem(conn, root, "no_text", "У пості немає тексту")
    post(app, f"/item/{item}", action="to_archive", **{"from": "/problem"})
    assert _status(conn, item) == "archived"


def test_old_post_problems_do_not_promise_a_recheck_button(conn, root, app):
    """R-B6: у старих постів нема папки — текст про «В архів» і Власника, кнопки повтору нема."""
    from test_ux004 import problem

    item = problem(conn, root, "no_photos", "Немає фото", folder="borh-3659", post_id=3659)
    assert not web.problem_actions("no_photos", "borh-3659").recheck
    page = html(get(app, "/problem"))
    assert "Продано — «В архів». Не продано — скажіть Власнику." in page.replace(
        "продано — «В архів». Не продано", "Продано — «В архів». Не продано"
    )
    assert "додайте в папку фото" not in page
    assert f"/item/{item}" in page


def test_recheck_new_message_explains_the_wait():
    """R-B7: після «Перевірити ще раз» — коли Картка з'явиться."""
    text = web.MESSAGES["recheck_new"]
    assert "Перевірено: усе гаразд" in text and "зазвичай до години" in text


def test_robot_publish_blocked_has_no_buttons_and_own_line(conn, root, app):
    """R-B8: захист робота — нічого не натискати, кнопок повтору нема."""
    from test_ux004 import problem

    acts = web.problem_actions("robot_publish_blocked", "1342")
    assert not (acts.archive or acts.recheck or acts.to_review or acts.manual)
    problem(conn, root, "robot_publish_blocked", "Захист")
    assert "нічого не натискайте, скажіть Власнику" in html(get(app, "/problem"))


def test_no_photos_error_says_what_to_do_and_is_not_counted_as_skipped(conn, root, app):
    """R-B9: «немає фото: 1.jpg» → що робити; такі Картки не «пропущені»."""
    from test_ui004 import ready

    bare = ready(conn, root, folder="1342", post_id=1342)
    done = ready(conn, root, folder="1343", post_id=1343)
    with conn:
        conn.execute("DELETE FROM photos WHERE item_id = ?", (bare,))
    card = review_card(conn, bare)
    error = app._precheck(card).error
    assert error.startswith("у Картки немає фото. Додайте фото в папку поста")
    assert "1.jpg" not in error and "порожня" not in error
    response = post(app, f"/item/{done}", action="approve", **{"from": "/review"})
    target = location(response)
    assert "approved_left" not in target  # без фото не рахується пропущеною
    assert "approved_done" in target or f"/item/{bare}" not in target


def test_review_filters_do_not_show_all_zero_next_to_all_27(conn, root, app):
    """R-B10: «Усі · 0» не стоїть поруч із «Усі · N» іншого фільтра."""
    from test_ui004 import ready

    ready(conn, root, folder="1342", post_id=1342)
    page = html(get(app, "/review?k=old"))
    assert "Усі · 0" not in page


def test_daily_limit_text_names_both_numbers(conn, root, lock_file):
    """R-A5: «20 з 5» не пишемо — кажемо, що лічильник перевищив ліміт."""
    from test_ui004 import ready

    ready(conn, root, status="approved")
    for n in range(20):
        with conn:
            conn.execute(
                "INSERT INTO items (folder_name, status, publish_started_at, created_at, updated_at) "
                "VALUES (?, 'published', ?, ?, ?)",
                (f"x{n}", NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
    s = state(conn, lock_file, day_limit=5)
    assert s.why_code == "limit"
    assert "робот уже почав 20 публікацій, а ліміт на добу — 5 (Налаштування)" in s.why_not
    assert "20 з 5" not in s.why_not


def test_old_group_exhausted_without_new_approved_says_so(conn, root, lock_file, monkeypatch):
    """R-A6: старі вичерпано, нових Схвалених нема — окреме пояснення й текст кнопки."""
    from test_ui004 import ready

    monkeypatch.setenv("SHAFA_DAILY_OLD", "1")
    monkeypatch.setenv("SHAFA_DAILY_NEW", "10")
    ready(conn, root, folder="borh-3659", post_id=3659, status="approved")
    with conn:
        conn.execute(
            "INSERT INTO items (folder_name, status, publish_started_at, created_at, updated_at) "
            "VALUES ('borh-1', 'published', ?, ?, ?)",
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
    s = state(conn, lock_file, day_limit=30)
    assert s.why_code == "groups"
    assert s.why_not.startswith("Старі пости на сьогодні вичерпано (1 з 1).")
    assert "Схвалених нових немає — решту робот опублікує завтра." in s.why_not
    assert web.MESSAGES["robot_cannot_start_groups"] == (
        "Робота не запущено: на сьогодні місць для цих Карток немає"
    )


def test_quit_confirm_warns_about_robot_and_night_check(conn, app, lock_file):
    """R-A7: «Вийти» каже про нічну перевірку й, якщо робот працює, що він допише товар."""
    page = html(get(app, "/quit"))
    assert "Нічна пробна перевірка не пройде." in page
    assert "Робот зараз публікує" not in page
    with lock.hold("publish", lock_file):
        assert "Робот зараз публікує — він допише поточний товар і зупиниться." in html(
            get(app, "/quit")
        )


def test_stale_token_error_explains_and_offers_back(app):
    """R-A8: після перезапуску — зрозумілий текст і кнопка «Повернутися» (лише свій хост)."""
    from urllib.parse import parse_qs

    def send(referer):
        headers = {"host": f"127.0.0.1:{PORT}", "referer": referer}
        return app.handle(web.Request("POST", "/settings", {}, {"token": ["старий"]}, headers))

    def body(response):
        assert response.status == 403
        return response.body.decode("utf-8")

    page = body(send(f"http://127.0.0.1:{PORT}/settings"))
    assert "Програму перезапускали — зміни не збережено." in page
    assert "оновіть сторінку (F5) і внесіть зміни ще раз" in page
    assert 'href="/settings"' in page and "Повернутися" in page
    foreign = body(send("http://evil.example/settings"))
    assert "Повернутися" not in foreign
    assert parse_qs("") == {}


def test_options_do_not_crash_on_superscript_digits():
    """R-A11: «²» у числовому полі — помилка поля, а не 500."""
    from shafa import options

    form = {option.name: "5" for option in options.OPTIONS if option.kind == "int"}
    name = next(n for n in form)
    form[name] = "²"
    values, errors, _ = options.check(form)
    assert name in errors and name not in values
    form[name] = "٣"  # арабська цифра — теж не приймаємо
    assert name in options.check(form)[1]
