"""UI-004: новий вигляд — верх із вкладками, смужка стану, сітка Карток, фільтри.

Тести без мережі й справжніх даних; браузерні (Playwright) пропускаються, якщо браузера немає.
"""

import re
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from shafa import review, web
from shafa.db import Status
from shafa.robot import control, lock
from test_qa002 import NO_SEASON, make_required
from test_review import add_card
from test_robot_plan import BLANK, BOOTS, TEXT
from test_web import PORT, TOKEN, get, html, post

ALL_TABS = ["/review", "/approved", "/problem", "/published", "/sessions", "/archived", "/settings"]


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN, lock_file=lock_file
    )


def ready(conn, root, folder="1342", post_id=1342, status=Status.REVIEW, **kw):
    """Картка, яку робот опублікує без зауважень (бланк і текст з тестів робота)."""
    return add_card(conn, root, folder=folder, post_id=post_id, status=status,
                    blank=BLANK, text=TEXT, **kw)  # fmt: skip


def set_note(conn, item_id, code, text):
    with conn:
        conn.execute(
            "UPDATE items SET problem_code = ?, problem_text = ? WHERE id = ?",
            (code, text, item_id),
        )


def marks(page: str) -> list[str]:
    return re.findall(r'class="mark[^"]*"[^>]*>([^<]*)<', page)


# --- Вкладки ---


@pytest.mark.parametrize("path", ["/review", "/approved", "/problem", "/published", "/sessions",
                                  "/archived", "/settings"])  # fmt: skip
def test_every_tab_is_reachable_from_every_page(conn, root, app, path):
    ready(conn, root)
    # Картка (UI-005) має власний верх: «← список» і смужка стану, без вкладок — див. test_ui005
    page = html(get(app, path))
    for tab in ALL_TABS:
        assert f'href="{tab}"' in page, (path, tab)


def test_counts_and_red_problem_counter(conn, root, app):
    page = html(get(app, "/review"))
    assert 'Проблеми <span class="count">0</span>' in page  # нуль — не червоний
    ready(conn, root)
    ready(conn, root, folder="1343", post_id=1343, status=Status.PROBLEM)
    page = html(get(app, "/review"))
    assert 'Проблеми <span class="count alert">1</span>' in page
    assert 'На перевірку <span class="count">1</span>' in page


def test_current_tab_is_highlighted(app):
    page = html(get(app, "/approved"))
    assert 'aria-current="page" href="/approved" class="tab current"' in page
    assert page.count('aria-current="page"') == 1


def test_more_menu_holds_sessions_archive_settings(conn, root, app):
    page = html(get(app, "/review"))
    menu = page[page.index('<details class="more') : page.index("</details>")]
    assert all(f'href="{tab}"' in menu for tab in ("/sessions", "/archived", "/settings"))
    assert "/review" not in menu and "/problem" not in menu


def test_more_highlighted_on_its_pages(app):
    assert 'class="more current"' in html(get(app, "/settings"))
    assert 'class="more current"' in html(get(app, "/archived"))
    assert 'class="more"' in html(get(app, "/review"))


def test_page_works_offline_no_external_files(app):
    css = get(app, "/static/style.css").body.decode("utf-8")
    page = html(get(app, "/review"))
    for text in (css, page):
        assert "googleapis" not in text and "@import" not in text
        assert not re.search(r'(src|href)="https?://', text)
    assert "--accent: #1f4fd1" in css and "Inter, system-ui" in css


# --- Мітки Карток і фільтри ---


@pytest.mark.parametrize(
    ("n", "text"),
    [(1, "1 пропуск"), (2, "2 пропуски"), (4, "4 пропуски"), (5, "5 пропусків"),
     (11, "11 пропусків"), (12, "12 пропусків"), (21, "21 пропуск"), (22, "22 пропуски")],
)  # fmt: skip
def test_plural(n, text):
    assert web.plural(n, "пропуск", "пропуски", "пропусків") == text


def test_mark_ready_is_green_ready(conn, root, app):
    ready(conn, root)
    assert marks(html(get(app, "/review"))) == ["Готово"]
    assert 'class="mark mark-ok"' in html(get(app, "/review"))


def test_mark_yellow_field_and_skips(conn, root, app):
    # одне жовте поле: розділ-бланк із жовтим сезоном
    blank = [f for f in BLANK if f.name != "season"]
    from shafa import ai

    yellow = blank + [ai.Field("season", "Зимаа", True, "значення немає в довіднику")]
    add_card(conn, root, blank=yellow, text=TEXT)
    [text] = marks(html(get(app, "/review")))
    assert text.startswith("1 жовте")
    assert "жовте поле" in text or "пропуск" in text


def test_mark_yellow_alone_is_written_in_full(conn, root):
    from shafa import ai
    from shafa.robot.plan import Precheck

    blank = [f for f in BLANK if f.name != "season"]
    yellow = blank + [ai.Field("season", "Зимаа", True, "значення немає в довіднику")]
    item = add_card(conn, root, blank=yellow, text=TEXT)
    card = review.load_card(conn, item)
    assert card.yellow == 1
    assert web.card_mark(card, Precheck(None, [])) == web.Mark("warn", "1 жовте поле")
    assert web.card_mark(card, Precheck(None, ["Тип: немає"])) == web.Mark(
        "warn", "1 жовте · 1 пропуск"
    )


def test_mark_season_note_and_robot_error(conn, root, app):
    item = ready(conn, root)
    set_note(
        conn, item, review.SEASON_CHANGED, "Сезон змінено правилом: було Зима → стало Демісезон"
    )
    page = html(get(app, "/review"))
    assert marks(page) == ["Сезон змінено"]
    assert "Сезон змінено правилом" in page  # повний текст — у підказці
    make_required(BOOTS, "Сезон")
    add_card(conn, root, folder="1343", post_id=1343, blank=NO_SEASON, text=TEXT)
    assert "Робот не зможе опублікувати" in marks(html(get(app, "/review")))


def test_published_and_archive_have_no_mark(conn, root, app):
    ready(conn, root, status=Status.PUBLISHED)
    ready(conn, root, folder="1343", post_id=1343, status=Status.ARCHIVED)
    assert marks(html(get(app, "/published"))) == []
    assert marks(html(get(app, "/archived"))) == []


def build_review(conn, root):
    """2 готові, 1 жовта; повертає (готова, готова, жовта)."""
    from shafa import ai

    first = ready(conn, root, folder="1342", post_id=1342)
    second = ready(conn, root, folder="1343", post_id=1343)
    yellow = add_card(conn, root, folder="1344", post_id=1344, text=TEXT,
                      blank=[f for f in BLANK if f.name != "season"]
                      + [ai.Field("season", "Зимаа", True, "значення немає в довіднику")])  # fmt: skip
    return first, second, yellow


def test_filter_counts_and_cards(conn, root, app):
    first, second, yellow = build_review(conn, root)
    page = html(get(app, "/review"))
    assert "Усі · 3" in page and "Треба глянути · 1" in page and "Готові · 2" in page
    for item in (first, second, yellow):
        assert f'href="/item/{item}"' in page

    warn = html(get(app, "/review?f=warn"))
    assert f'href="/item/{yellow}"' in warn
    assert f'href="/item/{first}"' not in warn and f'href="/item/{second}"' not in warn
    assert "Усі · 3" in warn  # числа не залежать від вибраного фільтра

    ok = html(get(app, "/review?f=ok"))
    assert f'href="/item/{first}"' in ok and f'href="/item/{second}"' in ok
    assert f'href="/item/{yellow}"' not in ok

    assert f'href="/item/{yellow}"' in html(get(app, "/review?f=нісенітниця"))  # невідомий — усі


def test_filter_pills_are_links_and_current_is_marked(conn, root, app):
    build_review(conn, root)
    page = html(get(app, "/review?f=warn"))
    assert 'class="pill on" href="/review?f=warn" aria-current="true"' in page
    assert 'href="/review"' in page and 'href="/review?f=ok"' in page


def test_filter_and_search_work_together(conn, root, app):
    first, _, _ = build_review(conn, root)
    page = html(get(app, "/review?f=ok&q=1343"))
    assert 'name="f" value="ok"' in page  # пошук не губить фільтр
    assert f'href="/item/{first}"' not in page  # лише пост 1343
    assert "Готові · 1" in page  # числа — за пошуком


def test_filters_only_on_review_tab(conn, root, app):
    ready(conn, root, status=Status.APPROVED)
    page = html(get(app, "/approved?f=warn"))
    assert "Треба глянути" not in page
    assert "Готово" in page  # але мітка є і в «Схвалених»


# --- Смужка стану: всі дії лишилися ---


def test_every_panel_action_is_in_the_markup(conn, root, app, monkeypatch):
    ready(conn, root, status=Status.APPROVED)
    monkeypatch.setattr(
        web.App, "bot_state", lambda self, c: web.BotState("stopped", "вимкнений", True)
    )
    page = html(get(app, "/review"))
    assert re.search(r'<form method="post" action="/robot" class="status-actions">', page)
    for value in ("start", "rehearse"):
        assert f'name="action" value="{value}"' in page
    assert 'action="/ai"' in page and 'name="action" value="update"' in page
    assert 'action="/bot"' in page and ">Запустити бота</button>" in page
    assert 'action="/health"' in page and "Перевірити</button>" in page
    assert page.count('name="token"') >= 4
    assert "Детальніше" in page  # довгі підказки — під «Детальніше»


@pytest.mark.parametrize("button", ["pause", "stop"])
def test_running_panel_has_pause_stop_and_progress(conn, root, app, lock_file, button):
    item = ready(conn, root, status=Status.PUBLISHED)
    ready(conn, root, folder="1343", post_id=1343, status=Status.APPROVED)
    with conn:
        sid = conn.execute("INSERT INTO sessions (started_at) VALUES ('x')").lastrowid
        conn.execute("UPDATE items SET session_id = ? WHERE id = ?", (sid, item))
    with lock.hold("publish", lock_file):
        page = html(get(app, "/robot/panel?back=/review"))
    assert f'name="action" value="{button}"' in page
    assert "Робот публікує" in page
    assert "Публікує: у цій сесії 1 · у черзі 1" in page
    assert 'value="start"' not in page


def test_session_progress_counts_only_open_session(conn, root, lock_file):
    item = ready(conn, root, status=Status.PUBLISHED)
    with conn:
        old = conn.execute(
            "INSERT INTO sessions (started_at, ended_at) VALUES ('a', 'b')"
        ).lastrowid
        conn.execute("UPDATE items SET session_id = ? WHERE id = ?", (old, item))
        conn.execute("INSERT INTO sessions (started_at) VALUES ('c')")
    with lock.hold("publish", lock_file):
        assert control.state(conn, lock_file).session_published == 0
    assert control.state(conn, lock_file).session_published is None  # робот не працює


def test_panel_endpoint_forgets_running_after_session_ends(conn, root, app, lock_file):
    """Те, що читає `app.js`: після кінця сесії — «зупинений», не «публікує»."""
    ready(conn, root, status=Status.APPROVED)
    with conn:
        conn.execute("INSERT INTO sessions (started_at) VALUES ('x')")
    held = lock.acquire("publish", lock_file)
    assert "Робот публікує" in html(get(app, "/robot/panel?back=/review"))
    held.release()
    with conn:
        conn.execute("UPDATE sessions SET ended_at = 'y', stop_code = 'done'")
    page = html(get(app, "/robot/panel?back=/review"))
    assert "Робот чекає" in page and "Робот публікує" not in page
    assert 'value="start"' in page


def test_tones_of_the_dots(conn, root, app, lock_file):
    page = html(get(app, "/review"))
    assert "dot dot-idle" in page  # робот стоїть, самоперевірки ще не було
    with lock.hold("publish", lock_file):
        assert "dot-ok" in html(get(app, "/robot/panel?back=/review"))


def test_panel_endpoint_post_forms_keep_back(conn, app):
    page = html(get(app, "/robot/panel?back=/approved"))
    assert 'name="back" value="/approved"' in page
    assert post(app, "/robot", action="start", back="/approved").status == 303


# --- Скрипт оновлення смужки ---


def test_script_polls_panel_and_marks_lost_connection(app):
    js = get(app, "/static/app.js").body.decode("utf-8")
    assert "setInterval(refresh, 5000)" in js and "visibilitychange" in js
    assert "Немає зв'язку" in js and "AbortController" in js
    assert "fetch(" in js and js.count("fetch(") == 1  # лише панель, лише читання


# --- Справжній Chromium: оновлення після кінця сесії, обрив зв'язку, скриншоти ---


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import Error, sync_playwright

    playwright = sync_playwright().start()
    try:
        browser_ = playwright.chromium.launch()
    except Error:
        playwright.stop()
        pytest.skip("немає браузера Playwright")
    yield browser_
    browser_.close()
    playwright.stop()


@pytest.fixture
def live(conn, tmp_path, root, lock_file):
    app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", 0, token=TOKEN, lock_file=lock_file
    )
    server = web.make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app
    server.shutdown()
    server.server_close()


def wait_for(check, seconds=9.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.2)
    return False


def test_browser_status_strip_updates_after_session_ends(conn, root, live, chromium, lock_file):
    ready(conn, root, status=Status.APPROVED)
    with conn:
        conn.execute("INSERT INTO sessions (started_at) VALUES ('x')")
    held = lock.acquire("publish", lock_file)
    page = chromium.new_page()
    try:
        page.goto(f"http://127.0.0.1:{live.port}/review")
        assert "Робот публікує" in page.inner_text("#robot")
        held.release()
        with conn:
            conn.execute("UPDATE sessions SET ended_at = 'y', stop_code = 'done'")
        assert wait_for(lambda: "Робот чекає" in page.inner_text("#robot"))  # без перезавантаження
        assert page.locator("#robot button.approve").is_visible()
    finally:
        held.release()
        page.close()


def test_browser_shows_stale_when_server_is_gone(conn, root, live, chromium):
    page = chromium.new_page()
    try:
        page.goto(f"http://127.0.0.1:{live.port}/review")
        page.evaluate("document.getElementById('robot').dataset.panel = '/нема-такої-сторінки'")
        assert wait_for(lambda: page.locator(".status.stale .stale-note").count() == 1)
        page.evaluate(
            "document.getElementById('robot').dataset.panel = '/robot/panel?back=/review'"
        )
        assert wait_for(lambda: page.locator(".stale-note").count() == 0)
    finally:
        page.close()


def test_browser_keeps_opened_details_across_refresh(conn, root, live, chromium):
    page = chromium.new_page()
    try:
        page.goto(f"http://127.0.0.1:{live.port}/review")
        page.click("details[data-key=ai] summary")
        assert page.locator("details[data-key=ai]").evaluate("e => e.open")
        with conn:  # щось змінилось — смужку перемальовано
            conn.execute("INSERT INTO sessions (started_at, ended_at) VALUES ('x', 'y')")
        ready(conn, root, status=Status.APPROVED)
        assert wait_for(lambda: "Схвалених 1" in page.inner_text("#robot"))
        assert page.locator("details[data-key=ai]").evaluate("e => e.open")
    finally:
        page.close()


def test_browser_more_menu_opens_and_closes(live, chromium):
    page = chromium.new_page()
    try:
        page.goto(f"http://127.0.0.1:{live.port}/review")
        page.click("details.more summary")
        assert page.locator(".menu").is_visible()
        page.mouse.click(5, 400)
        assert not page.locator(".menu").is_visible()
    finally:
        page.close()


def demo(conn, root):
    """Підставні Картки для скриншотів: різні мітки, кольорові фото. Нічого справжнього."""
    from shafa import ai

    yellow_season = [f for f in BLANK if f.name != "season"] + [
        ai.Field("season", "Зимаа", True, "значення немає в довіднику")
    ]
    colors = ["#c96f4a", "#4a7fc9", "#6fa86f", "#b58bd1", "#d1b24a", "#7f7f7a"]
    ids = []
    for n in range(6):
        item = add_card(
            conn, root, folder=str(6500 + n), post_id=6500 + n, text=TEXT,
            blank=yellow_season if n in (2, 4) else BLANK,
            date=f"2026-09-{10 + n}T10:00:00",
        )  # fmt: skip
        ids.append(item)
        for photo in (root / str(6500 + n)).glob("*.JPG"):
            Image.new("RGB", (400, 300), colors[n]).save(photo, format="JPEG")
    set_note(
        conn, ids[3], review.SEASON_CHANGED, "Сезон змінено правилом: було Зима → стало Демісезон"
    )
    ready(conn, root, folder="7001", post_id=7001, status=Status.APPROVED)
    ready(conn, root, folder="7002", post_id=7002, status=Status.PROBLEM)
    return ids


def shoot(page, base, path, name, width, out: Path, height=1000):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{base}{path}")
    page.wait_for_load_state("networkidle")
    shot = out / f"{name}-{width}.png"
    page.screenshot(path=str(shot), full_page=False)
    return shot


@pytest.mark.parametrize("width", [1400, 390])
def test_browser_screenshots_of_list_and_panel(
    conn, root, live, chromium, tmp_path, width, lock_file
):
    demo_ids = demo(conn, root)
    page = chromium.new_page()
    try:
        base = f"http://127.0.0.1:{live.port}"
        shots = [shoot(page, base, "/review", "list", width, tmp_path),
                 shoot(page, base, "/review?f=warn", "list-warn", width, tmp_path)]  # fmt: skip
        with conn:
            conn.execute("INSERT INTO sessions (started_at) VALUES ('x')")
        with lock.hold("publish", lock_file):
            shots.append(shoot(page, base, "/review", "list-running", width, tmp_path))
        with conn:  # сесія скінчилась — далі робот «зупинений», а не «зупинився несподівано»
            conn.execute("UPDATE sessions SET ended_at = 'y', stop_code = 'done'")
        # інші сторінки лише підхопили спільні стилі: усе читається, кнопки на місці
        published = ready(conn, root, folder="7003", post_id=7003, status=Status.PUBLISHED)
        with conn:
            conn.execute(
                "UPDATE items SET shafa_url = 'https://shafa.ua/uk/item/1-x', published_at = ? "
                "WHERE id = ?", ("2026-10-13T10:00:00+00:00", published))  # fmt: skip
        for name, path in (("problem", "/problem"), ("approved", "/approved"),
                           ("published", "/published"), ("item", f"/item/{demo_ids[0]}"),
                           ("settings", "/settings"), ("sessions", "/sessions")):  # fmt: skip
            shots.append(shoot(page, base, path, name, width, tmp_path))
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
                name
            )
        for shot in shots:
            assert shot.stat().st_size > 5000
        # у вузькому вікні нічого не вилазить за край
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    finally:
        page.close()
