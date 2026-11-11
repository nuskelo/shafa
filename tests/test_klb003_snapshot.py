"""KLB-003 D3: знімок її оголошень Клубку — повний або нічого, захист від падіння, зниклі → продано."""

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from shafa.klubok import snapshot
from shafa.robot import klubok_fake as fake

FIXTURE = Path(__file__).parent / "fixtures" / "klubok-get-items.json"


@pytest.fixture
def server():
    srv = fake.Server(listings=1787)
    yield srv
    srv.close()


def http_fetch(srv):
    def fetch(path):
        try:
            with urllib.request.urlopen(srv.base + path, timeout=10) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, ""

    return fetch


def refresh(conn, srv, waits=None):
    return snapshot.refresh(
        conn, http_fetch(srv), pause=lambda: None,
        wait=(waits.append if waits is not None else lambda s: None), log=lambda line: None,
    )  # fmt: skip


def listings(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM klubok_listings")}


def read_row(conn):
    return conn.execute("SELECT read_at, count FROM klubok_listings_read").fetchone()


def test_full_snapshot_1787_in_one_request(conn, server):
    assert refresh(conn, server) == 1787
    assert server.item_reads == [0] and server.item_limits == [snapshot.PAGE_ALL]
    assert len(listings(conn)) == 1787
    assert read_row(conn)["count"] == 1787


def test_fixture_fields_are_mapped(conn):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot.save(conn, snapshot.Read(data["items"], data["items_count"]))
    rows = listings(conn)
    first = rows[900000101]
    assert first["name"] == "Вигадані черевики Testbrand 38 розміру"
    assert first["price"] == 1000 and first["sold"] == 0 and first["moderated"] == 0
    assert first["url"].startswith("https://klubok.com/item-") and first["photo"].endswith("_1.jpg")
    assert json.loads(first["sizes"]) == ["38"]
    assert json.loads(rows[900000103]["sizes"]) == ["38,5"]
    assert rows[900000102]["photo"] is None  # порожнє `img_link`
    assert first["renew_date"] == "2026-11-17T10:00:00Z"


def test_only_klubok_links_and_photos(conn):
    item = {"id": 5, "name": "Черевики 38", "link": "javascript:alert(1)",
            "img_link": "https://evil.example/x.jpg"}  # fmt: skip
    snapshot.save(conn, snapshot.Read([item], 1))
    row = listings(conn)[5]
    assert row["url"] is None and row["photo"] is None


@pytest.mark.parametrize(("fault", "code"), [(500, snapshot.FAILED), ("notjson", snapshot.FAILED),
                                             (429, snapshot.TOO_MANY)])  # fmt: skip
@pytest.mark.parametrize("offset", [0, 600])  # весь список одним запитом / сторінка запасного шляху
def test_error_on_a_page_writes_nothing(conn, server, fault, code, offset):
    refresh(conn, server)
    before, before_read = listings(conn), tuple(read_row(conn))
    server.listings.pop(0)
    server.items_limit_max = None if offset == 0 else snapshot.PAGE
    server.items_fault[offset] = fault

    with pytest.raises(snapshot.SnapshotError) as error:
        refresh(conn, server)

    assert error.value.code == code
    assert listings(conn) == before and tuple(read_row(conn)) == before_read


def test_renew_all_ties_are_read_whole_in_one_request(conn, server):
    """KLB-006: після «Обновить все» сторінки нестабільні (як живий: 1782 різних з 1787)."""
    server.renew_all()
    waits = []
    assert refresh(conn, server, waits) == 1787
    assert server.item_limits == [snapshot.PAGE_ALL] and waits == []
    assert len(listings(conn)) == 1787


def test_renew_all_pages_are_really_unstable(server):
    """Сторінками по 60 той самий список дає повтори й пропуски — без цього тест вище нічого б не
    доводив."""
    server.renew_all()
    fetch = http_fetch(server)
    ids = []
    for offset in range(0, 1787, snapshot.PAGE):
        ids += [item["id"] for item in json.loads(fetch(snapshot.items_path(offset))[1])["items"]]
    assert len(ids) == 1787 and len(set(ids)) < 1787


def unstable_first_pass(server):
    """Сайт віддає не більше 60; перший посторінковий прохід — з нестабільним порядком, далі — ні."""
    server.renew_all()
    server.items_limit_max = snapshot.PAGE
    fetch = http_fetch(server)

    def wrapped(path):
        if len(server.item_reads) >= 1 + 30:  # весь список + перший прохід
            server.unstable_ties = False
        return fetch(path)

    return wrapped


def test_capped_list_is_merged_by_id_over_passes(conn, server):
    waits, lines = [], []
    snap = snapshot.read(unstable_first_pass(server), lambda: None, waits.append, lines.append)
    assert len(snap.items) == 1787 == len({item["id"] for item in snap.items})
    assert waits == [snapshot.RETRY_WAIT]  # два проходи
    assert server.item_limits == [snapshot.PAGE] * (1 + 60)
    assert "одним запитом 60 різних з 1787" in lines[0]


def test_no_pass_is_whole_but_the_union_is(server):
    """Кожен прохід губить інше оголошення (на його місці — повтор сусіда); разом — усі."""
    server.items_limit_max = snapshot.PAGE
    fetch = http_fetch(server)
    lose = {1: 600, 2: 1200, 3: 1740}  # прохід → сторінка, де губиться перше оголошення

    def lossy(path):
        before = len(server.item_reads)
        status, text = fetch(path)
        attempt = (before - 1) // 30 + 1 if before else 0
        if lose.get(attempt) == server.item_reads[-1]:
            data = json.loads(text)
            data["items"][0] = data["items"][1]
            text = json.dumps(data)
        return status, text

    waits = []
    snap = snapshot.read(lossy, lambda: None, waits.append, lambda line: None)
    assert len({item["id"] for item in snap.items}) == 1787
    assert waits == [snapshot.RETRY_WAIT]  # зійшлось після другого проходу


def test_merge_keeps_the_fresher_record(server):
    server.items_limit_max = snapshot.PAGE
    fetch = http_fetch(server)
    first = server.listings[0]["id"]

    def renamed(path):
        if len(server.item_reads) == 1:  # після запиту «весь список» назва змінилась
            server.listings[0] = {**server.listings[0], "name": "Нова назва 38"}
        return fetch(path)

    snap = snapshot.read(renamed, lambda: None, log=lambda line: None)
    assert {item["id"]: item for item in snap.items}[first]["name"] == "Нова назва 38"


def test_not_converged_after_all_passes_writes_nothing(conn, server):
    refresh(conn, server)
    before, before_read = listings(conn), tuple(read_row(conn))
    server.items_limit_max = snapshot.PAGE
    server.items_count = 1790
    waits = []
    with pytest.raises(snapshot.SnapshotError) as error:
        refresh(conn, server, waits)
    assert error.value.code == snapshot.FAILED and "1787 різних з 1790" in error.value.text
    assert waits == [snapshot.RETRY_WAIT] * (snapshot.PASSES - 1)
    assert len(server.item_reads) == 1 + 1 + snapshot.PAGE_BUDGET  # межа запитів на знімок
    assert listings(conn) == before and tuple(read_row(conn)) == before_read


def test_items_count_mismatch_is_incomplete(conn, server):
    server.items_count = 1790
    with pytest.raises(snapshot.SnapshotError):
        refresh(conn, server)
    assert listings(conn) == {}


def test_drop_over_ten_percent_writes_nothing(conn, server):
    refresh(conn, server)
    before = listings(conn)
    del server.listings[1500:]  # 1500 < 0,9 × 1787

    with pytest.raises(snapshot.SnapshotError) as error:
        refresh(conn, server)

    assert error.value.code == snapshot.DROPPED and "1787" in error.value.text
    assert listings(conn) == before


def test_gone_become_sold_and_her_marks_survive(conn, server):
    refresh(conn, server)
    gone = server.listings.pop(3)["id"]
    kept = server.listings[0]["id"]
    with conn:
        conn.execute(
            "UPDATE klubok_listings SET post_id = 7301, asked_at = 'x', brand = 'Ecco', "
            "category_id = 7, added_at = 'перше' WHERE id IN (?, ?)",
            (gone, kept),
        )

    refresh(conn, server)

    rows = listings(conn)
    assert rows[gone]["sold"] == 1 and rows[kept]["sold"] == 0
    for item_id in (gone, kept):
        assert (rows[item_id]["post_id"], rows[item_id]["asked_at"], rows[item_id]["brand"],
                rows[item_id]["category_id"], rows[item_id]["added_at"]) == (
            7301, "x", "Ecco", 7, "перше")  # fmt: skip
    assert read_row(conn)["count"] == 1786


def test_snapshot_fills_link_of_ads_published_by_program(conn, server, root):
    from test_klb002_queue import add

    item_id = add(conn, root, 7301)
    klubok_id = server.listings[10]["id"]
    with conn:
        conn.execute(
            "INSERT INTO klubok_ads (item_id, state, klubok_id, created_at, updated_at) "
            "VALUES (?, 'published', ?, 'x', 'x')",
            (item_id, klubok_id),
        )
    refresh(conn, server)
    url = conn.execute("SELECT url FROM klubok_ads WHERE item_id = ?", (item_id,)).fetchone()[0]
    assert url == f"https://klubok.com/item-fake-{klubok_id}"
    assert snapshot.missing(conn, [klubok_id, 1]) == [1]


def test_too_many_pages_is_failed(conn, server, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_PAGES", 3)
    server.items_limit_max = snapshot.PAGE
    with pytest.raises(snapshot.SnapshotError) as error:
        refresh(conn, server)
    assert error.value.code == snapshot.FAILED and listings(conn) == {}


def test_fetch_goes_through_the_page_and_guard(server):

    pytest.importorskip("playwright")
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as p:
        try:
            browser_ = p.chromium.launch()
        except Error:
            pytest.skip("немає браузера Playwright")
        session = fake.FakeSession(None, server.base, context=browser_.new_context())
        try:
            assert session.open_form().ok
            status, text = session.fetch_text(snapshot.items_path(0))
            assert status == 200 and len(json.loads(text)["items"]) == 60
            status, _ = session.fetch_text("/renew/all")  # небезпечне — guard не пустив
            assert status == 0 and server.traps == []
        finally:
            session.close()
            browser_.close()
