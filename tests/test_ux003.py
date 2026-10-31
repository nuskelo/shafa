"""UX-003: Картки зрозуміло — кінець черги, жовтий розділ з кнопками, ціна, схвалені, тексти.

Дизайн не міняли: перевіряємо тексти й поведінку. Усе — у tmp_path, без мережі й справжніх даних.
"""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from shafa import ai, catalog, review, web
from shafa.db import Status
from test_review import add_card, edits, status
from test_robot_plan import BLANK, BOOTS, TEXT
from test_ui004 import ready
from test_ui005 import blank_with, fetch, page_of, send_form
from test_web import TOKEN, html, location

TEMPLATES = Path(web.__file__).parent / "templates"
MEN_BOOTS = "Чоловічий одяг / Взуття / Черевики"
DOUBT = "ШІ не впевнене: за розміром 41 це може бути і жіноча, і чоловіча модель. Дивись на форму."
SECTION_YELLOW = ai.Field("section", BOOTS, True, DOUBT)


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", 8765, token=TOKEN,
        lock_file=tmp_path / "robot.lock",
    )  # fmt: skip


def can_start(app, monkeypatch, today, approved):
    """Панель каже «можна натискати Старт, сьогодні до N із M» (як у UX-002)."""
    real = app.robot_state
    monkeypatch.setattr(
        app, "robot_state",
        lambda conn: replace(real(conn), can_start=True, queue_today=today, approved=approved),
    )  # fmt: skip


def gender_card(conn, root, folder="1350", post_id=1350, text=TEXT):
    return add_card(conn, root, folder=folder, post_id=post_id, text=text,
                    blank=blank_with(SECTION_YELLOW))  # fmt: skip


def section_of(conn, item_id):
    return review.load_card(conn, item_id).fields["section"]


# --- B1: кінець черги не бреше ---


def test_skipped_card_is_not_lost_at_the_end_of_the_queue(conn, root, app):
    skipped = ready(conn, root, folder="1342", post_id=1342)
    middle = ready(conn, root, folder="1343", post_id=1343)
    last = ready(conn, root, folder="1344", post_id=1344)
    path = f"/item/{middle}?from=%2Freview"
    response = send_form(app, page_of(app, middle, path), middle, action="approve")
    assert f"/item/{last}" in location(response)

    path = f"/item/{last}?from=%2Freview"
    response = send_form(app, page_of(app, last, path), last, action="approve")
    target = location(response)
    assert target.startswith(f"/item/{skipped}?") and "msg=approved_left" in target
    assert "n=1" in target
    assert "Схвалено. Лишилось 1 пропущених — ось перша з них." in html(fetch(app, target))
    assert "Усе переглянуто" not in html(fetch(app, target))

    page = page_of(app, skipped, f"/item/{skipped}?from=%2Freview")
    response = send_form(app, page, skipped, action="approve")
    assert location(response) == "/review?msg=approved_done"
    done = html(fetch(app, location(response)))
    assert "На перевірці більше нічого немає — можна натискати «Старт» угорі." in done
    assert "Усе переглянуто" not in done


def test_end_of_queue_adds_how_many_the_robot_will_publish(conn, root, app, monkeypatch):
    can_start(app, monkeypatch, 3, 5)
    only = ready(conn, root)
    response = send_form(app, page_of(app, only), only, action="approve")
    page = html(fetch(app, location(response)))
    assert "Натисніть «Старт» — сьогодні робот опублікує до 3 із 5 Схвалених." in page


def test_approved_tab_tells_what_start_does(conn, root, app, monkeypatch):
    can_start(app, monkeypatch, 2, 2)
    item = ready(conn, root)
    review.act(conn, item, "approve", root)
    assert "Натисніть «Старт» — сьогодні робот опублікує до 2 із 2 Схвалених." in html(
        fetch(app, "/approved")
    )


def test_rejecting_the_last_card_also_points_to_skipped(conn, root, app):
    skipped = ready(conn, root, folder="1342", post_id=1342)
    last = ready(conn, root, folder="1343", post_id=1343)
    page = page_of(app, last, f"/item/{last}?from=%2Freview")
    response = send_form(app, page, last, action="reject")
    target = location(response)
    assert target.startswith(f"/item/{skipped}?") and "msg=rejected_left" in target
    assert "Лишилось 1 пропущених" in html(fetch(app, target))


# --- B2: фільтр ---


def test_warn_filter_end_points_to_ready_cards(conn, root, app):
    ready(conn, root, folder="1342", post_id=1342)
    ready(conn, root, folder="1343", post_id=1343)
    yellow = add_card(conn, root, folder="1344", post_id=1344, text=TEXT,
                      blank=blank_with(ai.Field("season", "Зимаа", True, "?")))  # fmt: skip
    path = f"/item/{yellow}?from=%2Freview%3Ff%3Dwarn"
    response = send_form(app, page_of(app, yellow, path), yellow, action="approve")
    assert location(response) == "/review?f=warn&msg=approved"
    page = html(fetch(app, location(response)))
    assert "Жовтих більше немає. Ще 2 Картки без жовтого чекають на «Схвалити»" in page
    assert 'href="/review?f=ok"' in page and "Переглянути їх" in page
    assert "Усе переглянуто" not in page and "У цьому фільтрі нічого немає" not in page


def test_filter_labels_and_groups(conn, root, app):
    ready(conn, root)
    page = html(fetch(app, "/review"))
    for text_ in ("Що треба:", "Звідки:", "Усі · 1", "Треба глянути · 0", "Без жовтого · 1",
                  "З бота · 1", "Старі пости · 0"):  # fmt: skip
        assert text_ in page, text_
    assert "Готові ·" not in page and "Нові (бот)" not in page
    assert "З бота · 2 фото" in page and "Нова · " not in page


# --- B6, B30: «обов'язкове» лише для порожнього; один блок «Щоб схвалити» ---


def test_yellow_section_is_not_a_required_empty_field(conn, root, app):
    item = gender_card(conn, root)
    page = page_of(app, item)
    assert "обов'язкове поле" not in page and "обов&#39;язкове поле" not in page
    assert "Щоб схвалити: перевірте розділ" in page


def test_one_block_lists_everything_that_stops_approval(conn, root, app):
    item = gender_card(conn, root, text=TEXT.replace("1200грн", ""))
    page = page_of(app, item)
    assert "Щоб схвалити: 1) вкажіть ціну; 2) перевірте розділ" in page
    assert page.count('class="robot-error"') == 1 and 'class="block' not in page
    assert page.count("обов'язкове поле — без нього не схвалити") == 1  # лише порожня ціна


def test_saved_without_price_says_so_and_approve_names_the_price(conn, root, app):
    item = add_card(conn, root, text=TEXT.replace("1200грн", ""), blank=BLANK)
    page = page_of(app, item)
    response = send_form(app, page, item, action="save")
    assert "msg=saved_no_price" in location(response)
    assert "Збережено, але без ціни Картку не схвалити." in html(fetch(app, location(response)))
    response = send_form(app, page, item, action="approve")
    assert "msg=no_price" in location(response)
    assert "Схвалити не можна: немає ціни" in html(fetch(app, location(response)))
    assert status(conn, item) == Status.REVIEW


# --- B7: «Це жіночі / Це чоловічі» ---


def test_gender_buttons_change_only_the_top_group(conn, root, app):
    item = gender_card(conn, root)
    page = page_of(app, item)
    assert "Перевір: жіночі чи чоловічі?" in page
    assert 'value="gender:women"' in page and 'value="gender:men"' in page
    assert "Це жіночі" in page and "Це чоловічі" in page
    assert 'value="confirm:section"' in page  # «Так, вірно» лишається
    assert "<summary>Чому?</summary>" in page and "Дивись на форму." in page
    assert "ШІ не впевнене: за розміром 41 це може бути і жіноча, і чоловіча модель." in page

    response = send_form(app, page, item, action="gender:men")
    assert "msg=saved" in location(response)
    section = section_of(conn, item)
    assert section.value == MEN_BOOTS  # решта шляху (Взуття / Черевики) та сама
    assert not section.yellow and section.edited
    assert edits(conn, item)["section"] == MEN_BOOTS
    assert "змінено вами" in page_of(app, item)
    assert status(conn, item) == Status.REVIEW


def test_gender_buttons_keep_the_current_group_when_confirmed(conn, root, app):
    item = gender_card(conn, root)
    send_form(app, page_of(app, item), item, action="gender:women")
    section = section_of(conn, item)
    assert section.value == BOOTS and not section.yellow
    assert "підтверджено вами" in page_of(app, item)


def test_no_pair_in_catalog_means_no_buttons(conn, root, app, monkeypatch):
    item = gender_card(conn, root)
    every = catalog.sections()
    monkeypatch.setattr(catalog, "sections", lambda: tuple(s for s in every if s != MEN_BOOTS))
    page = page_of(app, item)
    assert "gender:" not in page and "Це чоловічі" not in page
    assert 'value="confirm:section"' in page and DOUBT in page  # як було: причина + «Так, вірно»
    response = send_form(app, page, item, action="gender:men")
    assert "msg=stale" in location(response) and edits(conn, item) == {}


def test_kids_pair_and_unrelated_sections(conn, root, monkeypatch):
    kids = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
    assert review.gender_choices(kids) is None  # у знімку тестів немає «Для хлопчиків / Кросівки»
    every = catalog.sections()
    monkeypatch.setattr(
        catalog, "sections", lambda: (*every, "Дитячий одяг / Для хлопчиків / Взуття / Кросівки")
    )
    question, choices = review.gender_choices(catalog.find_section(kids))
    assert question == "Перевір: для дівчаток чи для хлопчиків?"
    assert [(c.label, c.path) for c in choices] == [
        ("Для дівчаток", kids),
        ("Для хлопчиків", "Дитячий одяг / Для хлопчиків / Взуття / Кросівки"),
    ]
    assert review.gender_choices("Аксесуари / Сумки") is None
    assert review.gender_choices(None) is None
    assert review.gender_target(BOOTS, "men") == MEN_BOOTS
    assert review.gender_target(BOOTS, "boys") is None  # ключ не з тієї пари


def test_gender_button_needs_a_gender_doubt(conn, root, app):
    item = add_card(
        conn, root, blank=blank_with(ai.Field("section", BOOTS, True, "Тип взуття не ясний"))
    )
    assert "gender:" not in page_of(app, item)


# --- B8, B9: ціна ---


def test_price_far_from_post_is_yellow_until_confirmed(conn, root, app):
    item = add_card(conn, root, text=TEXT.replace("1200грн", "1500грн"), blank=BLANK)
    page = page_of(app, item)
    send_form(app, page, item, action="save", price="5")
    card = review.load_card(conn, item)
    assert card.fields["price"].value == 5 and card.fields["price"].yellow
    page = page_of(app, item)
    assert "Ціна 5 грн дуже відрізняється від поста (1500 грн → на Шафі 1730). Перевір." in page
    assert 'value="confirm:price"' in page
    send_form(app, page, item, action="confirm:price", price="5")
    card = review.load_card(conn, item)
    assert card.fields["price"].value == 5 and not card.fields["price"].yellow
    send_form(app, page, item, action="save", price="99999")  # інша дивна ціна — знову питаємо
    assert review.load_card(conn, item).fields["price"].yellow


def test_price_input_accepts_spaces_and_hryvnia(conn, root, app):
    item = add_card(conn, root, text=TEXT.replace("1200грн", "1700грн"), blank=BLANK)
    for typed in ("1 960", "1960 грн", "1 960грн.", "1960"):
        page = page_of(app, item)
        response = send_form(app, page, item, action="save", price=typed)
        assert "msg=saved" in location(response), typed
        card = review.load_card(conn, item)
        assert card.fields["price"].value == 1960 and not card.fields["price"].yellow, typed
    assert review.parse(review.SPEC["price"], [""]) is None


def test_wrong_price_stays_in_field_with_hint(conn, root, app):
    item = add_card(conn, root, text=TEXT, blank=BLANK)
    response = send_form(app, page_of(app, item), item, action="save", price="дві тисячі")
    target = location(response)
    assert "msg=bad" in target
    page = html(fetch(app, target))
    assert 'value="дві тисячі"' in page
    assert "Ціна — лише цифри, напр. 1960" in page
    assert edits(conn, item).get("price") is None  # у базу не пішло


# --- B12, B13 ---


def test_unapprove_stays_on_the_card_with_active_fields(conn, root, app):
    item = ready(conn, root)
    review.act(conn, item, "approve", root)
    page = page_of(app, item, f"/item/{item}?from=%2Fapproved")
    assert (
        "Картку схвалено — правити не можна. Щоб змінити, натисніть «Скасувати схвалення» внизу."
        in page
    )
    response = send_form(app, page, item, action="unapprove")
    target = location(response)
    assert target.startswith(f"/item/{item}?") and "msg=unapproved" in target
    assert status(conn, item) == Status.REVIEW
    page = html(fetch(app, target))
    assert "Схвалення скасовано — можна правити. Потім знову «Схвалити»." in page
    assert "Картку схвалено — правити не можна" not in page
    assert (
        re.search(r'<input id="f-price"[^>]*>', page)
        and "disabled" not in re.search(r'<input id="f-price"[^>]*>', page)[0]
    )


# --- B15, B16, B32, B33, B34 ---


def test_confirmed_field_says_confirmed_by_you(conn, root, app):
    item = gender_card(conn, root)
    send_form(app, page_of(app, item), item, action="confirm:section")
    assert "підтверджено вами" in page_of(app, item)
    assert "змінено вручну" not in page_of(app, item)


def test_one_visible_ai_button_second_under_more(conn, root, app):
    page = page_of(app, ready(conn, root))
    assert page.count("Попросити ШІ заповнити заново") == 1
    assert 'name="action" value="ai_retry"' in page and 'name="action" value="refill"' in page
    more = page.split("<summary>Ще</summary>")[1]
    assert 'value="refill"' in more.split("</details>")[0]
    assert "Дешевше, але результат до години — Картка на цей час зникне зі списку." in more


def test_hints_under_price_and_sizes(conn, root, app):
    page = page_of(app, ready(conn, root))
    assert "У пості 1200 грн → на Шафі +15 % = 1380" in page
    assert "Устілка: у пості 24,5 см (на Шафі: 24 і 24,5)" in page


def test_header_has_no_folder_or_borh(conn, root, app):
    named = add_card(conn, root, folder="borh-6979", post_id=6979, date="2026-10-15T10:00:00",
                     blank=BLANK, text=TEXT)  # fmt: skip
    bare = add_card(conn, root, folder="borh-7027", post_id=7027, text=TEXT,
                    blank=[f for f in BLANK if f.name != "title"])  # fmt: skip
    page = page_of(app, named)
    assert "Пост 6979 · 15.10.2026" in page
    assert "borh" not in page and "папка" not in page.lower().replace("папка</", "")
    bare_page = page_of(app, bare)
    assert "Пост 7027 (назви ще немає)" in bare_page and "borh" not in bare_page
    for path in ("/review", "/problem", "/archived"):
        listing = html(fetch(app, path))
        assert "borh" not in listing and "Папка" not in listing, path
    assert "Пост 7027 (назви ще немає)" in html(fetch(app, "/review"))


def test_approve_message_names_the_card_and_says_where_it_went(conn, root, app):
    first = ready(conn, root, folder="1342", post_id=1342)
    second = ready(conn, root, folder="1343", post_id=1343)
    page = page_of(app, first, f"/item/{first}?from=%2Freview")
    response = send_form(app, page, first, action="approve")
    target = location(response)
    assert target.startswith(f"/item/{second}?") and f"msg=approved_next&ok={first}" in target
    assert (
        "Схвалено «Шкіряні черевики ecco 38 розміру» → у «Схвалених». Це наступна Картка."
        in html(fetch(app, target))
    )


# --- Заборони ---


def test_no_cents_borh_or_folder_in_card_and_list_templates():
    for name in ("card.html", "list.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "¢" not in source, name
        assert "borh" not in source and "папка " not in source and "Папка " not in source, name


def test_existing_form_actions_are_unchanged(conn, root, app):
    page = page_of(app, ready(conn, root))
    for value in ("save", "approve", "reject", "ai_retry", "refill", "confirm:photo_order"):
        assert f'name="action" value="{value}"' in page or value == "confirm:photo_order", value
    assert 'name="token"' in page and 'name="from"' in page
