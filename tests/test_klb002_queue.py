"""KLB-002: черга Клубку — кандидати, порядок, ліміт, дублі, стани, фото (без мережі)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shafa import ai, fill, folders, options, review
from shafa.db import Status
from shafa.klubok import dup, fields, queue
from shafa.robot import publish
from test_review import make_photo

BOOTS = "Жіночий одяг / Взуття / Черевики"
SANDALS = "Жіночий одяг / Взуття / Босоніжки"
GIRLS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
TEXT = "Шкіряні черевики Ecco 38 розміру. В ідеальному стані. 1200грн"
TODAY = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).astimezone()


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def add(
    conn, root, post_id, *, folder=None, status=Status.APPROVED, date="2026-11-01T10:00:00",
    text=TEXT, section=BOOTS, season=("Демісезон",), sizes=("38",), brand="Ecco",
    title="Шкіряні черевики Ecco 38 розміру в ідеальному стані", photos=True,
):  # fmt: skip
    """Пост (якщо ще немає), Картка з бланком і фото в Головній."""
    folder = folder or str(post_id)
    base = folders.base_name(folder)
    if photos and not (root / base).exists():
        (root / base).mkdir()
        make_photo(root / base / "IMG_1.JPG", (400, 300))
    blank = [
        ai.Field(name, value)
        for name, value in {
            "title": title, "section": section, "sizes": list(sizes), "colors": ["Чорний"],
            "brand": brand, "season": list(season), "gender": None, "photo_order": ["IMG_1.JPG"],
        }.items()
    ]  # fmt: skip
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, 'export', 'x')", (post_id, date, text)
        )
        item_id = conn.execute(
            "INSERT INTO items (folder_name, post_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'x', 'x')",
            (folder, post_id, status),
        ).lastrowid
        conn.execute("INSERT INTO photos VALUES (?, 1, 'IMG_1.JPG')", (item_id,))
        fill.save_blank(conn, item_id, blank)
    return item_id


OTHER_PAIR = {"brand": "Geox", "title": "Кеди Geox 41 розміру", "sizes": ("41",)}


def ad_row(conn, item_id, state, **cols):
    names = ["item_id", "state", "created_at", "updated_at", *cols]
    with conn:
        conn.execute(
            f"INSERT INTO klubok_ads ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            (item_id, state, "x", "x", *cols.values()),
        )


def row(conn, item_id):
    return conn.execute("SELECT * FROM klubok_ads WHERE item_id = ?", (item_id,)).fetchone()


def snapshot(conn, at=TODAY, listings=()):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO klubok_listings_read VALUES (1, ?, ?)",
            (at.astimezone(UTC).isoformat(timespec="seconds"), len(listings)),
        )
        for i, (name, price) in enumerate(listings, start=900):
            conn.execute(
                "INSERT INTO klubok_listings (id, name, price, sizes, url, added_at) "
                "VALUES (?, ?, ?, '[]', ?, 'x')",
                (i, name, price, f"https://klubok.com/item/{i}"),
            )


def states(verdicts):
    return {v.item_id: (v.state, v.code) for v in verdicts}


# --- кандидати й порядок (D3) ---


def test_candidates_are_approved_and_published_newer_than_limit(conn, root):
    new = add(conn, root, 7301)
    published = add(conn, root, 7302, status=Status.PUBLISHED)
    add(conn, root, 7200)  # межа не входить
    add(conn, root, 7303, status=Status.REVIEW)
    add(conn, root, 7304, status=Status.PUBLISHING)
    add(conn, root, 7305, status=Status.PROBLEM)

    assert [v.item_id for v in queue.plan(conn, root, TODAY)] == [new, published]


def test_from_post_setting(conn, root, monkeypatch):
    add(conn, root, 7301)
    monkeypatch.setenv(queue.FROM_SETTING, "7301")
    assert queue.plan(conn, root, TODAY) == []
    monkeypatch.setenv(queue.FROM_SETTING, "0")
    with pytest.raises(queue.SetupError):
        queue.plan(conn, root, TODAY)


def test_winter_first_summer_last_older_posts_first(conn, root):
    summer = add(conn, root, 7301, section=SANDALS, season=("Літо",), date="2026-10-01T10:00:00")
    other_new = add(conn, root, 7302, date="2026-11-05T10:00:00")
    other_old = add(conn, root, 7303, date="2026-10-02T10:00:00")
    winter = add(conn, root, 7304, season=("Зима",), date="2026-11-06T10:00:00")

    order = [v.item_id for v in queue.plan(conn, root, TODAY)]

    assert order == [winter, other_old, other_new, summer]


def test_skip_codes_from_fields_and_gone_post(conn, root):
    no_price = add(conn, root, 7301, text="Черевики Ecco 38 розміру")
    gone = add(conn, root, 7302)
    with conn:
        conn.execute("INSERT INTO post_checks (post_id, gone_at) VALUES (7302, 'x')")

    got = states(queue.plan(conn, root, TODAY))

    assert got[no_price] == (queue.SKIPPED, "klubok_no_price")
    assert got[gone] == (queue.SKIPPED, queue.POST_GONE)


# --- фото «Опубліковано» (D7) ---


def test_published_photos_found_in_published_folder(conn, root):
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    assert publish.move_published(root, "7301") is None
    assert not (root / "7301").exists()

    result = fields.ad(review.load_card(conn, item_id), root)

    assert isinstance(result, fields.KlubokAd)
    assert result.photos[0].parent == root / folders.PUBLISHED_DIR / "7301"


def test_photo_folder_keeps_old_posts_in_data_dir(root):
    assert folders.photo_folder(root, "borh-12") == folders.item_folder(root, "borh-12")
    assert folders.photo_folder(root, "7301~38") == root / folders.PUBLISHED_DIR / "7301"


def test_purge_published_keeps_photos_klubok_still_needs(conn, root, monkeypatch):
    monkeypatch.delenv(queue.FLAG)  # захист не залежить від прапорця (Ітерація 1, DRIFT-1 → а)
    monkeypatch.setenv(publish.DELETE_SETTING, "1")
    needed = add(conn, root, 7301, status=Status.PUBLISHED)
    done = add(conn, root, 7302, status=Status.PUBLISHED)
    old = add(conn, root, 7100, status=Status.PUBLISHED)  # пост до межі — Клубку не потрібен
    ad_row(conn, done, queue.PUBLISHED)
    with conn:
        conn.execute("UPDATE items SET published_at = '2026-11-01T10:00:00+00:00'")
    for name in ("7301", "7302", "7100"):
        publish.move_published(root, name)

    removed = publish.purge_published(conn, root, TODAY)

    assert sorted(removed) == ["7100", "7302"]
    assert (root / folders.PUBLISHED_DIR / "7301").is_dir()
    assert queue.needs_photos(conn, needed) and not queue.needs_photos(conn, done)
    assert not queue.needs_photos(conn, old)


@pytest.mark.parametrize(
    ("state", "needed"),
    [("queued", True), ("publishing", True), ("problem", True), ("published", False),
     ("skipped", False), ("sold", False), ("removed", False)],
)  # fmt: skip
def test_needs_photos_by_state(conn, root, state, needed):
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, item_id, state)
    assert queue.needs_photos(conn, item_id) is needed


# --- стать дитячого (D8) ---


def test_kids_sex_from_section_first(conn, root):
    item_id = add(conn, root, 7301, section=GIRLS, text="Кросівки Nike 30 розміру. 800грн")
    result = fields.ad(review.load_card(conn, item_id), root)
    assert result.sex == fields.SEX_GIRL  # розділ «Для дівчаток», у тексті — ні слова
    assert fields.sex_for("для хлопчика", "Дитячий одяг / Для дівчаток / Взуття / Кеди") == 0
    assert fields.sex_for("для хлопчика") == fields.SEX_BOY


# --- sync (D3) ---


def test_sync_writes_queued_and_skipped_and_leaves_other_states(conn, root):
    queued = add(conn, root, 7301)
    skipped = add(conn, root, 7302, text="без ціни")
    others = {state: add(conn, root, 7310 + i, **OTHER_PAIR) for i, state in enumerate(
        ("publishing", "published", "problem", "sold_asked", "sold", "removed")
    )}  # fmt: skip
    for state, item_id in others.items():
        ad_row(conn, item_id, state, problem_code="mine")

    queue.sync(conn, root, TODAY)

    assert row(conn, queued)["state"] == queue.QUEUED and row(conn, queued)["queued_at"]
    assert (row(conn, skipped)["state"], row(conn, skipped)["problem_code"]) == (
        queue.SKIPPED, "klubok_no_price"
    )  # fmt: skip
    for state, item_id in others.items():
        assert (row(conn, item_id)["state"], row(conn, item_id)["problem_code"]) == (state, "mine")


def test_sync_twice_changes_nothing(conn, root):
    item_id = add(conn, root, 7301)
    queue.sync(conn, root, TODAY)
    first = tuple(row(conn, item_id))
    queue.sync(conn, root, TODAY)
    assert tuple(row(conn, item_id)) == first


def test_unapprove_drops_queued_row(conn, root):
    item_id = add(conn, root, 7301)
    queue.sync(conn, root, TODAY)
    assert review.act(conn, item_id, "unapprove") == "unapproved"
    assert row(conn, item_id) is None


def test_sync_drops_rows_of_cards_no_longer_candidates(conn, root):
    item_id = add(conn, root, 7301)
    queue.sync(conn, root, TODAY)
    with conn:
        conn.execute("UPDATE items SET status = 'problem' WHERE id = ?", (item_id,))
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id) is None


# --- дублі (D6) ---


def test_same_post_second_card_is_post_done(conn, root):
    first = add(conn, root, 7301, status=Status.PUBLISHED)
    second = add(conn, root, 7301, folder="23948278247301")
    ad_row(conn, first, queue.PUBLISHED, klubok_id=55)

    assert states(queue.plan(conn, root, TODAY))[second] == (queue.SKIPPED, queue.POST_DONE)


def test_uncertain_publication_of_same_post_also_blocks(conn, root):
    first = add(conn, root, 7301, status=Status.PUBLISHED)
    second = add(conn, root, 7301, folder="23948278247301")
    ad_row(conn, first, queue.PROBLEM, problem_code=queue.UNCERTAIN)
    assert states(queue.plan(conn, root, TODAY))[second] == (queue.SKIPPED, queue.POST_DONE)


def test_size_sister_is_neither_post_done_nor_duplicate(conn, root):
    a = add(conn, root, 7301, folder="7301~38", status=Status.PUBLISHED)
    b = add(conn, root, 7301, folder="7301~38,5", sizes=("38,5",))
    ad_row(conn, a, queue.PUBLISHED, klubok_id=55, url="https://klubok.com/item/55")
    snapshot(conn, listings=[("Шкіряні черевики Ecco 38 розміру", 1200)])
    with conn:  # її знімок має й оголошення сестри (виставила програма)
        conn.execute("UPDATE klubok_listings SET id = 55")

    assert states(queue.plan(conn, root, TODAY))[b] == (queue.QUEUED, None)


def test_like_her_active_listing_is_duplicate(conn, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco оригінал 38,5 розміру в ідеальному стані", 1100)])

    queue.sync(conn, root, TODAY)

    got = row(conn, item_id)
    assert (got["state"], got["problem_code"]) == (queue.PROBLEM, queue.DUPLICATE)
    assert got["dup_klubok_id"] == 900 and "Ecco" in got["dup_name"]


@pytest.mark.parametrize(
    ("name", "price"),
    [("Черевики Ecco 41 розміру", 1200),            # інший розмір
     ("Черевики Clarks 38 розміру", 1200),          # інший бренд у назві
     ("Кросівки Ecco 38 розміру", 1200),            # інший тип
     ("Черевики Ecco 38 розміру", 3000)],           # ціна далеко
)  # fmt: skip
def test_not_a_duplicate(conn, root, name, price):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[(name, price)])
    assert states(queue.plan(conn, root, TODAY))[item_id] == (queue.QUEUED, None)


def test_sold_listing_is_not_a_duplicate(conn, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])
    with conn:
        conn.execute("UPDATE klubok_listings SET sold = 1")
    assert states(queue.plan(conn, root, TODAY))[item_id] == (queue.QUEUED, None)


def test_like_ad_published_by_program_is_duplicate(conn, root):
    first = add(conn, root, 7301, status=Status.PUBLISHED)
    second = add(conn, root, 7302)
    ad_row(conn, first, queue.PUBLISHED, klubok_id=77, url="https://klubok.com/item/77")

    verdict = {v.item_id: v for v in queue.plan(conn, root, TODAY)}[second]

    assert (verdict.state, verdict.code) == (queue.PROBLEM, queue.DUPLICATE)
    assert verdict.dup == dup.Dup(77, verdict.dup.name, "https://klubok.com/item/77", "ours")


def test_other_pair_then_queued_and_never_duplicate_again(conn, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])
    queue.sync(conn, root, TODAY)
    assert row(conn, item_id)["state"] == queue.PROBLEM

    assert queue.dup_other(conn, item_id) is True
    queue.sync(conn, root, TODAY)

    got = row(conn, item_id)
    assert got["state"] == queue.QUEUED and got["dup_ok_at"] and got["problem_code"] is None
    assert [v.item_id for v in queue.take(conn, root, TODAY).cards] == [item_id]


def test_yes_duplicate_is_sticky_and_links_her_listing(conn, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])
    queue.sync(conn, root, TODAY)

    assert queue.dup_yes(conn, item_id) is True
    with conn:
        conn.execute("DELETE FROM klubok_listings")  # схожого вже немає — рішення лишається
    queue.sync(conn, root, TODAY)

    assert (row(conn, item_id)["state"], row(conn, item_id)["problem_code"]) == (
        queue.SKIPPED, queue.DUP_YES
    )  # fmt: skip
    assert queue.dup_other(conn, item_id) is False


def test_yes_duplicate_writes_post_to_her_listing(conn, root):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])
    queue.sync(conn, root, TODAY)
    queue.dup_yes(conn, item_id)
    assert conn.execute("SELECT post_id FROM klubok_listings WHERE id = 900").fetchone()[0] == 7301


def test_title_sizes_on_her_names():
    names = json.loads(
        (folders.Path(__file__).parent / "fixtures" / "klubok-names.json").read_text(
            encoding="utf-8"
        )
    )
    parsed = {name: dup.title_sizes(name) for name in names}
    assert parsed["Кросівки Nike оригінал 38,5 розміру в ідеальному стані"] == ["38,5"]
    assert parsed["Крокси Crocs 27-28 розміру в ідеальному стані"] == ["27", "28"]
    assert parsed["Кожаные кроссовки Adidas оригинал 40,5 размера в идеальном состоянии"] == [
        "40,5"
    ]
    assert parsed["Нові кросівки Nike оригінал. 35 розмір."] == ["35"]
    assert parsed["Нова шкіряна сумка Vera Pelle"] == []
    without = [n for n, s in parsed.items() if not s]
    assert all("сум" in n.casefold() for n in without), without  # без розміру — лише сумки


@pytest.mark.parametrize(
    ("name", "want"),
    [("Чоботи 36-38 розміру", ["36", "37", "38"]), ("Уги 30-40 розміру", ["30", "40"]),
     ("Кеди 37.5 розміру", ["37,5"]), ("Кросівки 2024 року", []), ("Чоботи 39 р.", [])],
)  # fmt: skip
def test_title_sizes_edges(name, want):
    assert dup.title_sizes(name) == want


def test_brand_key_and_brand_in_name():
    assert dup.brand_key("Dr. Martens") in dup.brand_key("Черевики Dr.Martens 39 розміру")
    assert dup.brand_key("Lily & Dan") == dup.brand_key("Lily&Dan")
    assert dup.brand_key("Jessica (C&A)") == "jessica"


# --- ліміт і видача (D4) ---


def test_limit_counts_attempts_of_local_day(conn, root):
    snapshot(conn)
    for n in range(3):
        add(conn, root, 7301 + n)
    morning = TODAY.replace(hour=0, minute=30)
    for i in range(9):
        item_id = add(conn, root, 7500 + i, photos=False, status=Status.PUBLISHED, **OTHER_PAIR)
        ad_row(conn, item_id, queue.PUBLISHED,
               publish_started_at=morning.astimezone(UTC).isoformat(timespec="seconds"))  # fmt: skip
    yesterday = morning - timedelta(hours=1)
    for i in range(5):
        item_id = add(conn, root, 7600 + i, photos=False, status=Status.PUBLISHED, **OTHER_PAIR)
        ad_row(conn, item_id, queue.PUBLISHED,
               publish_started_at=yesterday.astimezone(UTC).isoformat(timespec="seconds"))  # fmt: skip

    assert queue.started_today(conn, TODAY) == 9
    assert len(queue.take(conn, root, TODAY).cards) == 1  # 10 − 9

    item_id = add(conn, root, 7700, photos=False, status=Status.PUBLISHED, **OTHER_PAIR)
    ad_row(conn, item_id, queue.PUBLISHED,
           publish_started_at=TODAY.astimezone(UTC).isoformat(timespec="seconds"))  # fmt: skip
    taken = queue.take(conn, root, TODAY)
    assert (taken.cards, taken.code) == ([], queue.LIMIT_REACHED)


def test_take_respects_daily_limit_setting(conn, root, monkeypatch):
    snapshot(conn)
    for n in range(4):
        add(conn, root, 7301 + n)
    monkeypatch.setenv(queue.LIMIT_SETTING, "2")
    assert len(queue.take(conn, root, TODAY).cards) == 2
    monkeypatch.setenv(queue.LIMIT_SETTING, "31")
    with pytest.raises(queue.SetupError):
        queue.take(conn, root, TODAY)


def test_take_empty_when_off_or_snapshot_old(conn, root, monkeypatch):
    add(conn, root, 7301)
    assert queue.take(conn, root, TODAY).code == queue.SNAPSHOT_OLD  # знімка немає
    snapshot(conn, at=TODAY - timedelta(hours=25))
    taken = queue.take(conn, root, TODAY)
    assert (taken.cards, taken.code) == ([], queue.SNAPSHOT_OLD)
    snapshot(conn, at=TODAY - timedelta(hours=23))
    assert len(queue.take(conn, root, TODAY).cards) == 1
    monkeypatch.delenv(queue.FLAG)
    assert queue.take(conn, root, TODAY).code == queue.OFF


def test_take_rechecks_duplicate_before_each_card(conn, root):
    snapshot(conn)
    first = add(conn, root, 7301, date="2026-10-01T10:00:00")
    second = add(conn, root, 7302, date="2026-10-02T10:00:00")
    queue.sync(conn, root, TODAY)
    # KLB-003 виставило першу — друга (та сама пара з іншого поста) тепер дубль
    with conn:
        conn.execute("INSERT INTO sessions (id, started_at, site) VALUES (1, 'x', 'klubok')")
    assert queue.start(conn, first, 1) and queue.published(conn, first, 77, "u")

    taken = queue.take(conn, root, TODAY)

    assert taken.cards == []
    assert (row(conn, second)["state"], row(conn, second)["problem_code"]) == (
        queue.PROBLEM, queue.DUPLICATE
    )  # fmt: skip


# --- стани (D5) ---


def test_states_only_from_expected_state(conn, root):
    item_id = add(conn, root, 7301)
    queue.sync(conn, root, TODAY)
    with conn:
        conn.execute("INSERT INTO sessions (id, started_at, site) VALUES (1, 'x', 'klubok')")

    assert queue.published(conn, item_id, 5, "u") is False  # ще не publishing
    assert queue.start(conn, item_id, 1) is True
    assert queue.start(conn, item_id, 1) is False
    got = row(conn, item_id)
    assert got["state"] == queue.PUBLISHING and got["publish_started_at"] and got["session_id"] == 1
    assert queue.published(conn, item_id, 5, "https://klubok.com/item/5") is True
    assert (row(conn, item_id)["klubok_id"], row(conn, item_id)["published_at"] is not None) == (
        5, True
    )  # fmt: skip
    assert queue.problem(conn, item_id, "x", "y") is False


def test_recover_publishing_to_uncertain_problem(conn, root):
    item_id = add(conn, root, 7301)
    queue.sync(conn, root, TODAY)
    with conn:
        conn.execute("INSERT INTO sessions (id, started_at, site) VALUES (1, 'x', 'klubok')")
    queue.start(conn, item_id, 1)

    assert queue.recover(conn) == [item_id]

    got = row(conn, item_id)
    assert (got["state"], got["problem_code"]) == (queue.PROBLEM, queue.UNCERTAIN)
    assert "Перевірте на Клубку" in got["problem_text"]
    queue.sync(conn, root, TODAY)  # повтору немає: sync Проблему не чіпає
    assert row(conn, item_id)["state"] == queue.PROBLEM


# --- налаштування (D9) ---


def test_settings_validated_with_same_bounds():
    form = {o.name: o.default for o in options.OPTIONS} | {options.ROOT_SETTING: ""}
    form |= {queue.LIMIT_SETTING: "31", queue.FROM_SETTING: "0"}
    _, errors, _ = options.check(form)
    assert errors[queue.LIMIT_SETTING] == "ціле число від 1 до 30"
    assert errors[queue.FROM_SETTING] == "ціле число від 1 до 1000000"
    defaults = {o.name: o.default for o in options.OPTIONS}
    assert (defaults[queue.FLAG], defaults[queue.LIMIT_SETTING], defaults[queue.FROM_SETTING]) == (
        "", "10", "7200"
    )  # fmt: skip


def test_reasons_cover_every_code():
    codes = {"klubok_no_category", "klubok_size", "klubok_no_price", "klubok_title_long",
             "klubok_no_title", "klubok_no_photos"}  # fmt: skip
    assert codes <= set(queue.REASONS)
    assert all(text and not text.startswith("klubok") for text in queue.REASONS.values())


def test_old_post_photos_kept_while_klubok_needs_them(conn, root, monkeypatch):
    monkeypatch.setenv(publish.DELETE_SETTING, "1")
    for post_id in (7301, 7100):
        add(conn, root, post_id, folder=f"borh-{post_id}", status=Status.PUBLISHED, photos=False)
        folder = folders.old_root() / str(post_id)
        folder.mkdir(parents=True)
        make_photo(folder / "IMG_1.JPG", (400, 300))
    with conn:
        conn.execute("UPDATE items SET published_at = '2026-11-01T10:00:00+00:00'")

    from shafa import borh

    assert borh.purge_photos(conn, "2999-01-01T00:00:00+00:00") == ["borh-7100"]
    assert (folders.old_root() / "7301" / "IMG_1.JPG").is_file()
