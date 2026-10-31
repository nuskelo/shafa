"""UI-006: Проблеми, Архів, Сесії, Налаштування — новий вигляд і кнопки без нових дій на сервері.

Без мережі й справжніх даних; усе в `tmp_path`.
"""

import re
import shutil
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from shafa import review, web
from shafa.db import Status
from test_review import add_card
from test_ui004 import ready, set_note
from test_web import PORT, TOKEN, get, html, location, post


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def problem(conn, root, code, text="Причина від програми", folder="1342", post_id=1342, **kw):
    item = ready(conn, root, folder=folder, post_id=post_id, status=Status.PROBLEM, **kw)
    set_note(conn, item, code, text)
    return item


def row(page: str, item_id: int) -> str:
    """Рядок Проблеми з цією Карткою."""
    rows = re.findall(r'<li class="problem-row">.*?</li>', page, re.DOTALL)
    return next(r for r in rows if f'action="/item/{item_id}"' in r or f"/item/{item_id}?" in r)


def status_of(conn, item_id):
    return tuple(conn.execute(
        "SELECT status, problem_code FROM items WHERE id = ?", (item_id,)
    ).fetchone())  # fmt: skip


# --- Проблеми: мітка і «Що робити» для кожного коду ---

CODES = [
    ("many_items", "Кілька пар у пості", "опублікуйте на Шафі вручну"),
    ("no_section", "Не взуття й не аксесуар", "опублікуйте на Шафі вручну"),
    ("possible_duplicate", "Схожий є на Шафі", "відкрийте Картку й порівняйте"),
    ("publish_uncertain", "Не впевнений", "«Мої речі» на Шафі"),
    ("publish_error", "Шафа не прийняла при публікації", "відкрийте Картку"),
    ("shafa_declined", "Шафа відхилила оголошення", "відкрийте Картку"),
    ("photos_missing", "Фото", "перевірте папку з фото"),
    ("robot_photos", "Фото", "робот спробує ще раз"),
    ("post_deleted", "Пост видалено", "продано"),
    ("robot_field_missing", "Робот", "робот спробує знову"),
    ("robot_brand_missing", "Робот", "робот спробує знову"),
    ("щось_нове", "Проблема", "відкрийте Картку"),
]


@pytest.mark.parametrize(("code", "label", "todo"), CODES)
def test_problem_row_has_label_reason_and_todo(conn, root, app, code, label, todo):
    item = problem(conn, root, code)
    page = row(html(get(app, "/problem")), item)
    assert f'<span class="kind">{label}</span>' in page
    assert "Що робити:" in page and todo in page
    assert "Пост 1342" in page and "01.03.2025" in page
    assert f'/item/{item}?from=/problem"' in page  # «Відкрити Картку»
    assert f'<img src="/thumb/{item}/' in page


def test_reason_is_problem_text_or_sticky_title(conn, root, app):
    plain = problem(conn, root, "no_price", "У пості кілька товарів з різними цінами")
    sticky = problem(conn, root, "publish_error", "Шафа: price is invalid",
                     folder="1343", post_id=1343)  # fmt: skip
    page = html(get(app, "/problem"))
    assert "У пості кілька товарів з різними цінами" in row(page, plain)
    assert "<details" not in row(page, plain)
    # для проблем публікації причина — простими словами, текст Шафи — в «Подробицях»
    assert '<p class="reason-line">Шафа не прийняла при публікації</p>' in row(page, sticky)
    assert "Шафа: price is invalid" in row(page, sticky)


def test_codes_not_shown_to_user(conn, root, app):
    item = problem(conn, root, "robot_field_missing", "Поле не знайдено")
    assert "robot_field_missing" not in row(html(get(app, "/problem")), item)


# --- Кнопки: лише там, де дія доступна на Картці й працює на сервері ---


def buttons(page: str) -> set[str]:
    return set(re.findall(r'<button name="action" value="(\w+)"', page))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("many_items", {"recheck", "to_archive", "on_shafa"}),
        ("no_section", {"recheck", "to_archive", "on_shafa"}),
        ("robot_field_missing", {"recheck", "to_archive", "to_review", "on_shafa"}),
        ("photos_missing", {"recheck", "to_archive"}),
        ("щось_нове", {"recheck"}),
        ("possible_duplicate", {"to_archive", "on_shafa"}),
        ("already_published", {"to_archive"}),
        ("shafa_declined", {"to_archive"}),
        ("publish_uncertain", set()),  # потрібне посилання на товар — лише на Картці
        ("publish_error", {"to_review"}),
    ],
)
def test_buttons_only_where_action_is_allowed(conn, root, app, code, expected):
    item = problem(conn, root, code)
    assert buttons(row(html(get(app, "/problem")), item)) == expected


@pytest.mark.parametrize(
    "code",
    ["many_items", "photos_missing", "possible_duplicate", "already_published", "shafa_declined",
     "publish_uncertain", "publish_error", "robot_photos", "щось_нове"],
)  # fmt: skip
def test_list_buttons_match_card_buttons(conn, root, app, code):
    """Рядок у списку й Картка пропонують ті самі «В архів» / «Перевірити ще раз»."""
    item = problem(conn, root, code)
    in_list = buttons(row(html(get(app, "/problem")), item)) & {"to_archive", "recheck"}
    on_card = buttons(html(get(app, f"/item/{item}?from=/problem"))) & {"to_archive", "recheck"}
    assert in_list == on_card


@pytest.mark.parametrize(
    "code",
    ["possible_duplicate", "already_published", "shafa_declined", "many_items", "no_section"],
)
def test_archive_button_really_archives(conn, root, app, code):
    """`ARCHIVE_CODES` — рівно ті коди, які `publish.to_archive` справді приймає."""
    item = problem(conn, root, code)
    response = post(
        app, f"/item/{item}", action="to_archive", sure="1", **{"from": "/problem", "stay": "list"}
    )  # CORE-017 коди без повернення питають підтвердження (UX-004)
    assert location(response).startswith("/problem?msg=duplicate_archived")
    assert status_of(conn, item)[0] == Status.ARCHIVED


@pytest.mark.parametrize("code", ["no_price", "ai_error", "robot_logged_out"])
def test_no_archive_button_where_server_refuses(conn, root, app, code):
    """Для цих кодів `to_archive` нічого не робить — кнопки «В архів» немає (див. handoff)."""
    item = problem(conn, root, code)
    assert "to_archive" not in row(html(get(app, "/problem")), item)
    post(app, f"/item/{item}", action="to_archive")
    assert status_of(conn, item) == (Status.PROBLEM, code)


def test_archive_from_list_returns_to_problems(conn, root, app):
    item = problem(conn, root, "possible_duplicate")
    other = problem(conn, root, "photos_missing", folder="1343", post_id=1343)
    page = row(html(get(app, "/problem")), item)
    assert 'name="from" value="/problem"' in page and 'name="stay" value="list"' in page
    where = location(post(app, f"/item/{item}", action="to_archive", **{"from": "/problem"}))
    assert where.startswith("/problem?")
    assert "Картку перенесено в Архів" in html(get(app, where))
    assert status_of(conn, item)[0] == Status.ARCHIVED
    assert status_of(conn, other) == (Status.PROBLEM, "photos_missing")


def test_recheck_from_list_same_result_as_from_card(conn, root, app):
    """«Перевірити ще раз» зі списку й з Картки роблять те саме; зі списку — назад у список."""
    a = problem(conn, root, "robot_field_missing")
    b = problem(conn, root, "robot_field_missing", folder="1343", post_id=1343)
    from_list = post(app, f"/item/{a}", action="recheck", **{"from": "/problem", "stay": "list"})
    from_card = post(app, f"/item/{b}", action="recheck", **{"from": "/problem"})
    assert location(from_list).startswith("/problem?msg=robot_retry")
    assert location(from_card).startswith("/problem?msg=robot_retry")
    assert status_of(conn, a)[0] == status_of(conn, b)[0] == Status.APPROVED


def gone_folder(conn, root, code="no_text", folder="9999", post_id=9999):
    """Проблема, у якої папку вже прибрано: «Перевірити ще раз» нічого не міняє (`stale`)."""
    item = problem(conn, root, code, folder=folder, post_id=post_id)
    shutil.rmtree(root / folder)
    return item


def test_recheck_that_changes_nothing_stays_in_list(conn, root, app):
    """Зі списку повідомлення — у списку, з Картки — на Картці (як і було)."""
    item = gone_folder(conn, root)
    where = location(
        post(app, f"/item/{item}", action="recheck", **{"from": "/problem", "stay": "list"})
    )
    assert urlsplit(where).path == "/problem" and "msg=recheck_problem" in where
    on_card = location(post(app, f"/item/{item}", action="recheck", **{"from": "/problem"}))
    assert urlsplit(on_card).path == f"/item/{item}" and "msg=recheck_problem" in on_card


def test_list_forms_need_token(conn, root, app):
    item = problem(conn, root, "possible_duplicate")
    assert post(app, f"/item/{item}", token=None, action="to_archive").status == 403
    assert status_of(conn, item)[0] == Status.PROBLEM


@pytest.mark.parametrize(
    "bad",
    ["//evil.example/problem", "https://evil.example/", "/settings", "/problem\\x", "javascript:1"],
)
def test_foreign_from_is_ignored(conn, root, app, bad):
    item = problem(conn, root, "robot_field_missing")
    where = location(post(app, f"/item/{item}", action="recheck", **{"from": bad, "stay": "list"}))
    assert where.startswith("/problem?msg=robot_retry")  # адреса не з форми
    other = gone_folder(conn, root)
    where = location(post(app, f"/item/{other}", action="recheck", **{"from": bad, "stay": "list"}))
    assert (
        where.startswith(f"/item/{other}?") and "evil" not in where
    )  # чужий `from` — не повертаємо


def test_search_and_message_stay(conn, root, app):
    a = problem(conn, root, "many_items", "Текст А")
    problem(conn, root, "many_items", "Текст Б", folder="1343", post_id=1343)
    page = html(get(app, "/problem?q=1342"))
    assert 'name="q" value="1342"' in page
    assert f"/item/{a}?from=/problem%3Fq%3D1342" in page
    assert 'name="from" value="/problem?q=1342"' in page  # пошук лишається у `from`
    assert "Нічого не знайдено." in html(get(app, "/problem?q=нема-такого"))
    assert "Картку повернуто в Схвалені" in html(get(app, "/problem?msg=robot_retry"))


def test_empty_problems(conn, root, app):
    assert "Тут поки порожньо." in html(get(app, "/problem"))


# --- Картка-Проблема: права колонка не порожня ---


def test_problem_card_shows_post_text_on_the_right(conn, root, app):
    item = problem(conn, root, "many_items")
    page = html(get(app, f"/item/{item}?from=/problem"))
    right = re.search(
        r'<section class="right" aria-label="Текст поста">(.*?)</section>', page, re.DOTALL
    )
    assert right and "Текст поста" in right[1]
    assert page.count('class="post-box"') == 1  # лише раз


def test_photos_missing_card_offers_recheck_not_dead_archive(conn, root, app):
    """На Картці `photos_missing` була кнопка «В Архів», яку сервер ігнорував, і не було «Перевірити ще раз»."""
    item = problem(conn, root, "photos_missing")
    page = html(get(app, f"/item/{item}"))
    assert 'value="recheck"' in page and 'value="to_archive"' in page  # UX-004: «В архів» працює


# --- Архів ---


# вчора: фіксована дата «старіла» і Картку очищало (CORE-018: тест червонів 27.10.2026)
ARCHIVED_AT = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")


def archived(conn, root, code=None, text=None, folder="1342", post_id=1342, shafa_id=None):
    item = add_card(conn, root, folder=folder, post_id=post_id, status=Status.ARCHIVED)
    with conn:
        conn.execute(
            "UPDATE items SET archived_at = ?, problem_code = ?, problem_text = ?, shafa_id = ? "
            "WHERE id = ?",
            (ARCHIVED_AT, code, text, shafa_id, item),
        )
    return item


def test_archive_shows_deletion_date_reason_and_restore(conn, root, app):
    item = archived(conn, root, "post_deleted", "Пост видалено з каналу — продано")
    page = html(get(app, "/archived"))
    assert f"видалиться з програми {review.deletion_date(ARCHIVED_AT):%d.%m.%Y}" in page
    assert "пост видалено з каналу — пару, мабуть, продано" in page
    assert 'name="action" value="restore"' in page
    assert f'action="/item/{item}"' in page


def test_archive_restore_works_and_returns_to_archive(conn, root, app):
    item = archived(conn, root)
    where = location(post(app, f"/item/{item}", action="restore", **{"from": "/archived"}))
    assert where.startswith("/archived?msg=restored")
    assert status_of(conn, item)[0] == Status.REVIEW
    assert "Повернуто на перевірку" in html(get(app, where))


def test_archive_no_restore_for_card_on_shafa(conn, root, app):
    """Як на Картці: з `shafa_id` повернути на перевірку не можна."""
    item = archived(conn, root, "duplicate", "Уже є на Шафі", shafa_id="777")
    assert 'value="restore"' not in html(get(app, "/archived"))
    on_card = html(get(app, f"/item/{item}"))
    assert 'value="restore"' not in on_card


def test_archive_reason_hidden_for_other_codes(conn, root, app):
    archived(conn, root, "щось", "Службовий текст")
    assert "Службовий текст" not in html(get(app, "/archived"))


# --- Сесії ---


def session(conn, started="2026-10-20T10:00:00+00:00", ended=None, code=None, reason=None):
    with conn:
        return conn.execute(
            "INSERT INTO sessions (started_at, ended_at, stop_code, stop_reason) VALUES (?, ?, ?, ?)",
            (started, ended, code, reason),
        ).lastrowid


def test_sessions_empty(app):
    assert "Робот ще не запускався." in html(get(app, "/sessions"))


def test_sessions_one_card_per_session_with_tones(conn, root, app):
    done = session(conn, ended="2026-10-20T11:00:00+00:00", code="done")
    limit = session(conn, ended="2026-10-21T11:00:00+00:00", code="limit")
    login = session(conn, ended="2026-10-22T11:00:00+00:00", code="login")
    running = session(conn, started="2026-10-23T10:00:00+00:00")
    page = html(get(app, "/sessions"))
    assert page.count('class="session"') == 4
    for sid in (done, limit, login, running):
        assert f'href="/session/{sid}"' in page
        assert f"Сесія {sid}" in page
    assert re.search(r'tone-ok">Черга закінчилась', page)
    assert re.search(r'tone-ok">Денний ліміт вичерпано', page)
    assert re.search(r'tone-warn">Робот вийшов із Шафи', page)
    assert re.search(r'tone-run">працює', page)
    assert "Опубліковано <b>0</b>" in page and "У Проблемах <b>0</b>" in page


def test_sessions_newest_first(conn, app):
    first, second = session(conn), session(conn)
    page = html(get(app, "/sessions"))
    assert page.index(f"/session/{second}") < page.index(f"/session/{first}")


def test_session_page_cards_as_thumbnails(conn, root, app):
    sid = session(conn, ended="2026-10-20T11:00:00+00:00", code="stop")
    ok = ready(conn, root, status=Status.PUBLISHED)
    bad = problem(
        conn, root, "robot_field_missing", "Поле не знайдено", folder="1343", post_id=1343
    )
    with conn:
        conn.execute("UPDATE items SET session_id = ?", (sid,))
    page = html(get(app, f"/session/{sid}"))
    assert f"Сесія {sid}" in page and 'tone-ok">Зупинено кнопкою «Стоп»' in page
    assert f'<img src="/thumb/{ok}/' in page and f'<img src="/thumb/{bad}/' in page
    assert '<span class="mark mark-ok">Опубліковані</span>' in page
    assert '<span class="mark mark-bad">Робот</span>' in page
    assert "Поле не знайдено" in page


def test_session_page_empty_and_missing(conn, app):
    sid = session(conn)
    assert "жодну Картку" in html(get(app, f"/session/{sid}"))
    assert get(app, "/session/999").status == 404


# --- Налаштування ---


def test_settings_blocks(root, app):
    page = html(get(app, "/settings"))
    titles = re.findall(r'<section class="block-card">\s*<h2>([^<]*)</h2>', page)
    assert titles == ["Ліміти й робот", "Сайт: «Продано»", "«Мої речі» на Шафі", "Експорт каналу"]


def test_settings_every_option_once_in_order(root, app):
    from shafa import options

    page = html(get(app, "/settings"))
    names = re.findall(r'<input form="settings"(?: type="checkbox")? id="s-(\w+)"', page)
    assert sorted(names) == sorted(o.name for o in options.OPTIONS)
    assert len(names) == len(set(names))
    blocks = dict(web.settings_blocks())
    assert [o.name for o in blocks["Сайт: «Продано»"]] == [
        "SHAFA_SITE_SEND", "SHAFA_SITE_WAIT_DAYS", "SHAFA_SOLD_CHECK", "SHAFA_SOLD_FUSE",
    ]  # fmt: skip
    order = [o.name for o in options.OPTIONS]
    for opts in blocks.values():  # порядок усередині блоку — як в `OPTIONS`
        idx = [order.index(o.name) for o in opts]
        assert idx == sorted(idx)


def test_settings_forms_present(root, app):
    page = html(get(app, "/settings"))
    for action in ("/settings", "/listings/reread", "/site/release-sold", "/export"):
        assert f'action="{action}"' in page
    main = page[page.index("<main>") : page.index("</main>")]
    assert main.count('name="token"') == 4  # кожна форма зі своїм токеном
    assert "Перечитати зараз" in page and "Надіслати відкладені Продано" in page
    assert re.search(r'<button form="settings" class="approve">Зберегти</button>', page)


def test_settings_warnings_on_top(root, app, tmp_path):
    from test_options import form

    cloud = tmp_path / "OneDrive" / "Документи"
    cloud.mkdir(parents=True)
    page = html(post(app, "/settings", **form(cloud / "Шафа")))
    if 'class="note yellow"' in page:  # попередження — над блоками
        assert page.index('class="note yellow"') < page.index('class="blocks"')


def test_settings_save_error_marks_field(root, app):
    from test_options import form

    page = html(post(app, "/settings", **form(root, SHAFA_DAILY_LIMIT="0")))
    assert "Не збережено" in page and '<div class="field bad">' in page


def test_pages_render_without_javascript_and_scripts_are_local(conn, root, app):
    """Нічого ззовні й жодних inline-скриптів (CSP): всі нові сторінки — звичайний HTML."""
    problem(conn, root, "many_items")
    session(conn)
    for path in ("/problem", "/archived", "/sessions", "/settings"):
        page = html(get(app, path))
        assert "<script>" not in page and "http://" not in page.replace("http://127.0.0.1", "")


# --- CORE-017: «В архів» для «Кілька товарів» і «Нема розділу» ---

FINAL = ["many_items", "no_section"]


@pytest.mark.parametrize("code", FINAL)
def test_core017_archive_from_card_and_list(conn, root, app, code):
    item = problem(conn, root, code)
    assert 'value="to_archive"' in row(html(get(app, "/problem")), item)
    assert 'value="to_archive"' in html(get(app, f"/item/{item}?from=/problem"))
    where = location(
        post(app, f"/item/{item}", action="to_archive", sure="1", **{"from": "/problem"})
    )
    assert where.startswith("/problem?msg=duplicate_archived")
    assert "Картку перенесено в Архів" in html(get(app, where))
    assert status_of(conn, item) == (Status.ARCHIVED, code)
    assert conn.execute("SELECT archived_at FROM items WHERE id = ?", (item,)).fetchone()[0]


@pytest.mark.parametrize("code", FINAL)
def test_core017_hint_mentions_archive(conn, root, app, code):
    item = problem(conn, root, code)
    assert "В архів" in row(html(get(app, "/problem")), item)


@pytest.mark.parametrize("code", FINAL)
def test_core017_archived_stays_after_scan_and_purge(conn, root, app, code):
    from datetime import datetime, timedelta

    from shafa import review, scan

    item = problem(conn, root, code)
    post(app, f"/item/{item}", action="to_archive", sure="1")
    review.purge_archive(conn, datetime.fromisoformat(review.now()) + timedelta(days=8))
    assert status_of(conn, item) == (Status.ARCHIVED, code)
    assert scan.touchable(Status.ARCHIVED, code) is False


@pytest.mark.parametrize("code", FINAL)
def test_core017_restore_refused_and_hidden(conn, root, app, code):
    item = problem(conn, root, code)
    post(app, f"/item/{item}", action="to_archive", sure="1")
    assert 'value="restore"' not in html(get(app, "/archived"))
    assert 'value="restore"' not in html(get(app, f"/item/{item}"))
    where = location(post(app, f"/item/{item}", action="restore"))
    assert "msg=no_restore" in where
    assert status_of(conn, item) == (Status.ARCHIVED, code)
    assert "не можна" in html(get(app, where))


@pytest.mark.parametrize("code", FINAL)
def test_core017_archive_shows_reason(conn, root, app, code):
    problem(conn, root, code, "Причина X")
    item = conn.execute("SELECT id FROM items").fetchone()[0]
    post(app, f"/item/{item}", action="to_archive", sure="1")
    assert "Причина X" in html(get(app, "/archived"))
