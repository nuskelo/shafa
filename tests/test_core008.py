"""CORE-008: «Перезаповнити», її правки в правилах, ярлик «старі правила»."""

import json

import pytest

from shafa import ai, batch, fill, review, rules, web
from shafa.db import Status
from test_ai import GOOD, NAMES, answer, message
from test_batch import LIMITS, TODAY, FakeClient, line, sent_requests
from test_review import POST_TEXT, add_card, edits, status
from test_web import PORT, TOKEN, get, html, location, post

RECENT = "2026-10-12T10:00:00"  # день тому: звичайна черга ще не взяла б, перезаповнення бере
GOOD_FIELDS = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}


def blank_fields(season=("Демісезон",)):
    return [ai.Field("title", "Стара назва"), ai.Field("season", list(season))]


def refill_card(conn, root, folder="1", post_id=1, needs_ai=True, **kwargs):
    """Картка, якій нова інструкція ШІ може змінити бланк (CORE-009 B: лише такі перезаповнюються)."""
    kwargs.setdefault("date", RECENT)
    kwargs.setdefault("blank", [ai.Field("section", "Жіночий одяг / Взуття / Черевики")])
    item_id = add_card(conn, root, folder=folder, post_id=post_id, **kwargs)
    if kwargs["blank"]:
        with conn:
            fill.save_answer(conn, item_id, GOOD, 1, needs_ai=needs_ai)
    return item_id


def put_edit(conn, item_id, field, value):
    with conn:
        conn.execute(
            "INSERT INTO edits VALUES (?, ?, ?, 'x')",
            (item_id, field, json.dumps(value, ensure_ascii=False)),
        )


def set_problem(conn, item_id, code):
    with conn:
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = ? WHERE id = ?", (code, item_id)
        )


def item(conn, item_id):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def candidate_ids(conn):
    return [r["id"] for r in fill.refill_candidates(conn)]


# --- A. Кого можна перезаповнити ---


def test_only_review_and_ai_problems_are_candidates(conn, root):
    review_id = refill_card(conn, root, "1", 1)
    ai_error = refill_card(conn, root, "2", 2)
    set_problem(conn, ai_error, "ai_error")
    no_section = refill_card(conn, root, "3", 3)
    set_problem(conn, no_section, "no_section")  # у ШІ є бланк із розділом
    no_section_data = refill_card(conn, root, "4", 4, blank=[])
    set_problem(conn, no_section_data, "no_section")  # без бланка — це дані, ШІ не допоможе
    many = refill_card(conn, root, "5", 5)
    set_problem(conn, many, "many_items")
    for n, st in enumerate((Status.APPROVED, Status.PUBLISHED, Status.ARCHIVED, Status.NEW), 6):
        refill_card(conn, root, str(n), n, status=st)

    assert candidate_ids(conn) == [review_id, ai_error, no_section]


def test_card_waiting_in_batch_is_not_a_candidate(conn, root):
    item_id = refill_card(conn, root)
    with conn:
        conn.execute("INSERT INTO ai_batches (status, created_at) VALUES ('in_progress', 'x')")
        conn.execute("INSERT INTO ai_batch_items VALUES (1, ?, 1, 'pending')", (item_id,))

    assert candidate_ids(conn) == []


def test_mark_never_touches_approved_or_published(conn, root):
    approved = refill_card(conn, root, "1", 1, status=Status.APPROVED)
    published = refill_card(conn, root, "2", 2, status=Status.PUBLISHED)
    ready = refill_card(conn, root, "3", 3)

    assert fill.mark_refill(conn, [approved, published, ready]) == 1
    assert status(conn, approved) == Status.APPROVED
    assert status(conn, published) == Status.PUBLISHED
    assert status(conn, ready) == Status.NEW
    assert item(conn, ready)["refill_at"]


# --- A. Через пакет, без чекання 7 днів, правки живі ---


def test_refill_goes_in_batch_without_waiting_and_keeps_edits(conn, root):
    item_id = refill_card(conn, root)
    put_edit(conn, item_id, "title", "Моя назва")
    fill.mark_refill(conn, [item_id])
    client = FakeClient()

    sent = batch.send(conn, client, root, TODAY, LIMITS)

    assert sent.count == 1  # пост молодший за 7 днів, а пішов
    text = json.dumps(sent_requests(client)[0], ensure_ascii=False)
    assert "Продавчиня виправила: Назва → Моя назва" in text
    assert "cache_control" in text  # пакет з кешем, як звичайне заповнення

    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line("b1-i1", reply=message(GOOD_FIELDS))]
    batch.collect(conn, client, TODAY)

    row = item(conn, item_id)
    assert (row["status"], row["refill_at"], row["rules_version"]) == (
        Status.REVIEW,
        None,
        rules.RULES_VERSION,
    )
    assert edits(conn, item_id) == {"title": "Моя назва"}
    assert review.load_card(conn, item_id).fields["title"].value == "Моя назва"  # правка зверху
    blank = conn.execute("SELECT value FROM blanks WHERE field = 'title'").fetchone()[0]
    assert json.loads(blank) == GOOD["title"]  # решта — новий ШІ


def test_young_card_without_refill_still_waits(conn, root):
    refill_card(conn, root, status=Status.NEW)  # не перезаповнення — правило 7 днів як було

    assert batch.queue(conn, TODAY, 7).ready == []


def test_refill_ai_error_returns_card_to_review_with_old_blank(conn, root):
    item_id = refill_card(conn, root)
    fill.mark_refill(conn, [item_id])
    client = FakeClient()
    batch.send(conn, client, root, TODAY, LIMITS)
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line("b1-i1", kind="errored")]

    batch.collect(conn, client, TODAY)

    row = item(conn, item_id)
    assert (row["status"], row["problem_code"], row["refill_at"]) == (Status.REVIEW, None, None)
    assert conn.execute("SELECT count(*) FROM blanks WHERE field = 'section'").fetchone()[0] == 1


def test_refill_ai_error_of_problem_card_stays_problem(conn, root):
    item_id = refill_card(conn, root, blank=[])
    set_problem(conn, item_id, "ai_error")
    fill.mark_refill(conn, [item_id])
    client = FakeClient()
    batch.send(conn, client, root, TODAY, LIMITS)
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line("b1-i1", kind="errored")]

    batch.collect(conn, client, TODAY)

    assert (status(conn, item_id), item(conn, item_id)["problem_code"]) == (
        Status.PROBLEM,
        "ai_error",
    )


def test_refill_estimate_counts_cards_and_money(conn, root):
    refill_card(conn, root, "1", 1)
    refill_card(conn, root, "2", 2)
    refill_card(conn, root, "3", 3, status=Status.APPROVED)

    count, cost, skipped = batch.refill_estimate(conn, root)

    assert (count, skipped) == (2, 0)
    assert cost > 0


def test_refill_is_inside_daily_money_limit(conn, root):
    item_id = refill_card(conn, root)
    fill.mark_refill(conn, [item_id])
    tiny = batch.Limits(daily_micro_usd=1)

    sent = batch.send(conn, FakeClient(), root, TODAY, tiny)

    assert sent.count == 0 and sent.plan.over_budget
    assert status(conn, item_id) == Status.NEW  # чекає завтра, не губиться


# --- A. Екран ---


@pytest.fixture
def app(conn, tmp_path, root):
    client = FakeClient()
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        ai_client=lambda: client, run=lambda job: job(), today=lambda: TODAY,
    )  # fmt: skip


def test_panel_button_and_confirmation_page_show_estimate(conn, root, app):
    refill_card(conn, root, "1", 1)
    refill_card(conn, root, "2", 2)

    assert "Перезаповнити (2)" in html(get(app, "/review"))
    page = html(get(app, "/refill"))
    assert "≈ 2 карток" in page and "$" in page
    assert status(conn, 1) == Status.REVIEW  # перегляд нічого не міняє


def test_panel_has_no_refill_button_without_candidates(conn, root, app):
    refill_card(conn, root, status=Status.APPROVED)

    assert "Перезаповнити (" not in html(get(app, "/review"))


def test_refill_post_marks_all_and_sends_batch(conn, root, app):
    first = refill_card(conn, root, "1", 1)
    second = refill_card(conn, root, "2", 2)
    approved = refill_card(conn, root, "3", 3, status=Status.APPROVED)

    assert "msg=refill_started" in location(post(app, "/refill"))

    assert status(conn, first) == status(conn, second) == Status.NEW
    assert status(conn, approved) == Status.APPROVED
    assert conn.execute("SELECT count(*) FROM ai_batch_items").fetchone()[0] == 2


def test_card_refill_button_marks_one_card(conn, root, app):
    first = refill_card(conn, root, "1", 1)
    second = refill_card(conn, root, "2", 2)

    assert "Перезаповнити пакетом · ≈ 1,5 ¢</button>" in html(get(app, f"/item/{first}"))
    post(app, f"/item/{first}", action="refill")

    assert (status(conn, first), status(conn, second)) == (Status.NEW, Status.REVIEW)


def test_card_refill_of_approved_is_refused(conn, root, app):
    item_id = refill_card(conn, root, status=Status.APPROVED)

    assert "msg=stale" in location(post(app, f"/item/{item_id}", action="refill"))
    assert status(conn, item_id) == Status.APPROVED


# --- A4. «Заповнено старими правилами» ---


def test_old_rules_label_only_when_new_ai_instruction_may_change_card(conn, root):
    """CORE-009 B: ярлик — лише якщо Картці потрібне саме ШІ, а не лише нові правила коду."""
    old = refill_card(conn, root, "1", 1)
    code_only = refill_card(conn, root, "2", 2, needs_ai=False)
    with conn:
        conn.execute("UPDATE items SET rules_version = NULL WHERE id = ?", (code_only,))
    no_blank = refill_card(conn, root, "3", 3, blank=[])

    assert review.load_card(conn, old).old_rules
    assert not review.load_card(conn, code_only).old_rules  # це — «Перерахувати правилами»
    assert not review.load_card(conn, no_blank).old_rules


def test_old_rules_label_on_list_and_card(conn, root, app):
    item_id = refill_card(conn, root)

    assert "заповнено старими правилами" in html(get(app, "/review"))
    assert "заповнено старими правилами" in html(get(app, f"/item/{item_id}"))


def test_refill_clears_old_rules_label(conn, root):
    item_id = refill_card(conn, root)
    with conn:
        fill.save_blank(conn, item_id, blank_fields(), GOOD)  # нова відповідь ШІ

    assert not review.load_card(conn, item_id).old_rules


# --- B. Її правки — текст для правил ---

THERMO = "Термо черевики Ecco 38 розміру. 1200грн"


def test_edited_title_winter_beats_ai_demi_season():
    plain = ai.check_answer(answer(season=["Демісезон"]), NAMES, POST_TEXT)
    edited = ai.check_answer(
        answer(season=["Демісезон"]), NAMES, POST_TEXT, edited="Зимові черевики Sorel"
    )

    by_name = {f.name: f for f in edited}
    assert {f.name: f for f in plain}["season"].value == ["Демісезон"]
    assert by_name["season"].value == ["Зима"]
    assert not by_name["season"].yellow  # код, не жовте


def test_edited_title_decides_gender_and_shoe_type():
    fields = ai.check_answer(
        answer(section="Чоловічий одяг / Взуття / Черевики", sizes=["38"]),
        NAMES,
        POST_TEXT,
        edited="Жіночі черевики",
    )

    assert {f.name: f for f in fields}["section"].value == "Жіночий одяг / Взуття / Черевики"


def test_price_and_sizes_still_come_only_from_post():
    # у назві число не стає ціною чи розміром
    request = ai.build_request(POST_TEXT, [], corrections=[("Назва", "Кеди 999 грн")])
    text = json.dumps(request, ensure_ascii=False)

    assert "Ціна на Шафі: 1380 грн" in text or "Ціна на Шафі" in text
    assert "999 грн" in text.split("Правки Продавчині")[1]
    assert "Ціна на Шафі: 999" not in text


def test_load_card_applies_edited_title_to_season(conn, root):
    item_id = add_card(
        conn, root, text=THERMO,
        blank=[ai.Field("section", "Жіночий одяг / Взуття / Черевики"),
               ai.Field("season", ["Демісезон"])],
    )  # fmt: skip
    put_edit(conn, item_id, "title", "Зимові черевики Sorel")

    season = review.load_card(conn, item_id).fields["season"]

    assert season.value == ["Зима"] and not season.yellow and not season.edited


def test_her_own_season_edit_wins_over_title(conn, root):
    item_id = add_card(
        conn, root, text=THERMO,
        blank=[ai.Field("section", "Жіночий одяг / Взуття / Черевики")],
    )  # fmt: skip
    put_edit(conn, item_id, "title", "Зимові черевики")
    put_edit(conn, item_id, "season", ["Осінь"])

    season = review.load_card(conn, item_id).fields["season"]

    assert season.value == ["Осінь"] and season.edited


def test_corrections_list_her_edits_for_ai_without_photo_order(conn, root):
    item_id = refill_card(conn, root)
    put_edit(conn, item_id, "title", "Зимові Sorel")
    put_edit(conn, item_id, "colors", ["Чорний", "Білий"])
    put_edit(conn, item_id, "photo_order", ["IMG_2.JPG", "IMG_1.JPG"])

    assert fill.corrections(fill.edits_of(conn, item_id)) == [
        ("Назва", "Зимові Sorel"),
        ("Колір", "Чорний, Білий"),
    ]


def test_retry_sends_her_edits_and_rules_see_edited_title(conn, root):
    item_id = refill_card(conn, root, text=THERMO, status=Status.REVIEW)
    put_edit(conn, item_id, "title", "Зимові Sorel")
    planned = fill.plan(conn, root, fill.retry_row(conn, item_id))
    assert "Продавчиня виправила: Назва → Зимові Sorel" in json.dumps(
        planned.request, ensure_ascii=False
    )

    class Client:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **request):
            return message({**GOOD_FIELDS, "season": ["Демісезон"]})

    fill.retry(conn, Client(), planned)

    season = conn.execute("SELECT value FROM blanks WHERE field = 'season'").fetchone()[0]
    assert json.loads(season) == ["Зима"]


def test_ai_prompt_has_no_corrections_block_without_edits():
    assert "Правки Продавчині" not in ai.user_text(POST_TEXT)
