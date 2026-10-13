"""Виняток захисту для публікації (ROBOT-002): одна кнопка, один запит, лише Картка `publishing`."""

import json

import pytest

from shafa.db import Status
from shafa.robot import browser, guard
from shafa.robot.guard import CREATE_URL, publish_click_allowed, request_verdict
from test_robot_form import FakeLocator, FakeRequest, FakeRoute, bare_session

# Як шле сторінка: префікс WEB_, мутація з фрагментом помилок, окремий JSON (не пакет).
CREATE_QUERY = (
    "mutation WEB_CreateProduct($nameUk: String, $catalog: String!) {\n"
    "  createProduct(nameUk: $nameUk, catalog: $catalog) {\n"
    "    createdProduct { id }\n    firstProductsBonusProgress { current target }\n"
    "    errors { ...errorsData }\n  }\n}\n"
    "fragment errorsData on GraphResponseError { field messages { code message } }"
)


def create_body(op="WEB_CreateProduct", query=CREATE_QUERY):
    return json.dumps({"operationName": op, "query": query, "variables": {}}).encode()


def verdict(body, url=CREATE_URL, allow_create=True):
    return request_verdict("POST", url, "application/json", body, True, allow_create=allow_create)


def test_create_allowed_only_with_ticket():
    assert verdict(create_body()) == (True, guard.PUBLISHED)
    allowed, reason = verdict(create_body(), allow_create=False)
    assert not allowed and "WEB_CreateProduct" in reason


@pytest.mark.parametrize(
    "body",
    [
        create_body(op="WEB_EditProduct"),
        create_body(query=CREATE_QUERY.replace("createProduct(", "editProduct(")),
        create_body(query=CREATE_QUERY + "\nmutation B { deleteProduct(id: 1) { ok } }"),
        create_body(query=CREATE_QUERY.replace("  createProduct", "  deleteProduct(id: 1) { ok }\n  createProduct")),
        json.dumps([json.loads(create_body())]).encode(),  # у пакеті — ні
    ],
)  # fmt: skip
def test_ticket_lets_through_only_exact_create(body):
    assert not verdict(body)[0]


@pytest.mark.parametrize(
    "url", ["https://shafa.ua/api/v5/graphql", "https://shafa.ua/api/v3/graphiql-batch",
            "https://evil.example/api/v3/graphiql"],
)  # fmt: skip
def test_create_only_to_its_address(url):
    assert not verdict(create_body(), url=url)[0]


@pytest.mark.parametrize(
    ("text", "type_attr", "ok"),
    [
        ("Додати річ  ", "submit", True),
        ("Додати річ", "button", False),
        ("Зберегти зміни", "submit", False),
        ("Додати річ і підняти в топ", "submit", False),
        ("Додається...", "submit", False),
    ],
)
def test_publish_button_exact(text, type_attr, ok):
    assert publish_click_allowed(text, type_attr) is ok


def test_ordinary_click_still_refuses_publish_button():
    session = bare_session(publish=True)
    session.arm_publish(1, Status.PUBLISHING)
    with pytest.raises(guard.Forbidden):
        session.click(FakeLocator("Додати річ", "submit"))


def test_publish_click_needs_publish_mode():
    with pytest.raises(guard.Forbidden, match="--publish"):
        bare_session(allow_upload=True).arm_publish(1, Status.PUBLISHING)


@pytest.mark.parametrize("status", [Status.APPROVED, Status.PUBLISHED, Status.PROBLEM])
def test_publish_only_for_card_in_publishing(status):
    with pytest.raises(guard.Forbidden, match="publishing"):
        bare_session(publish=True).arm_publish(1, status)


def test_one_click_and_one_request_per_card():
    session = bare_session(publish=True)
    session.arm_publish(7, Status.PUBLISHING)
    button = FakeLocator("Додати річ", "submit")
    session.click_publish(button)
    assert button.clicked
    with pytest.raises(guard.Forbidden, match="без квитка"):  # другий клік
        session.click_publish(FakeLocator("Додати річ", "submit"))

    first, second = FakeRoute(), FakeRoute()
    session._route(first, FakeRequest("POST", CREATE_URL, create_body()))
    session._route(second, FakeRequest("POST", CREATE_URL, create_body()))  # другий запит
    assert (first.result, second.result) == ("continue", "abort")
    assert session.creates == 1
    assert session.publish_attempts() == ["мутація WEB_CreateProduct"]

    with pytest.raises(guard.Forbidden, match="Повторна"):  # той самий товар — ніколи
        session.arm_publish(7, Status.PUBLISHING)


def test_disarm_blocks_late_request_and_click():
    session = bare_session(publish=True)
    session.arm_publish(7, Status.PUBLISHING)
    session.disarm()
    route = FakeRoute()
    session._route(route, FakeRequest("POST", CREATE_URL, create_body()))
    assert route.result == "abort" and session.creates == 0
    with pytest.raises(guard.Forbidden):
        session.click_publish(FakeLocator("Додати річ", "submit"))


def test_click_publish_refuses_other_button_and_keeps_ticket_unused():
    session = bare_session(publish=True)
    session.arm_publish(7, Status.PUBLISHING)
    other = FakeLocator("Зберегти зміни", "submit")
    with pytest.raises(guard.Forbidden):
        session.click_publish(other)
    assert not other.clicked


def test_prefixed_create_counts_as_publish_attempt():
    """Сторінка шле `WEB_CreateProduct` — звіт про спроби має її бачити (у ROBOT-001 не бачив)."""
    session = bare_session(allow_upload=True)
    session._route(FakeRoute(), FakeRequest("POST", CREATE_URL, create_body()))
    assert session.publish_attempts() == ["мутація WEB_CreateProduct"]
    assert browser.PUBLISH.search("мутація WEB_EditProduct")
