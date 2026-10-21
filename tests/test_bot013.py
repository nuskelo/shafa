"""BOT-013: решта фонових подій у журнал і в бот — канал, сайт, «Продано», фото, програма,
самоперевірка. Усе пишеться лише в `tmp_path`; Telegram, сайт і ШІ — підставні."""

import json
from datetime import timedelta

import pytest

from conftest import make_folder
from shafa import app, borh, changes, config, events, health, scan, site
from shafa.bot import alerts
from shafa.bot.core import Bot, Settings
from shafa.db import Status
from shafa.export import Post
from shafa.folders import list_folders
from shafa.health import FAIL, OK, SKIP, WARN, Check
from shafa.robot import browser
from test_bot009 import DAY, Clock, at
from test_bot012 import QuietTelegram, journal, owner_texts, put, started, step
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER
from test_core015 import card_in_folder, channel_edit
from test_core015 import make as stale_card
from test_core015 import make_bot as core015_bot
from test_photo001 import old_card
from test_robot_publish import Env, add, bot_post
from test_site import KEY, FakeSite, add_post, job
from test_site import Clock as SiteClock
from test_site_wait import FakeChannel, many_gone, sold_posts, steps
from test_site_wait import make as make_worker

BASE_TEXT = "Кросівки Asics 38 розміру. 1200грн"


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "дані"
    folder.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setenv("SHAFA_DELETE_PUBLISHED", "1")
    monkeypatch.delenv("SHAFA_ALERTS", raising=False)
    return folder


@pytest.fixture
def tg():
    return QuietTelegram()


@pytest.fixture
def make_bot(conn, tg, root):
    def make(clock):
        settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
        return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)

    return make


def kinds(data):
    return [r["kind"] for r in journal(data)]


def texts(data, kind=None):
    return [r["text"] for r in journal(data) if kind in (None, r["kind"])]


# --- Таблиця видів ---


@pytest.mark.parametrize(
    ("kind", "emoji", "loud", "skip"),
    [
        ("post.new", "📥", False, False),
        ("post.changed", "✏️", False, False),
        ("post.gone", "🗑", False, False),
        ("post.failed", "⚠️", False, True),
        ("site.sent", "🌐", False, False),
        ("site.failed", "⚠️", True, True),
        ("site.fuse", "⛔", True, True),
        ("site.night", "🌙", False, False),
        ("site.sold", "💰", False, False),
        ("site.morning", "☀️", False, False),
        ("photos.purged", "🧹", False, False),
        ("app.start", "🟢", False, False),
        ("app.stop", "🔴", False, False),
        ("health.ok", "🩺", False, False),
    ],
)
def test_new_kinds_are_in_the_table(kind, emoji, loud, skip):
    assert events.info(kind) == events.Kind(emoji, loud, skip)


# --- 1. Канал: новий пост, зміна, видалення ---


def run_scan(conn, root, posts):
    return scan.scan(conn, posts, list_folders(root, "2394827824"))


def test_new_post_is_announced_with_photos_and_size(conn, root, data):
    make_folder(root, "7027", ["1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.jpg"])

    run_scan(conn, root, {7027: Post(7027, "2026-10-21T10:00:00", BASE_TEXT)})

    assert texts(data, "post.new") == ["Новий пост 7027: 5 фото, розмір 38 → чекає ШІ"]


def test_second_scan_announces_nothing(conn, root, data):
    make_folder(root, "7027", ["1.jpg"])
    posts = {7027: Post(7027, "2026-10-21T10:00:00", BASE_TEXT)}
    run_scan(conn, root, posts)
    run_scan(conn, root, posts)

    assert texts(data, "post.new") == ["Новий пост 7027: 1 фото, розмір 38 → чекає ШІ"]


def test_problem_card_is_left_to_bot010(conn, root, data):
    make_folder(root, "7028", ["1.jpg"])

    run_scan(conn, root, {7028: Post(7028, "2026-10-21T10:00:00", "Без ціни")})

    assert conn.execute("SELECT status FROM items").fetchone()[0] == Status.PROBLEM
    assert texts(data, "post.new") == []  # «Проблеми» шле alerts._problem_events


def test_many_new_posts_go_in_one_line(conn, root, data):
    posts = {}
    for n in range(7001, 7009):
        make_folder(root, str(n), ["1.jpg"])
        posts[n] = Post(n, "2026-10-21T10:00:00", BASE_TEXT)

    run_scan(conn, root, posts)

    assert texts(data, "post.new") == [
        "Нових постів: 8 (7001, 7002, 7003, 7004, 7005 …) → чекають ШІ"
    ]


def test_text_change_of_checked_card_goes_back_to_review(conn, root, data):
    stale_card(conn, root)

    changes.text_changed(conn, 1342, "Кросівки Asics 38 розміру. 800грн")

    assert texts(data, "post.changed") == ["1342: текст змінено → знову «На перевірку»"]


def test_text_change_that_makes_a_problem_says_so(conn, root, data):
    stale_card(conn, root)

    changes.text_changed(conn, 1342, "Кросівки Asics 38 розміру")  # ціну прибрали

    assert texts(data, "post.changed") == ["1342: текст змінено → у «Проблемах»"]


def test_text_change_after_publication_is_told(conn, root, data):
    stale_card(conn, root, status=Status.PUBLISHED)

    changes.text_changed(conn, 1342, "Кросівки Asics 38 розміру. 800грн")

    [line] = texts(data, "post.changed")
    assert line.startswith("1342: текст змінено після публікації на Шафі (Ціна на Шафі")


def test_same_text_says_nothing(conn, root, data):
    stale_card(conn, root)

    changes.text_changed(conn, 1342, BASE_TEXT)

    assert journal(data) == []


def test_bot_channel_edit_writes_the_event(conn, root, data):
    stale_card(conn, root)
    bot, _log = core015_bot(conn, root)

    channel_edit(bot, 1, "Кросівки Asics 38 розміру. 800грн")

    assert texts(data, "post.changed") == ["1342: текст змінено → знову «На перевірку»"]


def test_photo_change_goes_back_to_review(conn, root, data):
    card_in_folder(conn, root)
    (root / "1342" / "IMG_3.JPG").write_bytes(b"")

    changes.sync_photos(conn, list_folders(root, "2394827824"))

    assert texts(data, "post.changed") == [
        "1342: фото змінено (було 2, стало 3 (додано IMG_3.JPG)) → знову «На перевірку»"
    ]


def test_photos_unchanged_say_nothing(conn, root, data):
    card_in_folder(conn, root)

    changes.sync_photos(conn, list_folders(root, "2394827824"))

    assert journal(data) == []


# --- Видалено з каналу ---


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(site.KEY, KEY)
    monkeypatch.setenv(site.SEND_SETTING, "1")
    monkeypatch.setenv(site.WAIT_SETTING, "0")
    monkeypatch.setattr(site, "NIGHT_HOUR", 0)
    monkeypatch.setattr(site, "MORNING_HOUR", 0)


@pytest.fixture
def fake():
    return FakeSite()


@pytest.fixture
def clock():
    return SiteClock()


@pytest.fixture
def said():
    return {"owner": [], "seller": [], "log": []}


@pytest.fixture
def channel():
    return FakeChannel(posts={1401})


@pytest.fixture
def worker(conn, root, fake, clock, said, channel):
    return make_worker(conn, root, fake, clock, said, channel)


def test_deleted_before_release_is_told(conn, root, data, on, monkeypatch, worker, clock, channel):
    monkeypatch.setenv(site.WAIT_SETTING, "7")
    add_post(conn, root)
    channel.posts.discard(1401)
    clock.pass_(days=8)

    steps(worker)

    assert texts(data, "post.gone") == [
        "1401 видалено з каналу до виходу на сайт — на сайт не піде"
    ]
    assert job(conn)["state"] == "cancelled"


def test_present_post_says_nothing_about_deletion(conn, root, data, on, worker):
    add_post(conn, root)

    steps(worker)

    assert texts(data, "post.gone") == []


# --- 2. Сайт ---


def test_pair_sent_to_site_is_announced_once(conn, root, data, on, worker, fake):
    add_post(conn, root, photos=3)

    steps(worker)

    assert job(conn)["state"] == "done"
    assert texts(data, "site.sent") == ["1401 відправлено на сайт (поля + 3 фото)"]


def test_site_refusal_is_in_journal_but_not_sent_twice_to_owner(
    conn, root, data, on, worker, fake, said, tg, make_bot
):
    import httpx2

    add_post(conn, root)
    fake.fail("PUT", "/products/by-post/1401", httpx2.Response(400, json={"error": "bad"}))

    steps(worker)

    assert job(conn)["state"] == "failed"
    assert len(said["owner"]) == 1  # `Notify.owner` — як і було
    [line] = texts(data, "site.failed")
    assert line.startswith("1401: сайт не прийняв пару")
    # …а бот цей рядок журналу не шле: skip, інакше Власник отримав би двічі
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "site.failed", line)
    step(clock, bot)
    step(clock, bot, 6)
    assert owner_texts(tg) == []


def test_night_summary_lists_gone_posts(conn, root, data, on, worker, fake, channel):
    fake.listed = {6801, 6802, 6806}
    channel.posts.update({6802})  # 6801 і 6806 зникли з каналу

    steps(worker, 40)

    [line] = texts(data, "site.night")
    assert line == ("Ніч: перевірено 3, зникло з каналу 2 (6801, 6806) → «Продано» піде на сайт")
    assert sorted(int(p.split("/")[3]) for p in sold_posts(fake)) == [6801, 6806]
    assert texts(data, "site.sold") == [
        "6801 позначено «Продано» на сайті",
        "6806 позначено «Продано» на сайті",
    ]


def test_night_without_gone_posts_says_so(conn, root, data, on, worker, fake, channel):
    fake.listed = {6802}
    channel.posts.add(6802)

    steps(worker, 20)

    assert texts(data, "site.night") == ["Ніч: перевірено 1, зниклих постів немає"]
    assert texts(data, "site.sold") == []


def test_night_with_nothing_to_check_is_silent(conn, root, data, on, worker, fake):
    steps(worker, 10)

    assert texts(data, "site.night") == []


def test_fuse_is_loud_in_journal_and_told_once(conn, root, data, on, worker, fake, said, channel):
    many_gone(conn, fake, channel, 21)

    steps(worker, 40)

    assert len(said["owner"]) == 1  # Власнику вже пише `Notify.owner`
    [night] = texts(data, "site.night")
    assert "запобіжник" in night and "зникло з каналу 21" in night
    [fuse] = [r for r in journal(data) if r["kind"] == "site.fuse"]
    assert fuse["loud"] is True and "21" in fuse["text"]
    assert sold_posts(fake) == []


def test_fuse_event_is_not_sent_by_bot_again(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "site.fuse", "Запобіжник: за ніч зникло 21 постів", loud=True)
    step(clock, bot)
    step(clock, bot, 6)
    assert owner_texts(tg) == []


def test_morning_line_once_a_day_when_something_is_due(
    conn, root, data, on, monkeypatch, worker, clock
):
    monkeypatch.setenv(site.WAIT_SETTING, "7")
    add_post(conn, root)
    clock.pass_(days=6, hours=23)  # вийде вже сьогодні

    worker.step()  # ранок: пара ще чекає, але вийде сьогодні
    assert texts(data, "site.morning") == ["Сьогодні вийде на сайт пар: 1 (усього чекає 1)"]
    steps(worker, 3)  # пара пішла на сайт
    clock.pass_(hours=3)
    worker.step()
    assert len(texts(data, "site.morning")) == 1  # тієї ж доби вдруге — ні


def test_morning_silent_when_nothing_waits(conn, root, data, on, worker):
    worker.step()
    worker.step()

    assert texts(data, "site.morning") == []


def test_morning_not_before_the_hour(conn, root, data, on, monkeypatch, worker, clock):
    monkeypatch.setattr(site, "MORNING_HOUR", 24)
    monkeypatch.setenv(site.SOLD_SETTING, "ні")  # нічна перевірка тут ні до чого
    monkeypatch.setenv(site.WAIT_SETTING, "7")
    add_post(conn, root)
    clock.pass_(days=6, hours=23)

    worker.step()

    assert texts(data, "site.morning") == []


# --- 3. Фото ---


def test_purged_photos_one_line_with_size(conn, data):
    old_card(conn, data, 100, days=4)
    old_card(conn, data, 200, days=5)
    (data / "borh" / "100" / "1.jpg").write_bytes(b"x" * 5 * 1024 * 1024)
    (data / "borh" / "200" / "1.jpg").write_bytes(b"x" * 7 * 1024 * 1024)

    assert sorted(borh.purge_photos(conn, "2999-01-01T00:00:00+00:00")) == ["borh-100", "borh-200"]

    assert texts(data, "photos.purged") == ["Прибрано фото 2 постів (≈ 12 МБ)"]


def test_single_purged_post_is_in_genitive_singular(conn, data):
    old_card(conn, data, 100, days=4)

    borh.purge_photos(conn, "2999-01-01T00:00:00+00:00")

    assert texts(data, "photos.purged") == ["Прибрано фото 1 поста (≈ 1 МБ)"]


def test_nothing_purged_nothing_said(conn, data):
    old_card(conn, data, 100, days=1)  # ще рано

    assert borh.purge_photos(conn, "2000-01-01T00:00:00+00:00") == []

    assert journal(data) == []


# --- 4. Програма ---


def test_app_start_and_stop_are_in_journal(data):
    seen = {}

    def serve(server):
        seen["during"] = texts(config.data_dir())

    assert app.start(lambda url: None, port=0, serve=serve) == 0

    start, stop = texts(data)
    assert start.startswith("Програму запущено (версія ")
    assert stop == "Програму зупинено"
    assert [len(seen["during"])] == [1]  # під час роботи — лише «запущено»
    assert kinds(data) == ["app.start", "app.stop"]


def test_second_click_writes_nothing(data):
    from shafa.robot import lock

    (data / app.URL_NAME).write_text("http://127.0.0.1:8799/", encoding="utf-8")
    with lock.hold("ui", data / app.LOCK_NAME):
        app.start(lambda url: None, port=0, serve=lambda server: None)

    assert journal(data) == []


def test_quit_command_writes_stop(data):
    from shafa.robot import lock

    held = lock.acquire("ui", data / app.LOCK_NAME)

    def kill(pid, sig):
        held.release()

    assert app.quit_app(kill=kill, wait=1) is True
    assert texts(data, "app.stop") == ["Програму зупинено"]


def test_quit_when_not_running_writes_nothing(data):
    assert app.quit_app(kill=lambda pid, sig: None, wait=0.1) is False
    assert journal(data) == []


def test_version_comes_from_version_file_or_package(tmp_path, monkeypatch):
    monkeypatch.setattr(events.shafa, "__file__", str(tmp_path / "lib" / "shafa" / "__init__.py"))
    (tmp_path / "VERSION").write_text("0.1.0+abc1234 (2026-10-21)\n", encoding="utf-8")
    assert events.version() == "0.1.0+abc1234 (2026-10-21)"
    (tmp_path / "VERSION").unlink()
    monkeypatch.setattr(events.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert events.version() == events.shafa.__version__


def test_stop_arrives_after_next_start_even_hours_later(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "app.start", "Програму запущено (версія 0.1.0+abc)")
    step(clock, bot)  # бот бачив запис — позиція в журналі є
    put(data, at(DAY, 10, 5), "app.stop", "Програму зупинено")  # бота вже немає

    later = Clock(at(DAY, 14))  # новий запуск бота майже через чотири години
    second = make_bot(later)
    second.tick()
    later.go(minutes=1)
    second.tick()
    later.go(minutes=6)
    second.tick()

    shown = [t for t, _ in tg.flags]
    assert any("🔴 Програму зупинено" in t for t in shown), shown
    assert all(silent for _, silent in tg.flags)


def test_without_position_old_stop_is_dropped_as_before(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 7), "app.stop", "Програму зупинено")  # це правило BOT-012: першого разу
    step(clock, bot)
    step(clock, bot, 6)
    assert tg.flags == []


# --- 5. Самоперевірка ---


def checks(*statuses):
    return [Check(f"П{i}", status, "ок", "") for i, status in enumerate(statuses)]


def test_all_ok_is_said_quietly_with_count(data):
    assert health.announce_ok(checks(OK, OK, OK), at(DAY, 10), data) is True

    assert texts(data, "health.ok") == ["Самоперевірка: усе гаразд (3/3)"]


def test_skipped_checks_do_not_count_but_are_mentioned(data):
    health.announce_ok(checks(OK, OK, SKIP), at(DAY, 10), data)

    assert texts(data, "health.ok") == ["Самоперевірка: усе гаразд (2/2), пропущено 1"]


def test_ok_is_said_at_most_every_six_hours(data):
    assert health.announce_ok(checks(OK), at(DAY, 10), data) is True
    assert (
        health.announce_ok(checks(OK), at(DAY, 10) + timedelta(hours=5, minutes=59), data) is False
    )
    assert health.announce_ok(checks(OK), at(DAY, 10) + timedelta(hours=6), data) is True

    assert len(texts(data, "health.ok")) == 2


@pytest.mark.parametrize("bad", [WARN, FAIL])
def test_not_all_ok_says_nothing(data, bad):
    assert health.announce_ok(checks(OK, bad), at(DAY, 10), data) is False

    assert journal(data) == []  # FAIL шле BOT-010 сам (гучно, з кнопками)


def test_broken_state_file_does_not_stop_the_line(data):
    (data / health.OK_NAME).write_text("{не json", encoding="utf-8")

    assert health.announce_ok(checks(OK), at(DAY, 10), data) is True


def test_run_all_announces_ok_and_survives_journal_failure(data, root, monkeypatch):
    monkeypatch.setattr(health, "CHECKS", [("Одна", lambda ctx: Check("Одна", OK, "добре", ""))])
    deps = health.Deps(
        ai_models=lambda key: None,
        bot_rights=lambda token, channel: "бот",
        site_get=lambda key: None,
        now=lambda: at(DAY, 10),
    )

    health.run_all(data, root, deps=deps)
    health.run_all(data, root, deps=deps)  # другий раз за 6 год — нічого

    assert texts(data, "health.ok") == ["Самоперевірка: усе гаразд (1/1)"]
    assert (data / health.LAST_NAME).is_file()

    monkeypatch.setattr(events, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    (data / health.OK_NAME).unlink()
    assert [c.name for c in health.run_all(data, root, deps=deps)] == ["Одна"]


def test_fail_is_still_sent_by_bot010_loudly(conn, data, tg, make_bot):
    """BOT-013 нічого не міняє в тривозі про FAIL: вона йде з `health-last.json`, гучно."""
    clock, bot = started(tg, make_bot)
    stamp = (clock() + timedelta(minutes=1)).isoformat(timespec="seconds")
    (data / health.LAST_NAME).write_text(
        json.dumps(
            {
                "at": stamp,
                "checks": [
                    {
                        "name": "База",
                        "status": "fail",
                        "text": "Не відкрилась",
                        "fix": "Напишіть Власнику",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    step(clock, bot, 2)
    [(text, silent)] = owner_texts(tg)
    assert text.startswith("🔴 База") and silent is False


def test_health_ok_line_reaches_owner_in_quiet_batch(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "health.ok", "Самоперевірка: усе гаразд (9/9)")
    step(clock, bot)
    step(clock, bot, 5)
    [(text, silent)] = owner_texts(tg)
    assert text == "10:00 🩺 Самоперевірка: усе гаразд (9/9)" and silent is True


def test_detail_off_hides_the_new_kinds(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    state = alerts.load(data)
    state["detail"] = False
    alerts.save(data, state)
    put(data, at(DAY, 10), "post.new", "Новий пост 7027: 5 фото → чекає ШІ")
    put(data, at(DAY, 10), "site.sent", "7013 відправлено на сайт (поля + 5 фото)")
    step(clock, bot)
    step(clock, bot, 6)
    assert owner_texts(tg) == []


def test_new_events_do_not_touch_real_files(data):
    """Урок BOT-012: журнал пишеться лише в теку даних тесту."""
    events.emit("post.new", "Новий пост 1: 1 фото → чекає ШІ")
    assert (data / events.NAME).is_file()
    assert config.data_dir() == data


def test_robot_archives_deleted_post_and_says_so(conn, root, tmp_path, data, monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")  # як у test_robot_publish: межа «нових» не заважає
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")
    env = Env(conn, root, tmp_path)
    add(env, "1342", 1342, "2025-03-01T10:00:00")
    bot_post(env, 1342)
    env.presence = {1342: "gone"}

    env.run()

    assert texts(data, "post.gone") == [
        "1342 видалено з каналу — Картка в Архіві, на Шафі не публікую"
    ]
