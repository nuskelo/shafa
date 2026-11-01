import http.client
import re
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from shafa import web
from shafa.db import Status
from test_review import add_card, edits, status

TOKEN = "тестовий-токен"
PORT = 8765
HOST = f"127.0.0.1:{PORT}"


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


@pytest.fixture
def item_id(conn, root):
    return add_card(conn, root)


def get(app, path, host=HOST):
    url = urlsplit(path)
    return app.handle(web.Request("GET", url.path, parse_qs(url.query), {}, {"host": host}))


def post(app, path, token=TOKEN, origin=None, **form):
    headers = {"host": HOST}
    if origin:
        headers["origin"] = origin
    data = {k: v if isinstance(v, list) else [v] for k, v in form.items()}
    if token is not None:
        data["token"] = [token]
    return app.handle(web.Request("POST", path, {}, data, headers))


def plain(page: str) -> str:
    """Текст сторінки без тегів і зайвих пробілів: «<b>Бот:</b> працює» → «Бот: працює»."""
    return " ".join(re.sub(r"<[^>]+>", " ", page).split())


def html(response):
    assert response.status == 200, response.body[:300]
    return response.body.decode("utf-8")


def location(response):
    assert response.status == 303
    return response.headers["Location"]


# --- Захист ---


@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:9999", "127.0.0.1", None])
def test_foreign_host_refused(app, item_id, host):
    assert get(app, "/review", host=host).status == 403
    assert get(app, f"/photo/{item_id}/1", host=host).status == 403


def test_localhost_allowed(app, item_id):
    assert get(app, "/review", host=f"localhost:{PORT}").status == 200


@pytest.mark.parametrize(
    ("token", "origin"),
    [(None, None), ("", None), ("чужий", None), (TOKEN, "http://evil.example")],
)
def test_post_without_token_or_from_foreign_page_refused(conn, app, item_id, token, origin):
    response = post(app, f"/item/{item_id}", token=token, origin=origin, action="reject")
    assert response.status == 403
    assert "Програму перезапускали" in response.body.decode("utf-8")
    assert status(conn, item_id) == Status.REVIEW


def test_referrer_policy_keeps_own_origin_in_forms(app):
    # Із `no-referrer` Chrome надсилає форму з `Origin: null` — і сервер відмовляє.
    assert get(app, "/review").headers["Referrer-Policy"] == "same-origin"


def test_null_origin_refused(conn, app, item_id):
    assert post(app, f"/item/{item_id}", origin="null", action="reject").status == 403


def test_post_with_own_origin_allowed(conn, app, item_id):
    post(app, f"/item/{item_id}", origin=f"http://{HOST}", action="reject")
    assert status(conn, item_id) == Status.ARCHIVED


def test_changes_only_by_post(conn, app, item_id):
    assert get(app, f"/item/{item_id}?action=reject&token={TOKEN}").status == 200
    assert status(conn, item_id) == Status.REVIEW


def test_security_headers(app, item_id):
    for path in ("/review", f"/item/{item_id}", "/static/style.css", "/нема"):
        headers = get(app, path).headers
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_post_text_escaped(conn, app, root):
    item_id = add_card(conn, root, text="Кеди <script>alert(1)</script> 500грн")
    page = html(get(app, f"/item/{item_id}"))
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page


# --- Жодних зовнішніх ресурсів ---


def test_pages_use_only_own_resources(conn, app, item_id):
    pages = [html(get(app, f"/{tab}")) for tab in web.TAB_TITLES]
    pages.append(html(get(app, f"/item/{item_id}")))
    for page in pages:
        for link in re.findall(r'(?:src|href|action)="([^"]*)"', page):
            assert link.startswith("/") and not link.startswith("//"), link
    for name in web.STATIC:
        body = get(app, f"/static/{name}").body.decode("utf-8")
        body = body.replace(
            'xmlns="http://www.w3.org/2000/svg"', ""
        )  # простір імен, не завантаження
        assert not re.search(r"https?:|//[\w.-]+\.\w|@import|url\(", body.replace("// ", ""))


# --- Сторінки ---


def test_own_icon_so_browser_does_not_ask_favicon(app, item_id):
    # Без іконки Chrome просить /favicon.ico → 404 → помилка в консолі (знайшла «мавпа»).
    for path in ("/review", f"/item/{item_id}"):
        assert '<link rel="icon" href="/static/icon.svg">' in html(get(app, path))
    icon = get(app, "/static/icon.svg")
    assert (icon.status, icon.content_type) == (200, "image/svg+xml")


def test_root_redirects_to_review(app):
    assert location(get(app, "/")) == "/review"


def test_tabs_with_counts(conn, app, root, item_id):
    add_card(conn, root, folder="1349", post_id=1349, status=Status.APPROVED)
    page = html(get(app, "/review"))
    assert 'href="/review" class="tab current">На перевірку <span class="count">1</span>' in page
    assert 'Схвалені <span class="count">1</span>' in page
    assert f'href="/item/{item_id}"' in page
    assert "3 жовті · 2 пропуски" in page
    assert "1380 грн" in page and "Чоловічий одяг · Взуття · Кросівки" in page


def test_search(conn, app, root, item_id):
    assert f"/item/{item_id}" in html(get(app, "/review?q=ECCO"))
    assert "Нічого не знайдено" in html(get(app, "/review?q=Lowa"))


def test_card_page_shows_yellow_with_reason_and_confirm(app, item_id):
    page = html(get(app, f"/item/{item_id}"))
    assert 'class="field yellow"' in page
    assert "розділ не звірено з довідником" in page
    assert 'value="confirm:section"' in page
    assert "Щоб схвалити: перевірте розділ" in page
    assert page.count(f'src="/thumb/{item_id}/') == 2
    assert "Шкіряні кеди Ecco 38 розміру. Устілка" in page  # текст поста для звірки
    assert f'name="token" value="{TOKEN}"' in page


def test_card_of_approved_is_read_only(conn, app, root):
    item_id = add_card(conn, root, status=Status.APPROVED)
    page = html(get(app, f"/item/{item_id}"))
    assert "Скасувати схвалення" in page
    assert 'value="approve"' not in page
    assert 'value="confirm:' not in page
    assert " disabled>" in page


def test_unknown_pages(app):
    for path in ("/item/999", "/нема", "/item/abc", "/static/web.py", "/static/../web.py"):
        assert get(app, path).status == 404, path


# --- Дії ---


def test_save_edit(conn, app, item_id):
    response = post(app, f"/item/{item_id}", action="save", brand="ECCO", season="Зима")
    assert location(response) == f"/item/{item_id}?msg=saved"
    assert edits(conn, item_id) == {"brand": "ECCO", "season": ["Зима"]}
    assert "змінено вами" in html(get(app, f"/item/{item_id}?msg=saved"))


def test_bad_value_shown(conn, app, item_id):
    response = post(app, f"/item/{item_id}", action="save", price="дорого", brand="ECCO")
    assert location(response).startswith(f"/item/{item_id}?msg=bad&bad=price&v_price=")
    page = html(get(app, location(response)))
    assert "Ціна — лише цифри, напр. 1960" in page and 'value="дорого"' in page
    assert 'class="field bad"' in page
    assert edits(conn, item_id) == {"brand": "ECCO"}


def test_approve_blocked_then_confirmed(conn, app, item_id):
    response = post(app, f"/item/{item_id}", action="approve")
    assert location(response) == f"/item/{item_id}?msg=section_yellow"
    assert status(conn, item_id) == Status.REVIEW

    response = post(app, f"/item/{item_id}", action="confirm:section")
    assert location(response) == f"/item/{item_id}?msg=saved"
    response = post(app, f"/item/{item_id}", action="approve")
    assert location(response) == "/review?msg=approved"
    assert status(conn, item_id) == Status.APPROVED


def test_approve_saves_changed_section_first(conn, app, item_id):
    post(app, f"/item/{item_id}", action="approve", section="Жіночий одяг / Взуття / Кросівки")
    assert status(conn, item_id) == Status.APPROVED


def test_reject_restore_unapprove(conn, app, item_id):
    assert location(post(app, f"/item/{item_id}", action="reject")) == "/review?msg=rejected"
    page = html(get(app, "/archived"))
    assert "видалиться" in page
    assert location(post(app, f"/item/{item_id}", action="restore")) == "/archived?msg=restored"
    post(app, f"/item/{item_id}", action="confirm:section")
    post(app, f"/item/{item_id}", action="approve")
    assert location(post(app, f"/item/{item_id}", action="unapprove")) == (
        "/approved?msg=unapproved"
    )
    assert status(conn, item_id) == Status.REVIEW


def test_folder_order(conn, app, item_id):
    post(app, f"/item/{item_id}", action="folder_order")
    assert edits(conn, item_id) == {"photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}


def test_unknown_action(app, item_id):
    assert post(app, f"/item/{item_id}", action="delete").status == 400


def test_recheck_problem(conn, app, root, item_id):
    with conn:
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'no_photos', "
            "problem_text = 'У папці немає фото .jpg'"
        )
    page = html(get(app, f"/item/{item_id}"))
    assert "Додайте в папку фото .jpg." in page

    assert location(post(app, f"/item/{item_id}", action="recheck")) == "/problem?msg=recheck_new"
    assert status(conn, item_id) == Status.NEW


def test_recheck_problem_still_there(conn, app, root):
    item_id = add_card(conn, root, folder="Сукня", post_id=1)
    with conn:
        conn.execute("UPDATE items SET status = 'problem', problem_code = 'bad_folder_name'")
    response = post(app, f"/item/{item_id}", action="recheck")
    assert location(response) == f"/item/{item_id}?msg=recheck_problem"


def test_recheck_robot_problem_returns_to_approved_keeping_blank(conn, app, root, item_id):
    """TD-015: не в new (без нового запиту до ШІ) — бланк і правки лишаються."""
    with conn:
        conn.execute("INSERT INTO edits VALUES (?, 'title', '\"Кеди Ecco\"', 'x')", (item_id,))
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'robot_field_missing', "
            "problem_text = 'Робот не знайшов поле «Сезон»'"
        )
    blanks = conn.execute("SELECT count(*) FROM blanks WHERE item_id = ?", (item_id,)).fetchone()

    response = post(app, f"/item/{item_id}", action="recheck")

    assert location(response) == "/problem?msg=robot_retry"
    assert status(conn, item_id) == Status.APPROVED
    assert (
        conn.execute("SELECT count(*) FROM blanks WHERE item_id = ?", (item_id,)).fetchone()
        == blanks
    )
    assert conn.execute("SELECT value FROM edits WHERE item_id = ?", (item_id,)).fetchone()[0]


def test_recheck_stale_rules_problem_recalculates_and_goes_to_review(conn, app, root, item_id):
    """ROBOT-008: «Перевірити ще раз» для старих правил — перерахунок без ШІ, не назад у Схвалені."""
    with conn:
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'stale_rules', "
            "problem_text = 'старі правила', rules_version = NULL"
        )

    response = post(app, f"/item/{item_id}", action="recheck")

    assert location(response) == "/review?msg=recheck_done"
    assert status(conn, item_id) == Status.REVIEW


def test_recheck_never_returns_publication_problem(conn, app, root, item_id):
    with conn:
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = 'publish_uncertain', "
            "problem_text = 'Перевір на Шафі'"
        )
    assert "Робот сам не повторює" in html(get(app, f"/item/{item_id}"))

    response = post(app, f"/item/{item_id}", action="recheck")

    assert location(response) == f"/item/{item_id}?msg=sticky"
    assert status(conn, item_id) == Status.PROBLEM


def test_archive_purged_on_list(conn, app, item_id, tmp_path):
    get(app, f"/thumb/{item_id}/1")
    assert (tmp_path / "thumbs" / str(item_id)).is_dir()
    with conn:
        conn.execute(
            "UPDATE items SET status = 'archived', archived_at = '2000-01-01T00:00:00+00:00'"
        )

    assert f"/item/{item_id}" not in html(get(app, "/archived"))
    assert get(app, f"/item/{item_id}").status == 404
    assert not (tmp_path / "thumbs" / str(item_id)).exists()


# --- Фото ---


def test_photo_and_thumbnail(app, item_id, root, tmp_path):
    original = get(app, f"/photo/{item_id}/1")
    assert original.status == 200 and original.content_type == "image/jpeg"
    assert original.body == (root / "1342" / "IMG_1.JPG").read_bytes()

    thumb = get(app, f"/thumb/{item_id}/1")
    assert thumb.status == 200 and thumb.body[:2] == b"\xff\xd8"
    assert list((tmp_path / "thumbs" / str(item_id)).glob("*.jpg"))  # кеш поруч із базою
    assert sorted(p.name for p in (root / "1342").iterdir()) == ["IMG_1.JPG", "IMG_2.JPG"]


@pytest.mark.parametrize(
    "path",
    [
        "/photo/{id}/99",
        "/photo/999/1",
        "/photo/{id}/../../база.sqlite",
        "/photo/../../etc/passwd",
        "/photo/{id}/1/../../2",
        "/thumb/{id}/-1",
    ],
)
def test_photo_only_from_card_folders(app, item_id, path):
    assert get(app, path.format(id=item_id)).status == 404


def test_photo_outside_root_refused_even_if_in_db(conn, app, item_id, root):
    (root.parent / "secret.jpg").write_bytes(b"\xff\xd8secret")  # Головна/1342/../../
    with conn:
        conn.execute("UPDATE photos SET file_name = '../../secret.jpg' WHERE position = 1")
    assert get(app, f"/photo/{item_id}/1").status == 404


def test_missing_or_broken_photo(app, item_id, root):
    (root / "1342" / "IMG_1.JPG").unlink()
    (root / "1342" / "IMG_2.JPG").write_bytes(b"not a jpeg")
    assert get(app, f"/photo/{item_id}/1").status == 404
    assert get(app, f"/thumb/{item_id}/2").status == 404


def test_published_card_photos_from_published_folder(conn, app, item_id, root):
    """TD-016: робот переносить папку опублікованого в «Опубліковано» — фото звідти."""
    (root / "Опубліковано").mkdir()
    (root / "1342").rename(root / "Опубліковано" / "1342")
    with conn:
        conn.execute("UPDATE items SET status = 'published'")

    original = get(app, f"/photo/{item_id}/1")
    assert original.status == 200
    assert original.body == (root / "Опубліковано" / "1342" / "IMG_1.JPG").read_bytes()
    assert get(app, f"/thumb/{item_id}/2").status == 200


def test_published_card_photos_still_in_main_folder(conn, app, item_id):
    """Папку не вдалося перенести (D1 ROBOT-002) — фото з Головної папки."""
    with conn:
        conn.execute("UPDATE items SET status = 'published'")
    assert get(app, f"/photo/{item_id}/1").status == 200


def test_published_folder_only_for_published_cards(conn, app, item_id, root):
    (root / "Опубліковано").mkdir()
    (root / "1342").rename(root / "Опубліковано" / "1342")
    assert get(app, f"/photo/{item_id}/1").status == 404


def test_published_photo_outside_root_refused(conn, app, item_id, root):
    (root.parent / "secret.jpg").write_bytes(b"\xff\xd8secret")  # Опубліковано/1342/../../../
    with conn:
        conn.execute("UPDATE items SET status = 'published'")
        conn.execute("UPDATE photos SET file_name = '../../../secret.jpg' WHERE position = 1")
    (root / "Опубліковано" / "1342").mkdir(parents=True)
    assert get(app, f"/photo/{item_id}/1").status == 404


# --- Справжній сервер ---


class GoneBrowser:
    def write(self, data):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


def test_browser_leaving_early_is_not_an_error(capsys):
    # Гортали фото: браузер закрив з'єднання, поки оригінал ще відправлявся.
    handler = web.Handler.__new__(web.Handler)
    handler.wfile = GoneBrowser()
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /photo/1/1 HTTP/1.1"
    handler.command = "GET"

    handler._send(web.Response(200, b"x" * 1000, "image/jpeg"))

    assert "Traceback" not in capsys.readouterr().err


def test_server_listens_only_on_localhost(app, item_id):
    server = web.make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        connection = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
        connection.request("GET", "/review")
        response = connection.getresponse()
        assert response.status == 200
        assert "На перевірку" in response.read().decode("utf-8")

        connection = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
        body = f"token={TOKEN}&action=reject"
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Host": "evil.example"}
        connection.request("POST", f"/item/{item_id}", body.encode("utf-8"), headers)
        assert connection.getresponse().status == 403
    finally:
        server.shutdown()
        server.server_close()


def test_busy_port_falls_back_to_free_one(app):
    first = web.make_server(app, 0)
    try:
        second = web.make_server(app, first.server_address[1])
        assert second.server_address[1] != first.server_address[1]
        assert app.port == second.server_address[1]
        second.server_close()
    finally:
        first.server_close()
