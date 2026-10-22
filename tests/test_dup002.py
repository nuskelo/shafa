"""DUP-002: «наше» оголошення без фото в знімку — фото беремо з папки Картки або зі сторінки
оголошення; пост, з якого опубліковано знайдене, іде ШІ фактом. Жодних живих запитів."""

import shutil

import pytest

from shafa import dup, review
from shafa.db import Status
from shafa.folders import PUBLISHED_DIR
from shafa.robot import form, listings, lock, publish
from shafa.robot.form import Submitted
from shafa.robot.listings import Listing
from test_dup001 import Client, card_of, jpeg, reply
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    SAME,
    Env,
    new_cards_allowed,
    quiet,
    row,
    state,
)


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def old_card(env, folder="1766", post_id=1766, shafa_id=SAME.id):
    """Раніше опублікована нашою програмою Картка, яку Шафа віддала без фото."""
    from test_review import add_card

    item = add_card(
        env.conn, env.root, folder=folder, post_id=post_id, date="2025-02-01T10:00:00",
        status=Status.PUBLISHED,
    )  # fmt: skip
    with env.conn:
        env.conn.execute(
            "UPDATE items SET shafa_id = ?, shafa_url = ? WHERE id = ?", (shafa_id, SAME.url, item)
        )
    listings.add(env.conn, SAME)
    return item


def run(env, client, fetched=None, pages=None, page_url="https://img.shafa.ua/p.jpg", results=()):
    env.listings = [SAME]
    env.results = list(results)
    fetched = [] if fetched is None else fetched
    pages = [] if pages is None else pages

    def page_photo(url):
        pages.append(url)
        return page_url

    def fetch_photo(url):
        fetched.append(url)
        return jpeg()

    with lock.hold("publish", env.tmp / "robot.lock") as held:
        return publish.Run(
            env.conn, env.root, env.session, env.make_driver, env.load, env.tmp / "екрани", held,
            log=env.lines.append, sleep=lambda s: None, rand=lambda a, b: 0.0,
            today=lambda: env.today, fetch_photo=fetch_photo, page_photo=page_photo,
            photos_dir=env.tmp / "duplicates",
            judge=dup.Judge(env.conn, lambda: client, lambda: 1_000_000),
        ).start(30, 30)  # fmt: skip


def images(client):
    content = client.requests[0]["messages"][0]["content"]
    return [c for c in content if c["type"] == "image"], content[-1]["text"]


# --- Фото «нашого» оголошення ---


def test_own_card_photo_from_folder_goes_to_ai_without_touching_shafa(env):
    item = card_of(env)
    old_card(env)
    client, fetched, pages = Client(reply("no", "Інший колір.")), [], []
    run(env, client, fetched, pages)
    assert state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)
    assert fetched == [] and pages == []  # ні сторінки, ні картинки на Шафі
    assert len(images(client)[0]) == 2
    assert (env.tmp / "duplicates" / f"{item}.jpg").is_file()  # екран теж бачить фото


def test_own_card_photo_found_in_published_folder(env):
    item = card_of(env)
    other = old_card(env)
    folder = row(env.conn, other)["folder_name"]
    (env.root / PUBLISHED_DIR).mkdir()
    shutil.move(env.root / folder, env.root / PUBLISHED_DIR / folder)
    client, pages = Client(reply("no")), []
    run(env, client, pages=pages)
    assert state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)
    assert pages == [] and len(images(client)[0]) == 2


def test_folder_gone_takes_main_photo_from_listing_page_and_remembers_it(env):
    item = card_of(env)
    other = old_card(env)
    shutil.rmtree(env.root / row(env.conn, other)["folder_name"])  # PHOTO-001 прибрав
    client, fetched, pages = Client(reply("no", "Інший колір.")), [], []
    run(env, client, fetched, pages)
    assert pages == [SAME.url] and fetched == ["https://img.shafa.ua/p.jpg"]
    assert state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)
    assert len(images(client)[0]) == 2
    stored = env.conn.execute("SELECT photo FROM my_listings WHERE id = ?", (SAME.id,)).fetchone()
    assert stored[0] == "https://img.shafa.ua/p.jpg"  # наступного разу не шукаємо


def test_not_our_listing_takes_photo_from_page_too(env):
    item = card_of(env)
    listings.add(env.conn, SAME)  # Картки з таким shafa_id немає
    client, pages = Client(reply("no")), []
    run(env, client, pages=pages)
    assert pages == [SAME.url] and state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)


def test_page_without_photo_keeps_unsure_and_does_not_ask_ai(env):
    item = card_of(env)
    listings.add(env.conn, SAME)
    client = Client(reply("no"))
    run(env, client, page_url=None)
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert client.requests == [] and "немає фото" in row(env.conn, item)["problem_text"]


def test_listing_with_photo_in_snapshot_does_not_open_page(env):
    item = card_of(env)
    env.listings = [Listing(**{**SAME.__dict__, "photo": "https://img.shafa.ua/s.jpg"})]
    fetched, pages = [], []
    with lock.hold("publish", env.tmp / "robot.lock") as held:
        publish.Run(
            env.conn, env.root, env.session, env.make_driver, env.load, env.tmp / "екрани", held,
            log=env.lines.append, sleep=lambda s: None, rand=lambda a, b: 0.0,
            today=lambda: env.today, fetch_photo=lambda u: fetched.append(u) or jpeg(),
            page_photo=lambda u: pages.append(u),
            judge=dup.Judge(env.conn, lambda: Client(reply("no")), lambda: 1_000_000),
        ).start(30, 30)  # fmt: skip
    assert fetched == ["https://img.shafa.ua/s.jpg"] and pages == []
    assert state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)


# --- Фото при публікації ---


def test_published_listing_gets_photo_from_createproduct_response(env):
    card_of(env)
    env.listings = []
    env.results = [Submitted(True, product_id=700, photo="https://img.shafa.ua/new.jpg")]
    env.run()
    stored = env.conn.execute("SELECT photo FROM my_listings WHERE id = 700").fetchone()
    assert stored[0] == "https://img.shafa.ua/new.jpg"


def test_published_listing_without_photo_in_response_stays_empty(env):
    card_of(env)
    env.listings = []
    env.results = [Submitted(True, product_id=701)]
    env.run()
    assert env.conn.execute("SELECT photo FROM my_listings WHERE id = 701").fetchone()[0] is None


def test_outcome_reads_thumbnail_of_created_product():
    body = {"data": {"createProduct": {"createdProduct": {"id": "5", "thumbnail": "https://i/x"}}}}
    assert form.outcome(True, False, body).photo == "https://i/x"
    plain = {"data": {"createProduct": {"createdProduct": {"id": "5"}}}}
    assert form.outcome(True, False, plain).photo is None


def test_set_photo_does_not_overwrite_existing(conn):
    listings.add(conn, Listing(**{**SAME.__dict__, "photo": "https://i/old"}))
    listings.set_photo(conn, SAME.id, "https://i/new")
    assert conn.execute("SELECT photo FROM my_listings").fetchone()[0] == "https://i/old"


# --- Інший пост ---


def test_other_post_goes_to_ai_as_fact(env):
    card_of(env)
    old_card(env)
    client = Client(reply("no"))
    run(env, client)
    text = images(client)[1]
    assert "з іншого поста 1766 (новий товар — з поста 1342)" in text


def test_foreign_listing_has_no_post_fact(env):
    card_of(env)
    listings.add(env.conn, SAME)
    client = Client(reply("no"))
    run(env, client)
    assert "іншого поста" not in images(client)[1]


def test_same_post_has_no_fact(env):
    item = card_of(env)
    other = old_card(env)
    card = review.load_card(env.conn, item)
    found = Listing(**SAME.__dict__)
    assert dup.other_post(env.conn, card, found) == 1766
    with env.conn:
        env.conn.execute("UPDATE items SET post_id = ? WHERE id = ?", (card.post_id, other))
    assert dup.other_post(env.conn, card, found) is None


@pytest.mark.parametrize("same", ["unsure", "yes"])
def test_other_post_does_not_publish_by_itself(env, same):
    item = card_of(env)
    old_card(env)
    run(env, Client(reply(same, "Не знаю.")))
    assert state(env.conn, item)[0] != Status.PUBLISHED
