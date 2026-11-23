"""KLB-016: її оголошення на Клубку ↔ пости каналу за фото (без мережі; фото — підставні)."""

import json

import pytest

from shafa import config, events
from shafa.export import Post
from shafa.klubok import link, photo_dup, queue, screen, sold
from test_klb002_queue import TODAY, add
from test_klb005 import gone
from test_klb009 import app, lock_file, spawner  # noqa: F401
from test_klb011 import PHOTO, Net, our_photo, pattern, thumbs
from test_web import get, html

ECCO = "Шкіряні черевики Ecco 38 розміру в ідеальному стані\n1100 грн"
GEOX = "Шкіряні черевики Geox 38 розміру\n1000 грн"


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: out.append((kind, text, kw)))
    return out


def alive(conn, post_id):
    """Нічна перевірка бачила пост у каналі."""
    with conn:
        conn.execute(
            "INSERT INTO post_checks (post_id, checked_at) VALUES (?, '2026-11-20T03:00:00+00:00') "
            "ON CONFLICT (post_id) DO UPDATE SET checked_at = excluded.checked_at, gone_at = NULL",
            (post_id,),
        )


def her(conn, n, name="Черевики Ecco 38 розміру", post_id=None, photo=True):
    with conn:
        conn.execute(
            "INSERT INTO klubok_listings (id, name, price, sizes, url, photo, post_id, added_at) "
            "VALUES (?, ?, 1100, '[]', ?, ?, ?, 'x')",
            (n, name, f"https://klubok.com/item/{n}", PHOTO.format(n=n) if photo else None,
             post_id),
        )  # fmt: skip


def post_id_of(conn, n):
    return conn.execute("SELECT post_id FROM klubok_listings WHERE id = ?", (n,)).fetchone()[0]


class Export:
    """Підставний Експорт Каналу: пост → текст і (необов'язково) фото у `photos/`."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "експорт"
        (self.dir / "photos").mkdir(parents=True)
        self.posts: dict[int, Post] = {}

    def add(self, post_id, text=ECCO, seed=None):
        photo = None
        if seed is not None:
            photo = f"photos/{post_id}.jpg"
            pattern(seed).save(self.dir / photo, "JPEG", quality=92)
        self.posts[post_id] = Post(post_id, "2025-03-01T10:00:00", text, photo)


def run(conn, tmp_path, export, root, net, *, write=True):
    photos = photo_dup.Photos(tmp_path / "кеш", get=net, sleep=lambda s: None)
    return link.run(conn, export.posts, export.dir, root, photos, write=write, today=TODAY)


# --- правило B на відбитках (відстань = кількість різних бітів) ---


def posts_of(hashes, texts=None):
    return link.Posts(hashes, texts or dict.fromkeys(hashes, ECCO))


NAME, SIZES = "Черевики Ecco 38 розміру", frozenset({38.0})


def test_one_close_post_is_a_match():
    verdict = link.judge(0, posts_of({100: [0b1], 200: [0xFFFF]}), NAME, SIZES)
    assert (verdict.kind, verdict.post_id, verdict.distance) == (link.MATCH, 100, 1)


def test_two_different_photos_close_together_are_ambiguous():
    # 100: d = 2, 200: d = 3, між собою 5 > T1 — різні речі, відрив 1 < 3
    verdict = link.judge(0, posts_of({100: [0b11], 200: [0b11100]}), NAME, SIZES)
    assert verdict.kind == link.AMBIGUOUS and verdict.post_id is None


def test_far_enough_second_candidate_is_still_a_match():
    # 100: d = 0, 200 (інший бренд): d = 3 — відрив рівно 3
    texts = {100: ECCO, 200: GEOX}
    verdict = link.judge(0, posts_of({100: [0], 200: [0b111]}, texts), NAME, SIZES)
    assert (verdict.kind, verdict.post_id) == (link.MATCH, 100)


def test_repost_with_same_text_goes_to_the_newest_post():
    verdict = link.judge(0, posts_of({4004: [0b1], 7345: [0b11]}), NAME, SIZES)
    assert (verdict.kind, verdict.post_id, verdict.group) == (link.MATCH, 7345, (7345, 4004))


def test_same_photo_other_text_is_not_a_repost():
    # Правило B: те саме фото, але інший бренд — різні пари → неоднозначно
    texts = {4004: ECCO, 7345: GEOX}
    verdict = link.judge(0, posts_of({4004: [0b1], 7345: [0b11]}, texts), NAME, SIZES)
    assert verdict.kind == link.AMBIGUOUS


def test_distance_four_needs_text_that_does_not_contradict():
    hashes = {100: [0b1111]}  # d = 4 = T1
    contra = link.judge(0, posts_of(hashes, {100: GEOX}), NAME, SIZES)
    assert contra.kind == link.NONE and contra.by_text == 1
    fine = link.judge(0, posts_of(hashes, {100: ECCO}), NAME, SIZES)
    assert (fine.kind, fine.post_id) == (link.MATCH, 100)
    unknown = link.judge(0, posts_of(hashes, {100: "Черевики 1000 грн"}), NAME, SIZES)
    assert unknown.kind == link.MATCH  # бренд і розмір невідомі в пості — не суперечить
    # d ≤ 3 — текст не перевіряємо
    near = link.judge(0, posts_of({100: [0b111]}, {100: GEOX}), NAME, SIZES)
    assert near.kind == link.MATCH


# --- прохід по базі ---


def test_run_links_unique_match_and_keeps_known_post(conn, tmp_path, root, said):
    export = Export(tmp_path)
    export.add(7001, seed=1)
    export.add(7002, GEOX, seed=2)
    her(conn, 501)
    her(conn, 502, post_id=9999)  # уже відомий — не чіпаємо
    her(conn, 503)  # фото, якого немає в постах
    net = Net(thumbs(n501=pattern(1), n502=pattern(2), n503=pattern(3)))

    report = run(conn, tmp_path, export, root, net)

    assert post_id_of(conn, 501) == 7001
    assert post_id_of(conn, 502) == 9999
    assert post_id_of(conn, 503) is None
    assert report.written == [501]
    assert "Уже відомий пост ≠ за фото (не чіпаємо): 1" in report.lines()
    kinds = [k for k, *_ in said]
    assert kinds == ["klubok.linked"] and said[0][2]["data"]["linked"] == 1


def test_card_photos_count_as_post_photos(conn, tmp_path, root):
    export = Export(tmp_path)
    add(conn, root, 7500)  # пост бота: у Експорті немає, фото — лише в Картці
    our_photo(root, 7500, 5)
    her(conn, 601)

    run(conn, tmp_path, export, root, Net(thumbs(n601=pattern(5))))
    assert post_id_of(conn, 601) == 7500


def test_ambiguous_is_not_written(conn, tmp_path, root):
    export = Export(tmp_path)
    export.add(7001, ECCO, seed=1)
    export.add(7002, GEOX, seed=1)  # те саме фото, інша пара за текстом
    her(conn, 501)

    report = run(conn, tmp_path, export, root, Net(thumbs(n501=pattern(1))))
    assert post_id_of(conn, 501) is None
    assert [r.listing_id for r in report.of(link.AMBIGUOUS)] == [501]
    assert link.load_groups() == {}


def test_dry_run_writes_no_group_file_and_no_event(conn, tmp_path, root, said):
    export = Export(tmp_path)
    export.add(4004, seed=1)
    export.add(7345, seed=1)
    her(conn, 501)

    report = run(conn, tmp_path, export, root, Net(thumbs(n501=pattern(1))), write=False)
    assert report.groups == {501: [7345, 4004]}
    assert not link.groups_path().exists() and said == []
    assert report.lines()[0].endswith("(пробний прогін — нічого не записано)")


def test_klubok_429_writes_nothing(conn, tmp_path, root):
    export = Export(tmp_path)
    export.add(7001, seed=1)
    her(conn, 501)
    net = Net({PHOTO.format(n=501): (429, "text/html", b"")})

    report = run(conn, tmp_path, export, root, net)
    assert report.stopped and post_id_of(conn, 501) is None
    assert not link.groups_path().exists()


# --- наслідки: «Схоже, продано» ---


def test_linked_listing_with_gone_post_is_asked(conn, tmp_path, root):
    export = Export(tmp_path)
    export.add(7001, seed=1)
    her(conn, 501)
    gone(conn, 7001)

    report = run(conn, tmp_path, export, root, Net(thumbs(n501=pattern(1))))
    assert report.asks == [501]
    assert [(a.kind, a.key, a.post_id) for a in sold.asks(conn)] == [(sold.LISTING, 501, 7001)]


def test_repost_group_older_post_alive_is_not_asked(conn, tmp_path, root):
    export = Export(tmp_path)
    export.add(4004, seed=1)
    export.add(7345, seed=1)
    her(conn, 501)
    gone(conn, 7345)
    alive(conn, 4004)

    report = run(conn, tmp_path, export, root, Net(thumbs(n501=pattern(1))))
    assert post_id_of(conn, 501) == 7345
    assert json.loads(link.groups_path().read_text(encoding="utf-8")) == {"501": [7345, 4004]}
    assert report.asks == [] and sold.asks(conn) == []
    assert sold.answer(conn, sold.LISTING, 501, True) is False  # прихований рядок не відповідається

    gone(conn, 4004)  # старший теж зник — питаємо
    assert [a.key for a in sold.asks(conn)] == [501]


def test_broken_group_file_asks_as_before(conn, tmp_path, root):
    her(conn, 501, post_id=7345)
    gone(conn, 7345)
    alive(conn, 4004)
    path = config.data_dir() / link.FILE
    path.write_text(json.dumps({"501": [7345, 4004]}), encoding="utf-8")
    assert sold.asks(conn) == []
    path.write_text("{не json", encoding="utf-8")
    assert [a.key for a in sold.asks(conn)] == [501]
    path.unlink()
    assert [a.key for a in sold.asks(conn)] == [501]


def test_sold_block_shows_thirty_oldest_first(conn, app):  # noqa: F811
    for n in range(31):
        her(conn, 900 + n, post_id=8000 + n)
        gone(conn, 8000 + n, f"2026-11-{30 - n:02d}T03:00:00+00:00")  # 930 — найстаріше

    shown, total = sold.page(conn)
    assert total == 31 and len(shown) == sold.PAGE == 30
    assert shown[0].key == 930 and 900 not in [a.key for a in shown]
    assert screen.counts(conn)["decide"] == 31

    page = html(get(app, "/klubok/decide"))
    assert "Схоже, продано (31)" in page and "Ще 1 — з'являться, коли відповісте на ці." in page
    assert page.count("ksold-row") == 30
