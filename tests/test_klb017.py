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
    assert "Уже на Клубку (не імпортуємо): 1 · за фото 0 · за суворим текстом 1" in text
