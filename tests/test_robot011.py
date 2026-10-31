"""ROBOT-011: відхилені Шафою → «Проблеми» `shafa_declined`; без живої Шафи, на фікстурах feed."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from shafa import db, fill, review, scan, web
from shafa.bot import report
from shafa.db import Status
from shafa.robot import listings as lst
from shafa.robot import lock, publish
from shafa.robot.listings import Listing, ListingsError, mark_declined
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    Env,
    add,
    new_cards_allowed,
    other_item,
    quiet,
    row,
    state,
)
from test_web import PORT, TOKEN, get, location, post


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


@pytest.fixture
def app(env, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def listing(shafa_id, state=lst.DECLINED, reason=None, name="Кеди"):
    return Listing(
        shafa_id, name, None, "obuv/kedy", ("38",), f"https://shafa.ua/uk/item/{shafa_id}-x",
        state=state, reason=reason,
    )  # fmt: skip


def published(env, folder="1342", post_id=1342, shafa_id=900):
    """Опублікована Картка (як після роботи робота): має `shafa_id`."""
    item = add(env, folder, post_id)
    with env.conn:
        env.conn.execute(
            "UPDATE items SET status = ?, shafa_id = ?, shafa_url = ?, published_at = ? "
            "WHERE id = ?",
            (Status.PUBLISHED, shafa_id, f"https://shafa.ua/uk/item/{shafa_id}-x", db.now(), item),
        )
    return item


# --- Знімок: вкладка і причина ---


def feed(*nodes, more=False, total=None):
    return {"data": {"viewer": {"products": {
        "edges": [{"node": n} for n in nodes],
        "pageInfo": {"hasNextPage": more, "total": total if total is not None else len(nodes)},
    }}}}  # fmt: skip


def node(shafa_id, **extra):
    return {"id": shafa_id, "name": f"Товар {shafa_id}", "catalogSlug": "obuv/kedy",
            "sizes": [{"name": "38"}], **extra}  # fmt: skip


class Mouse:
    def wheel(self, x, y):
        pass


class Request:
    post_data = json.dumps({"operationName": lst.FEED_OP})


class Response:
    request = Request()

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class CabinetPage:
    """Кабінет: на кожну адресу вкладки — відповідь feed; `fail` — вкладка без відповіді."""

    def __init__(self, tabs, fail=()):
        self.tabs, self.fail = tabs, set(fail)
        self.handlers, self.visited = [], []
        self.mouse = Mouse()

    def on(self, event, handler):
        self.handlers.append(handler)

    def remove_listener(self, event, handler):
        self.handlers.remove(handler)

    def goto(self, url, wait_until="load"):
        tab = next(t for t, u in lst.TABS.items() if u == url)
        self.visited.append(tab)
        if tab not in self.fail:
            for handler in list(self.handlers):
                handler(Response(self.tabs[tab]))


class CabinetSession:
    def __init__(self, page):
        self.page = page


def cabinet(fail=(), **reason):
    return CabinetSession(
        CabinetPage(
            {
                "ACTIVE": feed(node(1), node(2)),
                "MODERATED": feed(node(3)),
                "DECLINED": feed(node(4, **reason), node(5)),
            },
            fail,
        )
    )


def test_load_remembers_tab_of_every_listing_and_snapshot_keeps_it(conn):
    found = lst.load(cabinet(), log=lambda line: None)

    assert {x.id: x.state for x in found} == {
        1: "ACTIVE", 2: "ACTIVE", 3: "MODERATED", 4: "DECLINED", 5: "DECLINED"
    }  # fmt: skip
    lst.store(conn, found)
    assert {x.id: x.state for x in lst.stored(conn, 7)} == {x.id: x.state for x in found}


def test_snapshot_keeps_reason_and_old_snapshot_rows_have_none(conn):
    lst.store(conn, [listing(7, reason="Ціна вище ринкової")])
    with conn:  # рядок знімка, зробленого до ROBOT-011
        conn.execute("UPDATE my_listings SET state = NULL, reason = NULL")
    assert [(x.state, x.reason) for x in lst.stored(conn, 7)] == [(None, None)]
    lst.store(conn, [listing(7, reason="Ціна вище ринкової")])
    assert [(x.state, x.reason) for x in lst.stored(conn, 7)] == [
        ("DECLINED", "Ціна вище ринкової")
    ]


def test_migration_from_old_database_adds_state_and_reason(tmp_path):
    path = tmp_path / "стара.sqlite"
    conn = db.connect(path)
    with conn:
        conn.executescript(
            "ALTER TABLE my_listings DROP COLUMN state; ALTER TABLE my_listings DROP COLUMN reason;"
        )
        conn.executescript(
            "DROP TABLE channel_posts; ALTER TABLE bot_draft_parts DROP COLUMN origin_post; "
            "ALTER TABLE bot_draft_parts DROP COLUMN origin_date;"
        )  # BOT-015 (16)
        conn.execute(
            "INSERT INTO my_listings VALUES (5, 'Кеди', NULL, 's', '[]', 'u', '[]', NULL, NULL, 'x')"
        )
        conn.execute("PRAGMA user_version = 14")
    conn.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 16
    assert tuple(conn.execute("SELECT id, name, state, reason FROM my_listings").fetchone()) == (
        5, "Кеди", None, None
    )  # fmt: skip
    conn.close()


def test_load_declined_reads_only_the_declined_tab():
    session, lines = cabinet(), []

    found = lst.load_declined(session, log=lines.append)

    assert session.page.visited == ["DECLINED"]
    assert [(x.id, x.state) for x in found] == [(4, "DECLINED"), (5, "DECLINED")]
    assert any("Поля товару" in line and "catalogSlug" in line for line in lines)


def test_load_declined_with_unreadable_tab_raises():
    with pytest.raises(ListingsError, match="DECLINED"):
        lst.load_declined(cabinet(fail=["DECLINED"]), log=lambda line: None)


# --- Звірка ---


def test_mark_declined_moves_only_our_published_cards(env):
    ours = published(env, "1342", 1342, shafa_id=900)
    alive = published(env, "1400", 1400, shafa_id=901)
    approved = add(env, "1500", 1500)  # ще не опублікована: shafa_id немає
    with env.conn:
        env.conn.execute("UPDATE items SET shafa_id = 902 WHERE id = ?", (approved,))

    marked = mark_declined(
        env.conn,
        [listing(900, reason="Ціна вище ринкової"), listing(777), listing(902)],  # 777 — чужий
        log=env.lines.append,
    )

    assert marked == [ours]
    r = row(env.conn, ours)
    assert (r["status"], r["problem_code"]) == (Status.PROBLEM, "shafa_declined")
    assert r["problem_text"] == (
        "Шафа відхилила оголошення: Ціна вище ринкової. Виправ на Шафі вручну або прибери в "
        "Архів. https://shafa.ua/uk/item/900-x"
    )
    assert r["shafa_id"] == 900  # посилання й id лишаються: це захист від другої публікації
    assert state(env.conn, alive) == (Status.PUBLISHED, None)
    assert state(env.conn, approved) == (Status.APPROVED, None)
    assert env.lines == ["Шафа відхилила: пост 1342 — Ціна вище ринкової"]


def test_mark_declined_without_reason_says_so(env):
    item = published(env)

    mark_declined(env.conn, [listing(900, reason=None)], log=env.lines.append)

    assert (
        "Шафа відхилила оголошення: причину не вказано. Виправ"
        in row(env.conn, item)["problem_text"]
    )
    assert env.lines == ["Шафа відхилила: пост 1342 — причину не вказано"]


def test_mark_declined_twice_changes_nothing(env):
    item = published(env)
    mark_declined(env.conn, [listing(900, reason="Ціна вище ринкової")], log=env.lines.append)
    with env.conn:
        env.conn.execute("UPDATE items SET updated_at = 'зафіксовано' WHERE id = ?", (item,))
    before = tuple(row(env.conn, item))
    env.lines.clear()

    assert (
        mark_declined(env.conn, [listing(900, reason="Інша причина")], log=env.lines.append) == []
    )

    assert tuple(row(env.conn, item)) == before
    assert env.lines == []


def test_mark_declined_does_not_touch_site(env, monkeypatch):
    """Відхилення Шафою ≠ продано: ні `site_jobs`, ні `post_checks`, ні `mark_sold`."""
    from shafa import site

    monkeypatch.setattr(
        site.Site, "mark_sold", lambda *a, **k: pytest.fail("«Продано» на сайт не шлемо")
    )
    published(env)
    with env.conn:
        env.conn.execute("INSERT INTO post_checks (post_id, old) VALUES (1342, 0)")
    before = [tuple(r) for r in env.conn.execute("SELECT * FROM post_checks")]

    mark_declined(env.conn, [listing(900)], log=env.lines.append)

    assert [tuple(r) for r in env.conn.execute("SELECT * FROM post_checks")] == before
    assert env.conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0


# --- Сесія робота ---


def run(env, load_declined):
    """Як `Env.run`, але з читанням «Відхилених»; у `env.order` — що було до першої Картки."""
    env.order = []
    inner = env.make_driver

    def make_driver(session):
        env.order.append("картки")
        return inner(session)

    with lock.hold("publish", env.tmp / "robot.lock") as held:
        return publish.Run(
            env.conn, env.root, env.session, make_driver, env.load, env.tmp / "екрани", held,
            log=env.lines.append, sleep=lambda s: None, clock=lambda: 0.0,
            rand=lambda a, b: (a + b) / 2, today=lambda: env.today, load_declined=load_declined,
        ).start(30, 30)  # fmt: skip


def test_session_marks_declined_before_first_card_with_fresh_snapshot(env):
    gone = published(env, "1100", 1100, shafa_id=900)
    item = add(env, "1342", 1342)
    lst.store(env.conn, [listing(1, "ACTIVE")])  # свіжий знімок — кабінет не читається повністю
    env.listings = RuntimeError("кабінет не мав читатись")

    def load_declined(session):
        env.order.append("відхилені")
        return [listing(900, reason="Ціна вище ринкової")]

    run(env, load_declined)

    assert env.order == ["відхилені", "картки"]
    assert state(env.conn, gone) == (Status.PROBLEM, "shafa_declined")
    assert state(env.conn, item)[0] == Status.PUBLISHED  # сесія пішла далі
    assert "Шафа відхилила: пост 1100 — Ціна вище ринкової" in env.lines


def test_session_reading_whole_cabinet_uses_it_instead_of_second_read(env):
    gone = published(env, "1100", 1100, shafa_id=900)
    add(env, "1342", 1342)
    env.listings = [listing(900, reason="Ціна вище ринкової"), listing(1, "ACTIVE")]

    run(env, lambda session: pytest.fail("«Відхилені» окремо — лише при свіжому знімку"))

    assert state(env.conn, gone) == (Status.PROBLEM, "shafa_declined")


def test_session_goes_on_when_declined_cannot_be_read(env):
    item = add(env)
    lst.store(env.conn, [listing(1, "ACTIVE")])

    def load_declined(session):
        raise ListingsError("Шафа не віддала список «DECLINED»")

    report_ = run(env, load_declined)

    assert state(env.conn, item)[0] == Status.PUBLISHED
    assert report_.code == "done"
    assert any("Відхилені Шафою не прочитано" in line and "DECLINED" in line for line in env.lines)


def test_session_goes_on_when_browser_fails_on_declined_tab(env):
    item = add(env)
    lst.store(env.conn, [listing(1, "ACTIVE")])

    def load_declined(session):
        raise TimeoutError("Timeout 30000ms exceeded")

    assert run(env, load_declined).code == "done"
    assert state(env.conn, item)[0] == Status.PUBLISHED


def test_session_without_declined_reader_is_unchanged(env):
    item = add(env)
    lst.store(env.conn, [listing(1, "ACTIVE")])

    assert run(env, None).code == "done"
    assert state(env.conn, item)[0] == Status.PUBLISHED


# --- Вдруге не публікуємо ніколи ---


def declined_card(env, folder="1342", post_id=1342, shafa_id=900):
    item = published(env, folder, post_id, shafa_id)
    mark_declined(env.conn, [listing(shafa_id)], log=lambda line: None)
    return item


def test_declined_card_is_not_queued_by_any_button(env, root):
    item = declined_card(env)

    # «Схвалити» і Архів → «Повернути» → «На перевірку» закриті
    assert review.act(env.conn, item, "approve", root) == review.ON_SHAFA
    publish.to_archive(env.conn, item)
    assert state(env.conn, item) == (Status.ARCHIVED, "shafa_declined")
    assert review.act(env.conn, item, "restore") == review.ON_SHAFA
    assert row(env.conn, item)["status"] == Status.ARCHIVED
    # «Перезаповнити»
    assert fill.mark_refill(env.conn, [item]) == 0
    # робот-кнопки, що повертають у Схвалені
    assert not review.robot_retry(env.conn, item)
    assert publish.not_on_shafa(env.conn, item) == "stale"
    assert publish.other_item(env.conn, item) == "stale"
    assert row(env.conn, item)["status"] == Status.ARCHIVED


def test_card_with_shafa_id_cannot_be_approved_from_review(env, root):
    item = add(env)
    with env.conn:
        env.conn.execute(
            "UPDATE items SET status = ?, shafa_id = 900 WHERE id = ?", (Status.REVIEW, item)
        )

    assert review.act(env.conn, item, "approve", root) == review.ON_SHAFA
    assert row(env.conn, item)["status"] == Status.REVIEW


def test_refill_ignores_card_with_shafa_id(env):
    item = add(env)
    with env.conn:
        env.conn.execute(
            "UPDATE items SET status = ?, shafa_id = 900 WHERE id = ?", (Status.REVIEW, item)
        )
    assert fill.mark_refill(env.conn, [item]) == 0
    assert row(env.conn, item)["status"] == Status.REVIEW


def test_publisher_never_takes_approved_card_that_has_shafa_id(env):
    item = add(env)
    with env.conn:  # навіть якщо якийсь шлях усе ж поставив її у Схвалені
        env.conn.execute("UPDATE items SET shafa_id = 900 WHERE id = ?", (item,))

    assert publish.queue(env.conn) == []
    report_ = env.run()

    assert report_.code == "done" and report_.published == []
    assert state(env.conn, item) == (Status.APPROVED, None)
    assert env.driver is None  # форма не відкривалась, жодного кліку


def test_other_folder_of_same_post_is_not_published_after_decline(env):
    declined_card(env, "1342", 1342)
    with env.conn:  # друга папка того самого поста: Картка зі статусом «Схвалені»
        twin = env.conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES ('1342-копія', 1342, 'approved', 'x', 'x')"
        ).lastrowid

    assert publish.already_published(env.conn, twin, 1342) == "1342"
    env.run()

    assert state(env.conn, twin) == (Status.PROBLEM, "already_published")


def test_rescan_does_not_touch_declined_problem(env, root):
    item = declined_card(env)
    assert scan.touchable(Status.PROBLEM, "shafa_declined") is False
    assert "shafa_declined" in scan.STICKY
    assert state(env.conn, item) == (Status.PROBLEM, "shafa_declined")


# --- Екран і звіт ---


def test_card_page_offers_only_open_on_shafa_and_archive(env, app):
    item = declined_card(env, shafa_id=900)
    with env.conn:
        env.conn.execute(
            "UPDATE items SET problem_text = 'Шафа відхилила оголошення: Ціна вище ринкової.' "
            "WHERE id = ?",
            (item,),
        )

    page = get(app, f"/item/{item}").body.decode("utf-8")

    assert "Шафа відхилила оголошення" in page
    assert "Ціна вище ринкової" in page
    assert "Відкрити на Шафі" in page and "https://shafa.ua/uk/item/900-x" in page
    assert 'value="to_archive"' in page
    for forbidden in ("recheck", "approve", "not_on_shafa", "other_item", "on_shafa", "refill"):
        assert f'value="{forbidden}"' not in page


def test_archive_button_works_and_restore_is_hidden_and_refused(env, app):
    item = declined_card(env)

    assert location(post(app, f"/item/{item}", action="to_archive")).endswith(
        "/problem?msg=duplicate_archived"
    )
    assert state(env.conn, item)[0] == Status.ARCHIVED
    page = get(app, f"/item/{item}").body.decode("utf-8")
    assert 'value="restore"' not in page
    assert location(post(app, f"/item/{item}", action="restore")) == (
        f"/item/{item}?msg=on_shafa_block"
    )
    assert state(env.conn, item)[0] == Status.ARCHIVED
    assert "вдруге не можна" in get(app, f"/item/{item}?msg=on_shafa_block").body.decode("utf-8")


def test_recheck_button_refused_for_declined(env, app):
    item = declined_card(env)

    assert location(post(app, f"/item/{item}", action="recheck")) == f"/item/{item}?msg=sticky"
    assert state(env.conn, item) == (Status.PROBLEM, "shafa_declined")


def test_problem_hint_and_report_have_human_text(env):
    item = declined_card(env)
    assert "вдруге не публікує" in web.PROBLEM_HINTS["shafa_declined"]
    assert "shafa_declined" in web.STICKY_TITLES
    with env.conn:
        env.conn.execute(
            "UPDATE items SET updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(timespec="seconds"), item),
        )

    text = report.build(env.conn, env.tmp, datetime.now().astimezone().date())

    assert "Шафа відхилила" in text
    assert "shafa_declined" not in text


def test_fresh_database_has_state_and_reason(tmp_path):
    conn = db.connect(tmp_path / "нова.sqlite")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(my_listings)")}
    assert {"state", "reason"} <= columns
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO my_listings (id) VALUES (1)")  # обов'язкові поля лишились
    conn.close()
