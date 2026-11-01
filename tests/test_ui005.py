"""UI-005: новий вигляд Картки — верх, жовта примітка, поля й чипи, панель дій, «Схвалити і далі».

Тести без мережі й справжніх даних; браузерні (Playwright) пропускаються, якщо браузера немає.
"""

import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from shafa import ai, web
from shafa.db import Status
from test_qa002 import NO_SEASON, make_required
from test_review import add_card, edits, status
from test_robot_plan import BLANK, BOOTS, TEXT
from test_ui004 import build_review, ready, set_note
from test_web import HOST, TOKEN, html, location, post


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def app(conn, tmp_path, root, lock_file):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", 8765, token=TOKEN, lock_file=lock_file
    )


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


ORDER_YELLOW = ai.Field("photo_order", ["IMG_2.JPG", "IMG_1.JPG"], True, "ШІ не впевнений")


def blank_with(*fields: ai.Field) -> list[ai.Field]:
    names = {f.name for f in fields}
    return [f for f in BLANK if f.name not in names] + list(fields)


def fetch(app, path, referer=None):
    url = urlsplit(path)
    headers = {"host": HOST} | ({"referer": referer} if referer else {})
    return app.handle(web.Request("GET", url.path, parse_qs(url.query), {}, headers))


def page_of(app, item, path=None, referer=None):
    return html(fetch(app, path or f"/item/{item}", referer))


def hidden_value(page: str, name: str) -> str | None:
    found = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)"', page)
    return found[1].replace("&amp;", "&").replace("&#39;", "'") if found else None


def send_form(app, page: str, item: int, **extra):
    """Те, що відправить форма Картки: `from` і `next` з розмітки + натиснута кнопка."""
    data = {k: hidden_value(page, k) for k in ("from", "next")}
    return post(app, f"/item/{item}", **{k: v for k, v in data.items() if v}, **extra)


# --- Верх ---


def test_top_has_back_link_place_and_small_status_without_buttons(conn, root, app):
    first, second, _ = build_review(conn, root)
    page = page_of(app, second, referer=f"http://{HOST}/review?f=ok")
    assert 'class="back-link" href="/review?f=ok"' in page and "На перевірку" in page
    assert "2 з 2" in page  # у фільтрі «Готові» лише дві Картки, ця — друга
    assert 'class="status mini"' in page and "Робот" in page and "ШІ" in page and "Бот" in page
    assert 'action="/robot"' not in page and 'action="/ai"' not in page  # без кнопок
    assert 'data-panel="/robot/panel?mini=1' in page
    # без списку, звідки прийшли, — вкладка самої Картки
    assert 'class="back-link" href="/review"' in page_of(app, first)
    assert "1 з 3" in page_of(app, first)


def test_back_link_ignores_foreign_referer_and_junk(conn, root, app):
    item = ready(conn, root)
    for referer in ("http://evil.example/review?f=ok", "http://127.0.0.1:1/review", "javascript:1"):
        page = page_of(app, item, referer=referer)
        assert 'class="back-link" href="/review"' in page, referer
    junk = page_of(app, item, f"/item/{item}?from=//evil.example/review")
    assert 'class="back-link" href="/review"' in junk


def test_mini_panel_endpoint_has_dots_and_no_forms(conn, app):
    page = html(fetch(app, "/robot/panel?mini=1&back=/review"))
    assert 'class="status mini"' in page and "<form" not in page and "<button" not in page


def test_title_and_meta(conn, root, app):
    item = ready(conn, root)
    page = page_of(app, item)
    assert "Пост 1342 · 01.03.2025" in re.sub(r"\s+", " ", page)
    assert "папка" not in page.lower()


# --- Що перевірити ---


def test_yellow_note_lists_fields_and_reasons(conn, root, app):
    season = ai.Field("season", "Зимаа", True, "значення немає в довіднику")
    item = add_card(conn, root, text=TEXT, blank=blank_with(season))
    page = page_of(app, item)
    assert "Перевір 1 поле: Сезон" in page and "значення немає в довіднику" in page
    assert page.index('class="attention"') < page.index('id="fields"')


def test_yellow_note_with_two_fields_names_each_reason(conn, root, app):
    item = add_card(
        conn, root, text=TEXT,
        blank=blank_with(ai.Field("season", "Зимаа", True, "сезон?"), ai.Field("brand", "x", True, "бренд?")),
    )  # fmt: skip
    page = page_of(app, item)
    assert "Перевір 2 поля: Бренд, Сезон" in page or "Перевір 2 поля: Сезон, Бренд" in page
    assert "Сезон: сезон?" in page and "Бренд: бренд?" in page


def test_note_without_yellow_shows_note_text(conn, root, app):
    item = ready(conn, root)
    set_note(conn, item, "season_changed", "Сезон змінено правилом: було Зима → стало Демісезон")
    page = page_of(app, item)
    assert (
        'class="attention"' in page
        and "Сезон змінено правилом: було Зима → стало Демісезон" in page
    )
    assert "Перевір " not in page


def test_no_note_when_nothing_to_check(conn, root, app):
    assert 'class="attention"' not in page_of(app, ready(conn, root))


def test_season_was_is_shown_on_yellow_season(conn, root, app):
    season = ai.Field("season", "Зимаа", True, "значення немає в довіднику")
    item = add_card(conn, root, text=TEXT, blank=blank_with(season))
    set_note(conn, item, "season_changed", "Сезон змінено правилом: було Зима → стало Демісезон")
    assert "було «Зима»" in page_of(app, item)


def test_missing_required_field_is_red(conn, root, app):
    item = add_card(conn, root, text=TEXT, blank=blank_with(ai.Field("sizes", [])))
    page = page_of(app, item)
    assert 'class="robot-error"' in page and "Щоб схвалити: вкажіть розмір" in page
    assert re.search(r'class="field[^"]*\bmissing[^"]*" data-field="sizes"', page)


# --- Поля ---


def order_of(page: str, *needles: str) -> list[int]:
    return [page.index(n) for n in needles]


def test_yellow_main_field_goes_first_in_yellow_frame(conn, root, app):
    item = add_card(
        conn, root, text=TEXT,
        blank=blank_with(ai.Field("season", "Зимаа", True, "значення немає в довіднику")),
    )  # fmt: skip
    page = page_of(app, item)
    yellow, title = order_of(page, 'class="field yellow" data-field="season"', 'data-field="title"')
    assert yellow < title
    assert page.count('data-field="season"') == 1  # не двічі
    assert 'value="confirm:season"' in page


def test_plain_fields_keep_main_order(conn, root, app):
    page = page_of(app, ready(conn, root))
    names = [
        "title",
        "price",
        "sizes",
        "section",
        "brand",
        "condition",
        "colors",
        "season",
        "material_upper",
    ]
    positions = order_of(page, *[f'data-field="{n}"' for n in names])
    assert positions == sorted(positions)
    assert "У пості 1200 грн → на Шафі +15 % = 1380" in page
    assert "Устілка: у пості 24,5 см (на Шафі: 24 і 24,5)" in page


def test_rare_fields_are_folded_and_open_when_yellow(conn, root, app):
    closed = page_of(app, ready(conn, root))
    assert '<details class="rare">' in closed
    for name in ("membrane", "insole", "keywords", "description"):
        assert closed.index('<details class="rare">') < closed.index(f'data-field="{name}"')
    assert "Ще поля:" in closed

    item = add_card(
        conn, root, folder="1343", post_id=1343, text=TEXT,
        blank=blank_with(ai.Field("style", "Повсякденний", True, "стиль під сумнівом")),
    )  # fmt: skip
    opened = page_of(app, item)
    assert '<details class="rare" open>' in opened
    assert 'class="field yellow" data-field="style"' in opened
    assert opened.index('<details class="rare" open>') < opened.index('data-field="style"')


def test_chip_markup_is_there_and_plain_fields_stay(conn, root, app):
    page = page_of(app, ready(conn, root))
    for target in ("season", "colors", "material_upper", "condition"):
        assert f'data-target="{target}"' in page
    assert 'name="season" value="Зима"' in page  # звичайне поле — його й приймає сервер
    assert page.count('name="colors"') == 2 and 'name="condition"' in page
    assert 'data-color="Чорний"' in page
    own = ('<script src="/static/theme.js"></script>', '<script src="/static/app.js"></script>')
    assert "<script" not in page.replace(own[0], "").replace(own[1], "")


@pytest.mark.parametrize("status_", [Status.APPROVED, Status.PUBLISHED])
def test_read_only_card_has_disabled_fields_and_no_save_buttons(conn, root, app, status_):
    page = page_of(app, ready(conn, root, status=status_))
    assert " disabled>" in page and "data-readonly" in page
    assert 'value="approve"' not in page and 'value="reject"' not in page


# --- Кожна дія на місці ---


def test_review_card_has_all_actions(conn, root, app):
    item = add_card(
        conn,
        root,
        text=TEXT,
        blank=blank_with(ORDER_YELLOW, ai.Field("season", "Зимаа", True, "?")),
    )
    page = page_of(app, item)
    for value in ("save", "approve", "reject", "ai_retry", "refill", "confirm:season",
                  "confirm:photo_order", "folder_order"):  # fmt: skip
        assert f'name="action" value="{value}"' in page, value
    assert page.count('value="save"') >= 2  # прихована (Enter) і видима
    assert "Схвалити і далі" in page and "Попросити ШІ заповнити заново" in page
    assert "¢" not in page
    # кнопки панелі належать формі полів, а «ШІ, спробуй ще раз» — окрема форма
    assert re.search(r'<button form="fields" name="action" value="approve"', page)
    assert re.search(r'<form method="post" action="/item/\d+" class="ai-retry">', page)


def test_action_bar_is_sticky_and_outside_main_form(conn, root, app):
    page = page_of(app, ready(conn, root))
    assert page.index('id="fields"') < page.index("</form>") < page.index('class="actionbar"')
    css = (Path(web.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.actionbar \{[^}]*position: sticky; bottom: 0", css)


def test_approved_and_archived_actions(conn, root, app):
    approved = page_of(app, ready(conn, root, status=Status.APPROVED))
    assert 'name="action" value="unapprove"' in approved
    archived = page_of(app, ready(conn, root, folder="1343", post_id=1343, status=Status.ARCHIVED))
    assert 'name="action" value="restore"' in archived


@pytest.mark.parametrize(
    ("code", "actions"),
    [
        ("no_photos", ["recheck"]),
        ("publish_uncertain", ["on_shafa", "not_on_shafa"]),
        ("publish_error", ["on_shafa", "not_on_shafa"]),
        ("possible_duplicate", ["other_item", "to_archive"]),
        ("shafa_declined", ["to_archive"]),
        ("already_published", ["to_archive"]),
    ],
)
def test_problem_card_has_its_buttons(conn, root, app, code, actions):
    item = ready(conn, root, status=Status.PROBLEM)
    set_note(conn, item, code, "причина")
    page = page_of(app, item)
    for value in actions:
        assert f'name="action" value="{value}"' in page, (code, value)
    assert 'class="problem-box"' in page and 'id="fields"' not in page
    assert 'class="actionbar"' not in page


def test_robot_error_disables_approve_with_explanation(conn, root, app):
    make_required(BOOTS, "Сезон")
    item = add_card(conn, root, blank=NO_SEASON, text=TEXT)
    page = page_of(app, item)
    assert 'value="approve" class="approve" disabled' in page
    assert (
        "Щоб схвалити: робот не зможе опублікувати — обов&#39;язкові поля Шафи без значення: Сезон"
        in page
    )
    assert page.count('class="robot-error"') == 1


# --- «Схвалити і далі» ---


def test_approve_goes_to_next_card_of_same_filter(conn, root, app):
    first, _, yellow = build_review(conn, root)
    # другий готовий жовтий, щоб у фільтрі «Треба глянути» було 2 Картки
    other = add_card(conn, root, folder="1345", post_id=1345, text=TEXT,
                     blank=blank_with(ai.Field("season", "Зимаа", True, "?")))  # fmt: skip
    ref = f"http://{HOST}/review?f=warn"
    page = page_of(app, yellow, referer=ref)
    assert hidden_value(page, "next") == f"/item/{other}?from=%2Freview%3Ff%3Dwarn"
    assert "1 з 2" in page
    response = send_form(app, page, yellow, action="approve")
    assert location(response) == (
        f"/item/{other}?from=%2Freview%3Ff%3Dwarn&msg=approved_next&ok={yellow}"
    )
    assert status(conn, yellow) == Status.APPROVED
    # останню жовту схвалили: у фільтрі порожньо, але «Готові» ще чекають — кажемо про них
    page = page_of(app, other, f"/item/{other}?from=%2Freview%3Ff%3Dwarn")
    assert hidden_value(page, "next") is None
    response = send_form(app, page, other, action="approve")
    assert location(response) == "/review?f=warn&msg=approved"
    page = html(fetch(app, location(response)))
    assert "Жовтих більше немає. Ще 2 Картки без жовтого чекають на «Схвалити»" in page
    assert "Усе переглянуто" not in page
    assert status(conn, other) == Status.APPROVED and status(conn, first) == Status.REVIEW


def test_unfiltered_next_follows_list_order_and_search(conn, root, app):
    first, second, _ = build_review(conn, root)
    page = page_of(app, first, referer=f"http://{HOST}/review?q={quote('1343 1342')}")
    assert hidden_value(page, "next") is None  # у пошуку «1343 1342» — жоден пост не збігається
    page = page_of(app, first)
    assert hidden_value(page, "next") == f"/item/{second}?from=%2Freview"


def test_reject_also_goes_next_and_last_says_all_done(conn, root, app):
    first, second, _ = build_review(conn, root)
    page = page_of(app, first)
    response = send_form(app, page, first, action="reject")
    assert location(response) == f"/item/{second}?from=%2Freview&msg=rejected&ok={first}"
    assert status(conn, first) == Status.ARCHIVED
    last = page_of(app, second, f"/item/{second}?from=%2Freview%3Ff%3Dok")
    assert hidden_value(last, "next") is None  # у «Готових» 1343 — остання (1342 вже в архіві)
    response = send_form(app, last, second, action="reject")
    assert location(response) == f"/review?f=ok&msg=rejected&ok={second}"  # жовта ще на перевірці


@pytest.mark.parametrize(
    "target",
    ["//evil.example/item/5", "https://evil.example/", "/settings", "/item/abc", "javascript:1",
     "/item/5\\@evil.example", "http://evil.example/item/2"],
)  # fmt: skip
def test_foreign_next_is_ignored(conn, root, app, target):
    first, *_ = build_review(conn, root)
    response = post(app, f"/item/{first}", action="approve", **{"from": "/review", "next": target})
    target = location(response)  # чужий next не береться: ідемо до пропущених цього ж списку
    assert target.startswith("/item/") and "msg=approved_left&n=2" in target
    assert "evil" not in target


def test_old_post_without_list_keeps_old_redirect(conn, root, app):
    first, *_ = build_review(conn, root)
    assert location(post(app, f"/item/{first}", action="approve")) == "/review?msg=approved"


def test_blocked_approve_stays_on_card_and_keeps_list(conn, root, app):
    item = add_card(conn, root, text=TEXT, blank=blank_with(ai.Field("sizes", [])))
    response = post(app, f"/item/{item}", action="approve", **{"from": "/review?f=warn"})
    assert location(response) == f"/item/{item}?from=%2Freview%3Ff%3Dwarn&msg=no_sizes"
    assert status(conn, item) == Status.REVIEW


def test_save_stays_on_card_with_list(conn, root, app):
    item = ready(conn, root)
    response = post(app, f"/item/{item}", action="save", **{"from": "/review?f=ok"}, brand="Nike")
    assert location(response) == f"/item/{item}?from=%2Freview%3Ff%3Dok&msg=saved"
    assert edits(conn, item)["brand"] == "Nike"


def test_safe_list_and_safe_next():
    assert web.safe_list("/review?f=warn&q=ecco&x=1") == "/review?f=warn&q=ecco"
    assert web.safe_list("/approved?f=warn") == "/approved"  # фільтр лише в «На перевірку»
    assert web.safe_list("/review?f=bad") == "/review"
    assert web.safe_list("/item/1") is None and web.safe_list("//evil.example/review") is None
    assert web.safe_list("/review/") is None and web.safe_list("") is None
    assert web.safe_list("http://127.0.0.1:1/review", {"127.0.0.1:1"}) == "/review"
    assert web.safe_list("https://127.0.0.1:1/review", {"127.0.0.1:1"}) is None
    assert web.safe_next("/item/5?from=%2Freview%3Ff%3Dwarn") == "/item/5?from=%2Freview%3Ff%3Dwarn"
    assert web.safe_next("/item/5?from=//evil.example") == "/item/5"
    assert web.safe_next("//evil.example/item/5") is None and web.safe_next("") is None


# --- Браузер ---

BASE = "http://127.0.0.1:{}"


def open_card(page, live, item, from_="/review"):
    page.goto(BASE.format(live.port) + f"/item/{item}?from={quote(from_)}")
    page.wait_for_load_state("networkidle")


def test_browser_chips_write_what_the_server_accepts(conn, root, live, chromium, app):
    typed = ready(conn, root, folder="1343", post_id=1343)
    clicked = ready(conn, root)
    page = chromium.new_page()
    try:
        open_card(page, live, clicked)
        season = page.locator('.chipset[data-target="season"]')
        assert season.locator('[data-value="Зима"]').get_attribute("aria-pressed") == "true"
        season.locator('[data-value="Весна"]').click()
        assert page.input_value("#f-season") == "Зима, Весна"
        season.locator('[data-value="Літо"]').click()
        season.locator('[data-value="Літо"]').click()  # ще раз — знімає
        assert not page.locator("#f-season").is_visible()  # з JS видно чипи, не поле
        # колір: до двох, список із пошуком
        colors = page.locator('.chipset[data-target="colors"]')
        colors.locator(".add").click()
        colors.locator(".picker-search").fill("синій")
        assert colors.locator(".opt:visible").count() == 1
        colors.locator('.opt[data-value="Синій"]').click()
        colors.locator(".picker-search").fill("")
        colors.locator('.opt[data-value="Білий"]').dispatch_event("click")  # третій — не прийметься
        values = page.eval_on_selector_all('[name="colors"]', "els => els.map(e => e.value)")
        assert values == ["Чорний", "Синій"]
        assert colors.locator('.opt[data-value="Білий"]').get_attribute("aria-disabled") == "true"
        colors.locator('[data-remove="Чорний"]').click()
        values = page.eval_on_selector_all('[name="colors"]', "els => els.map(e => e.value)")
        assert values == ["Синій", ""]
        # матеріал: «+ матеріал»
        material = page.locator('.chipset[data-target="material_upper"]')
        material.locator(".add").click()
        material.locator('.opt[data-value="Замша"]').click()
        assert page.input_value("#f-material_upper") == "Шкіра, Замша"
        # стан
        page.locator('.chipset[data-target="condition"] [data-value="Новий"]').click()
        assert page.input_value("#f-condition") == "Новий"
        page.click('button[form="fields"][value="save"]')
        page.wait_for_url("**msg=saved")
    finally:
        page.close()

    # ті самі значення, введені руками у звичайні поля, дають ті самі правки
    post(app, f"/item/{typed}", action="save", season="Зима, Весна", colors=["Синій", ""],
         material_upper="Шкіра, Замша", condition="Новий")  # fmt: skip
    mine, theirs = edits(conn, clicked), edits(conn, typed)
    for name in ("season", "colors", "material_upper", "condition"):
        assert mine[name] == theirs[name], name


def test_browser_without_js_card_can_be_saved_and_approved(conn, root, live, chromium):
    item, other = ready(conn, root), ready(conn, root, folder="1343", post_id=1343)
    context = chromium.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        open_card(page, live, item)
        assert page.locator("#f-season").is_visible()  # звичайне поле
        assert not page.locator('.chipset[data-target="season"]').is_visible()
        page.fill("#f-season", "Весна")
        page.click('button[form="fields"][value="save"]')
        page.wait_for_load_state("networkidle")
        assert edits(conn, item)["season"] == ["Весна"]
        page.click('button[form="fields"][value="approve"]')
        page.wait_for_load_state("networkidle")
        assert status(conn, item) == Status.APPROVED
        assert page.url.endswith(
            f"/item/{other}?from=%2Freview&msg=approved_next&ok={item}"
        )  # без JS
        assert "Схвалено «" in page.inner_text(".message")
    finally:
        context.close()


def test_browser_approve_and_next_and_last(conn, root, live, chromium):
    first, second, yellow = build_review(conn, root)
    page = chromium.new_page()
    try:
        base = BASE.format(live.port)
        page.goto(f"{base}/review")
        page.click(f'a[href="/item/{first}"]')
        page.wait_for_load_state("networkidle")
        assert "1 з 3" in page.inner_text(".place")
        for expected in (second, yellow):
            page.click('button[value="approve"]')
            page.wait_for_load_state("networkidle")
            assert page.url.split("?")[0].endswith(f"/item/{expected}")
        page.click('button[value="approve"]')
        page.wait_for_load_state("networkidle")
        assert page.url.endswith("/review")  # A23: `?msg=` прибрано з адреси після показу
        assert "На перевірці більше нічого немає" in page.inner_text(".message")
        assert {status(conn, i) for i in (first, second, yellow)} == {Status.APPROVED}
    finally:
        page.close()


def test_browser_photos_thumbs_change_big_and_arrows(conn, root, live, chromium):
    item = ready(conn, root)  # порядок фото зі бланка: IMG_2 перша
    page = chromium.new_page()
    try:
        open_card(page, live, item)
        hrefs = page.eval_on_selector_all(
            ".thumbs .photo", "els => els.map(e => e.getAttribute('href'))"
        )
        assert len(hrefs) == 2 and hrefs[0] != hrefs[1]
        big = lambda: page.get_attribute(".big img", "src")
        assert big().endswith(hrefs[0])
        page.click(".thumbs .photo:nth-child(2)")
        assert big().endswith(hrefs[1])
        assert page.get_attribute(".thumbs .photo:nth-child(2)", "aria-current") == "true"
        assert page.get_attribute(".thumbs .photo:nth-child(1)", "aria-current") is None
        page.keyboard.press("ArrowLeft")
        assert big().endswith(hrefs[0])
        page.click(".big")
        assert page.locator("#viewer").evaluate("d => d.open")
        page.keyboard.press("ArrowRight")
        assert page.get_attribute("#viewer img", "src").endswith(hrefs[1])
        assert big().endswith(hrefs[1])
    finally:
        page.close()


def test_browser_yellow_in_rare_is_open_and_bar_is_always_visible(conn, root, live, chromium):
    item = add_card(conn, root, text=TEXT,
                    blank=blank_with(ai.Field("style", "Повсякденний", True, "стиль під сумнівом")))  # fmt: skip
    page = chromium.new_page()
    try:
        page.set_viewport_size({"width": 1400, "height": 500})
        open_card(page, live, item)
        assert page.locator("details.rare").evaluate("d => d.open")
        assert page.locator("#f-style").is_visible()
        bar = page.locator(".actionbar")
        page.mouse.wheel(0, 0)
        assert bar.bounding_box()["y"] + bar.bounding_box()["height"] <= 501
        page.evaluate("window.scrollTo(0, 0)")
        assert bar.is_visible() and page.locator('button[value="approve"]').is_visible()
    finally:
        page.close()


SHOTS = Path(os.environ.get("SHAFA_SHOTS", ""))


@pytest.mark.parametrize("width", [1400, 390])
def test_browser_screenshots_of_card(conn, root, live, chromium, tmp_path, width):
    from PIL import Image

    out = SHOTS if os.environ.get("SHAFA_SHOTS") else tmp_path
    out.mkdir(parents=True, exist_ok=True)
    colors = ["#c96f4a", "#4a7fc9"]

    def paint(folder):
        for n, photo in enumerate(sorted((root / folder).glob("*.JPG"))):
            Image.new("RGB", (800, 600), colors[n % 2]).save(photo, format="JPEG")

    plain = ready(conn, root, folder="6601", post_id=6601)
    yellow = add_card(
        conn, root, folder="6602", post_id=6602, text=TEXT,
        blank=blank_with(ai.Field("season", ["Зима"], True, "«термо» у Вікторії — це мембрана, не утеплення")),
    )  # fmt: skip
    set_note(conn, yellow, "season_changed", "Сезон змінено правилом: було Зима → стало Демісезон")
    dup = ready(conn, root, folder="6603", post_id=6603, status=Status.PROBLEM)
    set_note(conn, dup, "possible_duplicate", "Схоже оголошення — ШІ не певен")
    with conn:
        conn.execute(
            "UPDATE items SET duplicate_url = 'https://shafa.ua/uk/item/1-x', "
            "duplicate_name = 'Шкіряні черевики Ecco 38' WHERE id = ?", (dup,))  # fmt: skip
    for folder in ("6601", "6602", "6603"):
        paint(folder)
    page = chromium.new_page()
    try:
        page.set_viewport_size({"width": width, "height": 1000})
        for name, item in (("card", plain), ("card-yellow", yellow), ("card-problem", dup)):
            page.goto(BASE.format(live.port) + f"/item/{item}?from=%2Freview")
            page.wait_for_load_state("networkidle")
            shot = out / f"{name}-{width}.png"
            page.screenshot(path=str(shot))
            assert shot.stat().st_size > 5000
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
                name
            )
        # 390: панель дій лишається в межах екрана
        page.goto(BASE.format(live.port) + f"/item/{yellow}?from=%2Freview")
        bar = page.locator(".actionbar").bounding_box()
        assert bar["x"] >= 0 and bar["x"] + bar["width"] <= width + 1
    finally:
        page.close()
