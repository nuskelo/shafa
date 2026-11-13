"""KLB-011: дублі Клубку — відбиток фото → ШІ-зір → лише тоді людина (без мережі й ключів)."""

import io
import json
import random

import pytest
from PIL import Image

from shafa import dup as shafa_dup
from shafa import events
from shafa.klubok import dup, dupcheck, photo_dup, queue, refresh, screen
from shafa.review import load_card
from test_klb002_queue import TODAY, add, row

PHOTO = "https://img.klubok.com/img/used/2026/03/10/{n}/m/{n}_2.jpg"


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def pattern(seed: int, size=(800, 600)) -> Image.Image:
    """Плавний випадковий візерунок — у кожного «фото» свій відбиток."""
    rng = random.Random(seed)
    small = Image.new("L", (16, 12))
    small.putdata([rng.randrange(256) for _ in range(16 * 12)])
    return small.resize(size, Image.Resampling.BICUBIC).convert("RGB")


def jpeg(image: Image.Image, quality=90, **kw) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality, **kw)
    return buf.getvalue()


def klubok_thumb(image: Image.Image) -> bytes:
    """Як Клубок робить мініатюру: центральний квадрат → 158×158, сильне стиснення."""
    return jpeg(photo_dup.square(image).resize((158, 158)), quality=60)


def our_photo(root, post_id, seed):
    """Фото Картки в Головній (замість однотонного з `add`)."""
    path = root / str(post_id) / "IMG_1.JPG"
    pattern(seed).save(path, "JPEG", quality=92)
    return path


def listing(conn, n, name="Черевики Ecco оригінал 38,5 розміру", price=1100, photo=True):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO klubok_listings_read VALUES (1, '2026-11-10T11:00:00+00:00', 1)"
        )
        conn.execute(
            "INSERT INTO klubok_listings (id, name, price, sizes, url, photo, added_at) "
            "VALUES (?, ?, ?, '[]', ?, ?, 'x')",
            (n, name, price, f"https://klubok.com/item/{n}", PHOTO.format(n=n) if photo else None),
        )


class Net:
    """Підставний img.klubok.com: адреса → (код, тип, тіло)."""

    def __init__(self, pages=None):
        self.pages, self.calls = dict(pages or {}), []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.pages:
            return 404, "text/html", b""
        return self.pages[url]


def thumbs(**by_listing):
    """{n: зображення} → сторінки підставного хоста (мініатюра /m/ і велике /l/)."""
    out = {}
    for n, image in by_listing.items():
        n = int(n.removeprefix("n"))
        out[PHOTO.format(n=n)] = (200, "image/jpeg", klubok_thumb(image))
        out[PHOTO.format(n=n).replace("/m/", "/l/")] = (200, "image/jpeg", jpeg(image))
    return out


class FakeJudge:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, card, found, our, theirs):
        self.calls.append((card.id, found.id, our, len(theirs)))
        same, why = self.answers.pop(0) if self.answers else ("unsure", "не видно")
        return shafa_dup.Verdict(same, why, asked=True)


def photos(tmp_path, net):
    return photo_dup.Photos(tmp_path / "kesh", get=net, sleep=lambda s: None)


def problem(conn, root, post_id=7301, seed=1):
    """Картка з дублем у «Проблемах» (як після `queue.sync`)."""
    item_id = add(conn, root, post_id)
    our_photo(root, post_id, seed)
    queue.sync(conn, root, TODAY)
    assert (row(conn, item_id)["state"], row(conn, item_id)["problem_code"]) == (
        queue.PROBLEM, queue.DUPLICATE,
    )  # fmt: skip
    return item_id


# --- відбиток ---


def test_same_photo_resized_recompressed_rotated_is_close(tmp_path):
    image = pattern(1)
    ours = tmp_path / "наше.jpg"
    image.save(ours, "JPEG", quality=95)
    rotated = tmp_path / "exif.jpg"  # знято боком, поворот лише в EXIF
    exif = Image.Exif()
    exif[0x0112] = 6
    image.rotate(90, expand=True).save(rotated, "JPEG", quality=95, exif=exif)

    theirs = photo_dup.theirs_hash(klubok_thumb(image))
    assert photo_dup.distance(photo_dup.ours_hash(ours), theirs) <= photo_dup.T1
    assert photo_dup.distance(photo_dup.ours_hash(rotated), theirs) <= photo_dup.T1


def test_different_photos_are_far():
    for a, b in [(1, 2), (3, 4), (5, 6)]:
        ours = io.BytesIO(jpeg(pattern(a)))
        with Image.open(ours) as image:
            mine = photo_dup.dhash(photo_dup.square(image))
        assert photo_dup.distance(mine, photo_dup.theirs_hash(klubok_thumb(pattern(b)))) > 10


def test_large_url_only_by_exact_pattern():
    assert photo_dup.large_url(PHOTO.format(n=5)) == PHOTO.format(n=5).replace("/m/", "/l/")
    assert photo_dup.large_url("https://img.klubok.com/x/m/5_2.png") is None
    assert photo_dup.large_url("https://evil.com/img/m/5_2.jpg") is None
    assert photo_dup.large_url("https://img.klubok.com/img/m/5_2.jpg?a=/m/") is None


# --- завантаження ---


def test_only_img_klubok_com_is_fetched(tmp_path):
    net = Net()
    with pytest.raises(photo_dup.FetchFailed) as error:
        photos(tmp_path, net).small(5, "https://evil.example/5.jpg")
    assert error.value.permanent and net.calls == []


@pytest.mark.parametrize("code", [429, 403])
def test_429_or_403_stops(tmp_path, code):
    net = Net({PHOTO.format(n=5): (code, "text/html", b"")})
    with pytest.raises(photo_dup.FetchStop):
        photos(tmp_path, net).small(5, PHOTO.format(n=5))


@pytest.mark.parametrize(
    ("page", "permanent"),
    [((404, "text/html", b""), True), ((200, "text/html", b"<html>"), True),
     ((500, "text/html", b""), False)],
)  # fmt: skip
def test_fetch_failures_permanent_or_not(tmp_path, page, permanent):
    net = Net({PHOTO.format(n=5): page})
    with pytest.raises(photo_dup.FetchFailed) as error:
        photos(tmp_path, net).small(5, PHOTO.format(n=5))
    assert error.value.permanent is permanent


def test_cache_and_large_falls_back_to_small(tmp_path):
    small = PHOTO.format(n=5)
    net = Net({small: (200, "image/jpeg", b"jpg")})
    got = photos(tmp_path, net)
    assert got.large(5, small) == b"jpg"  # /l/ — 404, ШІ отримує мініатюру
    assert got.small(5, small) == b"jpg"
    assert net.calls == [small.replace("/m/", "/l/"), small]  # друге — з кешу


# --- find_all ---


def test_find_is_first_of_find_all(conn, root):
    item_id = add(conn, root, 7301)
    listing(conn, 901)
    listing(conn, 902, name="Черевики Ecco 38 розміру шкіряні")
    card = load_card(conn, item_id)
    found = dup.find_all(conn, card)
    assert [d.klubok_id for d in found] == [901, 902]
    assert dup.find(conn, card) == found[0] and found[0].photo == PHOTO.format(n=901)


# --- рішення ---


def test_same_photo_is_duplicate_without_ai(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    judge = FakeJudge()
    report = dupcheck.check(
        conn, root, photos(tmp_path, Net(thumbs(n901=pattern(1)))), judge, write=True
    )
    got = row(conn, item_id)
    assert (got["state"], got["problem_code"]) == (queue.SKIPPED, queue.DUP_YES)
    assert got["dup_klubok_id"] == 901 and got["problem_text"].startswith("Дубль — фото")
    assert judge.calls == [] and report.count(dupcheck.DUP) == 1
    assert "пост 7301: Дубль — фото збіглось з оголошенням 901" in report.lines()
    post = conn.execute("SELECT post_id FROM klubok_listings WHERE id = 901").fetchone()[0]
    assert post == 7301  # як «Так, дубль»: її оголошення знає свій пост


def test_photo_match_with_any_candidate_wins(conn, root, tmp_path):
    listing(conn, 901)
    listing(conn, 902, name="Черевики Ecco 38 розміру шкіряні")
    item_id = problem(conn, root)
    net = Net(thumbs(n901=pattern(7), n902=pattern(1)))
    dupcheck.check(conn, root, photos(tmp_path, net), FakeJudge())
    assert row(conn, item_id)["dup_klubok_id"] == 902


def test_all_ai_no_goes_to_queue_and_label_survives_two_syncs(conn, root, tmp_path):
    listing(conn, 901)
    listing(conn, 902, name="Черевики Ecco 38 розміру шкіряні")
    item_id = problem(conn, root)
    judge = FakeJudge(("no", "інша підошва"), ("no", "інший колір"))
    net = Net(thumbs(n901=pattern(7), n902=pattern(8)))
    dupcheck.check(conn, root, photos(tmp_path, net), judge)

    got = row(conn, item_id)
    assert got["state"] == queue.QUEUED and got["dup_ok_at"] is not None
    assert got["problem_text"] == "Інша пара — ШІ: інша підошва"
    assert len(judge.calls) == 2 and judge.calls[0][3] > 10_000  # ШІ — велике фото /l/
    queue.sync(conn, root, TODAY)
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["problem_text"] == "Інша пара — ШІ: інша підошва"  # D2
    shown = {r.card.id: r for r in screen.rows(conn, "queue")}
    assert shown[item_id].auto == "other"
    assert screen.auto_summary(conn) == screen.AutoSummary(0, 1, 0)


def test_human_other_pair_keeps_no_text(conn, root):
    listing(conn, 901)
    item_id = problem(conn, root)
    assert queue.dup_other(conn, item_id)
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["problem_text"] is None


def test_auto_other_then_post_gone_is_rewritten(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(7)))),
                   FakeJudge(("no", "інша")))  # fmt: skip
    with conn:  # картку прибрали з кандидатів — рядок черги зникає, як і раніше
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (item_id,))
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id) is None


def test_ai_yes_on_second_candidate_is_duplicate(conn, root, tmp_path):
    listing(conn, 901)
    listing(conn, 902, name="Черевики Ecco 38 розміру шкіряні")
    item_id = problem(conn, root)
    judge = FakeJudge(("no", "інша"), ("yes", "та сама подряпина"))
    dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(7), n902=pattern(8)))),
                   judge)  # fmt: skip
    got = row(conn, item_id)
    assert got["state"] == queue.SKIPPED and got["dup_klubok_id"] == 902
    assert got["problem_text"] == "Дубль — ШІ: та сама подряпина"


def test_ai_unsure_stays_for_human_and_is_not_checked_again(conn, root, tmp_path):
    listing(conn, 901)
    listing(conn, 902, name="Черевики Ecco 38 розміру шкіряні")
    item_id = problem(conn, root)
    judge = FakeJudge(("no", "інша"), ("unsure", "фото нечітке"))
    net = Net(thumbs(n901=pattern(7), n902=pattern(8)))
    dupcheck.check(conn, root, photos(tmp_path, net), judge)

    got = row(conn, item_id)
    assert (got["state"], got["dup_klubok_id"]) == (queue.PROBLEM, 902)
    assert got["problem_text"] == "ШІ не впевнений: фото нечітке"
    assert shafa_dup.verdict_text(got["problem_text"]) == "ШІ не впевнений: фото нечітке"
    assert dupcheck.pending(conn) == []
    assert screen.rows(conn, "decide")[0].note == "ШІ не впевнений: фото нечітке"


def test_more_than_three_candidates_go_to_human_without_ai(conn, root, tmp_path):
    for n in range(901, 905):
        listing(conn, n, name=f"Черевики Ecco 38 розміру модель {n} шкіряні")
    item_id = problem(conn, root)
    judge = FakeJudge()
    net = Net(thumbs(**{f"n{n}": pattern(n) for n in range(901, 905)}))
    dupcheck.check(conn, root, photos(tmp_path, net), judge)
    got = row(conn, item_id)
    assert got["state"] == queue.PROBLEM and got["problem_text"].startswith("ШІ не порівняв:")
    assert judge.calls == []


def test_more_than_three_candidates_photo_match_still_duplicate(conn, root, tmp_path):
    for n in range(901, 905):
        listing(conn, n, name=f"Черевики Ecco 38 розміру модель {n} шкіряні")
    item_id = problem(conn, root)
    net = Net(thumbs(n901=pattern(7), n902=pattern(8), n903=pattern(1), n904=pattern(9)))
    dupcheck.check(conn, root, photos(tmp_path, net), FakeJudge())
    assert row(conn, item_id)["dup_klubok_id"] == 903


def test_no_candidates_anymore_goes_to_queue(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    with conn:
        conn.execute("UPDATE klubok_listings SET sold = 1")
    dupcheck.check(conn, root, photos(tmp_path, Net()), FakeJudge())
    got = row(conn, item_id)
    assert got["state"] == queue.QUEUED
    assert got["problem_text"] == "Інша пара — схожого на Клубку вже немає"


def test_her_photo_missing_is_permanent_human(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    dupcheck.check(conn, root, photos(tmp_path, Net()), FakeJudge())  # 404
    got = row(conn, item_id)
    assert got["state"] == queue.PROBLEM and got["problem_text"].startswith("Фото не порівняно:")
    assert dupcheck.pending(conn) == []


def test_temporary_error_leaves_no_marker_and_next_pass_decides(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    broken = Net({PHOTO.format(n=901): (502, "text/html", b"")})
    report = dupcheck.check(conn, root, photos(tmp_path, broken), FakeJudge())
    assert report.count(dupcheck.RETRY) == 1
    assert row(conn, item_id)["problem_text"] == queue.REASONS[queue.DUPLICATE]
    assert dupcheck.pending(conn) == [item_id]

    dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(1)))), FakeJudge())
    assert row(conn, item_id)["state"] == queue.SKIPPED


def test_429_stops_whole_pass(conn, root, tmp_path):
    listing(conn, 901)
    first = problem(conn, root)
    second = problem(conn, root, 7302, seed=2)
    net = Net({PHOTO.format(n=901): (429, "text/html", b"")})
    report = dupcheck.check(conn, root, photos(tmp_path, net), FakeJudge())
    assert report.stopped and report.left == 2 and len(net.calls) == 1
    assert dupcheck.pending(conn) == [first, second]


def test_budget_exhausted_does_not_call_ai_and_retries(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)

    def client():
        raise AssertionError("ШІ не мав бути викликаний")

    judge = shafa_dup.Judge(conn, client, lambda: 0, site=shafa_dup.KLUBOK)
    report = dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(7)))), judge)
    assert report.count(dupcheck.RETRY) == 1 and report.ai_requests == 0
    assert dupcheck.pending(conn) == [item_id]


def test_ai_limit_per_pass_leaves_rest_for_next_time(conn, root, tmp_path):
    listing(conn, 901)
    problem(conn, root)
    problem(conn, root, 7302, seed=2)
    judge = FakeJudge(("no", "інша"), ("no", "інша"))
    net = Net(thumbs(n901=pattern(7)))
    report = dupcheck.check(conn, root, photos(tmp_path, net), judge, limit=1)
    assert report.count(dupcheck.OTHER) == 1 and report.count(dupcheck.RETRY) == 1


def test_dry_run_writes_nothing(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    before = dict(row(conn, item_id))
    report = dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(1)))),
                            FakeJudge(), write=False)  # fmt: skip
    assert report.count(dupcheck.DUP) == 1 and dict(row(conn, item_id)) == before


def test_auto_decision_is_journaled(conn, root, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: seen.append((kind, kw["data"])))
    listing(conn, 901)
    item_id = problem(conn, root)
    dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(1)))), FakeJudge())
    assert seen and seen[0][0] == "klubok.dup_auto" and seen[0][1]["item"] == item_id
    assert seen[0][1]["listing"] == 901 and seen[0][1]["way"].startswith("фото")
    assert "klubok.dup_auto" in events.KINDS and not events.KINDS["klubok.dup_auto"].loud


# --- відкат з Архіву ---


def test_undo_auto_duplicate_from_archive_goes_to_queue(conn, root, tmp_path):
    listing(conn, 901)
    item_id = problem(conn, root)
    dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(1)))), FakeJudge())
    archive = screen.rows(conn, "archive")
    assert archive[0].auto == "dup" and archive[0].reason == screen.AUTO_DUP_GROUP
    assert archive[0].listing.url == "https://klubok.com/item/901"

    assert queue.undo_auto_dup(conn, item_id)
    got = row(conn, item_id)
    assert got["state"] == queue.QUEUED and got["dup_ok_at"] is not None
    assert conn.execute("SELECT post_id FROM klubok_listings WHERE id = 901").fetchone()[0] is None
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["state"] == queue.QUEUED  # дублі більше не перевіряються


def test_undo_refuses_human_duplicate(conn, root):
    listing(conn, 901)
    item_id = problem(conn, root)
    assert queue.dup_yes(conn, item_id)
    assert not queue.undo_auto_dup(conn, item_id)


def test_undo_button_on_archive_page(conn, root, tmp_path):
    from shafa import web
    from test_web import PORT, TOKEN, get, html, location, post
    from test_web_robot import Spawner

    app = web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=Spawner(), lock_file=tmp_path / "robot.lock",
    )  # fmt: skip
    listing(conn, 901)
    item_id = problem(conn, root)
    dupcheck.check(conn, root, photos(tmp_path, Net(thumbs(n901=pattern(1)))), FakeJudge())
    page = html(get(app, "/klubok/archive"))
    assert "Дубль — вирішила програма" in page and 'value="klubok_undo_dup"' in page
    assert "Програма вирішила: 1 дубль" in html(get(app, "/klubok/decide"))

    response = post(app, f"/klubok/item/{item_id}", action="klubok_undo_dup", stay="list",
                    **{"from": "/klubok/archive"})  # fmt: skip
    assert location(response).startswith("/klubok/archive?msg=klubok_undo_dup")
    assert row(conn, item_id)["state"] == queue.QUEUED


# --- суддя: Шафа не змінилась, Клубок — свої слова ---


def test_judge_request_shafa_unchanged_klubok_adapted(conn, root):
    item_id = add(conn, root, 7301)
    card = load_card(conn, item_id)
    found = shafa_dup.Listing(5, "Черевики Ecco", None, "", ("38",), "u", price=1100)
    tiny = shafa_dup.photos.prepare_bytes(jpeg(pattern(1)), "a.jpg")
    shafa = shafa_dup.build_request(card, found, tiny, tiny)
    klubok = shafa_dup.build_request(card, found, tiny, tiny, site=shafa_dup.KLUBOK)

    texts = [b["text"] for b in shafa["messages"][0]["content"] if b["type"] == "text"]
    assert "Клубок" not in json.dumps(texts, ensure_ascii=False) + shafa["system"]
    assert texts[1] == "Фото 2 — уже опубліковане оголошення:"
    assert shafa["system"] == shafa_dup.PROMPT_FILE.read_text(encoding="utf-8")
    ktexts = [b["text"] for b in klubok["messages"][0]["content"] if b["type"] == "text"]
    assert "Клубку" in ktexts[1] and "Клубку" in klubok["system"]
    assert "Ціна: 1200 грн" in ktexts[2] and "Ціна на Шафі" not in ktexts[2]  # ціна з поста
    assert "на Шафі" not in klubok["system"]


def test_klubok_judge_does_not_use_shafa_other_post(conn, root, monkeypatch):
    item_id = add(conn, root, 7301)
    card = load_card(conn, item_id)
    called = []
    monkeypatch.setattr(shafa_dup, "other_post", lambda *a: called.append(a) or 1)
    judge = shafa_dup.Judge(conn, lambda: None, lambda: 0, site=shafa_dup.KLUBOK)
    path = root / "7301" / "IMG_1.JPG"
    judge(card, shafa_dup.Listing(5, "x", None, "", (), "u"), path, jpeg(pattern(1)))
    assert called == []


# --- фон ---


def test_background_checks_duplicates_after_queue(conn, root):
    from shafa import db

    path = conn.execute("PRAGMA database_list").fetchone()[2]
    listing(conn, 901)
    item_id = add(conn, root, 7301)
    seen = []

    def dups(c, r):
        seen.append(row(c, item_id)["problem_code"])  # черга вже записана

    bg = refresh.Background(lambda: db.connect(path), lambda: root, print, dups=dups)
    bg.start()
    bg.wait()
    assert seen == [queue.DUPLICATE]


def test_background_without_network_leaves_duplicate_for_next_time(conn, root):
    listing(conn, 901)
    item_id = problem(conn, root)
    report = dupcheck.background(conn, root)  # conftest: мережі немає — тимчасово
    assert report.count(dupcheck.RETRY) == 1 and dupcheck.pending(conn) == [item_id]
