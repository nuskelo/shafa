"""Старий борг (CORE-005): пости з Експорту з фото → Картки `borh-<номер>` з одним фото."""

import json

import pytest
from PIL import Image

from conftest import make_folder
from shafa import borh, fill
from shafa.__main__ import main
from shafa.db import Status
from shafa.export import parse_export, read_export
from shafa.folders import item_folder, list_folders, old_root
from shafa.robot.publish import move_published
from shafa.scan import rescan_folder, scan
from test_photos import make_photo

TEXT = "Шкіряні черевики Ecco 38 розміру. В ідеальному стані. 1200грн"
NOT_INCLUDED = "(File not included. Change data exporting settings to download.)"


def message(post_id, text=TEXT, photo=None):
    m = {"id": post_id, "type": "message", "date": "2024-05-01T10:00:00", "text": text}
    if photo is not None:
        m["photo"] = photo
    return m


@pytest.fixture
def export_dir(tmp_path):
    """Експорт з фото: `result.json` + `photos/` (вигадані пости й згенеровані фото)."""
    folder = tmp_path / "Експорт каналу"
    (folder / "photos").mkdir(parents=True)
    make_photo(folder / "photos" / "photo_1@01-05-2024_10-00-00.jpg", (600, 800))
    Image.new("RGB", (300, 400), "green").save(folder / "photos" / "png-inside.jpg", format="PNG")
    (folder / "photos" / "broken.jpg").write_bytes(b"not a photo")
    messages = [
        {"id": 1, "type": "service", "date": "2024-05-01T09:00:00", "text": ""},
        message(100, photo="photos/photo_1@01-05-2024_10-00-00.jpg"),
        message(101, photo=NOT_INCLUDED),
        message(102),  # без фото
        message(103, photo="photos/немає.jpg"),
        message(104, text="Черевики 38 розміру", photo="photos/photo_1@01-05-2024_10-00-00.jpg"),
        message(105, photo="photos/png-inside.jpg"),
        message(106, photo="photos/broken.jpg"),
    ]
    data = {"name": "Канал", "type": "private_channel", "id": 1, "messages": messages}
    (folder / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return folder


@pytest.fixture
def posts(export_dir):
    return read_export(export_dir / "result.json")


def run(conn, posts, export_dir, numbers):
    return borh.import_old(conn, posts, export_dir, numbers)


def item(conn, post_id):
    return conn.execute("SELECT * FROM items WHERE post_id = ?", (post_id,)).fetchone()


def photo_names(conn, item_id):
    return [
        r[0] for r in conn.execute("SELECT file_name FROM photos WHERE item_id = ?", (item_id,))
    ]


# --- Експорт ---


def test_export_reads_photo_path(posts):
    assert posts[100].photo == "photos/photo_1@01-05-2024_10-00-00.jpg"
    assert posts[101].photo is None  # експорт без медіа — пояснення замість шляху
    assert posts[102].photo is None


def test_export_without_photos_works_as_before(export_path):
    posts = read_export(export_path)
    assert posts[10].text == "Сукня літня, розмір M. Ціна 500 грн"
    assert all(p.photo is None for p in posts.values())
    raw = json.dumps({"messages": [message(5, photo=7)]}).encode()
    assert parse_export(raw)[5].photo is None


# --- Список ---


def test_read_list(tmp_path):
    path = tmp_path / "список.txt"
    path.write_text("﻿# старі пости\n100\n\n  101  \n100\n102 # коментар\n", encoding="utf-8")
    assert borh.read_list(path) == [100, 101, 102]


def test_read_list_rejects_not_numbers(tmp_path):
    path = tmp_path / "список.txt"
    path.write_text("100\nпост 101\n", encoding="utf-8")
    with pytest.raises(borh.ListError, match="рядок 2"):
        borh.read_list(path)


# --- Імпорт ---


def test_import_creates_card_with_copied_photo(conn, posts, export_dir):
    result = run(conn, posts, export_dir, [100])

    assert result.created == [100]
    row = item(conn, 100)
    assert (row["folder_name"], row["status"]) == ("borh-100", Status.NEW)
    assert photo_names(conn, row["id"]) == ["1.jpg"]
    copied = old_root() / "100" / "1.jpg"
    assert copied.read_bytes() == (export_dir / posts[100].photo).read_bytes()
    assert item_folder(export_dir, "borh-100") == old_root() / "100"
    post = conn.execute("SELECT date, text, source FROM posts WHERE post_id = 100").fetchone()
    assert tuple(post) == ("2024-05-01T10:00:00", TEXT, "export")


def test_photo_stays_after_export_is_gone(conn, posts, export_dir):
    run(conn, posts, export_dir, [100])
    for path in (export_dir / "photos").iterdir():
        path.unlink()
    assert (old_root() / "100" / "1.jpg").is_file()


def test_rerun_does_not_duplicate(conn, posts, export_dir):
    run(conn, posts, export_dir, [100])
    result = run(conn, posts, export_dir, [100])
    assert (result.created, result.existed) == ([], [100])
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_no_photo_is_a_problem(conn, posts, export_dir):
    result = run(conn, posts, export_dir, [101, 102, 103, 106])

    assert result.created == result.no_photo == [101, 102, 103, 106]
    for post_id in (101, 102, 103, 106):
        row = item(conn, post_id)
        assert (row["status"], row["problem_code"]) == (Status.PROBLEM, "no_photos")
        assert row["problem_text"] == "У пості немає фото"
        assert photo_names(conn, row["id"]) == []
    assert not (old_root() / "106").exists()


def test_not_jpeg_inside_is_converted(conn, posts, export_dir):
    run(conn, posts, export_dir, [105])
    with Image.open(old_root() / "105" / "1.jpg") as image:
        assert (image.format, image.size) == ("JPEG", (300, 400))


def test_text_rules_apply(conn, posts, export_dir):
    result = run(conn, posts, export_dir, [104])
    row = item(conn, 104)
    assert (row["status"], row["problem_code"]) == (Status.PROBLEM, "no_price")
    assert result.problems == [(104, "У поста 104 немає ціни")]


def test_not_in_export(conn, posts, export_dir):
    result = run(conn, posts, export_dir, [999, 100])
    assert result.not_in_export == [999]
    assert result.created == [100]
    assert "немає в експорті: 1" in result.text() and "999" in result.text()


def test_known_posts_are_skipped(conn, posts, export_dir, root):
    make_folder(root, "100", ["1.jpg"])
    scan(conn, posts, list_folders(root, None))  # папка в Головній
    with conn:  # пост від бота (папка ще не зіставлена)
        conn.execute("UPDATE posts SET source = 'bot' WHERE post_id = 103")

    result = run(conn, posts, export_dir, [100, 103])

    assert result.existed == [100, 103]
    assert result.created == []
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1


def test_summary_text(conn, posts, export_dir):
    run(conn, posts, export_dir, [100])
    result = run(conn, posts, export_dir, [100, 102, 999])
    assert result.text().splitlines()[0] == (
        "Створено: 1 · вже було: 1 · немає в експорті: 1 · без фото: 1"
    )


# --- Разом зі скануванням Головної ---


def test_scan_leaves_old_cards_alone(conn, posts, export_dir, root):
    run(conn, posts, export_dir, [100, 102])
    scan(conn, posts, list_folders(root, None))
    assert item(conn, 100)["status"] == Status.NEW
    assert item(conn, 102)["problem_code"] == "no_photos"  # не «Папку не знайдено»


def test_folder_for_old_post_is_a_problem(conn, posts, export_dir, root):
    run(conn, posts, export_dir, [100])
    make_folder(root, "100", ["1.jpg"])
    scan(conn, posts, list_folders(root, None))
    row = conn.execute("SELECT * FROM items WHERE folder_name = '100'").fetchone()
    assert (row["status"], row["problem_code"]) == (Status.PROBLEM, "duplicate_post")
    assert "уже завантажено як старий" in row["problem_text"]


def test_borh_names_in_main_folder_are_reserved(root):
    make_folder(root, "borh-100", ["1.jpg"])
    assert list_folders(root, None) == []


def test_recheck_old_card(conn, posts, export_dir, root):
    run(conn, posts, export_dir, [103])
    assert item(conn, 103)["status"] == Status.PROBLEM
    (old_root() / "103").mkdir(parents=True)
    make_photo(old_root() / "103" / "1.jpg", (400, 300))  # фото поклали руками

    assert rescan_folder(conn, "borh-103", []) == Status.NEW
    assert photo_names(conn, item(conn, 103)["id"]) == ["1.jpg"]


# --- Фото старої Картки для ШІ, екрана й робота ---


def test_fill_reads_photo_from_data_dir(conn, posts, export_dir, root):
    run(conn, posts, export_dir, [100])
    planned = fill.plan(conn, root, item(conn, 100))
    assert isinstance(planned, fill.Planned)
    assert [p.file_name for p in planned.photos] == ["1.jpg"]


def test_published_old_card_is_not_moved(root):
    assert move_published(root, "borh-100") is None


def test_cli_import_old(tmp_path, export_dir, capsys):
    numbers = tmp_path / "список.txt"
    numbers.write_text("100\n102\n999\n", encoding="utf-8")
    base = tmp_path / "база.sqlite"

    assert main(["import-old", "--export", str(export_dir), "--list", str(numbers),
                 "--db", str(base)]) == 0  # fmt: skip

    out = capsys.readouterr().out
    assert "Створено: 2 · вже було: 0 · немає в експорті: 1 · без фото: 1" in out


def test_cli_import_old_bad_list(tmp_path, export_dir):
    numbers = tmp_path / "список.txt"
    numbers.write_text("сто\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["import-old", "--export", str(export_dir), "--list", str(numbers),
              "--db", str(tmp_path / "база.sqlite")])  # fmt: skip


def test_screen_shows_old_card_photo(conn, posts, export_dir, root, tmp_path):
    from shafa import web
    from test_web import PORT, TOKEN, get

    run(conn, posts, export_dir, [100])
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)
    item_id = item(conn, 100)["id"]

    response = get(app, f"/photo/{item_id}/1")
    assert response.status == 200
    assert response.body == (old_root() / "100" / "1.jpg").read_bytes()
    assert get(app, f"/thumb/{item_id}/1").status == 200


def test_robot_plan_takes_photo_from_data_dir(conn, posts, export_dir, root):
    from shafa import review
    from shafa.robot.plan import plan
    from test_robot_plan import BLANK

    run(conn, posts, export_dir, [100])
    item_id = item(conn, 100)["id"]
    blank = [f for f in BLANK if f.name != "photo_order"]
    fill.save_blank(conn, item_id, [*blank, fill.ai.Field("photo_order", ["1.jpg"])])
    with conn:
        conn.execute("UPDATE items SET status = ? WHERE id = ?", (Status.APPROVED, item_id))

    steps, _ = plan(review.load_card(conn, item_id), root)

    assert steps[0].value == [str(old_root() / "100" / "1.jpg")]


# --- Порядок у списку (CORE-006) ---


def positions(conn):
    return {
        r["post_id"]: r["queue_pos"] for r in conn.execute("SELECT post_id, queue_pos FROM items")
    }


def test_import_records_list_position(conn, posts, export_dir):
    run(conn, posts, export_dir, [104, 100, 105])

    assert positions(conn) == {104: 1, 100: 2, 105: 3}


def test_second_import_reorders_unpublished_without_duplicates(conn, posts, export_dir):
    run(conn, posts, export_dir, [100, 104, 105])

    result = run(conn, posts, export_dir, [105, 100, 104])

    assert positions(conn) == {105: 1, 100: 2, 104: 3}
    assert result.created == [] and result.reordered == 3
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 3


def test_second_import_does_not_touch_published_or_non_old(conn, posts, export_dir):
    run(conn, posts, export_dir, [100, 104])
    with conn:
        conn.execute("UPDATE items SET status = 'published' WHERE post_id = 100")
        conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES ('105', 105, 'new', 'x', 'x')"
        )  # Картка з папки, не старий борг

    run(conn, posts, export_dir, [104, 105, 100])

    assert positions(conn) == {100: 1, 104: 1, 105: None}


def test_unchanged_order_reports_nothing_reordered(conn, posts, export_dir):
    run(conn, posts, export_dir, [100, 104])

    assert run(conn, posts, export_dir, [100, 104]).reordered == 0
