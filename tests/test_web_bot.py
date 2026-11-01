"""Екран: стан бота на панелі й «Запустити бота» (BOT-001)."""

import pytest

from shafa import web
from shafa.robot import lock
from test_web import PORT, TOKEN, get, html, location, plain, post


@pytest.fixture
def started():
    return []


@pytest.fixture
def bot_lock(tmp_path):
    return tmp_path / "bot.lock"


@pytest.fixture
def app(conn, tmp_path, root, started, bot_lock):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        bot_spawner=started.append, bot_lock=bot_lock,
    )  # fmt: skip


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:секрет")


def test_no_token(app, started):
    page = html(get(app, "/review"))
    assert "Бот: не налаштований — скажіть Власнику" in plain(page)
    assert "Запустити бота" not in page
    assert location(post(app, "/bot", action="start", back="/review")) == (
        "/review?msg=bot_no_token"
    )
    assert started == []


def test_stopped_bot_can_be_started(app, started, with_token, tmp_path, root):
    page = html(get(app, "/review"))
    assert "Бот: вимкнений" in plain(page) and ">Запустити бота</button>" in page
    assert "123:секрет" not in page

    response = post(app, "/bot", action="start", back="/approved")

    assert location(response) == "/approved?msg=bot_started"
    assert started == [["--db", str(tmp_path / "база.sqlite"), "--root", str(root)]]


def test_running_bot(app, started, with_token, bot_lock):
    with lock.hold("bot", bot_lock):
        page = html(get(app, "/review"))
        assert "Бот: працює" in plain(page) and "Запустити бота" not in page
        assert location(post(app, "/bot", action="start")) == "/review?msg=bot_running"
    assert started == []


def test_failed_bot_shows_reason(conn, app, with_token):
    with conn:
        conn.execute("INSERT INTO bot_status VALUES (1, 'x', 'x', 'Бот не адміністратор каналу')")
    page = html(get(app, "/review"))
    assert "Бот: зупинився через помилку — якщо повториться, скажіть Власнику" in plain(page)
    assert "Для Власника: Бот не адміністратор каналу" in page
    assert "Запустити бота" in page


def test_bot_post_needs_token_and_known_action(app, started, with_token):
    assert post(app, "/bot", token=None, action="start").status == 403
    assert post(app, "/bot", action="stop").status == 400
    assert started == []
