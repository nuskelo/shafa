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
                    "parameters": {"retry_after": 17}}  # fmt: skip
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
    assert fake.requests == [] and channel.forwards() == []

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
    assert fake.requests == []
    assert site.summary(conn, clock()) == (0, 0, 1)  # продано за тиждень


def test_present_after_7_days_goes_with_edited_text(conn, root, on, worker, fake, clock, channel):
    add_post(conn, root)
    new_text = "Шкіряні черевики Ecco 38 розміру. 1200грн\n#VS_38_розмір"
    assert site.edited(conn, 1401, new_text)
    clock.pass_(days=8)

    steps(worker)

    assert fake.products[1401]["description"] == new_text
    assert fake.products[1401]["price"] == 1200
    assert not site.edited(conn, 1401, "ще раз")  # уже не чекає — текст сайту не міняємо


@pytest.mark.parametrize("mode", ["down", "429", "protected"])
def test_network_429_or_other_error_is_not_sold(conn, root, on, worker, fake, clock, channel, mode):
    add_post(conn, root)
    channel.mode = mode
    clock.pass_(days=8)

    steps(worker)

    assert job(conn)["state"] == "waiting"  # не cancelled: «невідомо» — не продаж
    assert fake.requests == []
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
    worker.step()

    assert channel.forwards() == [1401]  # другої спроби до кінця Retry-After немає
    clock.pass_(seconds=18)
    worker.step()
    assert len(channel.forwards()) == 1  # job чекає свого next_at (повтор через 30 с)
    clock.pass_(seconds=30)
    worker.step()
    assert len(channel.forwards()) == 2


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


def test_gone_after_site_marked_sold_through_interface(conn, root, on, fake, clock, said, channel):
    sold = []
    worker = make(conn, root, fake, clock, said, channel,
                  mark_sold=lambda s, p: sold.append(p) or True)  # fmt: skip
    done_post(conn, root, worker, clock)
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)

    assert sold == [1401]
    row = conn.execute("SELECT * FROM post_checks WHERE post_id = 1401").fetchone()
    assert row["gone_at"] is not None and row["sold_at"] is not None
    assert site.summary(conn, clock())[2] == 1


def test_sold_stub_only_logs_td(conn, root, on, worker, clock, said, channel):
    done_post(conn, root, worker, clock)
    channel.posts.discard(1401)
    clock.pass_(days=1)

    steps(worker)

    row = conn.execute("SELECT * FROM post_checks WHERE post_id = 1401").fetchone()
    assert row["gone_at"] is not None and row["sold_at"] is None
    assert any("TD-035" in line and "Продано" in line for line in said["log"])


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
    with conn:
        conn.execute("INSERT INTO post_checks (post_id, old) VALUES (1401, 1)")

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
    worker = make(conn, root, fake, clock, said, channel, site_list=lambda s: old, night_max=2)

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


def test_checks_spaced_about_one_per_second(conn, root, on, worker, channel):
    with conn:
        conn.execute("INSERT INTO post_checks (post_id, old) VALUES (1401, 1)")
    assert worker.step() >= 1.0


def test_no_site_list_stub_logged(conn, root, on, worker, said):
    steps(worker, 3)
    assert any("TD-035" in line and "старі" in line for line in said["log"])


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

    assert site.summary(conn, clock()) == (2, 1, 1)


def test_enqueue_keeps_posted_time_zone_independent(conn, root, on):
    add_post(conn, root)
    not_before = datetime.fromisoformat(job(conn)["not_before"])
    assert not_before == datetime.fromisoformat(POSTED) + timedelta(days=7)
