"""CORE-015 (TD-004): пост чи фото змінено після того, як Картка вже зроблена."""

import json
from datetime import UTC, date, datetime

import pytest

from conftest import CHANNEL_ID, make_folder
from shafa import ai, changes, fill, review
from shafa.bot import core, report
from shafa.bot.core import Bot, Settings
from shafa.db import Status
from shafa.folders import list_folders
from shafa.scan import scan
from test_ai import answer
from test_catalog import WOMEN_SNEAKERS
from test_core009 import blank, stale_card
from test_review import add_card

OLD = "Кросівки Asics 38 розміру. 1200грн"  # ціна на Шафі 1380
NEW = "Кросівки Asics 38 розміру. 800грн"  # 920
NOON = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
CHANNEL = -1002394827824
PORT = 8799


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    """Жодного запиту до ШІ: перерахунок іде зі збереженої відповіді."""

    def boom(*args, **kwargs):
        raise AssertionError("запит до ШІ")

    monkeypatch.setattr(ai, "call", boom)


def make(conn, root, status=Status.REVIEW, text=OLD, folder="1342", post_id=1342):
    stored = answer(section=WOMEN_SNEAKERS, sizes=["38"], material_upper=[])
    return stale_card(
        conn, root, folder=folder, post_id=post_id, text=text, answer_=stored, status=status
    )


def row(conn, item_id):
    return conn.execute(
        "SELECT status, problem_code, problem_text FROM items WHERE id = ?", (item_id,)
    ).fetchone()


def edit(conn, text=NEW, post_id=1342, **kwargs):
    return changes.text_changed(conn, post_id, text, moment=NOON, **kwargs)


def price(conn, item_id):
    return review.load_card(conn, item_id).fields["price"].value


# --- 1. Текст поста ---


def test_review_gets_new_text_price_and_note(conn, root):
    item_id = make(conn, root)
    assert price(conn, item_id) == 1380

    result = edit(conn)

    assert result.cards == [item_id]
    assert conn.execute("SELECT text FROM posts WHERE post_id = 1342").fetchone()[0] == NEW
    assert price(conn, item_id) == 920  # нова ціна за правилом +15% до десятка
    status, code, text = row(conn, item_id)
    assert (status, code) == (Status.REVIEW, review.POST_CHANGED)
    assert text == "Пост змінено 21.10.2026: Ціна на Шафі, грн: 1380 → 920"


def test_review_blank_fields_follow_new_text_without_ai(conn, root):
    item_id = make(conn, root)

    edit(conn, "Кеди Vans. Вказаний 38 розмір, ідуть на 37. 1200грн")

    assert blank(conn, item_id)["sizes"][0] == ["37"]  # «вказаний X, ідуть на Y» → Y
    assert "Розмір: 38 → 37" in row(conn, item_id)["problem_text"]


def test_approved_goes_back_to_review_with_same_note(conn, root):
    item_id = make(conn, root, status=Status.APPROVED)

    edit(conn)

    assert tuple(row(conn, item_id)) == (
        Status.REVIEW,
        review.POST_CHANGED,
        "Пост змінено 21.10.2026: Ціна на Шафі, грн: 1380 → 920",
    )


def test_approved_text_without_price_goes_to_problems_with_note(conn, root):
    item_id = make(conn, root, status=Status.APPROVED)

    edit(conn, "Кросівки Asics 38 розміру")

    status, code, text = row(conn, item_id)
    assert (status, code) == (Status.PROBLEM, "no_price")
    assert text.startswith("У поста 1342 немає ціни. Пост змінено 21.10.2026")


def test_many_items_after_edit_is_a_problem(conn, root):
    item_id = make(conn, root)

    edit(conn, "Кросівки Asics 38 розміру. 1200грн\nКеди Vans 40 розміру. 900грн")

    assert row(conn, item_id)["problem_code"] == "many_items"


@pytest.mark.parametrize("status", [Status.NEW, Status.PROBLEM])
def test_new_and_problem_only_get_the_text(conn, root, status):
    item_id = make(conn, root, status=status)
    before = tuple(row(conn, item_id))
    blanks = blank(conn, item_id)

    result = edit(conn)

    assert result.cards == [] and tuple(row(conn, item_id)) == before
    assert blank(conn, item_id) == blanks  # ШІ побачить новий текст у наступному пакеті
    assert conn.execute("SELECT text FROM posts WHERE post_id = 1342").fetchone()[0] == NEW


@pytest.mark.parametrize("status", [Status.PUBLISHING, Status.ARCHIVED])
def test_publishing_and_archived_are_not_touched(conn, root, status):
    item_id = make(conn, root, status=status)
    before = tuple(row(conn, item_id))

    result = edit(conn)

    assert result == changes.TextChange() and tuple(row(conn, item_id)) == before


def test_published_keeps_status_and_is_reported(conn, root):
    item_id = make(conn, root, status=Status.PUBLISHED)

    result = edit(conn)

    assert result.cards == [] and result.published == [(1342, "Ціна на Шафі, грн: 1380 → 920")]
    assert tuple(row(conn, item_id)) == (Status.PUBLISHED, None, None)


def test_same_edit_twice_does_nothing(conn, root):
    item_id = make(conn, root)
    edit(conn)
    stamp = conn.execute("SELECT updated_at FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    note = tuple(row(conn, item_id))

    again = edit(conn)

    assert again == changes.TextChange()
    assert tuple(row(conn, item_id)) == note
    assert (
        conn.execute("SELECT updated_at FROM items WHERE id = ?", (item_id,)).fetchone()[0] == stamp
    )


def test_second_edit_adds_to_unseen_note(conn, root):
    item_id = make(conn, root)
    edit(conn)

    edit(conn, "Кросівки Asics 38 розміру. 700грн")

    text = row(conn, item_id)["problem_text"]
    assert "1380 → 920" in text and "920 → 810" in text and " · " in text


def test_unknown_post_is_ignored(conn, root):
    make(conn, root)
    assert edit(conn, post_id=999) == changes.TextChange()


def test_export_post_ignored_when_old_posts_not_allowed(conn, root):
    item_id = make(conn, root)

    assert edit(conn, allow_old=False) == changes.TextChange()
    assert price(conn, item_id) == 1380


def test_approve_clears_the_note(conn, root):
    item_id = make(conn, root, status=Status.APPROVED)
    edit(conn)

    assert review.act(conn, item_id, "approve", root) == "approved"

    assert tuple(row(conn, item_id)) == (Status.APPROVED, None, None)


def test_recheck_keeps_the_note(conn, root):
    item_id = make(conn, root)
    edit(conn)
    with conn:
        conn.execute("UPDATE items SET rules_version = 1 WHERE id = ?", (item_id,))

    assert fill.recheck(conn) == 1

    assert row(conn, item_id)["problem_code"] == review.POST_CHANGED


def test_note_shows_on_card_and_list(conn, root, tmp_path):
    from shafa import web  # екран: примітка в Картці й значок у списку

    item_id = make(conn, root)
    edit(conn)
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token="t")

    def get(path):
        request = web.Request("GET", path, {}, {}, {"host": f"127.0.0.1:{PORT}"})
        return app.handle(request).body.decode("utf-8")

    assert "Пост змінено 21.10.2026" in get(f"/item/{item_id}")
    assert 'class="badge changed"' in get("/review")


# --- Бот: edited_channel_post ---


class Tg:
    pass


def make_bot(conn, root, export=CHANNEL):
    log = []
    settings = Settings(CHANNEL, 111, 222, root, export_channel_id=export)
    return Bot(conn, Tg(), settings, log=log.append, sleep=lambda s: None), log


def channel_edit(bot, update_id, text, post_id=1342, chat=CHANNEL):
    bot.handle(
        {
            "update_id": update_id,
            "edited_channel_post": {"chat": {"id": chat}, "message_id": post_id, "caption": text},
        }
    )


def test_bot_channel_edit_updates_card(conn, root):
    item_id = make(conn, root)
    bot, log = make_bot(conn, root)

    channel_edit(bot, 1, NEW)

    assert price(conn, item_id) == 920
    assert row(conn, item_id)["problem_code"] == review.POST_CHANGED
    assert any("Картки оновлено" in line for line in log)


def test_bot_other_channel_does_nothing(conn, root):
    item_id = make(conn, root)
    bot, _ = make_bot(conn, root)

    channel_edit(bot, 1, NEW, chat=-100555)

    assert price(conn, item_id) == 1380


def test_bot_old_post_skipped_when_bot_channel_is_not_export_channel(conn, root):
    """Бот у тестовому каналі: номер повідомлення там міг збігтися зі старим постом з Експорту."""
    item_id = make(conn, root)
    bot, _ = make_bot(conn, root, export=-100777)

    channel_edit(bot, 1, NEW)

    assert price(conn, item_id) == 1380 and row(conn, item_id)["problem_code"] is None


def test_bot_post_always_updates(conn, root):
    item_id = make(conn, root)
    with conn:
        conn.execute("UPDATE posts SET source = 'bot'")
    bot, _ = make_bot(conn, root, export=None)

    channel_edit(bot, 1, NEW)

    assert price(conn, item_id) == 920


def test_bot_published_change_goes_to_log_and_daily_report(conn, root, tmp_path):
    make(conn, root, status=Status.PUBLISHED)
    bot, log = make_bot(conn, root)

    channel_edit(bot, 1, NEW)

    line = "Пост 1342 змінено після публікації на Шафі: Ціна на Шафі, грн: 1380 → 920"
    assert line in log
    today = datetime.now(UTC).astimezone().date()
    assert line in report.build(conn, core.config.data_dir(), today)
    assert report.build(conn, core.config.data_dir(), date(2000, 1, 1)).count("змінено після") == 0
    assert (
        json.loads((core.config.data_dir() / changes.LOG_NAME).read_text(encoding="utf-8"))[0][
            "post"
        ]
        == 1342
    )


def test_bot_survives_broken_change(conn, root, monkeypatch):
    make(conn, root)
    bot, log = make_bot(conn, root)

    def broken(*args, **kwargs):
        raise RuntimeError("база зайнята")

    monkeypatch.setattr(changes, "text_changed", broken)
    channel_edit(bot, 1, NEW)

    assert any("не застосовано" in line for line in log)


# --- 2. Фото в папці (TD-004) ---


def folders(root):
    return list_folders(root, str(CHANNEL_ID))


def photo_names(conn, item_id):
    return [
        r[0]
        for r in conn.execute(
            "SELECT file_name FROM photos WHERE item_id = ? ORDER BY position", (item_id,)
        )
    ]


def card_in_folder(conn, root, status=Status.REVIEW):
    item_id = add_card(conn, root, status=status)
    with conn:
        fill.save_answer(conn, item_id, answer(section=WOMEN_SNEAKERS, sizes=["38"]))
    return item_id


def test_added_photo_updates_list_and_note(conn, root):
    item_id = card_in_folder(conn, root)
    (root / "1342" / "IMG_3.JPG").write_bytes(b"")

    result = changes.sync_photos(conn, folders(root), moment=NOON)

    assert photo_names(conn, item_id) == ["IMG_1.JPG", "IMG_2.JPG", "IMG_3.JPG"]
    assert result.changed == [(1342, "було 2, стало 3 (додано IMG_3.JPG)")]
    assert tuple(row(conn, item_id)) == (
        Status.REVIEW,
        review.PHOTOS_CHANGED,
        "Фото змінено 21.10.2026: було 2, стало 3 (додано IMG_3.JPG)",
    )
    assert [p.file_name for p in review.load_card(conn, item_id).photos][-1] == "IMG_3.JPG"


def test_removed_photo_approved_goes_back_to_review(conn, root):
    item_id = card_in_folder(conn, root, status=Status.APPROVED)
    (root / "1342" / "IMG_2.JPG").unlink()

    changes.sync_photos(conn, folders(root), moment=NOON)

    assert photo_names(conn, item_id) == ["IMG_1.JPG"]
    assert tuple(row(conn, item_id))[:2] == (Status.REVIEW, review.PHOTOS_CHANGED)
    assert "прибрано IMG_2.JPG" in row(conn, item_id)["problem_text"]


def test_photos_unchanged_nothing_happens(conn, root):
    item_id = card_in_folder(conn, root)
    before = tuple(row(conn, item_id))

    result = changes.sync_photos(conn, folders(root), moment=NOON)

    assert result == changes.PhotoSync() and tuple(row(conn, item_id)) == before


def test_folder_deleted_is_robot_photos_problem(conn, root):
    item_id = card_in_folder(conn, root, status=Status.APPROVED)
    other = make_folder(root, "1500", ["1.jpg"])
    for file in (root / "1342").iterdir():
        file.unlink()
    (root / "1342").rmdir()

    result = changes.sync_photos(conn, folders(root))

    assert other.exists() and result.lost == [(1342, changes.NO_FOLDER)]
    assert tuple(row(conn, item_id)) == (Status.PROBLEM, "robot_photos", changes.NO_FOLDER)
    assert blank(conn, item_id)  # бланк цілий


def test_no_photos_left_is_robot_photos_problem(conn, root):
    item_id = card_in_folder(conn, root)
    for file in (root / "1342").iterdir():
        file.unlink()

    changes.sync_photos(conn, folders(root))

    assert tuple(row(conn, item_id)) == (Status.PROBLEM, "robot_photos", changes.NO_PHOTOS)
    assert photo_names(conn, item_id) == ["IMG_1.JPG", "IMG_2.JPG"]


def test_no_folders_at_all_means_home_folder_unavailable(conn, root):
    item_id = card_in_folder(conn, root)
    before = tuple(row(conn, item_id))

    assert changes.sync_photos(conn, []) == changes.PhotoSync()
    assert tuple(row(conn, item_id)) == before


@pytest.mark.parametrize("status", [Status.PUBLISHING, Status.PUBLISHED, Status.ARCHIVED])
def test_photos_of_other_statuses_not_touched(conn, root, status):
    item_id = card_in_folder(conn, root, status=status)
    (root / "1342" / "IMG_3.JPG").write_bytes(b"")

    assert changes.sync_photos(conn, folders(root)) == changes.PhotoSync()
    assert photo_names(conn, item_id) == ["IMG_1.JPG", "IMG_2.JPG"]


def test_old_posts_cards_are_skipped(conn, root):
    item_id = card_in_folder(conn, root)
    with conn:
        conn.execute("UPDATE items SET folder_name = 'borh-1342' WHERE id = ?", (item_id,))
    make_folder(root, "1500", ["1.jpg"])

    assert changes.sync_photos(conn, folders(root)) == changes.PhotoSync()


def test_scan_runs_the_photo_check(conn, root):
    item_id = card_in_folder(conn, root, status=Status.APPROVED)
    (root / "1342" / "IMG_3.JPG").write_bytes(b"")

    scan(conn, {}, folders(root))

    assert photo_names(conn, item_id)[-1] == "IMG_3.JPG"
    assert row(conn, item_id)["status"] == Status.REVIEW
