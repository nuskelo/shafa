"""Екран: панель робота, Опубліковані, Сесії, кнопки для проблем публікації (UI-002)."""

import pytest

from shafa import web
from shafa.db import Status, now
from shafa.robot import browser, lock
from test_review import add_card
from test_web import PORT, TOKEN, get, html, location, post


@pytest.fixture(autouse=True)
def new_cards_allowed(monkeypatch):
    """Тут перевіряється робот як такий: межа «нових на день» (CORE-005) не заважає."""
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


class Spawner:
    """Підставний запуск процесу: запам'ятовує команди, нічого не запускає."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return Process()


class Process:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


@pytest.fixture
def spawner():
    return Spawner()


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def app(conn, tmp_path, root, spawner, lock_file):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=lock_file,
    )  # fmt: skip


def card(conn, root, status=Status.APPROVED, folder="1342", post_id=1342, **fields):
    item = add_card(conn, root, folder=folder, post_id=post_id, status=status)
    if fields:
        with conn:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE items SET {sets} WHERE id = ?", (*fields.values(), item))
    return item


def status_of(conn, item):
    return conn.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()[0]


# --- Панель ---


def test_panel_on_every_page_and_start_disabled_without_approved(app):
    page = html(get(app, "/review"))
    assert "ПІДСТАВНА" not in page
    assert "Робот зупинений" in page
    assert 'value="start" class="approve" disabled' in page
    assert "Немає Схвалених Карток" in page
    assert "Сьогодні: старих 0 з 0 · нових 0 з 30" in page


def test_start_spawns_robot_process(conn, root, app, spawner):
    card(conn, root)
    assert 'value="start" class="approve">' in html(get(app, "/approved"))

    response = post(app, "/robot", action="start", back="/approved")

    assert location(response) == "/approved?msg=robot_started"
    assert spawner.calls == [
        ["--publish", "--db", str(app.db_path), "--root", str(app.root)]
    ]  # fmt: skip
    assert "Робот запускається" in html(get(app, "/approved"))


def test_start_refused_without_approved_or_when_running(conn, root, app, spawner, lock_file):
    assert location(post(app, "/robot", action="start", back="/review")).endswith(
        "msg=robot_cannot_start"
    )
    card(conn, root)
    with lock.hold("publish", lock_file):  # робот уже працює (інший процес)
        assert location(post(app, "/robot", action="start", back="/review")).endswith(
            "msg=robot_cannot_start"
        )
        assert "Робот публікує" in html(get(app, "/review"))
    assert spawner.calls == []


def test_start_in_fake_mode_passes_flag(conn, root, tmp_path, spawner, lock_file):
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "t", PORT, token=TOKEN,
                  spawner=spawner, lock_file=lock_file, fake_shafa=True)  # fmt: skip
    card(conn, root)
    assert "ПІДСТАВНА ШАФА" in html(get(app, "/review"))
    post(app, "/robot", action="start", back="/review")
    assert spawner.calls[0][-1] == "--fake-shafa"
    assert location(post(app, "/robot", action="login", back="/review")).endswith(
        "msg=robot_fake_login"
    )
    assert len(spawner.calls) == 1  # у підставному режимі справжній вхід не відкривається


@pytest.mark.parametrize(
    ("action", "stored", "message"),
    [("pause", "pause", "robot_paused"), ("resume", None, "robot_resumed"),
     ("stop", "stop", "robot_stopping")],
)  # fmt: skip
def test_pause_resume_stop(conn, app, action, stored, message):
    assert location(post(app, "/robot", action=action, back="/review")).endswith(
        "msg=robot_not_running"
    )
    with conn:
        conn.execute("INSERT INTO sessions (started_at, command) VALUES ('x', 'pause')")
    assert location(post(app, "/robot", action=action, back="/review")).endswith(f"msg={message}")
    assert conn.execute("SELECT command FROM sessions").fetchone()[0] == stored


def test_running_panel_buttons(conn, app, lock_file):
    with conn:
        conn.execute("INSERT INTO sessions (started_at, item_started_at) VALUES ('x', ?)",
                     ("2026-10-13T11:59:00+00:00",))  # fmt: skip
    with lock.hold("publish", lock_file):
        page = html(get(app, "/robot/panel?back=/review"))
    assert 'value="pause"' in page and 'value="stop"' in page and 'value="start"' not in page


def test_login_button_after_logout(conn, root, app, spawner):
    card(conn, root)
    with conn:
        conn.execute(
            "INSERT INTO sessions (started_at, ended_at, stop_code) VALUES ('x', 'y', 'login')"
        )
    page = html(get(app, "/review"))
    assert "Увійти на Шафу" in page and "увійдіть, як завжди" in page
    assert location(post(app, "/robot", action="login", back="/review")).endswith("msg=robot_login")
    assert spawner.calls == [["--login"]]


def test_login_done_button_closes_login_window(conn, app, lock_file):
    flag = browser.login_done_flag()
    with lock.hold("login", lock_file):
        assert "Я увійшов — закрити вікно" in html(get(app, "/robot/panel?back=/review"))
        assert location(post(app, "/robot", action="login_done", back="/review")).endswith(
            "msg=robot_login_done"
        )
    assert flag.exists()  # процес входу побачить і закриє Chrome штатно


def test_login_done_without_login_window(conn, app, lock_file):
    for mode in (None, "publish"):
        if mode:
            with lock.hold(mode, lock_file):
                answer = post(app, "/robot", action="login_done", back="/review")
        else:
            answer = post(app, "/robot", action="login_done", back="/review")
        assert location(answer).endswith("msg=robot_no_login_window")
    assert not browser.login_done_flag().exists()


def test_login_refused_while_robot_busy(conn, app, spawner, lock_file):
    with lock.hold("publish", lock_file):
        assert location(post(app, "/robot", action="login", back="/review")).endswith(
            "msg=robot_busy"
        )
    assert spawner.calls == []


def test_failed_start_shows_last_log_line(conn, root, app, spawner, monkeypatch, tmp_path):
    card(conn, root)
    log = tmp_path / "дані.log"
    log.write_text("…\nРобот уже працює — другий запуск нічого не чіпає\n", encoding="utf-8")
    monkeypatch.setattr(web.control, "log_path", lambda: log)
    app.process = Process(code=2)
    page = html(get(app, "/review"))
    assert "Робот не запустився: Робот уже працює" in page


@pytest.mark.parametrize("back", ["//evil.com/x", "https://evil.com", "", "\\\\evil"])
def test_back_only_own_path(app, back):
    assert location(post(app, "/robot", action="stop", back=back)).startswith("/review?")


def test_panel_post_needs_token(app, spawner):
    response = post(app, "/robot", token="чужий", action="start", back="/review")
    assert response.status == 403 and spawner.calls == []


def test_csp_allows_only_own_server_for_polling(app):
    policy = get(app, "/review").headers["Content-Security-Policy"]
    assert "connect-src 'self'" in policy
    assert "default-src 'none'" in policy and "script-src 'self'" in policy
    assert "unsafe" not in policy and "*" not in policy


# --- Вкладки ---


def test_published_tab_with_link(conn, root, app):
    item = card(conn, root, Status.PUBLISHED, shafa_url="https://shafa.ua/uk/item/555-shafa",
                published_at="2026-10-13T10:00:00+00:00")  # fmt: skip
    page = html(get(app, "/published"))
    assert "Відкрити на Шафі" in page and 'href="https://shafa.ua/uk/item/555-shafa"' in page
    assert f'/item/{item}"' in page and "опубліковано 13.10.2026" in page
    assert 'Опубліковані <span class="count">1</span>' in page


def test_unsafe_link_never_rendered(conn, root, app):
    card(conn, root, Status.PUBLISHED, shafa_url="javascript:alert(1)")
    assert "javascript:" not in html(get(app, "/published"))


def test_sessions_tab_and_session_cards(conn, root, app):
    item = card(conn, root, Status.PUBLISHED)
    with conn:
        conn.execute(
            "INSERT INTO sessions (started_at, ended_at, published, problems, stop_code) "
            "VALUES ('2026-10-13T09:00:00+00:00', '2026-10-13T09:30:00+00:00', 1, 0, 'limit')"
        )
        conn.execute("UPDATE items SET session_id = 1 WHERE id = ?", (item,))
    page = html(get(app, "/sessions"))
    assert "Ліміт на сьогодні вичерпано" in page and "/session/1" in page
    assert f"/item/{item}" in html(get(app, "/session/1"))
    assert get(app, "/session/99").status == 404


# --- Кнопки для проблем публікації ---


def problem(conn, root, code, **fields):
    return card(conn, root, Status.PROBLEM, problem_code=code, problem_text="т", **fields)


def test_unsure_problem_buttons_and_on_shafa(conn, root, app):
    item = problem(conn, root, "publish_uncertain")
    page = html(get(app, f"/item/{item}"))
    assert "Не впевнений, чи товар з&#39;явився на Шафі" in page
    assert "publish_uncertain" not in page.replace('value="publish_uncertain"', "")
    assert 'value="on_shafa"' in page and 'value="not_on_shafa"' in page
    assert 'value="recheck"' not in page

    bad = post(app, f"/item/{item}", action="on_shafa", shafa_link="https://evil.com/1-x")
    assert location(bad) == f"/item/{item}?msg=bad_link"
    assert "Це не посилання на Шафу" in html(get(app, location(bad)))
    ok = post(app, f"/item/{item}", action="on_shafa", shafa_link="221913242")
    assert location(ok) == "/problem?msg=on_shafa"
    assert status_of(conn, item) == Status.PUBLISHED


def test_not_on_shafa_button(conn, root, app):
    item = problem(conn, root, "publish_error")
    assert (
        location(post(app, f"/item/{item}", action="not_on_shafa")) == "/problem?msg=not_on_shafa"
    )
    assert status_of(conn, item) == Status.APPROVED


def test_duplicate_buttons_and_link(conn, root, app):
    item = problem(conn, root, "possible_duplicate",
                   duplicate_url="https://shafa.ua/uk/women/x/206042583-krosivki")  # fmt: skip
    page = html(get(app, f"/item/{item}"))
    assert "Схожий товар уже є на Шафі" in page
    assert 'href="https://shafa.ua/uk/women/x/206042583-krosivki"' in page
    assert 'value="other_item"' in page and 'value="to_archive"' in page
    assert location(post(app, f"/item/{item}", action="other_item")) == "/problem?msg=other_item"
    assert status_of(conn, item) == Status.APPROVED
    assert conn.execute("SELECT duplicate_ok_at FROM items").fetchone()[0]


def test_already_published_only_archive(conn, root, app):
    item = problem(conn, root, "already_published")
    page = html(get(app, f"/item/{item}"))
    assert "Цей пост уже опубліковано з іншої папки" in page
    assert 'value="to_archive"' in page and 'value="other_item"' not in page
    assert location(post(app, f"/item/{item}", action="other_item")) == f"/item/{item}?msg=stale"
    assert location(post(app, f"/item/{item}", action="to_archive")) == (
        "/problem?msg=duplicate_archived"
    )
    assert status_of(conn, item) == Status.ARCHIVED


def test_robot_problem_keeps_recheck(conn, root, app):
    item = problem(conn, root, "robot_field_missing")
    page = html(get(app, f"/item/{item}"))
    assert 'value="recheck"' in page and 'value="on_shafa"' not in page


def test_duplicate_shows_name_and_photo_of_similar(conn, root, app, tmp_path):
    """CORE-007: у картці Проблеми — фото й назва схожого оголошення, не лише посилання."""
    item = problem(conn, root, "possible_duplicate", duplicate_name="Тапочки UGG 40 розміру",
                   duplicate_url="https://shafa.ua/uk/women/x/5-x")  # fmt: skip
    page = html(get(app, f"/item/{item}"))
    assert "Тапочки UGG 40 розміру" in page
    assert f'src="/duplicate/{item}.jpg"' not in page  # фото ще немає
    assert get(app, f"/duplicate/{item}.jpg").status == 404
    photos = tmp_path / "duplicates"
    photos.mkdir()
    (photos / f"{item}.jpg").write_bytes(b"jpeg")
    page = html(get(app, f"/item/{item}"))
    assert f'src="/duplicate/{item}.jpg"' in page
    response = get(app, f"/duplicate/{item}.jpg")
    assert (response.status, response.body, response.content_type) == (200, b"jpeg", "image/jpeg")
    assert get(app, "/duplicate/..%2F..%2Fetc.jpg").status == 404


def test_reread_listings_button(conn, app):
    from shafa.robot import listings

    listings.store(conn, [listings.Listing(1, "Кеди", None, "s", ("38",), "https://shafa.ua/1")])
    page = html(get(app, "/settings"))
    assert "Її товарів у знімку: 1, прочитано" in page
    assert "Перечитувати «Мої речі» кожні N днів" in page
    response = post(app, "/listings/reread")
    assert "робот перечитає «Мої речі»" in html(response)
    assert listings.stored(conn, 7) is None
    assert "перечитається на наступному запуску" in html(get(app, "/settings"))


def test_deleted_post_reason_shown_in_archive(conn, root, app):
    """SITE-002: робот поклав Картку в Архів — пост видалено з каналу; причина видна в списку."""
    item = card(conn, root)
    with conn:
        conn.execute(
            "UPDATE items SET status = 'archived', archived_at = ?, "
            "problem_code = 'post_deleted', problem_text = 'Пост видалено з каналу — продано' "
            "WHERE id = ?",
            (now(), item),  # свіжа дата: через 7 днів Архів прибирає запис
        )
    assert "Пост видалено з каналу — продано" in html(get(app, "/archived"))


def test_site_line_on_panel_only_when_site_on(conn, root, app, monkeypatch):
    """SITE-002: «Сайт: чекає N · вийде сьогодні M · продано за тиждень K»."""
    assert "вийде сьогодні" not in html(get(app, "/robot/panel"))
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")
    with conn:
        conn.execute(
            "INSERT INTO site_jobs (post_id, folder_name, posted_at, text, state, not_before, "
            "created_at, updated_at) VALUES (1, '1', 'p', 't', 'waiting', '2999-01-01', 'c', 'u')"
        )
        conn.execute("INSERT INTO post_checks (post_id, gone_at) VALUES (5, '2999-01-01')")

    page = html(get(app, "/robot/panel"))

    assert "чекає 1 · вийде сьогодні 0 · продано за тиждень 1" in page
    assert "чекає 1" in html(get(app, "/review"))  # і на сторінках


def test_held_sold_on_panel_and_release_button(conn, root, app, monkeypatch):
    """SITE-003: «відкладено (запобіжник) M» на панелі; кнопка в «Налаштуваннях» знімає запобіжник."""
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")
    with conn:
        conn.execute(
            "INSERT INTO post_checks (post_id, gone_at, hold) VALUES (5, '2999-01-01', 'fuse'), "
            "(6, '2999-01-01', 'fuse'), (7, '2999-01-01', NULL)"
        )

    assert "продано за тиждень 1 · відкладено (запобіжник) 2" in html(get(app, "/robot/panel"))
    assert "Відкладено запобіжником: 2" in html(get(app, "/settings"))

    page = html(post(app, "/site/release-sold"))

    assert "«Продано» для 2 пар надішле бот" in page
    assert (
        conn.execute("SELECT COUNT(*) FROM post_checks WHERE hold IS NOT NULL").fetchone()[0] == 0
    )
    assert "відкладено (запобіжник) 0" in html(get(app, "/robot/panel"))
    assert "Відкладених «Продано» немає" in html(post(app, "/site/release-sold"))
