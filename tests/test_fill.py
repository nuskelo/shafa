import json

import anthropic
import httpx2
import pytest

from shafa import ai, fill
from shafa.db import Status
from test_ai import GOOD, FakeClient, message
from test_photos import make_photo

POST_TEXT = "Шкіряні черевики Pavers 38 розміру. 1200грн"


def add_item(conn, root, folder="1342", post_id=1342, photos=("IMG_1.JPG", "IMG_2.JPG")):
    """Пост, Картка `new` і справжні (згенеровані) фото в папці."""
    (root / folder).mkdir()
    for name in photos:
        make_photo(root / folder / name, (400, 300))
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, '2025-01-01T00:00:00', ?, 'export', 'x')",
            (post_id, POST_TEXT),
        )
        item_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'new', 'x', 'x')",
            (folder, post_id),
        ).lastrowid
        conn.executemany(
            "INSERT INTO photos VALUES (?, ?, ?)",
            [(item_id, i, n) for i, n in enumerate(photos, start=1)],
        )
    return item_id


def planned_for(conn, root):
    (row,) = fill.pick_items(conn, None, 10)
    result = fill.plan(conn, root, row)
    assert isinstance(result, fill.Planned)
    return result


def item(conn, item_id):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def test_success_saves_blank_cost_and_moves_to_review(conn, root):
    item_id = add_item(conn, root)
    good = {
        **GOOD,
        "photo_order": ["IMG_1.JPG", "IMG_2.JPG"],
        "keywords": ["черевики", "черевики жіночі", "черевики pavers"],
    }
    client = FakeClient(message(good, usage=(9000, 500)))

    result = fill.fill(conn, client, planned_for(conn, root))

    assert result == fill.Filled("1342", ai.Usage(9000, 500), yellow=1)
    assert item(conn, item_id)["status"] == Status.REVIEW
    blanks = {
        r["field"]: r for r in conn.execute("SELECT * FROM blanks WHERE item_id = ?", (item_id,))
    }
    assert set(blanks) == set(ai.FIELDS)
    assert json.loads(blanks["title"]["value"]) == GOOD["title"]
    assert blanks["keywords"]["yellow"] == 1
    assert blanks["keywords"]["reason"] == "ключових слів не 4–6"
    request = conn.execute("SELECT * FROM ai_requests").fetchone()
    assert (request["item_id"], request["input_tokens"], request["output_tokens"]) == (
        item_id,
        9000,
        500,
    )
    assert request["cost_micro_usd"] == 9000 * 2 + 500 * 10
    assert request["error"] is None


def test_no_section_from_ai_is_problem_not_similar_section(conn, root):
    """FIX-002: рукавички — розділу на Шафі немає; ШІ повертає порожній розділ → «Проблеми»."""
    item_id = add_item(conn, root)
    good = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"], "section": ""}

    fill.fill(conn, FakeClient(message(good)), planned_for(conn, root))

    row = item(conn, item_id)
    assert (row["status"], row["problem_code"]) == (Status.PROBLEM, "no_section")
    assert row["problem_text"].startswith("Немає підходящого розділу на Шафі")


def test_api_error_moves_to_problem_without_blank(conn, root):
    item_id = add_item(conn, root)
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x.test"))

    result = fill.fill(conn, FakeClient(error), planned_for(conn, root))

    assert result.error == "Немає з'єднання з Claude API"
    row = item(conn, item_id)
    assert (row["status"], row["problem_code"], row["problem_text"]) == (
        Status.PROBLEM,
        "ai_error",
        "Немає з'єднання з Claude API",
    )
    assert conn.execute("SELECT count(*) FROM blanks").fetchone()[0] == 0
    request = conn.execute("SELECT * FROM ai_requests").fetchone()
    assert (request["cost_micro_usd"], request["error"]) == (0, "Немає з'єднання з Claude API")


def test_truncated_answer_is_problem_and_cost_counted(conn, root):
    item_id = add_item(conn, root)
    client = FakeClient(message('{"title": "Шк', stop_reason="max_tokens", usage=(9000, 4000)))

    fill.fill(conn, client, planned_for(conn, root))

    assert item(conn, item_id)["problem_text"] == "Відповідь ШІ обрізана"
    cost = conn.execute("SELECT cost_micro_usd FROM ai_requests").fetchone()[0]
    assert cost == 9000 * 2 + 4000 * 10


def test_plan_prepares_photos_in_folder_order(conn, root):
    add_item(conn, root, photos=("IMG_2.JPG", "IMG_10.JPG"))
    planned = planned_for(conn, root)
    assert [p.file_name for p in planned.photos] == ["IMG_2.JPG", "IMG_10.JPG"]
    assert (planned.photos[0].width, planned.photos[0].height) == (400, 300)
    assert planned.estimate.input_tokens > 2 * planned.photos[0].tokens


def test_plan_skips_broken_photo(conn, root):
    add_item(conn, root)
    (root / "1342" / "IMG_2.JPG").write_bytes(b"not a jpeg")
    (row,) = fill.pick_items(conn, None, 10)
    assert fill.plan(conn, root, row) == fill.Skipped("1342", "Фото не відкривається: IMG_2.JPG")


def test_plan_skips_missing_photo(conn, root):
    add_item(conn, root)
    (root / "1342" / "IMG_1.JPG").unlink()
    (row,) = fill.pick_items(conn, None, 10)
    assert fill.plan(conn, root, row).text == "Фото не відкривається: IMG_1.JPG"


def test_pick_items_only_new_and_capped(conn, root):
    ids = [add_item(conn, root, folder=str(n), post_id=n) for n in range(1, 13)]
    with conn:
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (ids[0],))
    assert [r["post_id"] for r in fill.pick_items(conn, None, 50)] == list(range(2, 12))
    assert [r["post_id"] for r in fill.pick_items(conn, None, 3)] == [2, 3, 4]
    assert [r["post_id"] for r in fill.pick_items(conn, 7, 10)] == [7]
    assert fill.pick_items(conn, 1, 10) == []


@pytest.mark.parametrize("status", [Status.REVIEW, Status.PROBLEM])
def test_status_changes_only_from_new(conn, root, status):
    item_id = add_item(conn, root)
    planned = planned_for(conn, root)
    with conn:
        conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
    fill.fill(conn, FakeClient(message({**GOOD, "photo_order": []})), planned)
    assert item(conn, item_id)["status"] == status
    assert conn.execute("SELECT count(*) FROM blanks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("lining", "row"), [("Текстиль", (0, None, "[]")), ("Шкіра", (0, None, '["Шкіра"]'))]
)
def test_lining_checked_against_post_text(conn, root, lining, row):
    item_id = add_item(conn, root)  # текст поста: «Шкіряні черевики Pavers…»; не з тексту — геть
    good = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"], "material_lining": lining}

    fill.fill(conn, FakeClient(message(good)), planned_for(conn, root))

    saved = conn.execute(
        "SELECT yellow, reason, value FROM blanks WHERE item_id = ? AND field = 'material_lining'",
        (item_id,),
    ).fetchone()
    assert tuple(saved) == row


def test_cache_tokens_and_cost_are_logged(conn, root):
    item_id = add_item(conn, root)
    good = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}
    reply = message(good, usage=(1000, 400), cache=(9000, 0))

    fill.fill(conn, FakeClient(reply), planned_for(conn, root))

    row = conn.execute("SELECT * FROM ai_requests WHERE item_id = ?", (item_id,)).fetchone()
    assert (row["input_tokens"], row["cache_write_tokens"], row["cache_read_tokens"]) == (
        1000, 9000, 0
    )  # fmt: skip
    assert row["cost_micro_usd"] == 1000 * 2 + 400 * 10 + 9000 * 4
    assert row["batch_id"] is None


def test_pick_items_skips_cards_waiting_in_batch(conn, root):
    first = add_item(conn, root, folder="1", post_id=1)
    add_item(conn, root, folder="2", post_id=2)
    with conn:
        conn.execute(
            "INSERT INTO ai_batches (id, status, created_at) VALUES (1, 'in_progress', 'x')"
        )
        conn.execute("INSERT INTO ai_batch_items VALUES (1, ?, 30000, 'pending')", (first,))
    assert [r["post_id"] for r in fill.pick_items(conn, None, 10)] == [2]
    with conn:
        conn.execute("UPDATE ai_batch_items SET state = 'returned'")
    assert [r["post_id"] for r in fill.pick_items(conn, None, 10)] == [1, 2]


def review_card_with_edit(conn, root):
    """Картка на перевірці зі старим бланком і ручною правкою назви."""
    item_id = add_item(conn, root)
    with conn:
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (item_id,))
        conn.execute(
            "INSERT INTO blanks VALUES (?, 'title', '\"Стара назва\"', 1, 'x')", (item_id,)
        )
        conn.execute("INSERT INTO edits VALUES (?, 'title', '\"Моя назва\"', 'x')", (item_id,))
    return item_id


def retry_planned(conn, root, item_id):
    planned = fill.plan(conn, root, fill.retry_row(conn, item_id))
    assert isinstance(planned, fill.Planned)
    return planned


def test_retry_replaces_blank_but_keeps_edits(conn, root):
    item_id = review_card_with_edit(conn, root)
    planned = retry_planned(conn, root, item_id)
    assert "cache_control" not in json.dumps(planned.request)  # без кешу (D2)
    client = FakeClient(message({**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}))

    result = fill.retry(conn, client, planned)

    assert result.error is None
    assert item(conn, item_id)["status"] == Status.REVIEW
    title = conn.execute(
        "SELECT value FROM blanks WHERE item_id = ? AND field = 'title'", (item_id,)
    ).fetchone()[0]
    assert json.loads(title) == GOOD["title"]
    edits = conn.execute("SELECT field, value FROM edits WHERE item_id = ?", (item_id,)).fetchall()
    assert [tuple(e) for e in edits] == [("title", '"Моя назва"')]
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 1


def test_retry_error_keeps_old_blank_and_status(conn, root):
    item_id = review_card_with_edit(conn, root)
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x.test"))

    result = fill.retry(conn, FakeClient(error), retry_planned(conn, root, item_id))

    assert result.error == "Немає з'єднання з Claude API"
    assert item(conn, item_id)["status"] == Status.REVIEW
    assert conn.execute("SELECT value FROM blanks").fetchone()[0] == '"Стара назва"'
    assert conn.execute("SELECT error FROM ai_requests").fetchone()[0] == result.error


def test_retry_does_not_touch_card_approved_meanwhile(conn, root):
    item_id = review_card_with_edit(conn, root)
    planned = retry_planned(conn, root, item_id)
    with conn:
        conn.execute("UPDATE items SET status = 'approved' WHERE id = ?", (item_id,))

    fill.retry(conn, FakeClient(message({**GOOD, "photo_order": []})), planned)

    assert item(conn, item_id)["status"] == Status.APPROVED
    assert conn.execute("SELECT value FROM blanks").fetchone()[0] == '"Стара назва"'
    assert fill.retry_row(conn, item_id) is None
