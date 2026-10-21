"""PHOTO-001 (TD-030): фото старих постів видаляються через 3 дні після публікації."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from shafa import borh, changes, config
from shafa.bot import report
from shafa.db import Status
from shafa.folders import Folder
from shafa.robot import publish
from test_robot_publish import Env

NOW = datetime(2026, 10, 21, 12, 0).astimezone()


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "дані"
    folder.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setenv("SHAFA_DELETE_PUBLISHED", "1")
    return folder


@pytest.fixture
def env(conn, root, tmp_path, data):
    env = Env(conn, root, tmp_path)
    env.today = NOW
    return env


def days_ago(days: float) -> str:
    return (NOW - timedelta(days=days)).astimezone(UTC).isoformat(timespec="seconds")


def old_card(conn, data, post_id, *, days=4.0, status=Status.PUBLISHED, name=None, photo=True):
    """Картка старого поста й тека `<дані>/borh/<номер>/1.jpg`."""
    name = name or f"borh-{post_id}"
    folder = data / "borh" / str(post_id)
    if photo:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "1.jpg").write_bytes(b"jpg")
    with conn:
        item_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at, published_at) "
            "VALUES (?, ?, ?, 'x', 'x', ?)",
            (name, post_id, status, days_ago(days) if status == Status.PUBLISHED else None),
        ).lastrowid
    return item_id


def purge(conn, today=NOW):
    return publish.purge_old_photos(conn, today)


def exists(data, post_id) -> bool:
    return (data / "borh" / str(post_id)).is_dir()


# --- Коли видаляємо ---


def test_four_days_removed_two_days_kept(conn, data):
    old_card(conn, data, 100, days=4)
    old_card(conn, data, 200, days=2)

    assert purge(conn) == ["borh-100"]
    assert not exists(data, 100) and exists(data, 200)


def test_disabled_setting_removes_nothing(conn, data, monkeypatch):
    old_card(conn, data, 100)
    monkeypatch.delenv("SHAFA_DELETE_PUBLISHED")
    assert purge(conn) == [] and exists(data, 100)


@pytest.mark.parametrize(
    "status", [Status.NEW, Status.PROBLEM, Status.REVIEW, Status.APPROVED, Status.ARCHIVED]
)
def test_not_published_never_removed(conn, data, status):
    old_card(conn, data, 100, status=status)
    with conn:  # навіть якщо в записі лишилась дата публікації
        conn.execute("UPDATE items SET published_at = ?", (days_ago(30),))
    assert purge(conn) == [] and exists(data, 100)


def test_card_stays_published_and_visible(conn, data):
    item_id = old_card(conn, data, 100)
    purge(conn)
    row = conn.execute("SELECT status, purged_at FROM items WHERE id = ?", (item_id,)).fetchone()
    assert (row["status"], row["purged_at"]) == (
        Status.PUBLISHED,
        None,
    )  # у «Опублікованих» лишається
    assert purge(conn) == []  # вдруге — немає чого видаляти


def test_other_folders_untouched(conn, data, root):
    old_card(conn, data, 100)
    (data / "borh" / "999").mkdir()  # теки без Картки не чіпаємо
    (root / "Опубліковано" / "100").mkdir(parents=True)
    (data / "Опубліковано").mkdir()
    purge(conn)
    assert (data / "borh" / "999").is_dir()
    assert (root / "Опубліковано" / "100").is_dir() and (data / "Опубліковано").is_dir()


def test_bot_and_folder_cards_not_in_borh(conn, data):
    """Картка з папкою в Головній (не `borh-…`) цим прибиранням не зачіпається."""
    old_card(conn, data, 100, name="100")
    assert purge(conn) == [] and exists(data, 100)


# --- Варіанти одного поста (CORE-014) ---


def test_variants_removed_only_when_all_old(conn, data):
    old_card(conn, data, 300, days=5, name="borh-300~36")
    later = old_card(conn, data, 300, days=1, name="borh-300~37", photo=False)  # пізніша — 1 день

    assert purge(conn) == [] and exists(data, 300)
    with conn:
        conn.execute("UPDATE items SET published_at = ? WHERE id = ?", (days_ago(4), later))
    assert purge(conn) == ["borh-300"]  # тека одна, лічимо один раз
    assert not exists(data, 300)


def test_variant_not_yet_published_keeps_folder(conn, data):
    old_card(conn, data, 300, days=9, name="borh-300~36")
    old_card(conn, data, 300, status=Status.APPROVED, name="borh-300~37", photo=False)
    assert purge(conn) == [] and exists(data, 300)


# --- Лише всередині <дані>/borh/ ---


def test_path_outside_borh_never_removed(conn, data):
    outside = data / "чуже"
    outside.mkdir()
    (outside / "1.jpg").write_bytes(b"jpg")
    for name in ("borh-../чуже", "borh-1/../../чуже", "borh-", "borh-12x"):
        old_card(conn, data, abs(hash(name)) % 10**6 + 1000, name=name, photo=False)

    assert purge(conn) == [] and (outside / "1.jpg").is_file()


def test_symlink_to_outside_not_followed(conn, data, tmp_path):
    outside = tmp_path / "чуже"
    outside.mkdir()
    (outside / "1.jpg").write_bytes(b"jpg")
    (data / "borh").mkdir()
    try:
        (data / "borh" / "100").symlink_to(outside, target_is_directory=True)
    except OSError:  # Windows без прав на символьні посилання
        pytest.skip("символьні посилання недоступні")
    old_card(conn, data, 100, photo=False)

    assert purge(conn) == [] and (outside / "1.jpg").is_file()


# --- Після прибирання нічого не шукає фото як помилку ---


def test_after_purge_no_photo_errors(conn, data, root):
    item_id = old_card(conn, data, 100)
    purge(conn)

    sync = changes.sync_photos(conn, [Folder("5000", 5000, ("1.jpg",))])
    assert (sync.changed, sync.lost) == ([], [])
    row = conn.execute("SELECT status, problem_code FROM items WHERE id = ?", (item_id,)).fetchone()
    assert (row["status"], row["problem_code"]) == (Status.PUBLISHED, None)


def test_repeat_import_old_does_not_restore_photo(conn, data):
    """`import-old` пропускає пост, який уже є в базі, тож стерте фото не повертається."""
    old_card(conn, data, 100)
    purge(conn)
    assert borh._known(conn, 100) and not exists(data, 100)


# --- Сесія робота і звіт ---


def test_session_purges_and_notes(env, data):
    old_card(env.conn, data, 100)
    old_card(env.conn, data, 200)

    report_ = env.run()
    assert "Прибрано фото старих постів: 2" in report_.notes
    assert not exists(data, 100) and not exists(data, 200)


def test_session_without_setting_keeps_photos(env, data, monkeypatch):
    old_card(env.conn, data, 100)
    monkeypatch.delenv("SHAFA_DELETE_PUBLISHED")
    env.run()
    assert exists(data, 100)


def test_daily_report_line(conn, data):
    old_card(conn, data, 100)
    old_card(conn, data, 200)
    purge(conn)
    day = NOW.date()

    assert "Прибрано фото: 2 пости" in report.build(conn, data, day)
    assert "Прибрано фото" not in report.build(conn, data, day - timedelta(days=1))


def test_daily_report_without_purge_has_no_line(conn, data):
    assert "Прибрано фото" not in report.build(conn, data, NOW.date())


@pytest.mark.parametrize(
    ("count", "text"),
    [(1, "1 пост"), (2, "2 пости"), (5, "5 постів"), (11, "11 постів"), (12, "12 постів"),
     (21, "21 пост"), (24, "24 пости")],
)  # fmt: skip
def test_report_plural(data, count, text):
    borh.record_purged(data, count, NOW)
    assert report._purged(data, NOW.date()) == f"Прибрано фото: {text}"


def test_purge_log_survives_broken_file(data):
    (data / borh.PURGE_LOG).write_text("не json", encoding="utf-8")
    borh.record_purged(data, 3, NOW)
    assert borh.purged_on(data, NOW.date()) == 3
    assert borh.purged_on(data, date(2020, 1, 1)) == 0
    assert config.data_dir() == data
