"""KLB-017 (TD-077): `klubok-import` — «вже на Клубку» за фото (`klubok-link`), суворим текстом чи ШІ-зором."""

import io
import random

import pytest
from PIL import Image

from shafa import dup as shafa_dup
from shafa.klubok import importer, link, photo_dup
from test_klb002_queue import TODAY, snapshot
from test_klb008 import WINTER, linked, posts_of
from test_review import make_photo

SOREL = "Термо черевики Sorel 38 розміру"
_buffer = io.BytesIO()
Image.new("RGB", (300, 300), "gray").save(_buffer, format="JPEG")
JPEG = _buffer.getvalue()


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
    snapshot(conn, listings=[(SOREL, 1400)])  # ±25 % — старий «є», > 10 % — не суворий
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
    snapshot(conn, listings=[(SOREL, 1400)])
    if missing == "post_id":
        link.save_groups({})  # файл є, а жодного `post_id` у знімку
    else:
        linked(conn)
        link.groups_path().unlink()

    with pytest.raises(importer.ImportFailed, match="спершу klubok-link"):
        run(conn, posts_of({301: WINTER}), export_dir, root, apply=apply)
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_repeat_adds_nothing(conn, root, export_dir):
    snapshot(conn, listings=[(SOREL, 1400)])
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
    snapshot(conn, listings=[("Кросівки Nike 38 розміру", 1050)])
    linked(conn, 302, "Кросівки Nike 39 розміру")
    sport = "Кросівки Nike. 38 розмір. 900грн"

    rep = run(conn, posts_of({301: sport, 302: sport}), export_dir, root, apply=False, wave=4)

    assert {d.post_id: d.action for d in rep.decisions} == {
        301: importer.NEW, 302: importer.ON_KLUBOK,
    }  # fmt: skip
    assert rep.decisions[0].text_match


# --- Ітерація 1: суворий текст (бренд + рівно розмір + підтип + ціна ±10 %) ---

# Аркуш KLB-017-pary.html, переглянутий Архітектором 24.11: пост (перша фраза, розмір, ціна) і її
# оголошення. True — та сама пара (у неї каталожне фото чи інший кадр), False — інша.
PAIRS = [
    (307, "Черевики Dr. Martens в стані нових. 33,5 розмір. 1200грн",
     "Черевики Dr. Martens оригінал 33,5 розміру в стані нових", 1200, True),
    (508, "Термо черевики Scarpa з мембраною gore-tex в ідеальному стані. 37 розмір. 1990грн",
     "Термо черевики Scarpa з мембраною gore-tex 37 розміру", 1990, True),
    (647, "Замшеві уги Koolaburra від UGG у відмінному стані. 37 розмір. 800грн",
     "Замшеві уги Koolaburra від UGG 37 розміру у відмінному стані", 850, True),
    (802, ("Термо кросівки Meindl з мембраною gore-tex в ідеальному стані. Вказаний 4,5 розмір. "
     "Ідуть на 37. 1800грн"),
     "Нові шкіряні термо кросівки Meindl з мембраною gore-tex 37,5 розміру", 1900, False),
    (1596, "Термо кросівки Columbia в стані нових. Вказаний 36 розмір. Ідуть на 35. 1500грн",
     "Термо кросівки Columbia 35 розміру в стані нових", 1500, True),
    (3280, "Нові натуральні уги Lily&Dan. Вказаний 33 розмір. Ідуть на 32. 900грн",
     "Нові натуральні уги Lily&Dan 32 розміру", 900, True),
    (3399, "Зимові термо сапожки Columbia в ідеальному стані. 37 розмір. 1400грн",
     "Зимові термо сапожки Columbia 37 розміру в ідеальному стані", 1400, True),
    (3536, ("Термо черевики Salomon з мембраною gore-tex в ідеальному стані. Вказаний 37,5 розмір. "
     "Ідуть на 36-36,5. 1700грн"),
     "Термо кросівки Salomon 37 розміру в ідеальному стані", 1400, False),
    (3626, "Замшеві черевики Gabor у відмінному стані. Вказаний 6,5 розмір. Ідуть на 39,5. 990грн",
     "Замшевые кроссовки Gabor 39,5 размера в отличном состоянии", 750, False),
    (3736, "Нові шкіряні черевики Dr.Martens оригінал. 23 розмір. 850грн",
     "Новые кожаные ботинки Dr. Martens оригинал 23 размера", 900, True),
    (3987, "Нові шкіряні черевики Dr. Martens оригінал. 39 розмір. 2900грн",
     "Шкіряні черевики Dr. Martens оригінал 39 розміру", 3000, True),
    (4032, ("Шкіряні черевики Timberland в ідеальному стані. Вказаний 37 розмір. Ідуть на 36. "
     "1600грн"),
     "Шкіряні черевики Timberland 37 розміру в ідеальному стані", 1400, False),
    (4398, "Шкіряні черевики Timberland в ідеальному стані. 40 розмір. 1800грн",
     "Шкіряні черевики Timberland 39,5 розміру в ідеальному стані", 1900, False),
    (6156, "Гаманець Kipling в ідеальному стані. Розмір 18см*10см. 750грн",
     "Сумка Kipling в новому стані", 900, False),
    (6895, ("Шкіряні черевики Chelsea&Violet в ідеальному стані. Вказаний 6 М розмір. Ідуть на 36. "
     "1600грн"),
     "Шкіряні черевики Chelsea&Violet 36 розміру в ідеальному стані", 1500, True),
]  # fmt: skip


@pytest.mark.parametrize(("post", "text", "name", "price", "same"), PAIRS)
def test_strict_text_on_sheet_pairs(post, text, name, price, same):
    strict = importer.Strict([(post, name, price, "[]")])
    assert (strict.match(text) is not None) is same


def test_strict_text_sheet_all_listings_together():
    strict = importer.Strict([(post, name, price, "[]") for post, _, name, price, _ in PAIRS])
    found = {post: strict.match(text) for post, text, *_ in PAIRS}
    assert {p for p, ad in found.items() if ad} == {p for p, *_, same in PAIRS if same}
    assert all(ad.id == p for p, ad in found.items() if ad)  # 9 → своє оголошення, не чуже


@pytest.mark.parametrize(
    ("text", "name", "price", "same"),
    [
        ("Сумка Kipling в ідеальному стані. 900грн", "Сумка Kipling в новому стані", 950, True),
        ("Шкіряна сумка без бренду. 900грн", "Шкіряна сумка", 900, False),  # без бренду
        ("Черевики Geox. 900грн", "Черевики Geox", 900, False),  # взуття без розміру
        ("Черевики Geox. 38,5 розмір. 900грн", "Черевики Geox 38 розміру", 900, False),
        ("Черевики Geox. 38 розмір. 900грн", "Черевики Geox 38 розміру", 1001, False),  # > 10 %
        ("Черевики Geox. 38 розмір", "Черевики Geox 38 розміру", 900, False),  # без ціни
        ("Черевики Geox. Ідуть на 36-36,5. 900грн", "Черевики Geox 36 розміру", 900, False),
        ("Черевики Geox. 38 розмір. 900грн", "Кросівки Geox 38 розміру", 900, False),  # підтип
    ],
)
def test_strict_text_edges(text, name, price, same):
    assert (importer.Strict([(1, name, price, "[]")]).match(text) is not None) is same


def test_strict_text_is_on_klubok_and_reported(conn, root, export_dir):
    snapshot(conn, listings=[("Термо черевики Sorel 38 розміру в ідеальному стані", 1250)])
    linked(conn)

    rep = run(conn, posts_of({301: WINTER}), export_dir, root)

    assert [(d.action, d.by_text) for d in rep.decisions] == [(importer.ON_KLUBOK, True)]
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    text = "\n".join(rep.lines())
    assert "Уже на Клубку (не імпортуємо): 1 · за фото 0 · за текстом / ШІ 1" in text


# --- Ітерація 2: ШІ-зір для «схоже за текстом», перепости ---

STRICT_AD = ("Термо черевики Sorel 38 розміру в ідеальному стані", 1250)  # Б: суворий текст «так»
LOOSE_AD = (SOREL, 1400)  # А: старий текст «є», суворий — ні


def noise(path, seed):
    rnd = random.Random(seed)
    image = Image.new("L", (64, 64))
    image.putdata([rnd.randrange(256) for _ in range(64 * 64)])
    image.convert("RGB").resize((400, 300)).save(path, format="JPEG")


def with_photos(conn):
    """Мініатюри її оголошень у знімку (як дає `klubok-snapshot`)."""
    with conn:
        conn.execute(
            "UPDATE klubok_listings SET photo = printf(?, id, id)",
            ("https://img.klubok.com/img/used/2026/03/10/%d/m/%d_2.jpg",),
        )


class Net:
    def __init__(self, code=200):
        self.code, self.calls = code, []

    def __call__(self, url):
        self.calls.append(url)
        return self.code, "image/jpeg", JPEG


class FakeJudge:
    def __init__(self, *answers, retry=False):
        self.answers, self.calls, self.retry = list(answers), [], retry

    def __call__(self, card, found, our, theirs):
        self.calls.append((card.post_id, found.id))
        if self.retry:
            return shafa_dup.unasked("вичерпано денний бюджет ШІ", retry=True)
        same = self.answers.pop(0) if self.answers else "unsure"
        return shafa_dup.Verdict(same, f"ШІ каже {same}", asked=True)


def ai_run(conn, root, export_dir, tmp_path, posts, judge, net=None, apply=False):
    photos = photo_dup.Photos(tmp_path / "кеш", get=net or Net(), sleep=lambda s: None)
    return importer.run(
        conn, posts, export_dir, root, 1, apply=apply, today=TODAY, judge=judge, photos=photos
    )


@pytest.mark.parametrize(
    ("ad", "said", "action"),
    [
        (STRICT_AD, "yes", importer.ON_KLUBOK),
        (STRICT_AD, "no", importer.NEW),
        (STRICT_AD, "unsure", importer.ON_KLUBOK),  # не впевнений → суворий текст
        (LOOSE_AD, "yes", importer.ON_KLUBOK),
        (LOOSE_AD, "no", importer.NEW),
        (LOOSE_AD, "unsure", importer.NEW),
    ],
)
def test_ai_verdict_decides(conn, root, export_dir, tmp_path, ad, said, action):
    snapshot(conn, listings=[ad])
    with_photos(conn)
    linked(conn)
    judge = FakeJudge(said)

    rep = ai_run(conn, root, export_dir, tmp_path, posts_of({301: WINTER}), judge)

    assert judge.calls == [(301, 900)]
    assert [(d.action, d.ai) for d in rep.decisions] == [(action, said)]
    assert any("запитів ШІ: 1" in line for line in rep.lines())


@pytest.mark.parametrize("strict", [True, False])
def test_ai_not_asked_falls_back_to_strict_text(conn, root, export_dir, tmp_path, strict):
    snapshot(conn, listings=[STRICT_AD if strict else LOOSE_AD])
    with_photos(conn)
    linked(conn)
    judge = FakeJudge(retry=True)

    rep = ai_run(conn, root, export_dir, tmp_path, posts_of({301: WINTER, 302: WINTER}), judge)

    assert len(judge.calls) == 1  # бюджет вичерпано — далі ШІ не питаємо
    expected = importer.ON_KLUBOK if strict else importer.NEW
    assert {d.action for d in rep.decisions if d.post_id == 302} == {expected}  # 301 — перепост
    assert rep.ai_stopped and any("ШІ-зір зупинено" in line for line in rep.lines())


def test_klubok_429_stops_ai(conn, root, export_dir, tmp_path):
    snapshot(conn, listings=[STRICT_AD])
    with_photos(conn)
    linked(conn)
    judge = FakeJudge("no")

    net = Net(429)

    rep = ai_run(conn, root, export_dir, tmp_path, posts_of({301: WINTER, 302: WINTER}), judge, net)

    assert judge.calls == [] and len(net.calls) == 1  # після 429 на Клубок більше не ходимо
    assert [(d.action, d.ai) for d in rep.decisions] == [(importer.ON_KLUBOK, "none")] * 2


def test_dry_run_does_not_ask_ai(conn, root, export_dir, tmp_path, monkeypatch, capsys):
    from shafa.__main__ import main
    from shafa.klubok import dupcheck

    snapshot(conn, listings=[STRICT_AD])
    with_photos(conn)
    linked(conn)
    monkeypatch.setattr(dupcheck, "make_judge", lambda c: pytest.fail("ШІ в --dry-run"))
    (export_dir / "result.json").write_text(
        '{"messages": [{"id": 301, "type": "message", "date": "2024-01-01T10:00:00", '
        f'"text": "{WINTER}", "photo": "photos/301.jpg"}}]}}',
        encoding="utf-8",
    )
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    code = main(["klubok-import", "--export", str(export_dir), "--wave", "1", "--dry-run",
                 "--db", db_path, "--root", str(root)])  # fmt: skip

    assert code == 0
    assert "піде на ШІ: 1, ≈ 0.01 $" in capsys.readouterr().out


def test_post_card_goes_through_real_judge(conn, root, export_dir):
    from test_dup001 import Client, reply

    snapshot(conn, listings=[STRICT_AD])
    with_photos(conn)
    client = Client(reply("yes", "Та сама пара"))
    judge = shafa_dup.Judge(conn, lambda: client, lambda: 10**9, site=shafa_dup.KLUBOK)
    card = importer.post_card(posts_of({301: WINTER})[301])
    listing, _ = importer._their_listing(conn, 900)

    verdict = judge(card, listing, export_dir / "photos" / "301.jpg", JPEG)

    assert (verdict.same, verdict.asked) == ("yes", True)
    text = client.requests[0]["messages"][0]["content"][-1]["text"]
    assert "Ціна: 1200 грн" in text and "Розміри: 38" in text and "Термо черевики Sorel" in text
    row = conn.execute("SELECT item_id FROM ai_requests").fetchone()
    assert row is not None and row[0] is None  # запит без Картки


def test_repost_imports_only_newest(conn, root, export_dir, tmp_path):
    snapshot(conn)
    linked(conn)
    noise(export_dir / "photos" / "303.jpg", 7)
    posts = posts_of({301: WINTER, 302: WINTER, 303: WINTER})  # 301 і 302 — те саме фото

    rep = importer.run(conn, posts, export_dir, root, 1, apply=True, today=TODAY)

    assert {d.post_id: d.action for d in rep.decisions} == {
        301: importer.REPOST, 302: importer.NEW, 303: importer.NEW,
    }  # fmt: skip
    assert rep.decisions[0].note == "перепост 302"
    assert {r[0] for r in conn.execute("SELECT post_id FROM items")} == {302, 303}

    again = importer.run(conn, posts, export_dir, root, 1, apply=True, today=TODAY)

    assert {d.post_id: d.action for d in again.decisions}[301] == importer.REPOST
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2


def test_same_photo_other_brand_is_not_repost(conn, root, export_dir):
    snapshot(conn)
    linked(conn)
    other = "Термо черевики Columbia в ідеальному стані. 38 розмір. 1200грн"

    rep = run(conn, posts_of({301: WINTER, 302: other}), export_dir, root, apply=False)

    assert [d.action for d in rep.decisions] == [importer.NEW, importer.NEW]


def test_newer_repost_after_import_adds_nothing(conn, root, export_dir):
    snapshot(conn)
    linked(conn)
    importer.run(conn, posts_of({301: WINTER}), export_dir, root, 1, apply=True, today=TODAY)

    rep = importer.run(
        conn, posts_of({301: WINTER, 302: WINTER}), export_dir, root, 1, apply=True, today=TODAY
    )

    assert {d.post_id: (d.action, d.note) for d in rep.decisions} == {
        301: (importer.DONE, ""), 302: (importer.REPOST, "перепост 301"),
    }  # fmt: skip
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
