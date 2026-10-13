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
        "colors": ["Чорний", "Синій", "Сірий"],
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
    assert blanks["colors"]["yellow"] == 1
    assert blanks["colors"]["reason"] == "більше двох кольорів"
    request = conn.execute("SELECT * FROM ai_requests").fetchone()
    assert (request["item_id"], request["input_tokens"], request["output_tokens"]) == (
        item_id,
        9000,
        500,
    )
    assert request["cost_micro_usd"] == 9000 * 2 + 500 * 10
    assert request["error"] is None


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
    ("lining", "row"), [("Текстиль", (1, "немає в тексті: Текстиль")), ("Шкіра", (0, None))]
)
def test_lining_checked_against_post_text(conn, root, lining, row):
    item_id = add_item(conn, root)  # текст поста: «Шкіряні черевики Pavers…»
    good = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"], "material_lining": lining}

    fill.fill(conn, FakeClient(message(good)), planned_for(conn, root))

    saved = conn.execute(
        "SELECT yellow, reason FROM blanks WHERE item_id = ? AND field = 'material_lining'",
        (item_id,),
    ).fetchone()
    assert tuple(saved) == row
