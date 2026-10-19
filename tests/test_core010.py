"""CORE-010: рукавички, шарфи, шапки — розділи «Аксесуари» (TD-033).

Розділи в `fixtures/shafa-catalog.json` (7 останніх) зібрані вручну за назвами з Шафи, не з живого
`--sync-catalog`: списки розмірів і варіантів — припущення, живий знімок їх уточнить.
"""

import pytest

from shafa import ai, catalog, fill, review
from shafa.bot.hashtag import accessory_group, decide, item_kind
from shafa.db import Status
from shafa.robot import lang, sync
from shafa.robot.plan import plan
from shafa.scan import NO_SECTION_TEXT, text_problem
from test_ai import GOOD, NAMES, FakeClient, message
from test_fill import add_item, item, planned_for
from test_review import add_card
from test_robot_sync import TREE_RESPONSE, node

GLOVES = "Аксесуари / Рукавички та рукавиці / Рукавички"
MITTENS = "Аксесуари / Рукавички та рукавиці / Мітенки"
GLOVES_NO_FINGERS = "Аксесуари / Рукавички та рукавиці / Рукавиці"
SCARVES = "Аксесуари / Шарфи і хустки / Шарфи"
HATS = "Аксесуари / Головні убори / Шапки"

GLOVES_TEXT = "Шкіряні рукавиці Tommy Hilfiger розмір M. В ідеальному стані. 900грн"


def answer(text=GLOVES_TEXT, **changes):
    base = {
        **GOOD,
        "title": "Шкіряні рукавички tommy hilfiger M розміру",
        "section": GLOVES,
        "sizes": ["M"],
        "brand": "Tommy Hilfiger",
        "season": [],
        "gender": "Для жінок",
        "keywords": ["рукавички", "рукавички жіночі", "рукавички tommy hilfiger", "рукавички M"],
        "photo_order": NAMES,
    }
    return by_name(ai.check_answer({**base, **changes}, NAMES, text))


def by_name(fields):
    return {f.name: f for f in fields}


# --- Знімок і sync ---


def test_sync_groups_have_three_new_branches():
    for group in (
        ("Аксесуари", "Рукавички та рукавиці"),
        ("Аксесуари", "Шарфи і хустки"),
        ("Аксесуари", "Головні убори"),
    ):
        assert group in sync.GROUPS


def test_sync_leaves_include_gloves_scarves_hats():
    tree = node(
        "Каталог",
        [
            node(
                "Аксесуари",
                [
                    node("Рукавички та рукавиці", [node("Рукавички", id_=1), node("Мітенки")]),
                    node("Шарфи і хустки", [node("Шарфи")]),
                    node("Головні убори", [node("Шапки")]),
                ],
            )
        ],
    )
    root, _ = sync.parse_tree(TREE_RESPONSE | {})  # дерево відповіді не потрібне — лише розбір
    assert root
    groups = tuple(g for g in sync.GROUPS if g[0] == "Аксесуари" and g[1] in tree_names(tree))
    assert sync.leaves(tree, groups) == [
        ("Аксесуари", "Рукавички та рукавиці", "Рукавички"),
        ("Аксесуари", "Рукавички та рукавиці", "Мітенки"),
        ("Аксесуари", "Шарфи і хустки", "Шарфи"),
        ("Аксесуари", "Головні убори", "Шапки"),
    ]


def tree_names(tree):
    return {c["name"] for c in tree["subCatalogs"][0]["subCatalogs"]}


def test_snapshot_has_new_sections():
    for path in (GLOVES, GLOVES_NO_FINGERS, MITTENS, SCARVES, HATS):
        assert catalog.find_section(path) == path
        assert catalog.section_kind(path) is catalog.Kind.ACCESSORY
    assert catalog.has_group("Аксесуари / Рукавички та рукавиці")
    assert not catalog.has_group("Аксесуари / Окуляри")
    assert catalog.find_section(GLOVES.lower()) == GLOVES


def test_letter_sizes_of_gloves_come_from_the_snapshot():
    assert catalog.size_system(GLOVES) == "Міжнародний"
    assert catalog.sizes(GLOVES) == ["XS", "S", "M", "L", "XL", "XXL"]
    assert catalog.sizes(SCARVES) == []
    shoes = catalog.sizes("Жіночий одяг / Взуття / Кросівки")  # взуття не змінилось: лише числа
    assert shoes[0] == "34" and all(s[0].isdigit() for s in shoes)


def test_numeric_glove_sizes_work_like_shoe_sizes():
    """Якщо живий знімок дасть рукавичкам числа (дюйми) — вони теж проходять."""
    catalog.snapshot()["sections"][GLOVES]["sizes"] = {"Європейський": ["7", "7.5", "8", "Інший"]}
    assert catalog.sizes(GLOVES) == ["7", "7,5", "8"]
    field = answer("Рукавички, розмір 7,5. 500грн", sizes=["7,5"])["sizes"]
    assert (field.value, field.yellow) == (["7,5"], False)


def test_render_lists_new_sections_and_sizes_for_the_prompt():
    text = catalog.render()
    assert "Аксесуари / Рукавички та рукавиці / … : Рукавички, Рукавиці, Мітенки" in text
    assert "Аксесуари / Рукавички та рукавиці: система «Міжнародний»: XS, S, M, L, XL, XXL" in text
    assert "Аксесуари / Шарфи і хустки: розміру немає" in text


# --- Слова поста → група ---


@pytest.mark.parametrize(
    "text, group",
    [
        ("Шкіряні рукавиці Tommy 700грн", "Аксесуари / Рукавички та рукавиці"),
        ("Чоловічі перчатки Zara. 500грн", "Аксесуари / Рукавички та рукавиці"),
        ("Мітенки вовняні 300грн", "Аксесуари / Рукавички та рукавиці"),
        ("Шарф Burberry 1500грн", "Аксесуари / Шарфи і хустки"),
        ("Палантин шовковий 800грн", "Аксесуари / Шарфи і хустки"),
        ("Шапка Nike 400грн", "Аксесуари / Головні убори"),
        ("Бейсболка Adidas 300грн", "Аксесуари / Головні убори"),
        ("Капелюх фетровий 600грн", "Аксесуари / Головні убори"),
        ("Окуляри Ray-Ban 900грн", None),
        ("Парасолька складна 300грн", None),
        ("Шкарпетки 100грн", None),
        ("Кросівки Nike 38 розміру 900грн", None),
    ],
)
def test_accessory_group_by_word(text, group):
    assert accessory_group(text) == group


def test_bot_hashtag_for_gloves_is_unchanged():
    """Бот, як і раніше: рукавички, шарфи, шапки — без хештега (BOT-001, правило її каналу)."""
    for text in ("Шкіряні рукавиці Tommy 700грн", "Шарф Burberry 1500грн", "Мітенки 300грн"):
        assert item_kind(text) == "other"
        assert decide(text).tag is None and decide(text).ask is None


# --- Scan: до ШІ лише те, що Шафа приймає ---


def test_gloves_scarf_hat_go_to_ai_when_snapshot_has_their_sections():
    for text in ("Шкіряні рукавиці Tommy 700грн", "Шарф Burberry 1500грн", "Шапка Nike 400грн"):
        assert text_problem(text) is None


@pytest.mark.parametrize(
    "text", ["Окуляри Ray-Ban 900грн", "Парасолька складна 300грн", "Шкарпетки Nike 100грн"]
)
def test_things_without_a_section_stay_in_problems(text):
    assert text_problem(text) == ("no_section", NO_SECTION_TEXT)


def test_gloves_stay_in_problems_until_the_branch_is_synced():
    """Знімок без гілки (до `--sync-catalog`): рукавички — «Проблеми», ШІ не витрачаємо."""
    sections = catalog.snapshot()["sections"]
    for path in [p for p in sections if p.startswith("Аксесуари / Рукавички")]:
        del sections[path]
    assert text_problem("Шкіряні рукавиці Tommy 700грн") == ("no_section", NO_SECTION_TEXT)
    assert text_problem("Шарф Burberry 1500грн") is None  # їхня гілка в знімку є


# --- ШІ-бланк ---


def test_leather_gloves_with_fingers_stay_gloves_not_mittens():
    fields = answer()
    assert fields["section"].value == GLOVES and not fields["section"].yellow


def test_ai_section_gloves_not_in_snapshot_is_yellow_not_silently_taken():
    fields = answer(section="Аксесуари / Рукавички та рукавиці / Краги")
    assert fields["section"].yellow and "довіднику" in fields["section"].reason


@pytest.mark.parametrize(
    "sizes, value, yellow",
    [
        (["M"], ["M"], False),
        (["m"], ["M"], False),
        (["S/M"], ["S", "M"], False),
        (["S", "M"], ["S", "M"], False),
        (["XL"], ["XL"], False),
        (["XXXL"], ["XXXL"], True),  # у списку розділу такого немає
        (["S", "M", "L"], ["S", "M", "L"], True),
        ([], [], True),
    ],
)
def test_letter_sizes_checked_against_the_section_list(sizes, value, yellow):
    field = answer(sizes=sizes)["sizes"]
    assert (field.value, field.yellow) == (value, yellow)


def test_scarf_has_no_size_and_no_yellow_for_it():
    fields = answer("Шарф Burberry. 900грн", section=SCARVES, sizes=[])
    assert fields["sizes"].value == [] and not fields["sizes"].yellow


def test_size_for_a_section_without_sizes_is_yellow():
    fields = answer("Шарф Burberry. 900грн", section=SCARVES, sizes=["M"])
    assert fields["sizes"].yellow


@pytest.mark.parametrize(
    "text, ai_gender, value, yellow",
    [
        ("Чоловічі шкіряні перчатки M. 900грн", "Для жінок", "Для чоловіків", False),
        ("Чоловічі шкіряні перчатки M. 900грн", None, "Для чоловіків", False),
        ("Рукавички для дівчинки M. 900грн", "Для жінок", "Для дівчаток", False),
        ("Жіночі рукавички M. 900грн", "Для чоловіків", "Для жінок", False),
        ("Рукавички шкіряні M. 900грн", "Для жінок", "Для жінок", False),
        ("Рукавички шкіряні M. 900грн", None, "Для жінок", False),
        ("Рукавички шкіряні M. 900грн", "Унісекс", "Унісекс", False),
        ("Рукавички шкіряні M. 900грн", "Для чоловіків", "Для чоловіків", True),
    ],
)
def test_gender_of_accessory_from_text_else_women(text, ai_gender, value, yellow):
    field = answer(text, gender=ai_gender)["gender"]
    assert (field.value, field.yellow) == (value, yellow)


def test_ai_doubt_about_gender_is_yellow_when_text_is_silent_but_ignored_when_text_says():
    doubts = [{"field": "gender", "reason": "не видно, чоловічі чи жіночі"}]
    silent = answer("Рукавички шкіряні M. 900грн", doubts=doubts)["gender"]
    said = answer("Чоловічі рукавички M. 900грн", gender="Для чоловіків", doubts=doubts)["gender"]
    assert silent.yellow and not said.yellow


def test_bag_gender_is_still_women_by_default():
    fields = by_name(
        ai.check_answer(
            {
                **GOOD,
                "section": "Аксесуари / Сумки та рюкзаки / Сумки",
                "sizes": [],
                "gender": None,
            },
            NAMES,
            "Шкіряна сумка Coach. 900грн",
        )
    )
    assert fields["gender"].value == "Для жінок" and not fields["gender"].yellow


# --- Повний шлях: Картка → ШІ → бланк ---


def test_card_with_gloves_goes_to_review_with_section(conn, root):
    item_id = add_item(conn, root)
    conn.execute("UPDATE posts SET text = ? WHERE post_id = 1342", (GLOVES_TEXT,))
    conn.commit()
    good = {
        **GOOD,
        "title": "Шкіряні рукавички tommy hilfiger M розміру",
        "section": GLOVES,
        "sizes": ["M"],
        "season": [],
        "gender": "Для жінок",
        "photo_order": ["IMG_1.JPG", "IMG_2.JPG"],
    }

    fill.fill(conn, FakeClient(message(good)), planned_for(conn, root))

    row = item(conn, item_id)
    assert row["status"] == Status.REVIEW and row["problem_code"] is None
    assert review.load_card(conn, item_id).fields["section"].value == GLOVES


# --- Версії і «Перезаповнити» ---


def test_old_answer_without_section_needs_ai_for_gloves_post_only():
    old = {**GOOD, "section": ""}
    assert ai.needs_ai(3, old, "Шкіряні рукавиці Tommy 700грн")
    assert not ai.needs_ai(3, old, "Окуляри Ray-Ban 900грн")
    assert not ai.needs_ai(4, old, "Шкіряні рукавиці Tommy 700грн")
    assert not ai.needs_ai(3, {**GOOD, "section": GLOVES}, "Шкіряні рукавиці Tommy 700грн")


def test_refill_of_seven_problem_cards_marks_only_the_asked_posts(conn, root):
    """Картки `no_section` без бланка (як 3396…) — позначаються за номерами постів, решта — ні."""
    ids = {}
    for post, folder in ((3396, "3396"), (3435, "3435"), (4000, "4000")):
        item_id = add_item(conn, root, folder=folder, post_id=post)
        text = GLOVES_TEXT if post != 4000 else "Шкіряні рукавиці Tommy 700грн"
        conn.execute("UPDATE posts SET text = ? WHERE post_id = ?", (text, post))
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'no_section' WHERE id = ?",
            (item_id,),
        )
        ids[post] = item_id
    conn.commit()

    result = fill.refill_posts(conn, [3396, 3435, 9999])

    assert result.marked == [3396, 3435] and result.skipped == [9999]
    assert item(conn, ids[3396])["status"] == Status.NEW
    assert item(conn, ids[3396])["refill_at"] is not None
    assert item(conn, ids[4000])["status"] == Status.PROBLEM  # не просили — не чіпаємо


def test_refill_posts_leaves_approved_and_published_alone(conn, root):
    approved = add_card(conn, root, status=Status.APPROVED)
    result = fill.refill_posts(conn, [1342])
    assert result.marked == [] and result.skipped == [1342]
    assert item(conn, approved)["status"] == Status.APPROVED


# --- Робот: план для рукавичок на нашій формі ---


def approved_gloves(conn, root, text=GLOVES_TEXT, **fields):
    blank = [
        ai.Field("title", "Шкіряні рукавички tommy hilfiger M розміру"),
        ai.Field("section", GLOVES),
        ai.Field("sizes", ["M"]),
        ai.Field("colors", ["Чорний"]),
        ai.Field("brand", "Tommy Hilfiger"),
        ai.Field("material_upper", ["Шкіра"]),
        ai.Field("material_lining", []),
        ai.Field("season", ["Демісезон"]),
        ai.Field("type", None),
        ai.Field("style", None),
        ai.Field("gender", "Для чоловіків"),
        ai.Field("keywords", ["рукавички", "рукавички чоловічі"]),
        ai.Field("photo_order", ["IMG_1.JPG", "IMG_2.JPG"]),
    ]
    blank = [ai.Field(f.name, fields.get(f.name, f.value), f.yellow, f.reason) for f in blank]
    item_id = add_card(conn, root, blank=blank, text=text, status=Status.APPROVED)
    return review.load_card(conn, item_id)


def test_plan_for_gloves_has_section_letter_sizes_and_gender(conn, root):
    steps, notes = plan(approved_gloves(conn, root), root)
    got = {(s.kind, s.label): s.value for s in steps}

    assert got[("category", "Розділ")] == ["Аксесуари", "Рукавички та рукавиці", "Рукавички"]
    category = next(s for s in steps if s.kind == "category")
    assert category.target == "aksesuary/rukavychky-rukavytsi/rukavychky"
    assert got[("size_system", "Розмір")] == "Міжнародний"
    assert got[("sizes", "Розмір")] == ["M"]
    assert got[("choice", "Стать")] == ["Для чоловіків"]
    assert got[("choice", "Матеріал")] == ["Шкіра"]
    assert got[("choice", "Сезон")] == ["Демісезон"]
    assert notes == []


def test_plan_for_scarf_has_no_size_steps(conn, root):
    card = approved_gloves(
        conn, root, text="Шарф Burberry. 900грн", section=SCARVES, sizes=[], season=[]
    )
    steps, _ = plan(card, root)
    kinds = {s.kind for s in steps}
    assert "size_system" not in kinds and "sizes" not in kinds
    assert (
        next(s for s in steps if s.kind == "category").target == "aksesuary/sharfy-khustky/sharfy"
    )


def test_gloves_card_without_size_cannot_be_approved(conn, root):
    item_id = add_card(
        conn,
        root,
        blank=[ai.Field("section", GLOVES), ai.Field("sizes", [])],
        text=GLOVES_TEXT,
    )
    assert review.load_card(conn, item_id).approve_block() == "no_sizes"


# --- Охоронець ROBOT-007: нові значення знімка відомі таблиці ---


def test_letter_sizes_and_system_names_of_accessories_are_known_to_the_table():
    unknown = []
    for path, entry in catalog.snapshot()["sections"].items():
        if catalog.section_kind(path) is not catalog.Kind.ACCESSORY:
            continue
        for system, names in entry["sizes"].items():
            unknown += [f"{path}: група {system}"] * (not lang.covered(system))
            unknown += [
                f"{path}: {n}" for n in names if catalog.is_letter_size(n) and not lang.covered(n)
            ]
    assert unknown == []


def test_guard_covers_gender_values_of_new_sections():
    values = {
        v
        for path, entry in catalog.snapshot()["sections"].items()
        if path.startswith("Аксесуари / Рукавички")
        for c in entry["characteristics"]
        if c["title"] == "Стать"
        for v in c["choices"]
    }
    assert {"Для жінок", "Для чоловіків", "Для немовлят"} <= values
    assert all(lang.covered(v) for v in values)


# --- Робот: ті самі кроки на підставній формі, дублі ---


def test_gloves_steps_run_on_the_form_without_special_cases(conn, root):
    from shafa.robot.form import execute
    from test_robot_form import FakeDriver

    steps, _ = plan(approved_gloves(conn, root), root)
    driver = FakeDriver(None)

    execute(steps, driver)

    assert ("category", "aksesuary/rukavychky-rukavytsi/rukavychky", "Аксесуари",
            "Рукавички та рукавиці", "Рукавички") in driver.calls  # fmt: skip
    assert ("pick", "Розмір", "Міжнародний") in driver.calls
    assert ("pick", "Розмір", "M") in driver.calls
    assert ("pick", "Стать", "Для чоловіків") in driver.calls


def test_letter_sizes_match_on_the_form_regardless_of_case():
    from shafa.robot.form import among

    assert among("xl", ["S", "M", "XL"]) and not among("XXL", ["S", "M", "XL"])
    assert among("Международный", ["Міжнародний"])


def test_duplicates_of_gloves_are_searched_among_gloves_only():
    from shafa.robot.listings import group_slugs

    gloves = group_slugs(GLOVES)
    assert (
        gloves
        == group_slugs(MITTENS)
        == {
            "aksesuary/rukavychky-rukavytsi/rukavychky",
            "aksesuary/rukavychky-rukavytsi/rukavytsi",
            "aksesuary/rukavychky-rukavytsi/mitenky",
        }
    )
    assert group_slugs(SCARVES) == {
        "aksesuary/sharfy-khustky/sharfy",
        "aksesuary/sharfy-khustky/khustky",
    }
    bags = group_slugs("Аксесуари / Сумки та рюкзаки / Сумки")
    assert "vsi-sumky/sumky" in bags and not bags & gloves
