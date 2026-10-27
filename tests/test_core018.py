"""CORE-018: пост бота зник з каналу → його Картки (`new`/`review`/`approved`) самі в Архів.
Підставні Telegram і сайт, усе в tmp_path, мережі й справжніх даних немає."""

# ruff: noqa: F811  — фікстури підставних Telegram і сайту беремо з test_site_wait

import json

import pytest

from shafa import app, config, db, fill, review
from shafa.db import Status
from shafa.robot import publish
from test_site import add_post, job
from test_site_wait import channel, clock, done_post, fake, on, said, steps, worker  # noqa: F401

TEXT = "Black Lace-Up Boots 38 розміру. 1450грн"
ARCHIVED = (Status.ARCHIVED, "post_deleted")


def card(conn, post_id=1401, status=Status.NEW, source="bot", shafa_id=None, folder=None):
    """Пост і Картка без фото й бланка (для архівації вони не потрібні)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, '2026-10-01T10:00:00', ?, ?, 'x')",
            (post_id, TEXT, source),
        )
        return conn.execute(
            "INSERT INTO items (folder_name, post_id, status, shafa_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'x', 'x')",
            (folder or f"{post_id}", post_id, status, shafa_id),
        ).lastrowid


def state(conn, item_id):
    r = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return r["status"], r["problem_code"]


def journal():
    path = config.data_dir() / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- archive_gone ---


@pytest.mark.parametrize("status", [Status.NEW, Status.REVIEW, Status.APPROVED])
def test_archive_gone_moves_bot_cards(conn, status):
    item = card(conn, status=status)

    assert review.archive_gone(conn, 1401) == 1

    assert state(conn, item) == ARCHIVED
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()
    assert row["problem_text"] == review.POST_DELETED_TEXT and row["archived_at"]
    assert review.archive_gone(conn, 1401) == 0  # повторно нічого


def test_archive_gone_all_sisters_of_the_post(conn):
    a = card(conn, folder="1401~38")
    b = card(conn, status=Status.REVIEW, folder="1401~39")

    assert review.archive_gone(conn, 1401) == 2

    assert state(conn, a) == ARCHIVED and state(conn, b) == ARCHIVED


def test_old_post_is_never_archived(conn):
    """Видалений старий пост (Експорт, `borh-`) — не «продано»."""
    export = card(conn, post_id=100, source="export", status=Status.APPROVED)
    borh = card(conn, post_id=101, source="export", folder="borh-101")

    assert review.archive_gone(conn, 100) == 0 and review.archive_gone(conn, 101) == 0

    assert state(conn, export) == (Status.APPROVED, None)
    assert state(conn, borh) == (Status.NEW, None)


@pytest.mark.parametrize(
    ("status", "shafa_id"),
    [
        (Status.PUBLISHED, "555"),
        (Status.PUBLISHED, None),
        (Status.APPROVED, "555"),  # уже є на Шафі
        (Status.PROBLEM, None),
        (Status.PUBLISHING, None),
        (Status.ARCHIVED, None),
    ],
)
def test_published_and_other_states_untouched(conn, status, shafa_id):
    item = card(conn, status=status, shafa_id=shafa_id)

    assert review.archive_gone(conn, 1401) == 0

    assert state(conn, item) == (status, None)


# --- виклики: вихід на сайт, нічна перевірка, робот, старт ---


@pytest.mark.parametrize("status", [Status.NEW, Status.REVIEW, Status.APPROVED])
def test_gone_before_site_release_archives_card(conn, root, on, worker, clock, channel, status):
    add_post(conn, root)
    item = card(conn, status=status)
    channel.posts.discard(1401)
    clock.pass_(days=8)

    steps(worker)

    assert job(conn)["state"] == "cancelled"
    assert state(conn, item) == ARCHIVED


def test_night_gone_archives_card_and_tells_count(conn, root, on, worker, clock, channel):
    item = card(conn, status=Status.REVIEW)
    done_post(conn, root, worker, clock)
    assert state(conn, item) == (Status.REVIEW, None)
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)

    assert state(conn, item) == ARCHIVED
    night = [e["text"] for e in journal() if e["kind"] == "site.night"]
    assert night and "1 Картка прибрано в Архів" in night[-1]


def test_night_gone_old_post_card_untouched(conn, root, on, worker, clock, channel):
    item = card(conn, source="export", status=Status.APPROVED)
    done_post(conn, root, worker, clock)
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)

    assert state(conn, item) == (Status.APPROVED, None)


def test_night_unknown_is_not_a_reason(conn, root, on, worker, clock, channel):
    item = card(conn, status=Status.NEW)
    done_post(conn, root, worker, clock)
    channel.mode = "down"
    clock.pass_(days=1)

    steps(worker)

    assert conn.execute("SELECT unknown_since FROM post_checks").fetchone()[0] is not None
    assert state(conn, item) == (Status.NEW, None)


def test_release_unknown_is_not_a_reason(conn, root, on, worker, clock, channel):
    add_post(conn, root)
    item = card(conn)
    channel.mode = "down"
    clock.pass_(days=8)

    steps(worker)

    assert state(conn, item) == (Status.NEW, None)


def test_gone_event_mentions_archived_cards(conn, root, on, worker, clock, channel):
    add_post(conn, root)
    card(conn)
    card(conn, folder="1401~39")
    channel.posts.discard(1401)
    clock.pass_(days=8)

    steps(worker)

    gone = [e for e in journal() if e["kind"] == "post.gone"]
    assert len(gone) == 1 and "2 Картки прибрано в Архів" in gone[0]["text"]


def test_robot_post_deleted_still_archives(conn):
    item = card(conn, status=Status.APPROVED)
    sister = card(conn, status=Status.APPROVED, folder="1401~39")

    class Run(publish.Run):
        def __init__(self):
            self.conn, self.presence, self.lines = conn, lambda post_id: "gone", []

        def log(self, text):
            self.lines.append(text)

    report = publish.Report(session_id=1)

    assert Run()._post_deleted(report, review.load_card(conn, item)) is True

    assert state(conn, item) == ARCHIVED and state(conn, sister) == ARCHIVED
    assert report.notes == [f"Пост 1401: {review.POST_DELETED_TEXT} — у Архіві"]


def seed_gone(conn):
    cancelled = card(conn, post_id=1, status=Status.NEW)
    checked = card(conn, post_id=2, status=Status.REVIEW)
    unknown = card(conn, post_id=3, status=Status.NEW)
    old = card(conn, post_id=4, source="export", status=Status.NEW)
    published = card(conn, post_id=5, status=Status.PUBLISHED, shafa_id="9")
    with conn:
        conn.execute(
            "INSERT INTO site_jobs (post_id, folder_name, posted_at, text, state, not_before, "
            "attempts, created_at, updated_at) VALUES (1, '1', 'x', 'x', 'cancelled', 'x', 0, "
            "'x', 'x')"
        )
        conn.execute("INSERT INTO post_checks (post_id, gone_at) VALUES (2, 'x')")
        conn.execute("INSERT INTO post_checks (post_id, unknown_since) VALUES (3, 'x')")
        conn.execute("INSERT INTO post_checks (post_id, gone_at) VALUES (4, 'x')")
        conn.execute("INSERT INTO post_checks (post_id, gone_at) VALUES (5, 'x')")
    return cancelled, checked, unknown, old, published


def test_cleanup_archives_cancelled_and_gone_idempotent(conn):
    cancelled, checked, unknown, old, published = seed_gone(conn)

    assert review.archive_all_gone(conn) == {1: 1, 2: 1}

    assert state(conn, cancelled) == ARCHIVED and state(conn, checked) == ARCHIVED
    assert state(conn, unknown) == (Status.NEW, None)
    assert state(conn, old) == (Status.NEW, None)
    assert state(conn, published) == (Status.PUBLISHED, None)
    query = "SELECT updated_at, archived_at FROM items WHERE id = ?"
    before = tuple(conn.execute(query, (cancelled,)).fetchone())
    assert review.archive_all_gone(conn) == {}  # другий запуск нічого не міняє
    assert tuple(conn.execute(query, (cancelled,)).fetchone()) == before


def test_app_start_cleans_up_and_tells():
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    conn = db.connect(app.db_path())
    cancelled, *_ = seed_gone(conn)
    conn.close()

    assert app.start(lambda url: None, port=0, serve=lambda server: None) == 0

    conn = db.connect(app.db_path())
    assert state(conn, cancelled) == ARCHIVED
    conn.close()
    texts = [e["text"] for e in journal() if e["kind"] == "post.gone"]
    assert "1 видалено з каналу — 1 Картка прибрано в Архів" in texts


# --- ШІ й робот не беруть Архів ---


def test_archived_card_does_not_go_to_ai_or_queue(conn):
    kept = card(conn, post_id=1, status=Status.NEW)
    gone = card(conn, post_id=2, status=Status.NEW)
    approved = card(conn, post_id=3, status=Status.APPROVED)
    assert {r["id"] for r in fill.pick_items(conn, None, 10)} == {kept, gone}
    assert [r["id"] for r in publish.queue_rows(conn)] == [approved]

    review.archive_gone(conn, 2)
    review.archive_gone(conn, 3)

    assert [r["id"] for r in fill.pick_items(conn, None, 10)] == [kept]
    assert publish.queue_rows(conn) == []
