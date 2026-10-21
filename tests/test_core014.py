"""CORE-014: кілька розмірів однієї пари в одному пості → окрема Картка на кожен розмір."""

import json
from datetime import UTC, datetime

import pytest

from conftest import make_folder
from shafa import ai, batch, changes, fill, review, rules, scan, variants
from shafa.db import Status
from shafa.export import Post
from shafa.folders import PUBLISHED_DIR, item_folder, list_folders
from shafa.robot import browser, publish
from test_ai import FakeClient, answer, message
from test_catalog import WOMEN_BOOTS
from test_photos import make_photo
from test_robot_plan import BLANK

# Тексти з бази пілоту (зараз — «Проблеми» `many_items`)
P3654 = """Нові термо черевики Peter Storm. В наявності декілька розмірів.
* 43 розмір. Устілка 28см. Ширина устілки 9см.
* 45 розмір. Устілка 29,3см. Ширина устілки 9,5см.
Завдяки мембрані не промокають.
1800грн
#VS_43_розмір
#VS_45_розмір

Пишіть сюди 👉 @viktoria"""
P3655 = """Нові термо черевики Peter Storm. В наявності декілька розмірів.
* Вказаний 37 розмір. Ідуть на 36. Устілка до згину на п'ятку 23см, від краю до краю 24см.  Ширина устілки 8см.
* Вказаний 38 розмір. Ідуть на 37. Устілка до згину на п'ятку 24см, від краю до краю 24,5см.  Ширина устілки 8,5см.
Верх - натуральна замша. Завдяки мембрані не промокають.
1900грн
#VS_36_розмір
#VS_37_розмір"""
P3659 = """Нові шкіряні термо черевики Hi-tec. В наявності декілька розмірів.
* 38 розмір. Устілка 24,5см. Ширина устілки 8,5см. 1600грн
* 39 розмір. Устілка 25,3см. Ширина устілки 9см. 1700грн
* 42 розмір. Устілка 27,5см. Ширина устілки 9,5см.
Верх - натуральна шкіра нубук. Завдяки мембрані не промокають.
1990грн
#VS_38_розмір
#VS_39_розмір
#VS_42_розмір"""
# Жіночі розміри знімка довідників тестів
BOOTS = """Нові черевики Pavers. В наявності декілька розмірів.
* 37 розмір. Устілка 24см.
* 38 розмір. Устілка 24,5см.
Натуральна шкіра.
1500грн"""
NOON = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
REAL_CALL = ai.call  # з підставним клієнтом — без мережі


@pytest.fixture(autouse=True)
def split_on(monkeypatch):
    """Код CORE-014 вимкнено (FIX-CORE-014-off); тут його перевіряємо ввімкненим."""
    monkeypatch.setattr(variants, "SPLIT_SIZES", True)


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    """Жодного справжнього запиту до ШІ; там, де ШІ потрібне, тест дає підставний клієнт."""

    def boom(*args, **kwargs):
        raise AssertionError("запит до ШІ")

    monkeypatch.setattr(ai, "call", boom)


def prices(text):
    return {v.size: rules.price_from_text(v.text) for v in rules.size_variants(text)}


# --- 1. Розбір поста ---


def test_3654_two_sizes_common_price():
    assert prices(P3654) == {"43": 1800, "45": 1800}
    assert rules.shafa_price(1800) == 2070


def test_3655_label_goes_on():
    assert prices(P3655) == {"36": 1900, "37": 1900}  # «вказаний 37, ідуть на 36» → 36


def test_3659_own_price_or_common():
    assert prices(P3659) == {"38": 1600, "39": 1700, "42": 1990}


def test_variant_text_only_own_line():
    v43, v45 = rules.size_variants(P3654)
    assert "45" not in v43.text and "29,3" not in v43.text
    assert "декілька розмірів" not in v43.text
    assert "#VS_43_розмір" in v43.text and "#VS_45_розмір" not in v43.text
    assert rules.insole_lengths(v45.text) == [29.0, 29.5]
    description = rules.clean_description(v43.text, rules.condition(v43.text))
    assert description.startswith("43 розмір. Устілка 28см")
    assert "45" not in description and "1800" not in description and "*" not in description


def test_own_price_drops_common_price_line():
    v38 = rules.size_variants(P3659)[0]
    assert "1990" not in v38.text and "1600грн" in v38.text


@pytest.mark.parametrize(
    "text",
    [
        "Нові черевики. Кілька розмірів.\n* 43 і 45 розмір.\n* 46 розмір.\n1800грн",  # два в пункті
        "Черевики. Кілька розмірів.\n* 43 розмір. Устілка 28см.\n* Дуже теплі.\n1800грн",  # без розміру
        "Черевики.\n* Ecco 43 розмір.\n* Geox 45 розмір.\n1800грн",  # різні моделі
        "Нові черевики Peter Storm 43 і 45 розміри. 1800грн",  # без пунктів (як раніше)
        "Черевики.\n* 43 розмір. 1800грн\n* 43 розмір. 1700грн",  # той самий розмір
        "Черевики.\n* 43 розмір.\n* 45 розмір.",  # без ціни
        "Черевики 44 розміру.\n* 43 розмір.\n* 45 розмір.\n1800грн",  # розмір і в спільному тексті
    ],
)
def test_ambiguous_stays_many_items(text):
    assert rules.size_variants(text) is None
    assert scan.text_problem(text)[0] == "many_items"


def test_title_gets_own_size_without_ai_sizes():
    assert variants.with_size("Нові термо черевики peter storm 43 розміру", "45") == (
        "Нові термо черевики peter storm 45"
    )
    assert variants.with_size("Замшеві черевики 36 і 37 розміру", "37") == "Замшеві черевики 37"
    assert variants.with_size("Черевики Ecco 38", "38") == "Черевики Ecco 38"


# --- 2. Картки з папки ---


def folders(root):
    return list_folders(root, None)


def items(conn, post_id):
    return {
        r["folder_name"]: r
        for r in conn.execute("SELECT * FROM items WHERE post_id = ? ORDER BY id", (post_id,))
    }


def test_scan_makes_card_per_size_and_twice_no_duplicates(conn, root):
    make_folder(root, "3654", ["1.jpg", "2.jpg"])
    posts = {3654: Post(3654, "2025-01-01T10:00:00", P3654)}

    scan.scan(conn, posts, folders(root))
    scan.scan(conn, posts, folders(root))

    found = items(conn, 3654)
    assert set(found) == {"3654~43", "3654~45"}
    assert {r["status"] for r in found.values()} == {Status.NEW}
    photos = conn.execute("SELECT count(*) FROM photos").fetchone()[0]
    assert photos == 4  # кожна Картка — ті самі 2 фото спільної папки
    assert item_folder(root, "3654~45") == root / "3654"


def test_export_fixture_post_22_two_cards(conn, root, export_path):
    from shafa.export import read_export

    make_folder(root, "22", ["1.jpg"])
    scan.scan(conn, read_export(export_path), folders(root))
    found = items(conn, 22)
    assert set(found) == {"22~36", "22~38"}
    assert review.load_card(conn, found["22~36"]["id"]).fields["price"].value == 920  # 800 грн
    assert review.load_card(conn, found["22~38"]["id"]).fields["price"].value == 1001  # 850 грн


def test_folder_gone_check_uses_shared_folder(conn, root):
    make_folder(root, "3654", ["1.jpg"])
    posts = {3654: Post(3654, "2025-01-01T10:00:00", P3654)}
    scan.scan(conn, posts, folders(root))
    assert {r["problem_code"] for r in items(conn, 3654).values()} == {None}


def test_scan_size_removed_from_post_is_problem(conn, root):
    make_folder(root, "3654", ["1.jpg"])
    scan.scan(conn, {3654: Post(3654, "2025-01-01T10:00:00", P3654)}, folders(root))
    without_45 = P3654.replace("* 45 розмір. Устілка 29,3см.", "* 44 розмір. Устілка 28,5см.")

    scan.scan(conn, {3654: Post(3654, "2025-01-01T10:00:00", without_45)}, folders(root))

    found = items(conn, 3654)
    assert (found["3654~45"]["status"], found["3654~45"]["problem_code"]) == (
        Status.PROBLEM, variants.GONE,
    )  # fmt: skip
    assert found["3654~45"]["problem_text"] == "Розмір 45 прибрано з поста — можливо, продано"
    assert found["3654~44"]["status"] == Status.NEW


# --- 3. Стара Картка `many_items` → варіанти ---


def stored_answer():
    return answer(
        title="Нові термо черевики peter storm 43 розміру", section=WOMEN_BOOTS,
        sizes=["43", "45"], material_upper=[],
    )  # fmt: skip


def old_card(conn, folder="borh-3654", post_id=3654, text=P3654, with_answer=True):
    with conn:
        conn.execute(
            "INSERT INTO posts VALUES (?, '2025-01-01T10:00:00', ?, 'export', 'x')",
            (post_id, text),
        )
        item_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, problem_code, problem_text, "
            "created_at, updated_at, queue_pos, rules_version) "
            "VALUES (?, ?, 'problem', 'many_items', 'x', 'x', 'x', 7, ?)",
            (folder, post_id, rules.RULES_VERSION),
        ).lastrowid
        conn.execute("INSERT INTO photos VALUES (?, 1, '1.jpg')", (item_id,))
        if with_answer:
            fill.save_answer(conn, item_id, stored_answer())
            fill.save_blank(conn, item_id, ai.check_answer(stored_answer(), ["1.jpg"], text))
    return item_id


def test_old_many_items_card_becomes_variants_without_ai(conn):
    item_id = old_card(conn)

    assert fill.recheck(conn) == 1

    found = items(conn, 3654)
    assert set(found) == {"borh-3654~43", "borh-3654~45"}
    assert found["borh-3654~43"]["id"] == item_id  # стара Картка — перший варіант
    for size, row in (("43", found["borh-3654~43"]), ("45", found["borh-3654~45"])):
        assert (row["status"], row["queue_pos"]) == (Status.REVIEW, 7)
        card = review.load_card(conn, row["id"])
        assert card.fields["sizes"].value == [size]
        assert card.fields["price"].value == 2070
        assert card.fields["title"].value == f"Нові термо черевики peter storm {size}"
        assert card.variant == size
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 0
    assert fill.recheck(conn) == 0 and len(items(conn, 3654)) == 2  # повтор нічого не робить


def test_old_card_without_answer_goes_to_ai_once(conn, root, monkeypatch):
    monkeypatch.setattr(ai, "call", REAL_CALL)
    old_card(conn, folder="3659", post_id=3659, text=P3659, with_answer=False)
    (root / "3659").mkdir()
    make_photo(root / "3659" / "1.jpg", (400, 300))

    fill.recheck(conn)

    assert {r["status"] for r in items(conn, 3659).values()} == {Status.NEW}
    picked = fill.pick_items(conn, None, 10)
    assert [r["folder_name"] for r in picked] == ["3659~38"]  # один запит на пост
    planned = fill.plan(conn, root, picked[0])
    assert "39 розмір" not in planned.request["messages"][0]["content"][-1]["text"]
    reply = answer(title="Шкіряні черевики hi-tec", section=WOMEN_BOOTS, sizes=["38"],
                   photo_order=["1.jpg"])  # fmt: skip
    fill.fill(conn, FakeClient(message(reply)), planned)

    found = items(conn, 3659)
    assert {r["status"] for r in found.values()} == {Status.REVIEW}
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 1
    card = review.load_card(conn, found["3659~39"]["id"])
    assert card.fields["sizes"].value == ["39"] and card.fields["price"].value == 1960
    assert card.fields["title"].value == "Шкіряні черевики hi-tec 39"


def test_batch_sends_one_sister_and_spreads_answer(conn, root):
    from test_batch import TODAY, line
    from test_batch import FakeClient as BatchClient

    old_card(conn, folder="3659", post_id=3659, text=P3659, with_answer=False)
    (root / "3659").mkdir()
    make_photo(root / "3659" / "1.jpg", (400, 300))
    fill.recheck(conn)
    client = BatchClient()

    sent = batch.send(conn, client, root, TODAY, batch.Limits())
    assert sent.count == 1
    assert batch.queue(conn, TODAY, 7).ready == []  # сестри чекають пакета, не йдуть окремо

    lead = items(conn, 3659)["3659~38"]["id"]
    reply = message(answer(section=WOMEN_BOOTS, photo_order=["1.jpg"]), usage=(9500, 370))
    batch.apply_result(conn, sent.batch_id, line(f"b{sent.batch_id}-i{lead}", reply=reply),
                       batch.Collected())  # fmt: skip

    assert {r["status"] for r in items(conn, 3659).values()} == {Status.REVIEW}


def test_approved_or_published_base_card_never_split(conn):
    item_id = old_card(conn)
    with conn:
        conn.execute("UPDATE items SET status = 'published', shafa_id = 5 WHERE id = ?", (item_id,))
    assert variants.split_card(conn, item_id) == []
    assert set(items(conn, 3654)) == {"borh-3654"}


# --- 4. Зміна поста і видалення ---


def two_reviewed(conn):
    old_card(conn)
    fill.recheck(conn)
    return items(conn, 3654)


def test_size_removed_from_post_is_problem(conn):
    found = two_reviewed(conn)
    without_45 = P3654.replace("* 45 розмір. Устілка 29,3см. Ширина устілки 9,5см.\n", "")

    changes.text_changed(conn, 3654, without_45, moment=NOON)

    gone = conn.execute(
        "SELECT status, problem_code, problem_text FROM items WHERE id = ?",
        (found["borh-3654~45"]["id"],),
    ).fetchone()
    assert (gone["status"], gone["problem_code"]) == (Status.PROBLEM, variants.GONE)
    assert gone["problem_text"].startswith("Розмір 45 прибрано з поста — можливо, продано")


def test_size_removed_from_published_is_reported(conn):
    found = two_reviewed(conn)
    with conn:
        conn.execute(
            "UPDATE items SET status = 'published' WHERE id = ?", (found["borh-3654~45"]["id"],)
        )
    without_45 = P3654.replace("* 45 розмір. Устілка 29,3см. Ширина устілки 9,5см.\n", "")

    result = changes.text_changed(conn, 3654, without_45, moment=NOON)

    assert (3654, "Розмір 45 прибрано з поста — можливо, продано") in result.published
    status = conn.execute(
        "SELECT status FROM items WHERE id = ?", (found["borh-3654~45"]["id"],)
    ).fetchone()[0]
    assert status == Status.PUBLISHED


# --- 5. Робот: сестри не дублі, папка, ліміт, видалений пост ---


@pytest.fixture
def env(conn, root, tmp_path, monkeypatch):
    from test_robot_publish import Env

    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    return Env(conn, root, tmp_path)


def sisters(env, sizes=("37", "38")):
    """Схвалені Картки-варіанти одного поста з тим самим бланком — без CORE-014 вони «схожі»."""
    (env.root / "1342").mkdir()
    for name in ("IMG_1.JPG", "IMG_2.JPG"):
        make_photo(env.root / "1342" / name, (400, 300))
    ids = []
    with env.conn:
        env.conn.execute(
            "INSERT INTO posts VALUES (1342, '2025-03-01T10:00:00', ?, 'export', 'x')", (BOOTS,)
        )
        for size in sizes:
            item_id = env.conn.execute(
                "INSERT INTO items (folder_name, post_id, status, created_at, updated_at, "
                "rules_version) VALUES (?, 1342, 'approved', 'x', 'x', ?)",
                (f"1342~{size}", rules.RULES_VERSION),
            ).lastrowid
            env.conn.executemany(
                "INSERT INTO photos VALUES (?, ?, ?)",
                [(item_id, 1, "IMG_1.JPG"), (item_id, 2, "IMG_2.JPG")],
            )
            fill.save_blank(env.conn, item_id, BLANK)
            ids.append(item_id)
    return ids


def test_published_sister_is_not_duplicate_nor_already_published(env):
    first, second = sisters(env)

    report = env.run()

    assert [env.conn.execute("SELECT status FROM items WHERE id = ?", (i,)).fetchone()[0]
            for i in (first, second)] == [Status.PUBLISHED, Status.PUBLISHED]  # fmt: skip
    assert len(report.published) == 2
    today = datetime.now().astimezone()  # `publish_started_at` — справжній час запису
    assert publish.started_today(env.conn, today) == 2  # кожен розмір — одне оголошення
    assert (env.root / PUBLISHED_DIR / "1342" / "IMG_1.JPG").is_file()  # папка — після обох
    assert not (env.root / "1342").exists()


def test_folder_stays_while_a_sister_is_not_published(env):
    _, second = sisters(env)
    with env.conn:
        env.conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (second,))

    env.run()

    assert (env.root / "1342" / "IMG_1.JPG").is_file()  # сестрі ще потрібні фото
    assert not (env.root / PUBLISHED_DIR / "1342").exists()


def test_deleted_post_archives_all_variants(env):
    ids = sisters(env)
    with env.conn:
        env.conn.execute("UPDATE posts SET source = 'bot' WHERE post_id = 1342")
    env.presence = {1342: "gone"}

    env.run()

    rows = [env.conn.execute("SELECT status, problem_code FROM items WHERE id = ?", (i,)).fetchone()
            for i in ids]  # fmt: skip
    assert [tuple(r) for r in rows] == [(Status.ARCHIVED, "post_deleted")] * 2


# --- 6. Екран ---


def test_screen_shows_size_and_place(conn, tmp_path, root):
    from shafa import web

    found = two_reviewed(conn)
    item_id = found["borh-3654~45"]["id"]
    assert review.load_card(conn, item_id).place == (2, 2)
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", 8798, token="t")

    def get(path):
        request = web.Request("GET", path, {}, {}, {"host": "127.0.0.1:8798"})
        return app.handle(request).body.decode("utf-8")

    assert "розмір 45 · 2 з 2" in get(f"/item/{item_id}")
    assert "розмір 43 · 1 з 2" in get("/review")


def test_blank_keeps_shared_answer_json(conn):
    found = two_reviewed(conn)
    stored = [
        json.loads(r[0]) for r in conn.execute("SELECT answer FROM ai_answers ORDER BY item_id")
    ]
    assert len(stored) == 2 and stored[0] == stored[1]  # відповідь ШІ спільна, розмір — правилами
    assert len(found) == 2
