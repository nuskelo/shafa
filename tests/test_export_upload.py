"""«Завантажити експорт каналу» на екрані (WIN-001, TD-021)."""

import http.client
import json
import threading

import pytest

from shafa import web
from test_web import HOST, PORT, TOKEN, html


def export_json(n=3, pad=0):
    messages = [
        {"id": 1340 + i, "type": "message", "date": "2026-10-01T10:00:00",
         "text": f"Кеди {38 + i} розмір. 800грн" + " " * pad}
        for i in range(n)
    ]  # fmt: skip
    return json.dumps({"name": "канал", "messages": messages}, ensure_ascii=False).encode("utf-8")


def multipart(token, data, filename="result.json"):
    boundary = "shafa-boundary-123"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="token"\r\n\r\n{token}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="export"; '
            f'filename="{filename}"\r\nContent-Type: application/json\r\n\r\n'
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return f"multipart/form-data; boundary={boundary}", body


def test_parse_multipart_fields_and_file():
    kind, body = multipart(TOKEN, export_json(), filename="Експорт каналу.json")
    form, files = web.parse_multipart(kind, body)
    assert form == {"token": [TOKEN]}
    assert json.loads(files["export"])["messages"][0]["id"] == 1340


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def upload(app, data, token=TOKEN):
    kind, body = multipart(token, data)
    form, files = web.parse_multipart(kind, body)
    return app.handle(web.Request("POST", "/export", {}, form, {"host": HOST}, files))


def test_upload_saves_posts_and_makes_cards(app, conn, root):
    (root / "1341").mkdir()
    (root / "1341" / "1.jpg").write_bytes(b"\xff\xd8")

    page = html(upload(app, export_json()))

    assert "Експорт завантажено: постів 3, нових 3. Папок 1, у «Проблемах» 0." in page
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 3
    assert (
        conn.execute("SELECT status FROM items WHERE folder_name = '1341'").fetchone()[0] == "new"
    )
    assert "нових 0" in html(upload(app, export_json()))  # удруге — нічого нового


@pytest.mark.parametrize("data", [b"not json", b"[]", b'{"name": "x"}', "Кеди".encode("cp1251")])
def test_not_an_export_explained(app, conn, data):
    page = html(upload(app, data))
    assert "Це не експорт каналу Telegram" in page
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0


def test_upload_needs_token(app, conn):
    assert upload(app, export_json(), token="чужий").status == 403
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0


def _post_raw(port, path, kind, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body, {"Host": f"127.0.0.1:{port}", "Content-Type": kind})
    response = conn.getresponse()
    return response.status, response.read().decode("utf-8")


def test_real_server_takes_big_export_only_on_export(app, conn):
    server = web.make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.port = server.server_address[1]
    try:
        big = export_json(n=3, pad=700_000)  # ≈ 2 МБ — більше за звичайний ліміт
        kind, body = multipart(TOKEN, big)
        status, page = _post_raw(app.port, "/export", kind, body)
        assert status == 200 and "постів 3" in page
        try:  # не дочитавши тіло, сервер відповідає 413 і закриває — клієнт може побачити розрив
            status, _ = _post_raw(app.port, "/settings", kind, body)
        except (ConnectionResetError, BrokenPipeError):
            status = 413
        assert status == 413
        assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 3
    finally:
        server.shutdown()
        server.server_close()
