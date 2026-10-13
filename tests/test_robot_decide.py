"""Рішення людини щодо «липких» проблем публікації (UI-002): кожна кнопка — свій перехід."""

import pytest

from shafa.db import Status
from shafa.folders import PUBLISHED_DIR
from shafa.robot import publish
from test_robot_publish import Env, add


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("221913242", (221913242, "https://shafa.ua/uk/item/221913242-shafa")),
        (" 221913242 ", (221913242, "https://shafa.ua/uk/item/221913242-shafa")),
        (
            "https://shafa.ua/uk/women/zhenskaya-obuv/krossovki/206042583-krosivki-skechers?x=1#a",
            (206042583, "https://shafa.ua/uk/women/zhenskaya-obuv/krossovki/206042583-krosivki-skechers"),
        ),
        ("http://www.shafa.ua/uk/item/221913242-x", (221913242, "https://shafa.ua/uk/item/221913242-x")),
        ("shafa.ua/uk/item/221913242", (221913242, "https://shafa.ua/uk/item/221913242")),
        ("https://shafa.ua.evil.com/uk/item/221913242-x", None),
        ("https://evilshafa.ua/uk/item/221913242-x", None),
        ("https://user@shafa.ua/uk/item/221913242-x", None),
        ("https://shafa.ua:8080/uk/item/221913242-x", None),
        ("javascript:alert(1)", None),
        ("https://shafa.ua/uk/my/clothes/active", None),
        ("", None),
        ("12", None),
        ("Кросівки skechers", None),
    ],
)  # fmt: skip
def test_parse_shafa_link(text, parsed):
    assert publish.parse_shafa_link(text) == parsed


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def problem(env, code, folder="1342", post_id=1342):
    item = add(env, folder, post_id)
    with env.conn:
        env.conn.execute(
            "UPDATE items SET status = 'problem', problem_code = ?, problem_text = 'т' WHERE id = ?",
            (code, item),
        )
    return item


def row(env, item):
    return env.conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()


@pytest.mark.parametrize("code", ["publish_uncertain", "publish_error"])
def test_on_shafa_marks_published_and_moves_folder(env, code):
    item = problem(env, code)
    link = "https://shafa.ua/uk/women/zhenskaya-obuv/krossovki/206042583-krosivki"
    assert publish.on_shafa(env.conn, env.root, item, link) == "on_shafa"
    r = row(env, item)
    assert (r["status"], r["shafa_id"], r["shafa_url"]) == (Status.PUBLISHED, 206042583, link)
    assert r["published_at"] and r["problem_code"] is None
    assert (env.root / PUBLISHED_DIR / "1342").is_dir()


def test_on_shafa_refuses_bad_link_and_other_codes(env):
    item = problem(env, "publish_uncertain")
    assert (
        publish.on_shafa(env.conn, env.root, item, "https://evil.com/1/221913242-x") == "bad_link"
    )
    assert row(env, item)["status"] == Status.PROBLEM
    dup = problem(env, "possible_duplicate", "1400", 1400)
    assert publish.on_shafa(env.conn, env.root, dup, "221913242") == "stale"
    assert row(env, dup)["status"] == Status.PROBLEM


@pytest.mark.parametrize("code", ["publish_uncertain", "publish_error"])
def test_not_on_shafa_back_to_approved(env, code):
    item = problem(env, code)
    assert publish.not_on_shafa(env.conn, item) == "not_on_shafa"
    assert (row(env, item)["status"], row(env, item)["problem_code"]) == (Status.APPROVED, None)


@pytest.mark.parametrize("code", ["possible_duplicate", "already_published", "robot_field_missing"])
def test_not_on_shafa_only_for_unsure_publication(env, code):
    item = problem(env, code)
    assert publish.not_on_shafa(env.conn, item) == "stale"
    assert row(env, item)["status"] == Status.PROBLEM


def test_other_item_remembered_and_not_flagged_again(env):
    """«Це інший товар» → Схвалені; наступна публікація схожим оголошенням не зупиняється."""
    from test_robot_publish import SAME

    item = problem(env, "possible_duplicate")
    assert publish.other_item(env.conn, item) == "other_item"
    r = row(env, item)
    assert (r["status"], r["problem_code"]) == (Status.APPROVED, None) and r["duplicate_ok_at"]

    env.listings = [SAME]
    env.run()
    assert row(env, item)["status"] == Status.PUBLISHED


@pytest.mark.parametrize("code", ["already_published", "publish_uncertain"])
def test_other_item_only_for_similar_listing(env, code):
    """Той самий пост (already_published) «іншим товаром» не буває."""
    item = problem(env, code)
    assert publish.other_item(env.conn, item) == "stale"
    assert row(env, item)["status"] == Status.PROBLEM


@pytest.mark.parametrize(
    ("code", "result"),
    [
        ("possible_duplicate", "duplicate_archived"),
        ("already_published", "duplicate_archived"),
        ("publish_uncertain", "stale"),
    ],
)
def test_to_archive(env, code, result):
    item = problem(env, code)
    assert publish.to_archive(env.conn, item) == result
    r = row(env, item)
    if result == "stale":
        assert r["status"] == Status.PROBLEM
    else:
        assert r["status"] == Status.ARCHIVED and r["archived_at"]
