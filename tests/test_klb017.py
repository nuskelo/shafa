"""KLB-017 (TD-077): `klubok-import` — «вже на Клубку» лише за фото (`klubok-link`), не за текстом."""

import pytest

from shafa.klubok import importer, link
from test_klb002_queue import TODAY, snapshot
from test_klb008 import WINTER, linked, posts_of
from test_review import make_photo

SOREL = "Термо черевики Sorel 38 розміру"


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv("SHAFA_KLUBOK", "так")


@pytest.fixture
def export_dir(tmp_path):
    path = tmp_path / "Експорт"
    (path / "photos").mkdir(parents=True)
    for n in (301, 302):
        make_photo(path / "photos" / f"{n}.jpg", (400, 300))
    return path


def run(conn, posts, export_dir, root, *, apply=True, wave=1):
    return importer.run(conn, posts, export_dir, root, wave, apply=apply, today=TODAY)


def test_photo_link_is_on_klubok(conn, root, export_dir):
    snapshot(conn)
    linked(conn, 301, "Зимові чоботи")  # за текстом не схоже — вирішує лише фото

    rep = run(conn, posts_of({301: WINTER}), export_dir, root)

    assert [(d.action, d.note) for d in rep.decisions] == [
        (importer.ON_KLUBOK, "801 «Зимові чоботи»")
    ]
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM klubok_waves").fetchone()[0] == 0


def test_text_match_only_is_imported_and_reported(conn, root, export_dir):
    snapshot(conn, listings=[(SOREL, 1200)])  # за текстом — та сама пара, за фото — ні
    linked(conn)

    rep = run(conn, posts_of({301: WINTER}), export_dir, root)

    assert [d.action for d in rep.decisions] == [importer.NEW]
    assert rep.decisions[0].text_match == f"900 «{SOREL}»"
    card = conn.execute("SELECT status, only_klubok FROM items WHERE post_id = 301").fetchone()
    assert tuple(card) == ("new", 1)
    text = "\n".join(rep.lines())
    assert "Раніше вважались на Клубку за текстом, тепер імпортуємо: 1 (нова Картка 1" in text
    assert f"301: Термо черевики Sorel в ідеальному стані · за текстом схоже: 900 «{SOREL}»" in text
    assert len(rep.lines()) <= importer.LINES_MAX


def test_repost_group_is_on_klubok(conn, root, export_dir):
    snapshot(conn)
    linked(conn, 302, SOREL, groups={802: [302, 301]})  # 301 — старший перепост тієї ж пари

    rep = run(conn, posts_of({301: WINTER, 302: WINTER}), export_dir, root)

    assert {d.post_id: d.action for d in rep.decisions} == {
        301: importer.ON_KLUBOK, 302: importer.ON_KLUBOK,
    }  # fmt: skip
    assert "(перепост)" in rep.decisions[0].note


def test_sold_listing_does_not_count(conn, root, export_dir):
    snapshot(conn)
    linked(conn)
    linked(conn, 301, SOREL, groups={801: [301, 300]})
    conn.execute("UPDATE klubok_listings SET sold = 1 WHERE id = 801")

    rep = run(conn, posts_of({301: WINTER}), export_dir, root, apply=False)

    assert [d.action for d in rep.decisions] == [importer.NEW]


@pytest.mark.parametrize("missing", ["file", "post_id"])
@pytest.mark.parametrize("apply", [True, False])
def test_refuses_without_klubok_link(conn, root, export_dir, missing, apply):
    snapshot(conn, listings=[(SOREL, 1200)])
    if missing == "post_id":
        link.save_groups({})  # файл є, а жодного `post_id` у знімку
    else:
        linked(conn)
        link.groups_path().unlink()

    with pytest.raises(importer.ImportFailed, match="спершу klubok-link"):
        run(conn, posts_of({301: WINTER}), export_dir, root, apply=apply)
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_repeat_adds_nothing(conn, root, export_dir):
    snapshot(conn, listings=[(SOREL, 1200)])
    linked(conn, 302, "Інша пара")
    posts = posts_of({301: WINTER, 302: WINTER})
    run(conn, posts, export_dir, root)
    before = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("items", "klubok_waves")
    ]

    rep = run(conn, posts, export_dir, root)

    assert [d.action for d in rep.decisions] == [importer.DONE, importer.ON_KLUBOK]
    after = [
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("items", "klubok_waves")
    ]
    assert after == before == [1, 1]


def test_wave_4_same_rule(conn, root, export_dir):
    snapshot(conn, listings=[("Кросівки Nike 38 розміру", 900)])
    linked(conn, 302, "Кросівки Nike 39 розміру")
    sport = "Кросівки Nike. 38 розмір. 900грн"

    rep = run(conn, posts_of({301: sport, 302: sport}), export_dir, root, apply=False, wave=4)

    assert {d.post_id: d.action for d in rep.decisions} == {
        301: importer.NEW, 302: importer.ON_KLUBOK,
    }  # fmt: skip
    assert rep.decisions[0].text_match
