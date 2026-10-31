"""QA-002: що скаже робот про Картку — ще до «Схвалити» (екран, схвалення, `fill --precheck`)."""

import pytest

from shafa import __main__ as cli
from shafa import ai, catalog, fill, review, web
from shafa.db import Status
from shafa.robot.plan import Precheck, plan, precheck
from test_review import add_card, status
from test_robot_plan import BLANK, BOOTS, TEXT
from test_web import PORT, TOKEN, get, html, location, post

BACKPACK = [
    ai.Field("title", "Рюкзак шкіряний"),
    ai.Field("section", "Аксесуари / Сумки та рюкзаки / Рюкзаки"),
    ai.Field("colors", ["Коричневий"]),
    ai.Field("gender", "Для жінок"),
    ai.Field("season", ["Зима"]),  # у рюкзаків такого поля на Шафі немає
]
NO_SEASON = [f for f in BLANK if f.name != "season"]


def make_required(path, title):
    """Шафа може зробити поле обов'язковим; бланк без нього робот не заповнить."""
    for c in catalog.section(path)["characteristics"]:
        c["required"] |= c["title"] == title


def review_card(conn, root, blank=BLANK, **kw):
    return review.load_card(conn, add_card(conn, root, blank=blank, text=TEXT, **kw))


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


# --- precheck ---


def test_ready_card_in_review_has_no_error_and_no_notes(conn, root):
    assert precheck(review_card(conn, root), root) == Precheck(None, [])


def test_missing_required_field_is_an_error(conn, root):
    make_required(BOOTS, "Сезон")
    check = precheck(review_card(conn, root, blank=NO_SEASON), root)
    assert check.error == "Обов'язкові поля Шафи без значення: Сезон"


def test_value_outside_the_list_is_a_note_only(conn, root):
    blank = [f for f in BLANK if f.name != "type"] + [ai.Field("type", "Мартінси")]
    assert precheck(review_card(conn, root, blank=blank), root) == Precheck(
        None, ["Тип: немає у списку Шафи — Мартінси"]
    )


def test_field_missing_in_the_section_is_a_note_only(conn, root):
    check = precheck(review_card(conn, root, blank=BACKPACK), root)
    assert check.error is None
    assert check.notes == ["Сезон: у розділі Шафи такого поля немає"]


def test_missing_photo_file_is_an_error(conn, root):
    card = review_card(conn, root)
    (root / "1342" / "IMG_1.JPG").unlink()
    assert precheck(card, root).error == "Немає фото: IMG_1.JPG"
    assert precheck(card, None).error is None  # без Головної папки файли не перевіряються


def test_card_without_known_section_is_left_to_approve_block(conn, root):
    blank = [f for f in BLANK if f.name != "section"]
    card = review_card(conn, root, blank=blank)
    assert precheck(card, root) == Precheck(None, [])
    assert card.approve_block() == "no_section"


def test_plan_of_approved_card_is_the_same_as_before(conn, root):
    card = review_card(conn, root, status=Status.APPROVED)
    steps, notes = plan(card, root)
    assert notes == [] and steps[-1].target == "price"
    assert precheck(card, root).notes == notes  # одне ядро


# --- Екран Картки ---


def test_card_page_red_block_and_disabled_approve(conn, app, root):
    make_required(BOOTS, "Сезон")
    item_id = add_card(conn, root, blank=NO_SEASON, text=TEXT)
    page = html(get(app, f"/item/{item_id}"))
    assert (
        "Щоб схвалити: робот не зможе опублікувати — обов&#39;язкові поля Шафи без значення: Сезон"
        in page
    )
    assert 'value="approve" class="approve" disabled' in page


def test_card_page_yellow_block_lists_the_notes(conn, app, root):
    blank = [f for f in BLANK if f.name != "type"] + [ai.Field("type", "Мартінси")]
    item_id = add_card(conn, root, blank=blank, text=TEXT)
    page = html(get(app, f"/item/{item_id}"))
    assert "Робот пропустить:" in page
    assert "Тип: немає у списку Шафи — Мартінси" in page
    assert "Робот не зможе" not in page
    assert 'value="approve" class="approve" disabled' not in page


def test_card_page_without_blocks_when_all_is_well(conn, app, root):
    item_id = add_card(conn, root, blank=BLANK, text=TEXT)
    page = html(get(app, f"/item/{item_id}"))
    assert "Робот не зможе" not in page and "Робот пропустить" not in page


def test_card_page_of_approved_card_shows_the_error_too(conn, app, root):
    make_required(BOOTS, "Сезон")
    item_id = add_card(conn, root, blank=NO_SEASON, text=TEXT, status=Status.APPROVED)
    assert "Робот не зможе опублікувати" in html(get(app, f"/item/{item_id}"))


def test_list_marks_cards_without_text(conn, app, root):
    make_required(BOOTS, "Сезон")
    add_card(conn, root, blank=NO_SEASON, text=TEXT)
    page = html(get(app, "/review"))
    assert "Робот не зможе опублікувати" in page
    assert "Обов'язкові поля" not in page  # у списку лише позначка


def test_list_marks_notes_as_count(conn, app, root):
    blank = [f for f in BLANK if f.name != "type"] + [ai.Field("type", "Мартінси")]
    add_card(conn, root, blank=blank, text=TEXT)
    page = html(get(app, "/review"))
    assert "1 пропуск" in page and "Робот не зможе" not in page


# --- Схвалення ---


def test_approve_is_refused_when_robot_cannot_publish(conn, app, root):
    make_required(BOOTS, "Сезон")
    item_id = add_card(conn, root, blank=NO_SEASON, text=TEXT)
    response = post(app, f"/item/{item_id}", action="approve")
    assert location(response) == f"/item/{item_id}?msg=precheck"
    assert status(conn, item_id) == Status.REVIEW


def test_approve_goes_through_when_robot_can_publish(conn, app, root):
    item_id = add_card(conn, root, blank=BLANK, text=TEXT)
    assert location(post(app, f"/item/{item_id}", action="approve")) == "/review?msg=approved"
    assert status(conn, item_id) == Status.APPROVED


def test_review_act_returns_precheck_code(conn, root):
    make_required(BOOTS, "Сезон")
    item_id = add_card(conn, root, blank=NO_SEASON, text=TEXT)
    assert review.act(conn, item_id, "approve", root) == review.PRECHECK
    assert status(conn, item_id) == Status.REVIEW


# --- fill --precheck ---


def two_approved(conn, root):
    make_required(BOOTS, "Сезон")
    bad = add_card(conn, root, blank=NO_SEASON, text=TEXT, status=Status.APPROVED)
    good = add_card(
        conn, root, blank=BLANK, text=TEXT, status=Status.APPROVED, folder="1343", post_id=1343
    )
    return bad, good


def test_precheck_returns_only_bad_approved_cards(conn, root):
    bad, good = two_approved(conn, root)
    result = fill.precheck_approved(conn, root)
    assert result.returned == [(1342, "Обов'язкові поля Шафи без значення: Сезон")]
    assert result.kept == 1
    assert (status(conn, bad), status(conn, good)) == (Status.REVIEW, Status.APPROVED)
    text = conn.execute("SELECT problem_text FROM items WHERE id = ?", (bad,)).fetchone()[0]
    assert text == "Робот не зможе опублікувати: Обов'язкові поля Шафи без значення: Сезон"


def test_precheck_dry_run_writes_nothing(conn, root):
    bad, _ = two_approved(conn, root)
    before = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    result = fill.precheck_approved(conn, root, dry_run=True)
    assert len(result.returned) == 1
    assert conn.execute("SELECT * FROM items ORDER BY id").fetchall() == before
    assert status(conn, bad) == Status.APPROVED


def test_precheck_leaves_other_statuses_alone(conn, root):
    make_required(BOOTS, "Сезон")
    item_id = add_card(conn, root, blank=NO_SEASON, text=TEXT, status=Status.REVIEW)
    assert fill.precheck_approved(conn, root).returned == []
    assert status(conn, item_id) == Status.REVIEW


def test_precheck_cli(conn, root, tmp_path, capsys):
    two_approved(conn, root)
    conn.close()
    base = ["fill", "--db", str(tmp_path / "база.sqlite"), "--root", str(root), "--precheck"]

    assert cli.main([*base, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Пост 1342: Обов'язкові поля Шафи без значення: Сезон" in out
    assert "Буде повернуто на перевірку: 1; робот опублікує: 1" in out

    assert cli.main(base) == 0
    assert "Повернуто на перевірку: 1; робот опублікує: 1" in capsys.readouterr().out
