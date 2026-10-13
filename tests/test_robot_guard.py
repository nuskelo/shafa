import json

import pytest

from shafa.robot.guard import (
    UPLOAD_URL,
    click_allowed,
    has_mutation,
    key_allowed,
    request_verdict,
    root_fields,
)

GRAPHQL = "https://shafa.ua/api/v5/graphql"
UPLOAD_QUERY = (
    "mutation UploadPhoto($file: String!) {\n  uploadPhoto(uploadFile: $file) {\n"
    "    idStr\n    thumbnailUrl\n    originalUrl\n    errors { ...errorsData }\n  }\n}\n"
    "fragment errorsData on ErrorType { field messages }"
)
CREATE_QUERY = "mutation CreateProduct($nameUk: String) { createProduct(nameUk: $nameUk) { id } }"


def gql(*items):
    payload = [{"operationName": op, "query": q, "variables": {}} for op, q in items]
    return json.dumps(payload[0] if len(payload) == 1 else payload).encode("utf-8")


def multipart(fields, file=b"\xff\xd8jpeg", boundary="----WebKitFormBoundaryAbc123"):
    body = b""
    for name, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="IMG_1.JPG"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + file
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return f"multipart/form-data; boundary={boundary}", body


def upload_fields(**changes):
    return {
        "operationName": "UploadPhoto",
        "query": UPLOAD_QUERY,
        "variables": '{"file":"file"}',
        **changes,
    }


# --- Шар 1: клік ---


@pytest.mark.parametrize(
    "text",
    ["Додати річ", "ДОДАТИ  РІЧ", "Опублікувати", "Зберегти зміни",
     "Додати бренд: Skech\n\nБренд стане видимим після модерації", "Видалити", "Подарунок",
     "Підняти в топ", "Вийти"],
)  # fmt: skip
def test_forbidden_clicks(text):
    assert not click_allowed(text)


@pytest.mark.parametrize("text", ["Жіночий одяг", "Кросівки", "Ідеальний", "38.5", "Чорний", ""])
def test_ordinary_clicks(text):
    assert click_allowed(text, "button")


def test_submit_never_clicked_whatever_the_text():
    assert not click_allowed("Кросівки", "submit")
    assert not click_allowed("", "SUBMIT")


# --- Шар 3: клавіатура ---


def test_enter_only_in_keywords():
    assert not key_allowed("Enter", in_keywords=False)
    assert key_allowed("Enter", in_keywords=True)
    assert key_allowed("Tab", in_keywords=False)


# --- Шар 2: мережа ---


def test_reading_allowed():
    assert request_verdict("GET", "https://shafa.ua/uk/new", "", None, False)[0]
    body = gql(("WEB_ProductFormCatalog", "query WEB_ProductFormCatalog { catalog { id } }"))
    assert request_verdict("POST", GRAPHQL, "application/json", body, False) == (
        True,
        "читання GraphQL",
    )


@pytest.mark.parametrize("allow_upload", [False, True])
def test_create_product_blocked_always(allow_upload):
    body = gql(("CreateProduct", CREATE_QUERY))
    for url in (GRAPHQL, UPLOAD_URL, "https://shafa.ua/api/v4/graphiql/"):
        allowed, reason = request_verdict("POST", url, "application/json", body, allow_upload)
        assert not allowed and "мутація CreateProduct" in reason


def test_mutation_hidden_in_batch_or_behind_query_name_blocked():
    batch = gql(("WEB_HeaderUser", "query A { me { id } }"), ("X", CREATE_QUERY))
    assert not request_verdict("POST", GRAPHQL, "application/json", batch, True)[0]
    disguised = gql(("WEB_ProductFormCatalog", CREATE_QUERY))
    assert not request_verdict("POST", GRAPHQL, "application/json", disguised, True)[0]
    keywords = gql(("ProductFormKeywordSelect", "mutation ProductFormKeywordSelect { a }"))
    assert not request_verdict("POST", GRAPHQL, "application/json", keywords, True)[0]


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"operationName": "X", "extensions": {"persistedQuery": {}}}', b"[]", b"\xff"],
)
def test_unclear_bodies_blocked(body):
    assert not request_verdict("POST", GRAPHQL, "application/json", body, True)[0]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/g/collect",
        "https://tracker.shafa.ua/endpoint/pageviews",
        "https://shafa.ua/uk/new",
        "https://shafa.ua.evil.example/api/v5/graphql",
    ],
)
def test_other_posts_blocked(url):
    body = gql(("A", "query A { a }"))
    assert not request_verdict("POST", url, "application/json", body, True)[0]


def test_photo_upload_only_when_allowed_and_exact():
    content_type, body = multipart(upload_fields())

    assert request_verdict("POST", UPLOAD_URL, content_type, body, True) == (
        True,
        "завантаження фото",
    )
    assert not request_verdict("POST", UPLOAD_URL, content_type, body, False)[0]
    assert not request_verdict("POST", GRAPHQL, content_type, body, True)[0]  # інша адреса


@pytest.mark.parametrize(
    "changes",
    [
        {"operationName": "CreateProduct"},
        {"query": CREATE_QUERY},  # чужий запит під назвою UploadPhoto
        {"query": "mutation UploadPhoto($file: String!) { uploadPhoto(uploadFile: $file) { id } "
                   "createProduct(nameUk: \"x\") { id } }"},
        {"query": "mutation UploadPhoto { ...M } fragment M on Mutation { createProduct { id } }"},
        {"query": UPLOAD_QUERY + " mutation B { createProduct { id } }"},
    ],
)  # fmt: skip
def test_spoofed_upload_blocked(changes):
    content_type, body = multipart(upload_fields(**changes))
    assert not request_verdict("POST", UPLOAD_URL, content_type, body, True)[0]


def test_root_fields_and_mutation_detection():
    assert root_fields(UPLOAD_QUERY) == ["uploadPhoto"]
    assert root_fields("mutation { a: createProduct(x: {y: 1}) { id } b }") == [
        "createProduct",
        "b",
    ]
    assert root_fields("mutation { ...F }") == ["...F"]
    assert has_mutation(CREATE_QUERY)
    assert not has_mutation("query WEB_X { catalog { id } }")
    assert has_mutation("query A { a } mutation B { b }")
