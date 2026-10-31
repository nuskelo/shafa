"""UX-004: Проблеми й Архів — що сталось, що робити, без пасток.

Дизайн не міняли: перевіряємо тексти й поведінку. Усе — у tmp_path, без мережі й справжніх даних.
"""

import re

import pytest

from shafa import review, scan, web
from shafa.db import Status
from shafa.robot import publish
from test_review import add_card
from test_ui004 import ready, set_note
from test_ui006 import row, status_of
from test_web import PORT, TOKEN, get, html, location, post

LINK = "https://shafa.ua/uk/item/12345678-test"


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def problem(conn, root, code, text="Причина від програми", folder="1342", post_id=1342, **kw):
    item = ready(conn, root, folder=folder, post_id=post_id, status=Status.PROBLEM, **kw)
    set_note(conn, item, code, text)
    return item


def archived(conn, root, code=None, text=None, folder="1342", post_id=1342):
    item = ready(conn, root, folder=folder, post_id=post_id, status=Status.ARCHIVED)
    with conn:
        conn.execute(
            "UPDATE items SET archived_at = ?, problem_code = ?, problem_text = ? WHERE id = ?",
            ("2026-10-30T10:00:00+00:00", code, text, item),
        )
    return item


def buttons(fragment: str) -> set[str]:
    return set(re.findall(r'name="action" value="([^"]+)"', fragment))


# --- B10: проданої пари з Архіву просто так не повернути ---


def test_sold_card_is_not_restored_without_confirmation(conn, root, app):
    item = archived(conn, root, "post_deleted", "Пост видалено з каналу — продано")
    response = post(app, f"/item/{item}", action="restore", **{"from": "/archived"})
    page = html(response)  # сторінка-підтвердження, а не перехід
    assert "пару, мабуть, продано. Повернути все одно?" in page
    assert 'name="sure" value="1"' in page
    assert status_of(conn, item)[0] == Status.ARCHIVED


def test_sold_card_is_restored_after_confirmation(conn, root, app):
    item = archived(conn, root, "post_deleted", "Пост видалено з каналу — продано")
    response = post(app, f"/item/{item}", action="restore", sure="1", **{"from": "/archived"})
    assert location(response).startswith("/archived?msg=restored")
    assert status_of(conn, item)[0] == Status.REVIEW


def test_core_refuses_unconfirmed_restore_of_sold_card(conn, root):
    item = archived(conn, root, "post_deleted", "x")
    assert review.act(conn, item, "restore") == review.CONFIRM_RESTORE
    assert status_of(conn, item)[0] == Status.ARCHIVED
    assert review.act(conn, item, "restore", confirmed=True) == "restored"


def test_other_archived_card_restores_without_question(conn, root, app):
    item = archived(conn, root)  # відхилена нею самою
    response = post(app, f"/item/{item}", action="restore", **{"from": "/archived"})
    assert location(response).startswith("/archived?msg=restored")
    assert status_of(conn, item)[0] == Status.REVIEW


def test_card_page_restore_button_also_asks(conn, root, app):
    item = archived(conn, root, "post_deleted", "x")
    page = html(get(app, f"/item/{item}?from=/archived"))
    assert "Повернути</button>" in page
    again = html(post(app, f"/item/{item}", action="restore"))
    assert "Повернути все одно?" in again


# --- B5, B14: незворотне — з підтвердженням ---


def test_archive_of_no_restore_problem_asks_first(conn, root, app):
    item = problem(conn, root, "many_items")
    page = html(post(app, f"/item/{item}", action="to_archive", **{"from": "/problem"}))
    assert "Картку не можна буде повернути. Точно в архів?" in page
    assert status_of(conn, item) == (Status.PROBLEM, "many_items")
    where = location(
        post(app, f"/item/{item}", action="to_archive", sure="1", **{"from": "/problem"})
    )
    assert where.startswith("/problem?msg=duplicate_archived")
    assert status_of(conn, item)[0] == Status.ARCHIVED


def test_archive_of_restorable_problem_does_not_ask(conn, root, app):
    item = problem(conn, root, "size_gone", "Розмір 43 прибрано з поста — можливо, продано")
    post(app, f"/item/{item}", action="to_archive", **{"from": "/problem"})
    assert status_of(conn, item)[0] == Status.ARCHIVED


def test_reject_is_named_and_offers_undo(conn, root, app):
    item = ready(conn, root)
    page = html(get(app, f"/item/{item}?from=/review"))
    assert "Не публікувати (в Архів)" in page and ">Відхилити<" not in page
    where = location(post(app, f"/item/{item}", action="reject", **{"from": "/review"}))
    assert f"ok={item}" in where
    shown = html(get(app, where))
    assert "відкладено в Архів на 7 днів" in shown
    assert f'action="/item/{item}"' in shown and 'value="restore"' in shown
    post(app, f"/item/{item}", action="restore", **{"from": "/review"})
    assert status_of(conn, item)[0] == Status.REVIEW


def test_no_undo_for_card_that_cannot_return(conn, root, app):
    item = archived(conn, root)
    with conn:
        conn.execute("UPDATE items SET shafa_id = 5 WHERE id = ?", (item,))
    assert 'value="restore"' not in html(get(app, f"/review?msg=rejected&ok={item}"))


# --- B5: «Опублікувала вручну» ---

HAND = ["many_items", "no_section", "possible_duplicate", "robot_field_missing", "publish_error"]


@pytest.mark.parametrize("code", HAND)
def test_published_by_hand_moves_card_to_published(conn, root, app, code):
    item = problem(conn, root, code)
    response = post(
        app, f"/item/{item}", action="on_shafa", shafa_link=LINK, **{"from": "/problem"}
    )
    assert location(response).startswith(f"/{Status.PROBLEM}?msg=on_shafa")
    row_ = conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()
    assert row_["status"] == Status.PUBLISHED
    assert row_["shafa_id"] == 12345678 and "12345678" in row_["shafa_url"]
    assert row_["problem_code"] is None and row_["published_at"]


@pytest.mark.parametrize("link", ["https://example.com/uk/item/12345678", "щось", "shafa.ua/uk/"])
def test_bad_link_leaves_card_in_place(conn, root, app, link):
    item = problem(conn, root, "many_items")
    response = post(
        app, f"/item/{item}", action="on_shafa", shafa_link=link,
        **{"from": "/problem", "stay": "list"},
    )  # fmt: skip
    assert location(response).startswith("/problem?msg=bad_link")
    assert status_of(conn, item) == (Status.PROBLEM, "many_items")
    assert conn.execute("SELECT shafa_id FROM items WHERE id = ?", (item,)).fetchone()[0] is None


@pytest.mark.parametrize("code", ["no_price", "ai_error", "photos_missing", "size_gone"])
def test_published_by_hand_only_for_its_codes(conn, root, app, code):
    item = problem(conn, root, code)
    post(app, f"/item/{item}", action="on_shafa", shafa_link=LINK)
    assert status_of(conn, item) == (Status.PROBLEM, code)


def test_published_by_hand_form_is_in_list_and_on_card(conn, root, app):
    item = problem(conn, root, "many_items")
    in_list = row(html(get(app, "/problem")), item)
    assert "Опублікувала вручну" in in_list and 'name="shafa_link"' in in_list
    on_card = html(get(app, f"/item/{item}?from=/problem"))
    assert "Опублікувала вручну" in on_card and 'name="shafa_link"' in on_card
    other = problem(conn, root, "no_price", folder="1343", post_id=1343)
    assert "Опублікувала вручну" not in row(html(get(app, "/problem")), other)


def test_published_by_hand_card_gets_nightly_sold_like_any_published(conn, root):
    """Після цього Картка — звичайна опублікована: `shafa_id` і `shafa_url` є, статус `published`."""
    item = problem(conn, root, "no_section")
    assert publish.on_shafa(conn, root, item, "12345678") == "on_shafa"
    card = review.load_card(conn, item)
    assert card.status == Status.PUBLISHED
    assert publish.on_shafa(conn, root, item, "12345679") == "stale"  # вдруге — вже не Проблема


# --- B3: Картка `new` не зникає без пояснень ---


def put_in_batch(conn, item, status="in_progress"):
    with conn:
        batch_id = conn.execute(
            "INSERT INTO ai_batches (status, created_at) VALUES (?, ?)",
            (status, "2026-10-31T09:30:00+00:00"),
        ).lastrowid
        conn.execute(
            "INSERT INTO ai_batch_items (batch_id, item_id, estimate_micro_usd, state) "
            "VALUES (?, ?, 1, 'pending')",
            (batch_id, item),
        )


def test_new_card_in_batch_explains_itself(conn, root, app):
    item = add_card(conn, root, status=Status.NEW)
    put_in_batch(conn, item)
    page = html(get(app, f"/item/{item}"))
    assert "Ця Картка чекає на ШІ (відправлено 31.10 о " in page
    assert "Зазвичай до години" in page and "Правити зараз не можна" in page


def test_counter_of_waiting_cards_next_to_tabs(conn, root, app):
    assert "Чекають на ШІ" not in html(get(app, "/review"))
    add_card(conn, root, status=Status.NEW)
    assert "Чекають на ШІ: 1" in html(get(app, "/review"))


def no_key(app):
    def raise_():
        from shafa import config

        raise config.ConfigError("Немає ANTHROPIC_API_KEY у .env")

    app.ai_client = raise_


def test_refill_without_key_does_not_say_cards_went_to_ai(conn, root, app):
    no_key(app)
    item = add_card(conn, root)
    with conn:
        conn.execute("UPDATE items SET rules_version = 0 WHERE id = ?", (item,))
    response = post(app, f"/item/{item}", action="refill")
    where = location(response)
    assert "ai_unavailable" in where
    shown = html(get(app, where))
    assert "пішли на ШІ" not in shown
    assert "ШІ зараз недоступний — Картку поставлено в чергу; скажіть Власнику." in shown


def test_new_card_without_key_says_ai_unavailable(conn, root, app):
    no_key(app)
    item = add_card(conn, root, status=Status.NEW)
    page = html(get(app, f"/item/{item}"))
    assert "ШІ зараз недоступний" in page and "Правити зараз не можна" in page


# --- Тексти Проблем ---


def hint_of(app, item):
    return html(get(app, f"/item/{item}?from=/problem"))


def test_many_items_text_is_truthful(conn, root, app):
    item = problem(conn, root, "many_items")
    page = hint_of(app, item)
    assert "робот публікує лише одну пару на пост" in page
    assert "розбирається сам" not in page


def test_size_gone_has_archive_button_and_text(conn, root, app):
    item = problem(conn, root, "size_gone", "Розмір 43 прибрано з поста — можливо, продано")
    assert "to_archive" in buttons(row(html(get(app, "/problem")), item))
    page = hint_of(app, item)
    assert (
        "Розмір 43 прибрали з поста. Продано — «В архів». Не продано — поверніть розмір у пост"
        in page
    )
    assert "to_archive" in buttons(page)


def test_photos_missing_shows_reason_openly_and_can_archive(conn, root, app):
    item = problem(conn, root, "photos_missing", "Папку з фото не знайдено — поверніть її на місце")
    page = hint_of(app, item)
    open_text = page.split("Подробиці для Власника")[0]
    assert "Папку з фото не знайдено — поверніть її на місце" in open_text
    assert "to_archive" in buttons(page)
    assert "to_archive" in buttons(row(html(get(app, "/problem")), item))
    post(app, f"/item/{item}", action="to_archive", **{"from": "/problem"})
    assert status_of(conn, item)[0] == Status.ARCHIVED


def test_robot_problems_have_review_and_archive_and_hide_screenshot(conn, root, app):
    item = problem(
        conn, root, "robot_field_missing",
        "Робот не знайшов на Шафі поле «Матеріал». Екран: 20261031-120000-1342-нема-поля.png",
    )  # fmt: skip
    in_list = row(html(get(app, "/problem")), item)
    assert {"to_review", "to_archive", "recheck"} <= buttons(in_list)
    assert "Повернути на перевірку (виправити поля)" in in_list
    page = hint_of(app, item)
    before_details = page.split("Подробиці для Власника")[0]
    assert "нема-поля.png" not in before_details and "нема-поля.png" in page
    assert {"to_review", "to_archive", "recheck"} <= buttons(page)
    assert "нема-поля.png" not in in_list.split("Подробиці для Власника")[0]


def test_to_review_returns_robot_problem_for_editing(conn, root, app):
    item = problem(conn, root, "robot_field_missing")
    where = location(post(app, f"/item/{item}", action="to_review", **{"from": "/problem"}))
    assert where.startswith("/review?msg=to_review")
    assert status_of(conn, item)[0] == Status.REVIEW
    assert "виправте поля" in html(get(app, where))


def test_to_review_refuses_other_codes_and_published_cards(conn, root, app):
    other = problem(conn, root, "no_price")
    post(app, f"/item/{other}", action="to_review")
    assert status_of(conn, other) == (Status.PROBLEM, "no_price")
    done = problem(conn, root, "robot_not_sent", folder="1343", post_id=1343)
    with conn:
        conn.execute("UPDATE items SET shafa_id = 7 WHERE id = ?", (done,))
    post(app, f"/item/{done}", action="to_review")
    assert status_of(conn, done)[0] == Status.PROBLEM


def test_logged_out_text_has_no_command_line(conn, root, app):
    item = problem(conn, root, "robot_logged_out", "Робот не увійшов на Шафу")
    page = hint_of(app, item)
    assert "python -m" not in page and "Власник:" not in page
    assert "Натисніть на панелі «Увійти на Шафу»" in page
    assert "python -m" not in html(get(app, "/problem"))


def test_stale_rules_text_has_no_missing_tab(conn, root, app):
    item = problem(conn, root, "stale_rules", publish.STALE_TEXT)
    page = hint_of(app, item) + html(get(app, "/problem"))
    assert "вкладка ШІ" not in page and "вкладки ШІ" not in page
    assert "Програму оновлено — Картку треба переглянути ще раз" in page


def test_publish_error_shows_shafa_answer_openly_with_review_button(conn, root, app):
    item = problem(conn, root, "publish_error", "Шафа відповіла: ціна має бути більше нуля")
    page = hint_of(app, item)
    assert "Шафа не прийняла при публікації" in page
    assert "Шафа відповіла: ціна має бути більше нуля" in page.split("Подробиці")[0]
    assert "to_review" in buttons(page) and "on_shafa" in buttons(page)


def test_declined_and_error_have_different_titles(conn, root, app):
    a = problem(conn, root, "publish_error")
    b = problem(conn, root, "shafa_declined", folder="1343", post_id=1343)
    assert "Шафа не прийняла при публікації" in hint_of(app, a)
    assert "Шафа відхилила оголошення" in hint_of(app, b)
    page = html(get(app, "/problem"))
    assert "Шафа не прийняла при публікації" in row(page, a)
    assert "Шафа відхилила оголошення" in row(page, b)


def test_no_section_text_explains_what_robot_publishes(conn, root, app):
    old = problem(conn, root, "no_section", scan.OLD_NO_SECTION_TEXT)  # Картка з бази до UX-004
    fresh = problem(conn, root, "no_section", scan.NO_SECTION_TEXT, folder="1343", post_id=1343)
    for item in (old, fresh):
        page = hint_of(app, item)
        assert "Робот публікує лише взуття й аксесуари" in page
        assert "окуляри" not in page.split("Подробиці для Власника")[0]
        assert "Опублікувала вручну" in page
        assert "Робот публікує лише взуття й аксесуари" in row(html(get(app, "/problem")), item)


def test_published_shows_plain_duplicate_verdict_with_link(conn, root, app):
    item = ready(conn, root, status=Status.PUBLISHED)
    with conn:
        conn.execute(
            "UPDATE items SET problem_code = 'duplicate_checked', problem_text = ?, "
            "duplicate_url = ?, shafa_url = ? WHERE id = ?",
            (
                "Схоже: https://shafa.ua/uk/item/99-x — ШІ: інша пара (довгий абзац)",
                LINK,
                LINK,
                item,
            ),
        )
    page = html(get(app, "/published"))
    assert "Перевірено: на Шафі є схоже оголошення — це інша пара" in page
    assert "подивитись" in page and "довгий абзац" not in page
    assert "довгий абзац" in html(get(app, f"/item/{item}"))


def test_robot_will_skip_note_says_approve_is_fine(conn, root, app, monkeypatch):
    item = ready(conn, root)
    from shafa.robot.plan import Precheck

    monkeypatch.setattr(
        web, "precheck", lambda card, root: Precheck(None, ["Матеріал верху: немає"])
    )
    page = html(get(app, f"/item/{item}?from=/review"))
    assert "Робот пропустить:" in page
    assert "Схвалити можна — ці поля на Шафі будуть порожні." in page


def test_recheck_of_old_post_is_not_offered(conn, root, app):
    item = problem(conn, root, "many_items", folder="borh-3659", post_id=3659)
    assert "recheck" not in buttons(row(html(get(app, "/problem")), item))
    assert "recheck" not in buttons(hint_of(app, item))
    fresh = problem(conn, root, "many_items", folder="1343", post_id=1343)
    assert "recheck" in buttons(row(html(get(app, "/problem")), fresh))


def test_recheck_many_items_message_matches_code(conn, root, app, monkeypatch):
    item = problem(conn, root, "many_items", text="У пості кілька товарів з різними цінами")
    monkeypatch.setattr(
        review, "recheck", lambda *a: review.Recheck("recheck_problem", Status.PROBLEM)
    )  # пост не виправили: папка перевірена, проблема та сама
    where = location(post(app, f"/item/{item}", action="recheck", **{"from": "/problem"}))
    assert "recheck_many_items" in where
    shown = html(get(app, where))
    assert "у пості досі кілька пар чи розмірів" in shown


def test_old_post_is_named_plainly(conn, root, app):
    item = problem(
        conn, root, "no_section", "Папка borh-3659 без розділу", folder="borh-3659", post_id=3659
    )
    page = html(get(app, "/problem")) + hint_of(app, item)
    assert "borh" not in page
    assert "Старий пост 3659" in page


def test_no_internal_words_in_texts_for_her(conn, root, app):
    for code in web.PROBLEM_HINTS:
        text_ = web.PROBLEM_HINTS[code]
        assert "python -m" not in text_ and "вкладка ШІ" not in text_ and "borh" not in text_, code
    for label, todo in web.PROBLEM_KINDS.values():
        assert "borh" not in todo and "python -m" not in todo
        assert not re.search(
            r"\b(перейменуй|залиш|допиши|перевір|додай|опублікуй|відкрий|поверни)\b", todo
        )


# --- Архів, Опубліковані, інше ---


def test_archive_shows_reason_deletion_and_why_not_restorable(conn, root, app):
    sold = archived(conn, root, "post_deleted", "Пост видалено з каналу — продано")
    many = archived(conn, root, "many_items", "У пості кілька товарів", folder="1343", post_id=1343)
    page = html(get(app, "/archived"))
    assert "В Архіві: пост видалено з каналу — пару, мабуть, продано" in page
    assert "видалиться з програми" in page
    assert "Повернути не можна: робот не публікує такі пости" in page
    assert "Тут Картки, які не підуть на Шафу. Через 7 днів вони зникають з програми" in page
    on_card = html(get(app, f"/item/{many}?from=/archived"))
    assert "В Архіві: " in on_card and "Видалиться з програми " in on_card
    assert "(фото в папці лишаються)" in on_card
    assert "Повернути не можна:" in on_card
    assert "Повернути не можна" not in html(get(app, f"/item/{sold}?from=/archived"))


def test_refill_page_has_plain_title(conn, root, app):
    item = add_card(conn, root)
    with conn:
        conn.execute("UPDATE items SET rules_version = 0 WHERE id = ?", (item,))
    page = html(get(app, "/refill"))
    assert "<h1>Заповнити заново</h1>" in page or "Немає Карток" in page


def test_old_rules_label_is_plain():
    template = (web.PACKAGE / "templates" / "list.html").read_text(encoding="utf-8")
    card_html = (web.PACKAGE / "templates" / "card.html").read_text(encoding="utf-8")
    assert "можна заповнити краще" in template and "можна заповнити краще" in card_html
    assert "заповнено старими правилами" not in template + card_html


def test_existing_form_buttons_keep_their_names(conn, root, app):
    """Форми `action/value/name` існуючих кнопок — без змін."""
    a = problem(conn, root, "possible_duplicate")
    page = hint_of(app, a)
    assert {"other_item", "to_archive", "on_shafa"} <= buttons(page)
    b = problem(conn, root, "publish_uncertain", folder="1343", post_id=1343)
    assert {"on_shafa", "not_on_shafa"} <= buttons(hint_of(app, b))
    assert 'name="shafa_link"' in hint_of(app, b)
    c = problem(conn, root, "shafa_declined", folder="1344", post_id=1344)
    assert "to_archive" in buttons(hint_of(app, c))
    d = ready(conn, root, folder="1345", post_id=1345)
    assert {"save", "approve", "reject", "ai_retry", "refill"} <= buttons(
        html(get(app, f"/item/{d}?from=/review"))
    )
