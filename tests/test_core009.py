"""CORE-009: рішення Власника за QA-001 і перерахунок правилами без ШІ (перефразовані приклади)."""

import json

import pytest

from shafa import ai, batch, db, fill, review, rules, web
from shafa import app as app_mod
from shafa.db import Status
from test_ai import GOOD, NAMES, FakeClient, answer, by_name, message
from test_batch import TODAY, line, ok_answer, sent_batch
from test_batch import FakeClient as BatchClient
from test_core008 import item, put_edit
from test_fill import add_item, planned_for
from test_review import add_card, edits
from test_web import PORT, TOKEN, get, html, location, post

WOMEN_SNEAKERS = "Жіночий одяг / Взуття / Кросівки"
MEN_SNEAKERS = "Чоловічий одяг / Взуття / Кросівки"
GIRLS_SNEAKERS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"


def check(text, **changes):
    changes.setdefault("material_upper", [])
    changes.setdefault("season", [])
    return by_name(ai.check_answer(answer(**changes), NAMES, text))


def doubt(field, reason):
    return {"field": field, "reason": reason}


# --- A1: 40 без ознак статі — жіноче ---


@pytest.mark.parametrize("section", [WOMEN_SNEAKERS, GIRLS_SNEAKERS])
def test_40_without_signs_is_women_not_yellow(section):
    fields = check(
        "Кросівки Asics 40 розміру, носились мало. 950грн",
        section=section, sizes=["40"], teen=None,
        doubts=[doubt("section", "40 — жіночі чи чоловічі, з фото не видно")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].yellow) == (WOMEN_SNEAKERS, False)


def test_40_men_when_ai_sees_men_line_and_yellow_only_on_conflict():
    text = "Кросівки Asics 40 розміру. 950грн"
    fields = check(text, section=MEN_SNEAKERS, sizes=["40"])
    assert (fields["section"].value, fields["section"].yellow) == (MEN_SNEAKERS, False)
    fields = check(
        text, section=MEN_SNEAKERS, sizes=["40"],
        doubts=[doubt("section", "модель чоловіча, але рожеві вставки")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].yellow) == (MEN_SNEAKERS, True)


def test_40_said_men_in_text_is_men():
    fields = check(
        "Чоловічі кросівки Asics 40 розміру. 950грн", section=WOMEN_SNEAKERS, sizes=["40"]
    )
    assert (fields["section"].value, fields["section"].yellow) == (MEN_SNEAKERS, False)


@pytest.mark.parametrize("size", ["41", "42"])
def test_41_42_still_ai_with_doubt(size):
    fields = check(
        f"Кросівки Asics {size} розміру. 950грн", section=WOMEN_SNEAKERS, sizes=[size],
        doubts=[doubt("section", "жіночі чи чоловічі — не видно")],
    )  # fmt: skip
    assert fields["section"].yellow


# --- A2: «вказаний X, ідуть на Y» — на Шафу Y ---


@pytest.mark.parametrize(
    ("text", "ai_sizes", "sizes"),
    [
        ("Кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн", ["38", "37"], ["37"]),
        ("Нові кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн", ["38"], ["37"]),
        ("Кеди Vans, на ярлику 39, ідуть на 38,5. 700грн", ["39"], ["38,5"]),
    ],
)
def test_label_gives_goes_size_for_used_and_new(text, ai_sizes, sizes):
    fields = check(text, section=WOMEN_SNEAKERS, sizes=ai_sizes)
    assert (fields["sizes"].value, fields["sizes"].yellow) == (sizes, False)


def test_label_rule_keeps_description_from_post():
    assert rules.label_sizes("Кеди. Вказаний 38 розмір, ідуть на 37. 700грн") == ["37"]
    assert "Вказаний 38 розмір" in rules.clean_description(
        "Кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн", rules.IDEAL
    )


# --- A3: 35–36 у можливо дитячих моделях ---


@pytest.mark.parametrize(
    "text",
    [
        "Кросівки Salomon Speedcross Kids 35 розміру. 800грн",
        "Кросівки Adidas Duramo Junior 36 розміру. 600грн",
        "Черевики Keen Targhee Jr 35,5 розміру. 900грн",
        "Кросівки Salomon XA Pro J 36 розміру. 800грн",
    ],
)
def test_35_36_kid_line_women_but_yellow(text):
    size = rules.text_sizes(text)[0]
    fields = check(text, section=WOMEN_SNEAKERS, sizes=[ai._number(size)])
    assert fields["section"].value == WOMEN_SNEAKERS
    assert (fields["section"].yellow, fields["section"].reason) == (True, ai.MAYBE_KID)


def test_35_36_ai_sees_kid_signs_on_photo_women_but_yellow():
    fields = check(
        "Кросівки Geox 35 розміру. 600грн", section=GIRLS_SNEAKERS, sizes=["35"], teen=False,
        doubts=[doubt("section", "можливо дитяча модель: липучки, принт з єдинорогом")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].reason) == (WOMEN_SNEAKERS, ai.MAYBE_KID)
    assert not fields["teen"].yellow


@pytest.mark.parametrize("size", ["35", "36"])
def test_35_36_without_signs_women_not_yellow(size):
    fields = check(
        f"Кросівки Geox {size} розміру. 600грн", section=GIRLS_SNEAKERS, sizes=[size],
        doubts=[doubt("section", "35-36 — дитяче чи доросле")],
    )  # fmt: skip
    assert (fields["section"].value, fields["section"].yellow) == (WOMEN_SNEAKERS, False)


def test_kid_line_with_heel_is_adult_and_37_not_touched():
    fields = check(
        "Ботильйони Kids на каблуку 36 розміру. 900грн", section=WOMEN_SNEAKERS, sizes=["36"]
    )
    assert not fields["section"].yellow
    fields = check("Кросівки Nike Junior 37 розміру. 600грн", section=WOMEN_SNEAKERS, sizes=["37"])
    assert not fields["section"].yellow


@pytest.mark.parametrize(
    ("text", "kid"),
    [
        ("Кросівки Salomon Speedcross J 35", True),
        ("Кросівки Nike KIDS 36", True),
        ("Кросівки J.Crew 36", False),
        ("Кросівки Nike Air Jordan 36", False),
        ("Кросівки Jrlite 36", False),
    ],
)
def test_kid_model(text, kid):
    assert rules.kid_model(text) is kid


# --- B. Перерахувати правилами, без ШІ ---

LABEL_POST = "Кеди Vans. Вказаний 38 розмір, ідуть на 37. 700грн"


def stale_card(
    conn, root, folder="1", post_id=1, text=LABEL_POST, answer_=None, status=Status.REVIEW
):
    """Картка з бланком за старшими правилами коду (v3); `answer_` — збережена відповідь ШІ."""
    stored = answer_ or answer(section=WOMEN_SNEAKERS, sizes=["38", "37"], material_upper=[])
    item_id = add_card(
        conn, root, folder=folder, post_id=post_id, text=text, status=status,
        blank=ai.check_answer(stored, NAMES, text),
    )  # fmt: skip
    with conn:
        if answer_ is not False:
            fill.save_answer(conn, item_id, stored)
        conn.execute("UPDATE items SET rules_version = 3 WHERE id = ?", (item_id,))
        if text == LABEL_POST:  # так заповнив би код v3: вживане — X і Y
            conn.execute(
                "UPDATE blanks SET value = '[\"38\", \"37\"]' WHERE item_id = ? AND field = 'sizes'",
                (item_id,),
            )
    return item_id


def blank(conn, item_id):
    return {
        r["field"]: (json.loads(r["value"]), bool(r["yellow"]), r["reason"])
        for r in conn.execute("SELECT * FROM blanks WHERE item_id = ?", (item_id,))
    }


def stored(conn, item_id):
    return conn.execute("SELECT * FROM ai_answers WHERE item_id = ?", (item_id,)).fetchone()


def test_fill_saves_raw_ai_answer(conn, root):
    item_id = add_item(conn, root)
    reply = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"], "doubts": [doubt("brand", "x")]}
    fill.fill(conn, FakeClient(message(reply)), planned_for(conn, root))

    row = stored(conn, item_id)
    assert json.loads(row["answer"]) == reply
    assert (row["prompt_version"], row["rebuilt"], row["needs_ai"]) == (ai.PROMPT_VERSION, 0, 0)


def test_recheck_applies_new_code_rules_to_stored_answer_and_keeps_edits(conn, root):
    item_id = stale_card(conn, root)
    assert blank(conn, item_id)["sizes"][0] == ["38", "37"]  # v3: вживане — X і Y
    put_edit(conn, item_id, "title", "Мої кеди Vans 37 розміру")
    requests = conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0]

    assert fill.recheck(conn) == 1

    assert blank(conn, item_id)["sizes"] == (["37"], False, None)
    assert edits(conn, item_id) == {"title": "Мої кеди Vans 37 розміру"}
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == requests  # ШІ — ні
    assert item(conn, item_id)["rules_version"] == rules.RULES_VERSION
    assert fill.recheck(conn) == 0  # вдруге нічого


def test_recheck_rebuilds_answer_from_old_blank_with_ai_doubts(conn, root):
    old = answer(
        section=WOMEN_SNEAKERS, sizes=["38", "37"], material_upper=[], brand=None,
        doubts=[doubt("colors", "x"), doubt("keywords", "не певна; дуже")],
    )  # fmt: skip
    item_id = stale_card(conn, root, answer_=False, text=LABEL_POST)
    with conn:  # бланк до CORE-009 (код v3): відповідь не збереглась
        fill.save_blank(conn, item_id, ai.check_answer(old, NAMES, LABEL_POST))
        conn.execute("UPDATE items SET rules_version = 3 WHERE id = ?", (item_id,))
        conn.execute(
            "UPDATE blanks SET value = '[\"38\", \"37\"]' WHERE item_id = ? AND field = 'sizes'",
            (item_id,),
        )

    fill.recheck(conn)

    row = stored(conn, item_id)
    rebuilt = json.loads(row["answer"])
    assert (row["prompt_version"], row["rebuilt"]) == (1, 1)
    assert rebuilt["sizes"] == ["38", "37"] and rebuilt["size_system"] == "EU"
    assert rebuilt["doubts"] == [doubt("keywords", "не певна; дуже")]  # колір — сумнів не рахується
    assert blank(conn, item_id)["sizes"] == (["37"], False, None)
    assert blank(conn, item_id)["keywords"][1]


def test_old_rules_label_only_where_new_ai_instruction_may_change(conn, root):
    kid = stale_card(
        conn, root, "1", 1, text="Кросівки Geox 36 розміру. 600грн",
        answer_=answer(section=WOMEN_SNEAKERS, sizes=["36"], material_upper=[]),
    )  # fmt: skip
    plain = stale_card(
        conn, root, "2", 2, text="Кросівки Geox 38 розміру. 600грн",
        answer_=answer(section=WOMEN_SNEAKERS, sizes=["38"], material_upper=[]),
    )  # fmt: skip
    with conn:
        conn.execute("UPDATE ai_answers SET prompt_version = 2")

    fill.recheck(conn)

    assert review.load_card(conn, kid).old_rules  # 35–36: ШІ має глянути на фото
    assert not review.load_card(conn, plain).old_rules
    assert [r["id"] for r in fill.refill_candidates(conn)] == [kid]  # «Перезаповнити (1)»


@pytest.mark.parametrize(
    ("version", "text", "changes", "needs"),
    [
        (2, "Кросівки 36 розміру. 600грн", {"sizes": ["36"]}, True),
        (2, "Ботильйони на каблуку 36 розміру. 600грн", {"sizes": ["36"]}, False),
        (2, "Кросівки 40 розміру. 600грн", {"sizes": ["40"], "section": MEN_SNEAKERS}, True),
        (2, "Кросівки 40 розміру. 600грн", {"sizes": ["40"]}, False),
        (
            2,
            "Чоловічі кросівки 40 розміру. 600грн",
            {"sizes": ["40"], "section": MEN_SNEAKERS},
            False,
        ),
        (3, "Кросівки 36 розміру. 600грн", {"sizes": ["36"]}, False),  # уже за новою інструкцією
        (1, "Кросівки 38 розміру. 600грн", {"sizes": ["38"], "type": "Челсі"}, True),
        (1, "Кросівки 38 розміру. 600грн", {"sizes": ["38"], "title": "Зимові кросівки 38"}, True),
        (1, "Кросівки 38. 600грн", {"sizes": ["38"], "doubts": [doubt("brand", "x")]}, False),
        (
            1,
            "Кросівки 38. 600грн",
            {"sizes": ["38"], "doubts": [doubt("title", "мембрана?")]},
            True,
        ),
        (
            1,
            "Кросівки 41. 600грн",
            {"sizes": ["41"], "doubts": [doubt("section", "жіночі?")]},
            True,
        ),
        (
            1,
            "Кросівки 40. 600грн",
            {"sizes": ["40"], "doubts": [doubt("section", "жіночі?")]},
            False,
        ),
        (1, "Замша, 38. 600грн", {"sizes": ["38"], "title": "Замшеві кеди 38 розміру"}, False),
        (
            1,
            "Кросівки 38 розміру. 600грн",
            {"sizes": ["38"], "title": "Кросівки 38 розміру"},
            False,
        ),
    ],
)
def test_needs_ai(version, text, changes, needs):
    assert ai.needs_ai(version, {**answer(doubts=[], title="Кросівки"), **changes}, text) is needs


def test_recheck_moves_to_problems_and_back_and_never_touches_approved(conn, root):
    many = stale_card(conn, root, "1", 1, text="Кеди 37 розміру. 500грн\nКеди 39 розміру. 600грн")
    gone = stale_card(
        conn, root, "2", 2, text="Кросівки Geox 38 розміру. 600грн",
        answer_=answer(section="", sizes=[], material_upper=[]),
    )  # fmt: skip
    approved = stale_card(conn, root, "3", 3, status=Status.APPROVED)
    before = blank(conn, approved)

    assert fill.recheck(conn) == 2

    assert (item(conn, many)["status"], item(conn, many)["problem_code"]) == (
        "problem",
        "many_items",
    )
    assert (item(conn, gone)["status"], item(conn, gone)["problem_code"]) == (
        "problem",
        "no_section",
    )
    assert blank(conn, gone)["sizes"] == ([], False, None)  # «немає розміру» — шум у no_section
    assert blank(conn, approved) == before and item(conn, approved)["rules_version"] == 3


def test_panel_recheck_button_works_without_ai(conn, root, tmp_path):
    item_id = stale_card(conn, root)

    def no_ai():
        raise AssertionError("перерахунок не кличе ШІ")

    app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN, ai_client=no_ai,
        run=lambda job: job(), today=lambda: TODAY,
    )  # fmt: skip
    assert "Перерахувати правилами (1)" in html(get(app, "/review"))

    assert "msg=recheck_done" in location(post_ai(app, "recheck"))
    assert blank(conn, item_id)["sizes"][0] == ["37"]
    assert "Перерахувати правилами" not in html(get(app, "/review"))


def test_program_start_rechecks_by_itself(tmp_path, monkeypatch):
    path = app_mod.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    root = tmp_path / "Головна"
    root.mkdir()
    item_id = stale_card(conn, root)
    conn.close()

    assert app_mod.start(lambda url: None, port=0, serve=lambda server: None) == 0

    conn = db.connect(path)
    assert item(conn, item_id)["rules_version"] == rules.RULES_VERSION
    assert blank(conn, item_id)["sizes"][0] == ["37"]
    conn.close()


# --- B. Шум причин (QA-001 §5) ---


def test_unpaired_has_one_clear_reason():
    fields = check(
        "Нові кросівки Nike. Розпаровка: лівий 37, правий 38. 900грн",
        section=WOMEN_SNEAKERS, sizes=["37", "38"], doubts=[doubt("sizes", "розпаровка")],
    )  # fmt: skip
    assert (fields["sizes"].yellow, fields["sizes"].reason) == (True, ai.UNPAIRED)


def test_proper_support_is_not_unpaired():
    text = "Кросівки на корковій основі, що забезпечує правильну підтримку. 38 розмір. 900грн"
    assert not rules.unpaired(text)
    assert check(text, section=WOMEN_SNEAKERS, sizes=["38"])["sizes"].yellow is False


def post_ai(app, action):
    return post(app, "/ai", action=action, back="/review")


def test_batch_collect_saves_raw_ai_answer(conn, root):
    client = BatchClient()
    (only,), bid = sent_batch(conn, root, client, 1)
    client.batches.status["msgbatch_1"] = "ended"
    reply = {**ok_answer(), "doubts": [doubt("brand", "бірки не видно")]}
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{only}", reply=message(reply))]

    batch.collect(conn, client, TODAY)

    assert json.loads(stored(conn, only)["answer"]) == reply
