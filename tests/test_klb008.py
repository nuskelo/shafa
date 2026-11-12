"""KLB-008: Картки «лише Клубок», хвилі черги Клубку, `klubok-import` (без мережі й ШІ)."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from shafa import borh, changes, db, folders, review, scan, transfer
from shafa.__main__ import main
from shafa.bot import actions, alerts, report
from shafa.db import Status
from shafa.export import Post
from shafa.klubok import fields, importer, queue, waves
from shafa.robot import form, publish, rehearse
from test_klb002_queue import TODAY, add, row, snapshot
from test_review import make_photo
from test_web import PORT, TOKEN, get, html, post


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def photo(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    make_photo(path, (400, 300))


def klubok_card(conn, root, post_id, *, status=Status.REVIEW, wave=1, **kw):
    """Картка «лише Клубок» з бланком; фото — у `<дані>/klubok-old/<N>/`."""
    name = folders.klubok_folder_name(post_id)
    photo(folders.klubok_spare(post_id) / "IMG_1.JPG")
    item_id = add(conn, root, post_id, folder=name, status=status, photos=False, **kw)
    with conn:
        conn.execute("UPDATE items SET only_klubok = 1 WHERE id = ?", (item_id,))
        if wave:
            conn.execute(
                "INSERT OR IGNORE INTO klubok_waves VALUES (?, ?, 'зима', 'x')", (post_id, wave)
            )
    return item_id


def set_wave(conn, post_id, wave):
    with conn:
        conn.execute("INSERT INTO klubok_waves VALUES (?, ?, 'зима', 'x')", (post_id, wave))


def status_of(conn, item_id):
    return conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]


# --- міграція 18 → 19 ---


def _v18(path, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA_VERSION", 18)
        conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO posts VALUES (301, '2024-01-01T10:00:00', 'т', 'export', 'x')")
        conn.execute(
            "INSERT INTO items (id, folder_name, post_id, status, created_at, updated_at) "
            "VALUES (1, '301', 301, 'published', 'a', 'b')"
        )
        conn.execute("INSERT INTO photos (item_id, position, file_name) VALUES (1, 1, '1.jpg')")
        conn.execute(
            "INSERT INTO klubok_ads (item_id, state, created_at, updated_at) "
            "VALUES (1, 'queued', 'a', 'b')"
        )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    conn.close()


def _dump(conn):
    names = ("posts", "items", "photos", "klubok_ads")
    return {n: [tuple(r)[:8] for r in conn.execute(f"SELECT * FROM {n} ORDER BY 1")] for n in names}


def test_migration_18_to_19_keeps_rows(tmp_path, monkeypatch):
    path = tmp_path / "v18.sqlite"
    _v18(path, monkeypatch)
    raw = sqlite3.connect(path)
    before = _dump(raw)
    raw.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 19
    assert _dump(conn) == before
    assert conn.execute("SELECT only_klubok FROM items").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM klubok_waves").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_interrupted_migration_leaves_18(tmp_path, monkeypatch):
    path = tmp_path / "v18.sqlite"
    _v18(path, monkeypatch)
    monkeypatch.setattr(db, "WAVES", db.WAVES + "CREATE TABLE klubok_waves (x);")
    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)

    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 18
    assert "only_klubok" not in {r[1] for r in raw.execute("PRAGMA table_info(items)")}
    raw.close()

    monkeypatch.undo()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    conn.close()


# --- Шафа Картку «лише Клубок» не бачить (HYP-1) ---


@pytest.mark.parametrize(
    "status", [Status.NEW, Status.REVIEW, Status.APPROVED, Status.PROBLEM, Status.ARCHIVED]
)
def test_tabs_and_counters_hide_only_klubok(conn, root, status):
    shafa = add(conn, root, 7301, status=status)
    klubok_card(conn, root, 301, status=status)

    assert [c.id for c in review.list_tab(conn, status)] == [shafa]
    assert review.counts(conn)[status] == 1


def test_shafa_buttons_refuse_only_klubok(conn, root):
    item_id = klubok_card(conn, root, 301)
    for action in ("approve", "reject"):
        assert review.act(conn, item_id, action, root) == review.ONLY_KLUBOK
    assert status_of(conn, item_id) == Status.REVIEW
    review.approve_klubok(conn, item_id)
    assert review.act(conn, item_id, "unapprove") == review.ONLY_KLUBOK
    assert status_of(conn, item_id) == Status.APPROVED


def test_card_page_404_for_only_klubok(conn, root, tmp_path):
    from shafa import web

    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)
    item_id = klubok_card(conn, root, 301)
    shafa = add(conn, root, 7301, status=Status.REVIEW)

    assert get(app, f"/item/{item_id}").status == 404
    assert get(app, f"/item/{shafa}").status == 200
    assert post(app, f"/item/{item_id}", action="approve").status == 404
    assert status_of(conn, item_id) == Status.REVIEW


def test_klubok_line_on_wave_etalon(conn, root, tmp_path):
    from shafa import web

    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)
    item_id = add(conn, root, 300, status=Status.REVIEW)
    assert "Клубок: не піде: пост старіший за 7200" in html(get(app, f"/item/{item_id}"))
    set_wave(conn, 300, 1)
    assert "Клубок: піде після схвалення" in html(get(app, f"/item/{item_id}"))


def test_robot_queue_and_dry_fill_skip_only_klubok(conn, root):
    shafa = add(conn, root, 7301)
    klubok_card(conn, root, 301, status=Status.APPROVED)

    assert [r["id"] for r in publish.queue_rows(conn)] == [shafa]
    assert form.find_card(conn, 301) is None


def test_rehearse_skips_only_klubok(conn, root):
    klubok_card(conn, root, 301, status=Status.APPROVED)
    klubok_card(conn, root, 302, status=Status.REVIEW)

    assert rehearse.pick_sections(conn, root, 5) == []
    assert rehearse.pick_posts(conn, [301, 302]) == ([], [301, 302])


def test_bot_report_alerts_and_actions_skip_only_klubok(conn, root):
    klubok_card(conn, root, 301, status=Status.REVIEW)
    klubok_card(conn, root, 302, status=Status.APPROVED)
    problem = klubok_card(conn, root, 303, status=Status.PROBLEM)
    with conn:
        conn.execute(
            "UPDATE items SET problem_code = 'ai_error', updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(timespec="seconds"), problem),
        )
        conn.execute("INSERT INTO sessions (id, started_at, ended_at) VALUES (1, '0', '9')")

    assert alerts._problems_now(conn) == {}
    assert report._waiting(conn) == ""
    assert report._problems(conn, datetime.now().astimezone().date()) == ""
    session = conn.execute("SELECT * FROM sessions WHERE id = 1").fetchone()
    assert actions.session_problems(conn, session) == []


def test_scan_and_photo_sync_skip_only_klubok(conn, root, export_path):
    item_id = klubok_card(conn, root, 301, status=Status.NEW)
    review_ = klubok_card(conn, root, 302, status=Status.REVIEW)
    (root / "7301").mkdir()
    make_photo(root / "7301" / "1.jpg", (400, 300))
    (root / "klubok-9").mkdir()  # зарезервована назва — не папка товару

    listed = folders.list_folders(root, None)
    result = scan.scan(conn, {}, listed)
    changes.sync_photos(conn, listed)

    assert [f.name for f in listed] == ["7301"]
    assert status_of(conn, item_id) == Status.NEW  # не «Папку не знайдено»
    assert status_of(conn, review_) == Status.REVIEW  # не «Фото зникли»
    assert result.new == 0
    assert all(not p.folder_name.startswith("klubok-") for p in result.problems)


def test_import_old_skips_post_with_only_klubok_card(conn, root, tmp_path):
    klubok_card(conn, root, 301)
    result = borh.import_old(conn, {301: Post(301, "2024-01-01T10:00:00", "т")}, tmp_path, [301])
    assert result.only_klubok == [301] and result.created == [] and result.existed == []
    assert "лише Клубок" in result.text()


def test_item_folder_and_transfer_know_klubok_dir():
    assert folders.item_folder(folders.Path("/Г"), "klubok-301") == folders.klubok_spare(301)
    assert transfer._is_unit("data/klubok-old/301")
    assert folders.KLUBOK_DIR in transfer.DATA_DIRS


# --- хвилі й зіставлення (R7, R4) ---


@pytest.mark.parametrize(
    ("line", "kind", "wave"),
    [
        ("Нова сумка Adidas оригінал. Розмір 18см*19см. 750грн", "сумки", 1),
        ("Шкіряний гаманець Coach оригінал. Розмір 20см*11см. 1100грн", "сумки", 1),
        ("Термо черевики Dachstein в ідеальному стані", "зима", 1),
        ("Зимові Термо черевики Sorel у відмінному стані", "зима", 1),
        ("Натуральні уги Avenue в ідеальному стані", "зима", 1),
        ("Замшеві черевики Gabor у відмінному стані", "демі", 1),
        ("Нові пінетки Dr. Martens. 16 розмір. Устілка 9,5см. 350грн", "демі", 1),
        ("Шкіряні лофери COX в ідеальному стані", "туфлі", 4),
        ("Шкіряні туфлі-броги в новому стані", "туфлі", 4),
        ("Кросівки Reebok в ідеальному стані. 34,5 розмір. Устілка 22,5см. 750грн", "спорт", 4),
        ("Босоніжки Geox у відмінному стані. 36 розмір. Устілка 23,5см. 850грн", "літо", 4),
        ("Нові тапочки Primark. 29 розмір. Устілка 18,5см. 350грн", "домашнє", 4),
        ("Новий шарф Heritage Traditions. Розмір 30см*166см. 100% вовна. 400грн", "одяг", None),
        ("‼️Увага! Змінились реквізити для оплати👞👟👠", "інше", None),
    ],
)
def test_kind_and_wave_by_first_line(line, kind, wave):
    assert waves.kind(line + "\nКросівки в другому рядку не рахуються") == kind
    assert waves.wave_of(kind) == wave


def test_matcher_verdicts():
    m = waves.Matcher(
        [(1, "Шкіряні черевики Geox 39 розміру", 1000), (2, "Кеди Ecco 41 розміру", 500)]
    )
    assert m.match("Черевики Geox. 39 розмір. 1100грн").verdict == waves.ON
    assert m.match("Черевики Geox. 39 розмір. 3000грн").verdict == waves.SIMILAR
    assert m.match("Черевики Geox. 44 розмір. 1000грн").verdict == waves.NONE  # розмір не той
    assert m.match("Шкіряні черевики. 39 розмір. 1000грн").verdict == waves.NO_LATIN
    assert m.match("Кеди Ecco. Ідуть на 40,5. 500грн").listing.id == 2  # ±0,5


# --- імпорт (D4) ---

WINTER = "Термо черевики Sorel в ідеальному стані. 38 розмір. 1200грн"


@pytest.fixture
def export_dir(tmp_path):
    path = tmp_path / "Експорт"
    (path / "photos").mkdir(parents=True)
    for n in (301, 302, 303, 304, 305, 306):
        make_photo(path / "photos" / f"{n}.jpg", (400, 300))
    return path


def posts_of(texts, photo=True):
    return {
        n: Post(n, "2024-01-01T10:00:00", t, f"photos/{n}.jpg" if photo else None)
        for n, t in texts.items()
    }


def run(conn, posts, export_dir, root, *, apply=True, wave=1):
    return importer.run(conn, posts, export_dir, root, wave, apply=apply, today=TODAY)


def test_import_new_card_files_and_wave(conn, root, export_dir):
    snapshot(conn)
    rep = run(conn, posts_of({301: WINTER}), export_dir, root)

    assert [d.action for d in rep.decisions] == [importer.NEW]
    card = conn.execute("SELECT * FROM items WHERE post_id = 301").fetchone()
    assert (card["folder_name"], card["status"], card["only_klubok"]) == ("klubok-301", "new", 1)
    spare = folders.klubok_spare(301)
    assert (spare / "1.jpg").is_file()
    assert (spare / "текст.txt").read_text(encoding="utf-8") == WINTER
    assert queue.wave(conn, 301) == 1
    assert conn.execute("SELECT kind FROM klubok_waves").fetchone()[0] == "зима"


def test_import_etalon_keeps_shafa_card_and_adds_spare_photo(conn, root, export_dir):
    snapshot(conn)
    published = add(conn, root, 301, status=Status.PUBLISHED, photos=False)  # фото стерто
    with_photos = add(conn, root, 302, status=Status.APPROVED)
    waiting = add(conn, root, 303, status=Status.REVIEW)
    before = [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")]

    rep = run(conn, posts_of({301: WINTER, 302: WINTER, 303: WINTER}), export_dir, root)

    assert [(d.action, d.spare) for d in rep.decisions] == [
        (importer.ETALON, True), (importer.ETALON, False), (importer.ETALON_WAIT, False),
    ]  # fmt: skip
    assert [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")] == before
    assert conn.execute("SELECT count(*) FROM items WHERE only_klubok = 1").fetchone()[0] == 0
    assert (folders.klubok_spare(301) / "1.jpg").is_file()
    assert not folders.klubok_spare(302).exists()
    assert {queue.wave(conn, n) for n in (301, 302, 303)} == {1}
    assert published and with_photos and waiting


def test_import_skips_archived_on_klubok_no_photo_text_and_other_kinds(conn, root, export_dir):
    snapshot(conn, listings=[("Термо черевики Sorel 38 розміру", 1200)])
    add(conn, root, 301, status=Status.ARCHIVED)
    texts = {
        301: WINTER,
        302: WINTER,  # уже на Клубку
        303: "Термо черевики Columbia. 38 розмір.",  # немає ціни
        304: "Кросівки Nike. 38 розмір. 900грн",  # хвиля 4
        305: "Новий шарф Zara. 300грн",  # одяг
        7015: WINTER,  # пост бота — за межею
    }
    posts = posts_of(texts) | posts_of({306: "Черевики Gabor. 39 розмір. 900грн"}, photo=False)
    queue_limit = {"SHAFA_KLUBOK_FROM_POST": "7014"}
    with pytest.MonkeyPatch.context() as m:
        for k, v in queue_limit.items():
            m.setenv(k, v)
        rep = run(conn, posts, export_dir, root)

    assert {d.post_id: d.action for d in rep.decisions} == {
        301: importer.ARCHIVED, 302: importer.ON_KLUBOK, 303: importer.TEXT, 306: importer.NO_PHOTO,
    }  # fmt: skip
    assert (rep.total, rep.other_wave, dict(rep.skipped_kinds)) == (6, 1, {"одяг": 1})
    assert conn.execute("SELECT count(*) FROM klubok_waves").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM items WHERE only_klubok = 1").fetchone()[0] == 0
    text = "\n".join(rep.lines())
    assert "302: 900 «Термо черевики Sorel 38 розміру»" in text and len(rep.lines()) <= 60


def test_repeat_import_does_not_duplicate(conn, root, export_dir):
    snapshot(conn)
    add(conn, root, 302, status=Status.APPROVED)
    posts = posts_of({301: WINTER, 302: WINTER})
    run(conn, posts, export_dir, root)
    rows = conn.execute("SELECT count(*) FROM items").fetchone()[0]

    rep = run(conn, posts, export_dir, root)

    assert [d.action for d in rep.decisions] == [importer.DONE, importer.DONE]
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == rows
    assert conn.execute("SELECT count(*) FROM klubok_waves").fetchone()[0] == 2


def test_dry_run_writes_nothing(conn, root, export_dir):
    snapshot(conn, at=TODAY - timedelta(days=3))  # старий знімок пробному прогону не заважає
    rep = run(conn, posts_of({301: WINTER}), export_dir, root, apply=False)

    assert [d.action for d in rep.decisions] == [importer.NEW]
    assert "пробний прогін" in rep.lines()[0] and "старший за добу" in rep.lines()[1]
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM klubok_waves").fetchone()[0] == 0
    assert not folders.klubok_root().exists()


def test_real_import_needs_fresh_snapshot(conn, root, export_dir):
    snapshot(conn, at=TODAY - timedelta(days=3))
    with pytest.raises(importer.ImportFailed, match="не свіжий"):
        run(conn, posts_of({301: WINTER}), export_dir, root)
    with pytest.raises(importer.ImportFailed, match="хвиля 2"):
        run(conn, posts_of({301: WINTER}), export_dir, root, wave=2)


def test_cli_dry_run_leaves_file(tmp_path, root, export_dir, capsys):
    path = tmp_path / "копія.sqlite"
    conn = db.connect(path)
    snapshot(conn)
    conn.close()
    (export_dir / "result.json").write_text(
        '{"messages": [{"id": 301, "type": "message", "date": "2024-01-01T10:00:00", '
        f'"text": "{WINTER}", "photo": "photos/301.jpg"}}]}}',
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    code = main(["klubok-import", "--export", str(export_dir), "--wave", "1", "--dry-run",
                 "--db", str(path), "--root", str(root)])  # fmt: skip

    assert code == 0
    assert "Нових Карток «лише Клубок»: 1" in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


# --- черга (D3, R2) ---


def test_queue_order_wave_1_then_2_then_4(conn, root):
    w4 = add(conn, root, 400, season=("Зима",), date="2023-01-01T10:00:00")
    w2 = add(conn, root, 7301, season=("Зима",), date="2026-11-01T10:00:00")
    w1 = add(conn, root, 300, season=("Літо",), date="2024-06-01T10:00:00")  # лише літо: ранг 2
    old = add(conn, root, 500)  # старий пост без хвилі — не кандидат
    set_wave(conn, 300, 1)
    set_wave(conn, 400, 4)

    assert [v.item_id for v in queue.plan(conn, root, TODAY)] == [w1, w2, w4]
    queue.sync(conn, root, TODAY)
    assert queue.place(conn, root, w1) == 1 and queue.place(conn, root, w4) == 3
    assert old not in {r[0] for r in conn.execute("SELECT item_id FROM klubok_ads")}


def test_only_klubok_review_not_given_until_approve_klubok(conn, root):
    snapshot(conn)
    item_id = klubok_card(conn, root, 301, season=("Зима",))
    no_wave = klubok_card(conn, root, 302, status=Status.APPROVED, wave=None)

    assert queue.plan(conn, root, TODAY) == []
    assert review.approve_klubok(conn, item_id) is True
    assert review.approve_klubok(conn, item_id) is False  # уже схвалена

    taken = queue.take(conn, root, TODAY)
    assert [v.item_id for v in taken.cards] == [item_id]
    assert row(conn, item_id)["state"] == queue.QUEUED
    assert row(conn, no_wave) is None
    assert taken.cards[0].ad.photos == (folders.klubok_spare(301) / "IMG_1.JPG",)


def test_approve_klubok_refuses_shafa_card(conn, root):
    shafa = add(conn, root, 7301, status=Status.REVIEW)
    assert review.approve_klubok(conn, shafa) is False
    assert status_of(conn, shafa) == Status.REVIEW


def test_sync_and_forget_keep_wave(conn, root):
    item_id = add(conn, root, 300)
    set_wave(conn, 300, 1)
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["state"] == queue.QUEUED

    assert review.act(conn, item_id, "unapprove") == "unapproved"
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id) is None and queue.wave(conn, 300) == 1

    review.act(conn, item_id, "approve", root)
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["state"] == queue.QUEUED


def test_needs_photos_protects_wave_etalon(conn, root):
    item_id = add(conn, root, 300, status=Status.PUBLISHED)
    assert queue.needs_photos(conn, item_id) is False
    set_wave(conn, 300, 1)
    assert queue.needs_photos(conn, item_id) is True


def test_fields_ad_uses_spare_photo(conn, root):
    item_id = add(conn, root, 300, status=Status.PUBLISHED, photos=False)
    card = review.load_card(conn, item_id)
    assert fields.ad(card, root).code == "klubok_no_photos"

    photo(folders.klubok_spare(300) / "1.jpg")
    assert fields.ad(card, root).photos == (folders.klubok_spare(300) / "1.jpg",)


def test_gone_post_archives_only_klubok_and_sync_drops_row(conn, root):
    snapshot(conn)
    item_id = klubok_card(conn, root, 301, status=Status.APPROVED)
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["state"] == queue.QUEUED

    review.archive_gone(conn, 301)
    queue.sync(conn, root, TODAY)

    assert status_of(conn, item_id) == Status.ARCHIVED
    assert row(conn, item_id) is None
