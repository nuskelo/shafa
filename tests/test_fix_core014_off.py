"""FIX-CORE-014-off: пости з кількома розмірами не розбиваються на Картки — `many_items`, як до CORE-014."""

import pytest

from conftest import make_folder
from shafa import ai, fill, scan, variants
from shafa.db import Status
from shafa.export import Post
from shafa.folders import Folder, list_folders
from test_core014 import P3654, P3655, P3659, old_card

POSTS = {3654: P3654, 3655: P3655, 3659: P3659}
SINGLE = "У пості кілька розмірів — схоже, кілька товарів"
PRICES = "У пості кілька товарів з різними цінами"


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("запит до ШІ")

    monkeypatch.setattr(ai, "call", boom)


def test_switch_is_off_by_default():
    assert variants.SPLIT_SIZES is False


@pytest.mark.parametrize("post_id", POSTS)
def test_find_problem_is_many_items(post_id):
    folder = Folder(str(post_id), post_id, ["1.jpg"])
    problem = scan.find_problem(folder, {post_id: POSTS[post_id]}, {post_id: [folder.name]})
    assert problem is not None and problem[0] == "many_items"
    assert problem[1] in (SINGLE, PRICES)


def test_scan_makes_one_problem_card_without_tilde(conn, root):
    posts = {}
    for post_id, text in POSTS.items():
        make_folder(root, str(post_id), ["1.jpg"])
        posts[post_id] = Post(post_id, "2025-01-01T10:00:00", text)
    scan.scan(conn, posts, list_folders(root, None))
    scan.scan(conn, posts, list_folders(root, None))

    rows = conn.execute("SELECT folder_name, status, problem_code FROM items").fetchall()
    assert sorted(r["folder_name"] for r in rows) == ["3654", "3655", "3659"]
    assert all((r["status"], r["problem_code"]) == (Status.PROBLEM, "many_items") for r in rows)


def test_recheck_and_app_start_split_nothing_and_no_ai(conn):
    old_card(conn)
    assert fill.recheck(conn) == 0
    assert variants.split_waiting(conn) == 0
    names = [r[0] for r in conn.execute("SELECT folder_name FROM items")]
    assert names == ["borh-3654"]
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 0


def test_split_card_does_nothing_when_off(conn):
    item_id = old_card(conn)
    assert variants.split_card(conn, item_id) == []
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
