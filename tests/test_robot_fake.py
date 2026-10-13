"""Підставна Шафа (UI-002, D5): у цьому режимі браузер не пускає жодного запиту назовні."""

import json
import urllib.request

import pytest

from shafa.__main__ import main
from shafa.db import Status
from shafa.robot import fake
from test_robot_form import FakeLocator, FakeRequest, FakeRoute

BASE = "http://127.0.0.1:50123"


def bare(base=BASE):
    session = fake.FakeSession.__new__(fake.FakeSession)
    session._setup(allow_upload=True, publish=True)
    session.base = base
    return session


def route(session, method, url, body=b"{}"):
    r = FakeRoute()
    session._route(r, FakeRequest(method, url, body))
    return r.result


@pytest.mark.parametrize(
    "url",
    [
        "https://shafa.ua/uk/new",
        "https://shafa.ua/api/v3/graphiql",
        "https://shafa.ua/static/logo.png",
        "https://www.google-analytics.com/g/collect",
        "http://127.0.0.1:8765/approved",  # навіть екран перевірки — не підставна Шафа
        "http://127.0.0.1:501234/new",  # схожий порт
        "http://127.0.0.1:50123.evil.com/new",
        "http://localhost:50123/new",
    ],
)
def test_everything_outside_fake_shafa_is_blocked(url):
    session = bare()
    for method in ("GET", "POST"):
        assert route(session, method, url) == "abort"
    assert session.outside() == [url.split("?")[0]] * 2


def test_fake_shafa_pages_allowed():
    session = bare()
    assert route(session, "GET", BASE + "/new") == "continue"
    assert route(session, "GET", BASE) == "continue"
    assert session.outside() == []


def test_create_on_fake_shafa_needs_the_same_ticket():
    session = bare()
    assert route(session, "POST", BASE + fake.CREATE_PATH) == "abort"  # без квитка
    session.arm_publish(7, Status.PUBLISHING)
    session.click_publish(FakeLocator("Додати річ", "submit"))
    assert route(session, "POST", BASE + fake.CREATE_PATH) == "continue"
    assert route(session, "POST", BASE + fake.CREATE_PATH) == "abort"  # другий — ні
    assert session.creates == 1


def test_fake_shafa_server_answers_with_product_id():
    server = fake.Server()
    try:
        assert server.base.startswith("http://127.0.0.1:")
        page = urllib.request.urlopen(server.base + "/new", timeout=10).read().decode("utf-8")
        assert "Підставна Шафа" in page and "Додати річ" in page
        request = urllib.request.Request(server.base + fake.CREATE_PATH, data=b"{}", method="POST")
        body = json.loads(urllib.request.urlopen(request, timeout=10).read())
        assert body["data"]["createProduct"]["createdProduct"]["id"] > 900000000
    finally:
        server.close()


def test_fake_flag_only_with_publish(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["robot", "--login", "--fake-shafa"])
    assert "--fake-shafa лише разом із --publish" in capsys.readouterr().err


def test_fake_flag_hidden_from_help(capsys):
    with pytest.raises(SystemExit):
        main(["robot", "--help"])
    assert "fake" not in capsys.readouterr().out
