"""KLB-003: черга Клубку для сесії — `take(post_id)`, порожня черга, `release`, швидке `place`."""

from datetime import UTC, datetime

import pytest

from shafa import web
from shafa.klubok import dup, fields, queue
from shafa.review import load_card
from test_klb002_queue import OTHER_PAIR, TODAY, add, row, snapshot

NOW = datetime.now(UTC).astimezone()  # `start` пише справжній час
from test_web import PORT, TOKEN, get, html


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def session(conn):
    with conn:
        return conn.execute(
            "INSERT INTO sessions (started_at, site) VALUES ('x', 'klubok')"
        ).lastrowid


def test_take_post_filter_is_applied_before_the_limit(conn, root, monkeypatch):
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")
    snapshot(conn)
    add(conn, root, 7301)
    second = add(conn, root, 7302, **OTHER_PAIR)

    assert [v.post_id for v in queue.take(conn, root, TODAY).cards] == [7301]
    assert [v.item_id for v in queue.take(conn, root, TODAY, post_id=7302).cards] == [second]


def test_take_post_filter_keeps_the_daily_limit(conn, root, monkeypatch):
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")
    snapshot(conn, at=NOW)
    first = add(conn, root, 7301)
    add(conn, root, 7302, **OTHER_PAIR)
    queue.sync(conn, root, TODAY)
    queue.start(conn, first, session(conn))

    assert queue.take(conn, root, NOW, post_id=7302).code == queue.LIMIT_REACHED


def test_empty_queue_has_its_own_code(conn, root):
    snapshot(conn)
    taken = queue.take(conn, root, TODAY)
    assert taken.cards == [] and taken.code == queue.EMPTY
    assert "нічого немає" in taken.reason
    add(conn, root, 7301)
    assert queue.take(conn, root, TODAY, post_id=9999).code == queue.EMPTY


def test_release_returns_to_queue_and_keeps_the_attempt_in_the_limit(conn, root, monkeypatch):
    monkeypatch.setenv(queue.LIMIT_SETTING, "1")
    snapshot(conn, at=NOW)
    item_id = add(conn, root, 7301)
    queue.sync(conn, root, TODAY)
    assert queue.start(conn, item_id, session(conn))

    assert queue.release(conn, item_id)

    assert row(conn, item_id)["state"] == queue.QUEUED
    assert row(conn, item_id)["publish_started_at"] is not None
    assert queue.take(conn, root, NOW).code == queue.LIMIT_REACHED  # спроба в ліміті
    assert not queue.release(conn, item_id)  # лише з `publishing`


def test_place_needs_no_duplicates_and_no_fields(conn, root, tmp_path, monkeypatch):
    snapshot(conn)
    summer = add(conn, root, 7301, season=("Літо",), date="2026-10-01T10:00:00")
    other = add(conn, root, 7302, date="2026-10-02T10:00:00")
    winter = add(conn, root, 7303, season=("Зима",), date="2026-11-06T10:00:00")
    queue.sync(conn, root, TODAY)
    expected = [v.item_id for v in queue.plan(conn, root) if v.state == queue.QUEUED]

    def boom(*_args, **_kw):
        raise AssertionError("place не має шукати дублі чи збирати поля")

    monkeypatch.setattr(dup, "find", boom)
    monkeypatch.setattr(fields, "ad", boom)

    assert expected == [winter, other, summer]
    assert [queue.place(conn, root, i) for i in expected] == [1, 2, 3]
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)
    assert "Клубок: у черзі · місце 3" in html(get(app, f"/item/{summer}"))


def test_place_without_queued_row_is_none(conn, root):
    item_id = add(conn, root, 7301)
    assert queue.place(conn, root, item_id) is None
    queue.sync(conn, root, TODAY)
    queue.start(conn, item_id, session(conn))
    assert queue.place(conn, root, item_id) is None  # `publishing` — не в черзі
    assert load_card(conn, item_id) is not None
