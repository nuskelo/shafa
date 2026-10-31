import http.client
import re
import threading
from urllib.parse import urlencode, urlsplit

from shafa import app, config, db
from shafa.robot import lock


def test_first_start_creates_db_opens_browser_and_cleans_up():
    opened, seen = [], {}

    def serve(server):
        seen["url_file"] = (config.data_dir() / app.URL_NAME).read_text(encoding="utf-8")
        seen["busy"] = lock.busy(config.data_dir() / app.LOCK_NAME)

    assert app.start(opened.append, port=0, serve=serve) == 0

    assert app.db_path().is_file()
    assert opened == [seen["url_file"]]
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", opened[0])
    assert seen["busy"]["mode"] == "ui"
    assert not (config.data_dir() / app.URL_NAME).exists()
    assert lock.busy(config.data_dir() / app.LOCK_NAME) is None


def test_second_click_opens_same_screen_only():
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    (data / app.URL_NAME).write_text("http://127.0.0.1:8799/", encoding="utf-8")
    opened, served = [], []

    with lock.hold("ui", data / app.LOCK_NAME):
        assert app.start(opened.append, port=0, serve=served.append) == 0

    assert opened == ["http://127.0.0.1:8799/"] and served == []
    assert not app.db_path().exists()


def _get(url, path, headers=None):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("GET", path, headers={"Host": parts.netloc, **(headers or {})})
    response = conn.getresponse()
    return response.status, response.read().decode("utf-8")


def _post(url, path, form):
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    body = urlencode(form)
    conn.request(
        "POST",
        path,
        body,
        {"Host": parts.netloc, "Content-Type": "application/x-www-form-urlencoded"},
    )
    response = conn.getresponse()
    return response.status, response.read().decode("utf-8")


def test_quit_button_stops_screen_and_bot():
    opened, killed = [], []
    thread = threading.Thread(
        target=app.start,
        kwargs={
            "open_browser": opened.append,
            "port": 0,
            "kill": lambda pid, sig: killed.append(pid),
        },
        daemon=True,
    )
    with lock.hold("bot", config.data_dir() / "bot.lock"):  # «бот» — цей самий процес
        thread.start()
        for _ in range(100):
            if opened:
                break
            threading.Event().wait(0.05)
        url = opened[0]
        status, page = _get(url, "/review")
        assert status == 200 and ">Вийти</button>" in page
        token = re.search(r'name="token" value="([^"]+)"', page)[1]

        status, page = _post(url, "/quit", {"token": token, "sure": "1"})

    assert status == 200 and "Програму закрито" in page and "Бота зупинено." in page
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(killed) == 1
    assert lock.busy(config.data_dir() / app.LOCK_NAME) is None


def test_quit_asks_robot_to_stop_after_current_item(tmp_path):
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    conn = db.connect(app.db_path())
    with conn:
        conn.execute("INSERT INTO sessions (started_at) VALUES ('x')")
    with lock.hold("publish"):
        done = app.quit_all(kill=lambda pid, sig: None)
    assert done == ["робот допише поточний товар і зупиниться"]
    assert conn.execute("SELECT command FROM sessions").fetchone()[0] == "stop"
    conn.close()


def test_quit_without_anything_running():
    assert app.quit_all(kill=lambda pid, sig: None) == []


def test_no_quit_button_in_plain_ui(conn, tmp_path, root):
    from shafa import web
    from test_web import TOKEN, get, html

    screen = web.App(tmp_path / "база.sqlite", root, tmp_path / "t", 8765, token=TOKEN)
    assert "Вийти" not in html(get(screen, "/review"))
    assert screen.handle(web.Request("POST", "/quit", {}, {"token": [TOKEN]},
                                     {"host": "127.0.0.1:8765"})).status == 404  # fmt: skip


def test_quit_command_kills_screen_and_cleans_stale_url():
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    url = data / app.URL_NAME
    url.write_text("http://127.0.0.1:8799/", encoding="utf-8")
    killed = []
    held = lock.acquire("ui", data / app.LOCK_NAME)

    def kill(pid, sig):  # «процес помер» — ОС звільняє його замок
        killed.append(pid)
        held.release()

    assert app.quit_app(kill=kill, wait=1) is True
    assert len(killed) == 1 and not url.exists()


def test_quit_command_when_not_running():
    assert app.quit_app(kill=lambda pid, sig: None, wait=0.1) is False
