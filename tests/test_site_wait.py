"""SITE-002: на сайт — через 7 днів; видалене з каналу = продано. Telegram і сайт — підставні
(`MockTransport`), без мережі; справжніх даних і `.env` тести не бачать (conftest)."""

import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from shafa import site
from shafa.bot import presence
from shafa.bot.presence import GONE, PRESENT, UNKNOWN, Presence
from shafa.bot.telegram import Telegram
from shafa.site import Notify, Site, Worker
from test_site import KEY, POSTED, TEXT, Clock, FakeAi, FakeSite, add_post, job

BASE = "https://site.test/api/integration/v1"
CHANNEL = -1002394827824
OWNER = 222


class FakeChannel:
    """Підставний Telegram: пости каналу, пересилання в службовий чат і видалення копій."""

    def __init__(self, posts=()):
        self.posts = set(posts)
        self.copies = {}  # id копії → номер поста (ще не видалені)
        self.calls = []  # (метод, параметри)
        self.next_copy = 9000
        self.mode = None  # None / "down" / "429" / "protected" / "nodelete"

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        if self.mode == "down" and method == "forwardMessage":
            raise httpx2.ConnectError("немає зв'язку", request=request)
        if self.mode == "429" and method == "forwardMessage":
            body = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry",
                    "parameters": {"retry_after": 100}}  # fmt: skip
            return httpx2.Response(429, json=body)
        if self.mode == "protected" and method == "forwardMessage":
            description = "Bad Request: message has protected content and can't be forwarded"
            return httpx2.Response(400, json={"ok": False, "error_code": 400,
                                              "description": description})  # fmt: skip
        if method == "forwardMessage":
            assert params["disable_notification"] is True
            assert params["from_chat_id"] == CHANNEL and params["chat_id"] == OWNER
            if params["message_id"] not in self.posts:
                description = "Bad Request: message to forward not found"
                return httpx2.Response(400, json={"ok": False, "error_code": 400,
                                                  "description": description})  # fmt: skip
            self.next_copy += 1
            self.copies[self.next_copy] = params["message_id"]
            return httpx2.Response(200, json={"ok": True, "result": {"message_id": self.next_copy}})
        if method == "deleteMessage":
            assert params["chat_id"] == OWNER
            if self.mode == "nodelete":
                return httpx2.Response(502, json={"ok": False, "error_code": 502,
                                                  "description": "Bad Gateway"})  # fmt: skip
            self.copies.pop(params["message_id"], None)
            return httpx2.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"неочікуваний метод {method}")  # у каналі нічого не міняємо

    def forwards(self):
        return [p["message_id"] for m, p in self.calls if m == "forwardMessage"]


@pytest.fixture
def channel():
    return FakeChannel(posts={1401})


@pytest.fixture
def fake():
    return FakeSite()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def said():
    return {"owner": [], "seller": [], "log": []}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(site.KEY, KEY)
    monkeypatch.setenv(site.SEND_SETTING, "1")
    monkeypatch.setattr(
        site, "NIGHT_HOUR", 0
    )  # нічна перевірка — з будь-якої години (окрім тесту годин)


def checker(channel, said):
    tg = Telegram("123:секрет", httpx2.Client(transport=httpx2.MockTransport(channel)))
    return Presence(tg, CHANNEL, OWNER, log=said["log"].append)


def make(conn, root, fake, clock, said, channel, **extra):
    return Worker(
        conn, root, Notify(said["owner"].append, said["seller"].append),
        site_for=lambda key: Site(key, httpx2.Client(transport=httpx2.MockTransport(fake)), BASE),
        ai_client=lambda: FakeAi(), clock=clock, log=said["log"].append,
        presence=checker(channel, said), **extra,
    )  # fmt: skip


@pytest.fixture
def worker(conn, root, fake, clock, said, channel):
    return make(conn, root, fake, clock, said, channel)


def steps(worker, n=40):
    for _ in range(n):
        worker.step()


def sold_switch_off(monkeypatch):
    monkeypatch.setenv(site.SOLD_SETTING, "0")


# --- Черга: 7 днів ---


def test_waits_7_days_by_default_then_goes_with_check(conn, root, on, worker, fake, clock, channel):
    add_post(conn, root)

    row = job(conn)
    assert row["state"] == "waiting"
    assert row["not_before"] == "2026-10-24T09:30:00+00:00"  # пост 12:30 за Києвом + 7 днів
    clock.pass_(days=7)  # 09:00 UTC 24.10 — ще пів години
    steps(worker)
    assert fake.sent() == [] and channel.forwards() == []

    clock.pass_(minutes=31)
    steps(worker)

    assert channel.forwards()[0] == 1401
    assert job(conn)["state"] == "done"
    assert fake.products[1401]["description"] == TEXT


def test_wait_days_setting_zero_goes_at_once(conn, root, on, monkeypatch, worker, channel):
    monkeypatch.setenv(site.WAIT_SETTING, "0")
    add_post(conn, root)

    assert (job(conn)["state"], job(conn)["not_before"]) == ("fields", None)


@pytest.mark.parametrize("raw", ["", "так", "-1", "61"])
def test_bad_wait_days_setting_means_7(monkeypatch, raw):
    monkeypatch.setenv(site.WAIT_SETTING, raw)
    assert site.wait_days() == 7


def test_deleted_before_7_days_never_goes_to_site(conn, root, on, worker, fake, clock, channel):
    add_post(conn, root)
    channel.posts.discard(1401)  # продано на третій день
    clock.pass_(days=8)

    steps(worker)

    assert job(conn)["state"] == "cancelled"
    assert fake.sent() == []
    assert site.summary(conn, clock()) == (0, 0, 1, 0)  # продано за тиждень


def test_present_after_7_days_goes_with_edited_text(conn, root, on, worker, fake, clock, channel):
    add_post(conn, root)
    new_text = "Шкіряні черевики Ecco 38 розміру. 1200грн\n#VS_38_розмір"
    assert site.edited(conn, 1401, new_text)
    clock.pass_(days=8)

    steps(worker)

    assert fake.products[1401]["description"] == new_text
    assert fake.products[1401]["price"] == 1200
    # SITE-003: postedAt — момент відправки (UTC), не дата поста — щоб пара була в «Новинках»
    assert fake.products[1401]["postedAt"] == clock().isoformat(timespec="seconds")
    assert fake.products[1401]["postedAt"] == "2026-10-25T09:00:00+00:00"
    assert job(conn)["posted_at"] == POSTED
    assert not site.edited(conn, 1401, "ще раз")  # уже не чекає — текст сайту не міняємо


@pytest.mark.parametrize("mode", ["down", "429", "protected"])
def test_network_429_or_other_error_is_not_sold(conn, root, on, worker, fake, clock, channel, mode):
    add_post(conn, root)
    channel.mode = mode
    clock.pass_(days=8)

    steps(worker)

    assert job(conn)["state"] == "waiting"  # не cancelled: «невідомо» — не продаж
    assert fake.sent() == []
    assert conn.execute("SELECT gone_at FROM post_checks").fetchone()[0] is None

    channel.mode = None
    clock.pass_(hours=1)
    steps(worker)
    assert job(conn)["state"] == "done"


def test_429_pauses_checks_for_retry_after(conn, root, on, worker, clock, channel):
    add_post(conn, root)
    channel.mode = "429"
    clock.pass_(days=8)

    worker.step()
    channel.mode = None
    clock.pass_(seconds=31)  # власний повтор поста (30 с) минув, а Retry-After (100 с) — ні
    worker.step()

    assert channel.forwards() == [1401]
    clock.pass_(seconds=70)
    worker.step()
    assert channel.forwards() == [1401, 1401]


def test_copy_in_service_chat_deleted_every_time(conn, root, on, worker, clock, channel):
    add_post(conn, root)
    clock.pass_(days=8)

    steps(worker)

    assert channel.copies == {}
    deleted = [p["message_id"] for m, p in channel.calls if m == "deleteMessage"]
    assert deleted == [9001]


def test_copy_not_deleted_retried_on_next_check(said, channel):
    check = checker(channel, said)
    channel.posts.add(1500)
    channel.mode = "nodelete"
    assert check.check(1500) == PRESENT
    assert channel.copies == {9001: 1500}

    channel.mode = None
    assert check.check(1401) == PRESENT

    assert channel.copies == {}


def test_presence_answers(said, channel):
    check = checker(channel, said)
    assert check.check(1401) == PRESENT
    assert check.check(77) == GONE
    channel.mode = "down"
    assert check.check(1401) == UNKNOWN


def test_service_chat_owner_first():
    assert presence.service_chat(1, 2) == 1
    assert presence.service_chat(None, 2) == 2
    assert presence.service_chat(None, None) is None


def test_switch_off_nothing_checked_or_marked(conn, root, on, monkeypatch, worker, clock, channel):
    add_post(conn, root)
    monkeypatch.setenv(site.SEND_SETTING, "0")
    channel.posts.clear()
    clock.pass_(days=8)

    steps(worker)

    assert channel.calls == []
    assert job(conn)["state"] == "waiting"


# --- Продане: щоночі ---


def done_post(conn, root, worker, clock, post_id=1401):
    add_post(conn, root, post_id=post_id)
    clock.pass_(days=8)
    steps(worker)
    assert job(conn, post_id)["state"] == "done"


def test_gone_after_site_marked_sold_after_night(
    conn, root, on, worker, fake, clock, said, channel
):
    done_post(conn, root, worker, clock)
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)

    assert fake.sold == {1401}
    assert fake.paths()[-1] == "POST /products/by-post/1401/sold"
    row = conn.execute("SELECT * FROM post_checks WHERE post_id = 1401").fetchone()
    assert row["gone_at"] is not None and row["sold_at"] is not None and row["hold"] is None
    assert site.summary(conn, clock())[2:] == (1, 0)
    assert said["owner"] == []


def test_sold_repeat_changed_false_is_fine(conn, root, on, worker, fake, clock, said, channel):
    done_post(conn, root, worker, clock)
    body = {"id": "x", "postId": 1401, "status": "Sold", "changed": False}  # уже було
    fake.fail("POST", "/products/by-post/1401/sold", httpx2.Response(200, json=body))
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)

    assert conn.execute("SELECT sold_at FROM post_checks").fetchone()[0] is not None
    assert any("(уже було)" in line for line in said["log"])


def test_sold_404_pair_missing_noted_and_never_resent(conn, root, on, worker, fake, clock, said,
                                                      channel):  # fmt: skip
    done_post(conn, root, worker, clock)
    body = {"postId": 1401, "error": "not_found"}  # пару видалили на сайті після списку
    fake.fail("POST", "/products/by-post/1401/sold", httpx2.Response(404, json=body))
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)
    clock.pass_(days=1)
    steps(worker)

    assert fake.paths().count("POST /products/by-post/1401/sold") == 1
    row = conn.execute("SELECT sold_at, sold_note FROM post_checks").fetchone()
    assert row["sold_at"] is None and "немає" in row["sold_note"]


def test_sold_route_missing_tried_next_night(conn, root, on, worker, fake, clock, said, channel):
    """API-023 ще не розгорнуто: 404 без тіла — нічого не ламаємо, наступної ночі — знову."""
    done_post(conn, root, worker, clock)
    fake.sold_api = False
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)
    steps(worker)  # до кінця доби не повторюємо

    sold = "POST /products/by-post/1401/sold"
    assert fake.paths().count(sold) == 1
    assert conn.execute("SELECT sold_at FROM post_checks").fetchone()[0] is None
    assert any("ще не вміє «Продано»" in line for line in said["log"])
    assert any("списку пар у API сайту ще немає" in line for line in said["log"])
    assert job(conn)["state"] == "done" and said["owner"] == []

    fake.sold_api = True
    clock.pass_(days=1)
    steps(worker)
    assert fake.paths().count(sold) == 2 and fake.sold == {1401}


@pytest.mark.parametrize(("status", "retry"), [(500, True), (429, True), (400, False)])
def test_sold_errors(conn, root, on, worker, fake, clock, said, channel, status, retry):
    done_post(conn, root, worker, clock)
    channel.posts.discard(1401)
    sold = "/products/by-post/1401/sold"
    fake.fail("POST", sold, httpx2.Response(status, json={}))
    clock.pass_(days=1)

    steps(worker)
    clock.pass_(minutes=10)
    steps(worker)

    assert fake.paths().count("POST " + sold) == (2 if retry else 1)
    assert (fake.sold == {1401}) is retry
    assert any("Продано" in m and "1401" in m for m in said["owner"]) is not retry


def test_sold_network_down_retried_later(conn, root, on, worker, fake, clock, channel):
    done_post(conn, root, worker, clock)
    channel.posts.discard(1401)
    fake.fail("POST", "/products/by-post/1401/sold", httpx2.ConnectError)
    clock.pass_(days=1)

    steps(worker)
    assert fake.sold == set()
    clock.pass_(minutes=6)
    steps(worker)
    assert fake.sold == {1401}


def test_night_unknown_is_not_sold(conn, root, on, worker, clock, said, channel):
    done_post(conn, root, worker, clock)
    channel.mode = "down"
    clock.pass_(days=1)

    steps(worker)

    assert conn.execute("SELECT gone_at FROM post_checks").fetchone()[0] is None


def test_night_once_a_day_and_not_before_3(conn, root, on, monkeypatch, fake, said, channel):
    monkeypatch.setattr(site, "NIGHT_HOUR", 3)
    clock = Clock()
    clock.now = datetime(2026, 10, 18, 2, 0).astimezone().astimezone(UTC)  # 02:00 місцевого
    worker = make(conn, root, fake, clock, said, channel)
    fake.listed = {1401}

    steps(worker, 5)
    assert channel.forwards() == []

    clock.pass_(hours=1, minutes=1)  # 03:01
    steps(worker, 5)
    assert channel.forwards() == [1401]

    clock.pass_(hours=5)
    steps(worker, 5)
    assert channel.forwards() == [1401]  # сьогодні вже було

    clock.pass_(hours=20)  # наступна доба
    steps(worker, 5)
    assert channel.forwards() == [1401, 1401]


def test_night_switched_off_in_settings(conn, root, on, monkeypatch, worker, clock, channel):
    sold_switch_off(monkeypatch)
    with conn:
        conn.execute("INSERT INTO post_checks (post_id, old) VALUES (1401, 1)")

    steps(worker)

    assert channel.calls == []


def test_old_pairs_rotated_with_limit_and_checked_at(conn, root, on, fake, clock, said, channel):
    old = [11, 12, 13, 14, 15]
    channel.posts.update(old)
    fake.listed = set(old)
    worker = make(conn, root, fake, clock, said, channel, night_max=2)

    steps(worker, 10)
    assert channel.forwards() == [11, 12]
    clock.pass_(days=1)
    steps(worker, 10)
    assert channel.forwards()[2:] == [13, 14]
    clock.pass_(days=1)
    steps(worker, 10)
    assert channel.forwards()[4:] == [15, 11]  # по колу: найдавніше перевірені — першими

    rows = conn.execute("SELECT post_id, old, checked_at FROM post_checks ORDER BY post_id")
    assert all(r["old"] == 1 and r["checked_at"] for r in rows)


def test_checks_spaced_about_one_per_second(conn, root, on, worker, fake, channel):
    fake.listed = {1401}
    assert 1.0 <= worker.step() < 2
    assert channel.forwards() == [1401]


def test_site_list_once_a_night_sold_in_admin_not_checked(conn, root, on, worker, fake, clock,
                                                          said, channel):  # fmt: skip
    fake.listed = {11, 12}
    channel.posts.update({11, 12})
    steps(worker, 10)
    assert fake.paths() == ["GET /products/post-ids"]
    assert channel.forwards() == [11, 12]

    fake.listed = {12, 13}  # 11 продано в адмінці, 13 — нова стара пара
    channel.posts.add(13)
    clock.pass_(days=1)
    steps(worker, 10)

    assert fake.paths() == ["GET /products/post-ids"] * 2
    assert channel.forwards()[2:] == [13, 12]
    row = conn.execute("SELECT off_site_at FROM post_checks WHERE post_id = 11").fetchone()
    assert row["off_site_at"] is not None

    fake.listed = {11, 12, 13}  # повернули на сайт — знову перевіряємо
    clock.pass_(days=1)
    steps(worker, 10)
    assert sorted(channel.forwards()[4:]) == [11, 12, 13]


def test_night_runs_until_6_am(conn, root, on, monkeypatch, fake, said, channel):
    monkeypatch.setattr(site, "NIGHT_HOUR", 3)
    clock = Clock()
    clock.now = datetime(2026, 10, 18, 5, 59, 58).astimezone().astimezone(UTC)  # 05:59:58
    fake.listed = {11, 12, 13, 14}
    channel.posts.update(fake.listed)
    worker = make(conn, root, fake, clock, said, channel)

    for _ in range(10):
        clock.pass_(seconds=worker.step())

    assert channel.forwards() == [11, 12]  # о 06:00 ніч скінчилась; решта — наступної
    assert any("нічна перевірка — перевірено 2" in line for line in said["log"])


def test_unknown_3_days_owner_told_once(conn, root, on, worker, clock, said, channel):
    add_post(conn, root)
    channel.mode = "down"
    clock.pass_(days=8)
    for _ in range(4 * 24):  # 4 доби, щогодини
        steps(worker, 3)
        clock.pass_(hours=1)

    told = [m for m in said["owner"] if "3 доби" in m]
    assert len(told) == 1 and "1401" in told[0]
    assert job(conn)["state"] == "waiting"


def test_summary_counts(conn, root, on, clock):
    add_post(conn, root, post_id=1)
    add_post(conn, root, post_id=2)
    with conn:
        conn.execute(
            "UPDATE site_jobs SET not_before = ? WHERE post_id = 1",
            ((clock() + timedelta(minutes=1)).isoformat(timespec="seconds"),),
        )
        conn.execute(
            "INSERT INTO post_checks (post_id, gone_at) VALUES (5, ?), (6, '2026-01-01T00:00:00+00:00')",
            (clock().isoformat(timespec="seconds"),),
        )

    assert site.summary(conn, clock()) == (2, 1, 1, 0)


def test_enqueue_keeps_posted_time_zone_independent(conn, root, on):
    add_post(conn, root)
    not_before = datetime.fromisoformat(job(conn)["not_before"])
    assert not_before == datetime.fromisoformat(POSTED) + timedelta(days=7)


def test_server_error_with_not_found_text_is_unknown(said, channel):
    """Лише 400 «повідомлення немає» = видалено; 5xx з будь-яким текстом — невідомо."""

    def broken(request):
        body = {"ok": False, "error_code": 500, "description": "message to forward not found"}
        return httpx2.Response(500, json=body)

    tg = Telegram("123:секрет", httpx2.Client(transport=httpx2.MockTransport(broken)))
    assert Presence(tg, CHANNEL, OWNER, log=said["log"].append).check(1401) == UNKNOWN


# --- Запобіжник (SITE-003) ---


def many_gone(conn, fake, channel, n):
    """n старих пар сайту, пости яких зникли з каналу."""
    fake.listed = set(range(100, 100 + n))
    channel.posts.difference_update(fake.listed)


def sold_posts(fake):
    return [p for p in fake.paths() if p.endswith("/sold")]


def test_fuse_21_gone_in_one_night_nothing_sent_owner_told(conn, root, on, worker, fake, clock,
                                                           said, channel):  # fmt: skip
    many_gone(conn, fake, channel, 21)

    steps(worker, 40)

    assert sold_posts(fake) == []
    assert said["owner"] == [
        (
            "За ніч зникло 21 постів — схоже на збій, “Продано” не надіслано. Перевір канал. Якщо все "
            "правильно — «Налаштування» → «Надіслати відкладені Продано»."
        )
    ]
    assert site.summary(conn, clock())[2:] == (0, 21)

    clock.pass_(days=1)  # наступна ніч: знову перевіряємо; все ще зниклі — чекають Власника
    steps(worker, 40)
    assert sold_posts(fake) == [] and len(said["owner"]) == 1
    assert channel.forwards().count(100) == 2

    assert site.release_held(conn) == 21  # «Надіслати відкладені Продано»
    steps(worker, 40)
    assert len(sold_posts(fake)) == 21 and fake.sold == fake.listed
    assert site.summary(conn, clock())[2:] == (21, 0)


def test_fuse_20_gone_is_normal(conn, root, on, worker, fake, said, channel):
    many_gone(conn, fake, channel, 20)

    steps(worker, 60)

    assert len(sold_posts(fake)) == 20 and said["owner"] == []


def test_fuse_setting(conn, root, on, monkeypatch, worker, fake, said, channel):
    monkeypatch.setenv(site.FUSE_SETTING, "2")
    many_gone(conn, fake, channel, 3)

    steps(worker, 20)

    assert sold_posts(fake) == [] and "зникло 3 постів" in said["owner"][0]


@pytest.mark.parametrize("raw", ["", "0", "абв", "1001"])
def test_bad_fuse_setting_means_20(monkeypatch, raw):
    monkeypatch.setenv(site.FUSE_SETTING, raw)
    assert site.fuse() == 20


def test_fuse_glitch_heals_next_night(conn, root, on, worker, fake, clock, said, channel):
    many_gone(conn, fake, channel, 21)
    steps(worker, 40)
    channel.posts.update(fake.listed)  # збій минув: пости на місці

    clock.pass_(days=1)
    steps(worker, 40)

    assert site.summary(conn, clock())[2:] == (0, 0)
    assert (
        conn.execute("SELECT COUNT(*) FROM post_checks WHERE gone_at IS NOT NULL").fetchone()[0]
        == 0
    )
    assert site.release_held(conn) == 0
    steps(worker, 10)
    assert sold_posts(fake) == []


def test_gone_mid_night_not_sent_before_night_ends(conn, root, on, worker, fake, clock, channel):
    """Перезапуск посеред ночі: зниклі цієї ночі чекають кінця наступної, запобіжник рахує їх."""
    many_gone(conn, fake, channel, 3)
    for _ in range(2):
        worker.step()  # дві перевірки, ніч не скінчилась
    restarted = make(conn, root, fake, clock, {"owner": [], "seller": [], "log": []}, channel)

    steps(restarted, 10)  # ця доба вже була — нічна не починається, «Продано» не шлемо
    assert sold_posts(fake) == []

    clock.pass_(days=1)
    steps(restarted, 20)
    assert len(sold_posts(fake)) == 3


@pytest.mark.parametrize(
    ("status", "description", "expected"),
    [
        (400, "Bad Request: message to forward not found", GONE),
        (400, "Bad Request: MESSAGE_ID_INVALID", GONE),
        (400, "Bad Request: chat not found", UNKNOWN),
        (400, "Bad Request: message has protected content and can't be forwarded", UNKNOWN),
        (403, "Forbidden: bot is not a member of the channel chat", UNKNOWN),
        (403, "Forbidden: message to forward not found", UNKNOWN),
        (429, "Too Many Requests: retry after 5", UNKNOWN),
        (502, "Bad Gateway", UNKNOWN),
    ],
)
def test_only_explicit_not_found_is_gone(said, status, description, expected):
    def answer(request):
        body = {"ok": False, "error_code": status, "description": description}
        return httpx2.Response(status, json=body)

    tg = Telegram("123:секрет", httpx2.Client(transport=httpx2.MockTransport(answer)))
    assert Presence(tg, CHANNEL, OWNER, log=said["log"].append).check(1401) == expected
