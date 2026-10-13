import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import CHANNEL_ID
from shafa import ai, fill, review
from shafa.db import Status
from shafa.export import Post
from shafa.folders import list_folders
from shafa.scan import scan
from test_photos import make_photo

POST_TEXT = "Шкіряні кеди Ecco 38 розміру. Устілка 24,5 см, м'які. 1200грн"

BLANK = [
    ai.Field("title", "Шкіряні кеди Ecco 38 розміру"),
    ai.Field("section", "Чоловічий одяг / Взуття / Кеди", True, "розділ не звірено з довідником"),
    ai.Field("sizes", ["38"]),
    ai.Field("colors", ["Білий"]),
    ai.Field("brand", "Ecco"),
    ai.Field("material_upper", ["Шкіра"]),
    ai.Field("material_lining", None),
    ai.Field("season", "Зимаа", True, "значення немає в довіднику"),
    ai.Field("type", None),
    ai.Field("style", None),
    ai.Field("teen", None),
    ai.Field("gender", None),
    ai.Field("keywords", ["кеди", "ecco", "шкіряні", "білі"]),
    ai.Field("photo_order", ["IMG_2.JPG", "IMG_1.JPG"], True, "ШІ переставило фото"),
]


def add_card(
    conn,
    root,
    folder="1342",
    post_id=1342,
    status=Status.REVIEW,
    text=POST_TEXT,
    date="2025-03-01T10:00:00",
    blank=BLANK,
):
    """Пост, Картка з бланком і справжні (згенеровані) фото в папці."""
    photos = ("IMG_1.JPG", "IMG_2.JPG")
    (root / folder).mkdir()
    for name in photos:
        make_photo(root / folder / name, (400, 300))
    with conn:
        conn.execute("INSERT INTO posts VALUES (?, ?, ?, 'export', 'x')", (post_id, date, text))
        item_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'x', 'x')",
            (folder, post_id, status),
        ).lastrowid
        conn.executemany(
            "INSERT INTO photos VALUES (?, ?, ?)",
            [(item_id, i, n) for i, n in enumerate(photos, start=1)],
        )
        fill.save_blank(conn, item_id, blank)
    return item_id


def card(conn, item_id):
    return review.load_card(conn, item_id)


def form_of(c, **changes):
    """Що надіслала б сторінка без змін, плюс `changes`."""
    form = {f.spec.name: list(f.inputs) for f in c.visible}
    form.update({k: v if isinstance(v, list) else [v] for k, v in changes.items()})
    return form


def edits(conn, item_id):
    return {
        r[0]: json.loads(r[1])
        for r in conn.execute("SELECT field, value FROM edits WHERE item_id = ?", (item_id,))
    }


def status(conn, item_id):
    return conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]


# --- Поля ---


def test_fields_from_blank_and_code(conn, root):
    c = card(conn, add_card(conn, root))
    f = c.fields

    assert f["title"].value == "Шкіряні кеди Ecco 38 розміру"
    assert (f["section"].yellow, f["section"].reason) == (True, "розділ не звірено з довідником")
    assert f["price"].value == 1380  # 1200 × 1,15 → вгору до десятка
    assert f["condition"].value == "Ідеальний"
    assert f["insole"].inputs == ["24; 24,5"]  # «24,5» — правило дає два заміри (rules.md)
    assert f["description"].value.endswith("Ідеальний стан, без дефектів.")
    assert not f["price"].yellow
    assert c.yellow == 3  # розділ, сезон, порядок фото
    assert [p.file_name for p in c.photos] == ["IMG_2.JPG", "IMG_1.JPG"]  # порядок від ШІ


def test_teen_only_for_kids_and_gender_only_for_accessories(conn, root):
    c = card(conn, add_card(conn, root))
    names = {f.spec.name for f in c.visible}
    assert "teen" not in names and "gender" not in names

    review.save(conn, c, form_of(c, section="Дитячий одяг / Для дівчаток / Взуття / Кеди"))
    assert "teen" in {f.spec.name for f in card(conn, c.id).visible}

    review.save(conn, c, form_of(c, section="Аксесуари / Сумки та рюкзаки / Рюкзаки"))
    names = {f.spec.name for f in card(conn, c.id).visible}
    assert "gender" in names and "teen" not in names


def test_problem_card_without_post_or_blank(conn):
    with conn:
        item_id = conn.execute(
            "INSERT INTO items (folder_name, status, problem_code, problem_text, created_at, "
            "updated_at) VALUES ('Сукня', 'problem', 'bad_folder_name', 'Назва', 'x', 'x')"
        ).lastrowid
    c = card(conn, item_id)
    assert (c.post_text, c.fields["price"].value, c.fields["sizes"].value) == (None, None, [])
    assert c.yellow == 0


# --- Правки ---


def test_unchanged_form_saves_nothing(conn, root):
    c = card(conn, add_card(conn, root))
    form = form_of(c, description=c.fields["description"].inputs[0].replace("\n", "\r\n"))

    assert review.save(conn, c, form) == ["season"]  # «Зимаа» немає в довіднику — треба обрати
    assert edits(conn, c.id) == {}


def test_changed_field_saved_and_no_longer_yellow(conn, root):
    item_id = add_card(conn, root)
    c = card(conn, item_id)

    bad = review.save(
        conn,
        c,
        form_of(
            c,
            section="жіночий одяг/взуття/ кеди",
            season="Зима",
            sizes=["38.5", ""],
            keywords="кеди, ecco , білі,",
            insole="25",
            price="1400",
        ),
    )

    assert bad == []
    assert edits(conn, item_id) == {
        "section": "Жіночий одяг / Взуття / Кеди",  # написання Шафи
        "season": "Зима",
        "sizes": ["38,5"],
        "keywords": ["кеди", "ecco", "білі"],
        "insole": [25.0],
        "price": 1400,
    }
    f = card(conn, item_id).fields
    assert (f["section"].yellow, f["section"].reason, f["section"].edited) == (False, None, True)
    assert f["price"].value == 1400


def test_own_section_path_allowed(conn, root):
    c = card(conn, add_card(conn, root))
    review.save(conn, c, form_of(c, section="Жіночий одяг/Взуття/Мокасини дуже гарні"))
    assert edits(conn, c.id)["section"] == "Жіночий одяг / Взуття / Мокасини дуже гарні"


def test_confirm_records_same_value(conn, root):
    c = card(conn, add_card(conn, root))

    review.save(conn, c, form_of(c, season="Зима"), confirm="section")

    assert edits(conn, c.id)["section"] == "Чоловічий одяг / Взуття / Кеди"
    assert not card(conn, c.id).fields["section"].yellow


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", "дорого"),
        ("price", "0"),
        ("price", "1 400"),
        ("season", "Літечко"),
        ("colors", ["Фіолетовенький", ""]),
        ("condition", ""),
    ],
)
def test_bad_value_not_saved_others_saved(conn, root, field, value):
    c = card(conn, add_card(conn, root))

    bad = review.save(conn, c, form_of(c, **{"season": "Зима", "brand": "ECCO", field: value}))

    assert field in bad
    assert field not in edits(conn, c.id)
    assert edits(conn, c.id)["brand"] == "ECCO"


def test_empty_values_clear_field(conn, root):
    c = card(conn, add_card(conn, root))
    review.save(conn, c, form_of(c, season="", brand="", sizes=["", ""], price=""))
    assert edits(conn, c.id) == {"season": None, "brand": None, "sizes": [], "price": None}


def test_condition_edit_changes_description_ending(conn, root):
    c = card(conn, add_card(conn, root))
    review.save(conn, c, form_of(c, season="Зима", condition="Новий"))
    assert card(conn, c.id).fields["description"].value.endswith("Стан нових, без дефектів.")


def test_photo_order_confirm_and_folder_order(conn, root):
    c = card(conn, add_card(conn, root))
    review.save(conn, c, form_of(c, season="Зима", photo_order="IMG_1.JPG\nIMG_2.JPG"))
    after = card(conn, c.id)
    assert [p.file_name for p in after.photos] == ["IMG_1.JPG", "IMG_2.JPG"]
    assert not after.fields["photo_order"].yellow


def test_photo_order_must_be_the_folder_photos(conn, root):
    c = card(conn, add_card(conn, root))
    bad = review.save(conn, c, form_of(c, season="Зима", photo_order="../секрет.jpg"))
    assert "photo_order" in bad


def test_new_ai_blank_keeps_edits(conn, root):
    item_id = add_card(conn, root)
    c = card(conn, item_id)
    review.save(conn, c, form_of(c, season="Зима", brand="ECCO"))

    with conn:  # «ШІ, спробуй ще раз» перепише лише бланк
        fill.save_blank(conn, item_id, [ai.Field("brand", "Інший"), ai.Field("title", "Нова")])

    f = card(conn, item_id).fields
    assert (f["brand"].value, f["title"].value) == ("ECCO", "Нова")


def test_only_review_cards_are_edited(conn, root):
    c = card(conn, add_card(conn, root, status=Status.APPROVED))
    assert review.save(conn, c, form_of(c, brand="ECCO", season="Зима")) == []
    assert edits(conn, c.id) == {}


# --- Статуси ---


def test_approve_blocked_by_yellow_section(conn, root):
    item_id = add_card(conn, root)

    assert review.act(conn, item_id, "approve") == "section_yellow"
    assert status(conn, item_id) == Status.REVIEW

    c = card(conn, item_id)
    review.save(conn, c, form_of(c, season="Зима"), confirm="section")
    assert review.act(conn, item_id, "approve") == "approved"
    assert status(conn, item_id) == Status.APPROVED


def test_approve_after_section_changed(conn, root):
    item_id = add_card(conn, root)
    c = card(conn, item_id)
    review.save(conn, c, form_of(c, season="Зима", section="Жіночий одяг / Взуття / Кеди"))
    assert review.act(conn, item_id, "approve") == "approved"


def test_approve_blocked_without_section(conn, root):
    item_id = add_card(conn, root)
    c = card(conn, item_id)
    review.save(conn, c, form_of(c, season="Зима", section=""), confirm="section")
    assert review.act(conn, item_id, "approve") == "no_section"


def test_other_yellow_fields_do_not_block_approve(conn, root):
    item_id = add_card(conn, root, blank=[ai.Field("section", "Жіночий одяг / Взуття / Кеди")])
    assert review.act(conn, item_id, "approve") == "approved"


def test_reject_restore_unapprove(conn, root):
    item_id = add_card(conn, root, blank=[ai.Field("section", "Жіночий одяг / Взуття / Кеди")])

    assert review.act(conn, item_id, "reject") == "rejected"
    assert status(conn, item_id) == Status.ARCHIVED
    assert card(conn, item_id).archived_at is not None

    assert review.act(conn, item_id, "restore") == "restored"
    assert (status(conn, item_id), card(conn, item_id).archived_at) == (Status.REVIEW, None)

    review.act(conn, item_id, "approve")
    assert review.act(conn, item_id, "unapprove") == "unapproved"
    assert status(conn, item_id) == Status.REVIEW


@pytest.mark.parametrize(
    ("start", "action"),
    [
        (Status.APPROVED, "approve"),
        (Status.ARCHIVED, "reject"),
        (Status.REVIEW, "unapprove"),
        (Status.REVIEW, "restore"),
        (Status.PROBLEM, "approve"),
    ],
)
def test_action_from_wrong_status_is_stale(conn, root, start, action):
    item_id = add_card(conn, root, status=start)
    assert review.act(conn, item_id, action) in ("stale", "section_yellow")
    assert status(conn, item_id) == start


# --- Архів ---


def archive(conn, item_id, days_ago):
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "UPDATE items SET status = 'archived', archived_at = ? WHERE id = ?", (stamp, item_id)
        )


def test_purge_hides_old_archive_and_keeps_record(conn, root):
    old = add_card(conn, root)
    fresh = add_card(conn, root, folder="1349", post_id=1349)
    c = card(conn, old)
    review.save(conn, c, form_of(c, brand="ECCO", season="Зима"))
    archive(conn, old, days_ago=8)
    archive(conn, fresh, days_ago=6)

    assert review.purge_archive(conn) == [old]

    row = conn.execute("SELECT status, purged_at FROM items WHERE id = ?", (old,)).fetchone()
    assert row["status"] == Status.ARCHIVED and row["purged_at"]
    for table in ("photos", "blanks", "edits"):
        count = conn.execute(f"SELECT count(*) FROM {table} WHERE item_id = ?", (old,))
        assert count.fetchone()[0] == 0
    assert card(conn, old) is None
    assert [c.id for c in review.list_tab(conn, Status.ARCHIVED)] == [fresh]
    assert review.counts(conn)[Status.ARCHIVED] == 1
    assert (root / "1342" / "IMG_1.JPG").is_file()  # папку з фото не чіпаємо
    assert review.purge_archive(conn) == []


def test_purged_card_not_recreated_by_scan_or_sent_to_ai(conn, root):
    item_id = add_card(conn, root)
    archive(conn, item_id, days_ago=8)
    review.purge_archive(conn)

    posts = {1342: Post(1342, "2025-03-01T10:00:00", POST_TEXT)}
    result = scan(conn, posts, list_folders(root, CHANNEL_ID))

    assert result.new == 0
    rows = conn.execute("SELECT id, status FROM items").fetchall()
    assert [tuple(r) for r in rows] == [(item_id, Status.ARCHIVED)]
    assert fill.pick_items(conn, None, 10) == []


def test_restore_of_purged_card_impossible(conn, root):
    item_id = add_card(conn, root)
    archive(conn, item_id, days_ago=8)
    review.purge_archive(conn)
    assert review.act(conn, item_id, "restore") == "stale"


# --- Список ---


def test_list_sorted_by_post_date_and_searchable(conn, root):
    late = add_card(conn, root, folder="1395", post_id=1395, date="2025-03-08T10:00:00")
    early = add_card(conn, root, folder="1342", post_id=1342, date="2025-03-01T10:00:00")
    other = add_card(
        conn, root, folder="1386", post_id=1386, text="Черевики Lowa 39 розміру. 900грн",
        date="2025-03-09T10:00:00",
        blank=[ai.Field("title", "Черевики Lowa"), ai.Field("brand", "Lowa")],
    )  # fmt: skip

    assert [c.id for c in review.list_tab(conn, Status.REVIEW)] == [early, late, other]
    assert [c.id for c in review.list_tab(conn, Status.REVIEW, "ЧЕРЕВИКИ")] == [other]
    assert [c.id for c in review.list_tab(conn, Status.REVIEW, "1395")] == [late]
    assert [c.id for c in review.list_tab(conn, Status.REVIEW, "lowa 39")] == [other]
    assert review.list_tab(conn, Status.APPROVED) == []
