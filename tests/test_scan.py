import pytest

from conftest import CHANNEL_ID, make_folder
from shafa.db import Status
from shafa.export import read_export
from shafa.folders import list_folders
from shafa.scan import rescan_folder, scan


@pytest.fixture
def posts(export_path):
    return read_export(export_path)


def run(conn, posts, root):
    return scan(conn, posts, list_folders(root, CHANNEL_ID))


def items(conn):
    return {
        r["folder_name"]: (r["status"], r["problem_code"], r["problem_text"])
        for r in conn.execute("SELECT * FROM items")
    }


def photos(conn, folder_name):
    return [
        r[0]
        for r in conn.execute(
            "SELECT file_name FROM photos JOIN items ON items.id = item_id "
            "WHERE folder_name = ? ORDER BY position",
            (folder_name,),
        )
    ]


def test_both_folder_name_formats_become_new(conn, posts, root):
    make_folder(root, "10", ["IMG_10.JPG", "IMG_9.JPG"])
    make_folder(root, CHANNEL_ID + "11", ["1.jpeg"])

    result = run(conn, posts, root)

    assert (result.folders, result.new, result.problems) == (2, 2, [])
    assert items(conn) == {"10": ("new", None, None), CHANNEL_ID + "11": ("new", None, None)}
    assert photos(conn, "10") == ["IMG_9.JPG", "IMG_10.JPG"]
    row = conn.execute("SELECT post_id FROM items WHERE folder_name = ?", (CHANNEL_ID + "11",))
    assert row.fetchone()[0] == 11
    assert conn.execute("SELECT count(*) FROM posts WHERE source = 'export'").fetchone()[0] == 8


@pytest.mark.parametrize(
    ("name", "files", "code", "text"),
    [
        ("Сукня", ["1.jpg"], "bad_folder_name", "Назва папки не номер поста"),
        ("999", ["1.jpg"], "post_not_found", "Поста з номером 999 немає в експорті"),
        ("12", ["1.jpg"], "no_text", "У поста 12 немає тексту"),
        ("20", ["1.jpg"], "no_text", "У поста 20 немає тексту"),
        ("21", ["1.jpg"], "no_price", "У поста 21 немає ціни"),
        ("21", [], "no_price", "У поста 21 немає ціни"),
        ("22", ["1.jpg"], "many_items", "У пості кілька товарів з різними цінами"),
        ("13", ["знімок.png", "Thumbs.db"], "no_photos", "У папці немає фото .jpg"),
        ("13", [f"{i}.jpg" for i in range(11)], "too_many_photos", "Більше 10 фото"),
    ],
)
def test_problem_reasons(conn, posts, root, name, files, code, text):
    make_folder(root, name, files)

    result = run(conn, posts, root)

    assert result.new == 0
    assert [(p.folder_name, p.code, p.text) for p in result.problems] == [(name, code, text)]
    assert items(conn)[name] == ("problem", code, text)


def test_bought_for_and_strikethrough_price_is_one_item(conn, posts, root):
    make_folder(root, "23", ["1.jpg"])
    assert run(conn, posts, root).new == 1


def test_ten_photos_is_fine(conn, posts, root):
    make_folder(root, "13", [f"{i}.jpg" for i in range(10)])
    assert run(conn, posts, root).new == 1


def test_duplicate_post(conn, posts, root):
    make_folder(root, "10", ["1.jpg"])
    make_folder(root, CHANNEL_ID + "10", ["1.jpg"])

    result = run(conn, posts, root)

    text = f"Дві папки для одного поста: 10 і {CHANNEL_ID}10"
    assert [(p.code, p.text) for p in result.problems] == [("duplicate_post", text)] * 2


def test_rerun_does_not_duplicate(conn, posts, root):
    make_folder(root, "10", ["1.jpg", "2.jpg"])
    make_folder(root, "Сукня", [])
    run(conn, posts, root)
    before = conn.execute("SELECT * FROM items").fetchall()

    result = run(conn, posts, root)

    assert [tuple(r) for r in conn.execute("SELECT * FROM items")] == [tuple(r) for r in before]
    assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 8
    assert (result.folders, result.new, len(result.problems)) == (2, 1, 1)


def test_fixed_problem_returns_to_new(conn, posts, root):
    folder = make_folder(root, "10", [])
    assert run(conn, posts, root).new == 0
    (folder / "IMG_1.JPG").write_bytes(b"")

    result = run(conn, posts, root)

    assert result.problems == []
    assert items(conn)["10"] == ("new", None, None)
    assert photos(conn, "10") == ["IMG_1.JPG"]


def test_renamed_folder(conn, posts, root):
    make_folder(root, "10 сукня", ["1.jpg"])
    run(conn, posts, root)
    (root / "10 сукня").rename(root / "10")

    run(conn, posts, root)

    assert items(conn) == {
        "10 сукня": ("problem", "folder_missing", "Папку не знайдено"),
        "10": ("new", None, None),
    }


def test_folder_missing_only_for_new_and_problem(conn, posts, root):
    for name in ("10", "11", "Сукня"):
        make_folder(root, name, ["1.jpg"])
    run(conn, posts, root)
    with conn:
        conn.execute("UPDATE items SET status = ? WHERE folder_name = '11'", (Status.REVIEW,))
    for name in ("10", "11", "Сукня"):
        for file in (root / name).iterdir():
            file.unlink()
        (root / name).rmdir()

    result = run(conn, posts, root)

    assert items(conn) == {
        "10": ("problem", "folder_missing", "Папку не знайдено"),
        "11": ("review", None, None),
        "Сукня": ("problem", "folder_missing", "Папку не знайдено"),
    }
    assert (result.folders, result.new, len(result.problems)) == (0, 0, 2)


def test_scan_leaves_cards_in_work_alone(conn, posts, root):
    folder = make_folder(root, "10", ["1.jpg"])
    run(conn, posts, root)
    with conn:
        conn.execute("UPDATE items SET status = ?", (Status.APPROVED,))
    (folder / "1.jpg").unlink()

    run(conn, posts, root)

    assert items(conn)["10"] == ("approved", None, None)
    assert photos(conn, "10") == ["1.jpg"]


def test_bot_posts_not_overwritten(conn, posts, root):
    with conn:
        conn.execute("INSERT INTO posts VALUES (10, 'd', 'від бота', 'bot', 'x')")
    make_folder(root, "10", ["1.jpg"])

    run(conn, posts, root)

    assert conn.execute("SELECT text FROM posts WHERE post_id = 10").fetchone()[0] == "від бота"


def test_photos_updated_for_new_card(conn, posts, root):
    folder = make_folder(root, "10", ["IMG_2.JPG"])
    run(conn, posts, root)
    (folder / "IMG_1.JPG").write_bytes(b"")

    run(conn, posts, root)

    assert photos(conn, "10") == ["IMG_1.JPG", "IMG_2.JPG"]


def rescan(conn, root, name):
    return rescan_folder(conn, name, list_folders(root, CHANNEL_ID))


def test_rescan_one_folder_fixes_problem_and_leaves_others_alone(conn, posts, root):
    make_folder(root, "10", [])  # немає фото → Проблема
    make_folder(root, "11", ["1.jpg"])
    make_folder(root, "Сукня", ["1.jpg"])
    run(conn, posts, root)
    (root / "11" / "1.jpg").unlink()
    (root / "11").rmdir()  # папку 11 прибрали, але повного scan не було
    (root / "10" / "1.jpg").write_bytes(b"")

    assert rescan(conn, root, "10") == Status.NEW

    assert items(conn) == {
        "10": ("new", None, None),
        "11": ("new", None, None),  # не folder_missing: інші Картки не перевіряються
        "Сукня": ("problem", "bad_folder_name", "Назва папки не номер поста"),
    }
    assert photos(conn, "10") == ["1.jpg"]


def test_rescan_uses_post_texts_from_db(conn, posts, root):
    make_folder(root, "999", ["1.jpg"])
    run(conn, posts, root)
    with conn:
        conn.execute("INSERT INTO posts VALUES (999, 'd', 'Кеди 500грн', 'bot', 'x')")

    assert rescan(conn, root, "999") == Status.NEW


def test_rescan_finds_twin_folders(conn, posts, root):
    make_folder(root, "10", ["1.jpg"])
    run(conn, posts, root)
    with conn:
        conn.execute("UPDATE items SET status = 'problem', problem_code = 'ai_error'")
    make_folder(root, CHANNEL_ID + "10", ["1.jpg"])

    assert rescan(conn, root, "10") == Status.PROBLEM
    assert items(conn)["10"][1] == "duplicate_post"
    assert CHANNEL_ID + "10" not in items(conn)  # чужа папка не додається


def test_rescan_missing_folder(conn, posts, root):
    make_folder(root, "Сукня", ["1.jpg"])
    run(conn, posts, root)
    (root / "Сукня" / "1.jpg").unlink()
    (root / "Сукня").rmdir()

    assert rescan(conn, root, "Сукня") == Status.PROBLEM
    assert items(conn)["Сукня"] == ("problem", "folder_missing", "Папку не знайдено")


@pytest.mark.parametrize("status", [Status.REVIEW, Status.APPROVED, Status.ARCHIVED])
def test_rescan_leaves_cards_in_work_alone(conn, posts, root, status):
    make_folder(root, "10", ["1.jpg"])
    run(conn, posts, root)
    with conn:
        conn.execute("UPDATE items SET status = ?", (status,))

    assert rescan(conn, root, "10") is None
    assert rescan(conn, root, "нема") is None
    assert items(conn)["10"] == (status, None, None)


def test_published_folder_is_not_a_product_folder(conn, posts, root):
    make_folder(root, "10", ["1.jpg"])
    make_folder(root, "Опубліковано/11", ["1.jpg"])

    result = run(conn, posts, root)

    assert items(conn) == {"10": ("new", None, None)}
    assert (result.folders, result.problems) == (1, [])


@pytest.mark.parametrize(
    "code",
    ["publish_uncertain", "possible_duplicate", "publish_error", "robot_field_missing",
     "robot_brand_missing", "robot_not_sent"],
)  # fmt: skip
def test_scan_never_resets_publication_or_robot_problem(conn, posts, root, code):
    """Інакше Картка пішла б у new → ШІ → перевірка → друга публікація (D2)."""
    folder = make_folder(root, "10", ["1.jpg"])
    run(conn, posts, root)
    with conn:
        conn.execute(
            "UPDATE items SET status = 'problem', problem_code = ?, problem_text = 'т'", (code,)
        )

    run(conn, posts, root)
    assert items(conn)["10"] == ("problem", code, "т")
    assert rescan(conn, root, "10") is None
    for file in folder.iterdir():
        file.unlink()
    folder.rmdir()
    run(conn, posts, root)
    assert rescan(conn, root, "10") is None

    assert items(conn)["10"] == ("problem", code, "т")
