"""BOT-009: щоденний звіт Власнику — текст, час надсилання, `/zvit`, CLI, жодних секретів."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from shafa import __main__ as cli
from shafa import config, db
from shafa.bot import report
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import TelegramError
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, STRANGER, FakeTelegram, message

DAY = date(2026, 10, 21)
SECRET = "123456:BOT-SECRET-TOKEN"


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """Місцевий час системи (як у звіті): тест не залежить від пояса машини."""
    return datetime(day.year, day.month, day.day, hour, minute).astimezone()


def stamp(day: date, hour: int = 12) -> str:
    return at(day, hour).astimezone(UTC).isoformat(timespec="seconds")


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now

    def go(self, **delta):
        self.now += timedelta(**delta)


def item(conn, name, status, day=DAY, code=None, published=False):
    with conn:
        conn.execute(
            "INSERT INTO items (folder_name, post_id, status, problem_code, created_at, "
            "updated_at, published_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, int(name), status, code, stamp(day), stamp(day), stamp(day) if published else None),
        )  # fmt: skip


def seed(conn, day=DAY):
    """Типовий день: 3 опубліковано за 2 сесії, проблеми, дві черги, бот і сайт, ШІ."""
    for n in (1, 2, 3):
        item(conn, str(100 + n), "published", day, published=True)
    item(conn, "201", "problem", day, "robot_field_missing")
    item(conn, "202", "problem", day, "robot_field_missing")
    item(conn, "203", "problem", day, "no_price")
    item(conn, "204", "problem", day - timedelta(days=3), "no_price")  # не цієї доби
    item(conn, "301", "review", day)
    item(conn, "302", "review", day)
    item(conn, "401", "approved", day)
    with conn:
        conn.execute(
            "INSERT INTO sessions (started_at, ended_at, published, problems, stop_code) "
            "VALUES (?, ?, 2, 1, 'sleep')",
            (stamp(day, 9), stamp(day, 10)),
        )
        conn.execute(
            "INSERT INTO sessions (started_at, ended_at, published, problems, stop_code) "
            "VALUES (?, ?, 1, 2, 'limit')",
            (stamp(day, 14), stamp(day, 15)),
        )
        conn.execute(
            "INSERT INTO posts (post_id, date, text, source, added_at) VALUES "
            "(1500, '2026-10-21T10:00:00', 'a', 'bot', ?), (1501, '2026-10-21T11:00:00', 'b', "
            "'bot', ?), (1400, '2026-10-01T11:00:00', 'c', 'export', ?)",
            (stamp(day), stamp(day), stamp(day)),
        )
        for post, state in ((1500, "done"), (1501, "failed")):
            conn.execute(
                "INSERT INTO site_jobs (post_id, folder_name, posted_at, text, state, created_at, "
                "updated_at) VALUES (?, ?, ?, 't', ?, ?, ?)",
                (post, str(post), stamp(day), state, stamp(day), stamp(day)),
            )
        conn.execute(
            "INSERT INTO ai_requests (model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES ('m', 1, 1, 1_250_000, ?)",
            (stamp(day),),
        )


def write_json(folder, name, payload):
    (folder / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "дані"
    folder.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "3")
    return folder


# --- Текст ---


def test_report_text_on_fixture_db(conn, data):
    seed(conn)
    write_json(data, "rehearse-last.json", {"at": at(DAY, 3).isoformat(), "checked": 5,
                                            "problems": 2, "summary": "x"})  # fmt: skip
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    checks = [
        {"name": "Бот", "status": "ok", "text": "Бот працює", "fix": ""},
        {"name": "Сайт", "status": "warn", "text": "Сайт повільний", "fix": ""},
        {"name": "Ключ ШІ", "status": "fail", "text": "Ключ ШІ не працює", "fix": ""},
        {"name": "Диск", "status": "warn", "text": "Мало місця", "fix": ""},
    ]
    write_json(data, "health-last.json", {"at": now, "checks": checks})

    text = report.build(conn, data, DAY)

    assert text.splitlines() == [
        "📊 Звіт за 21.10",
        "Шафа: опубліковано 3 (сесій 2); зупинка: Ліміт на сьогодні вичерпано",
        "Проблеми за день: 3 (Робот не знайшов поле на формі ×2, Немає ціни ×1)",
        "Чекають тебе: На перевірку 2 · Схвалені 1 (у черзі на завтра)",
        "Пробний прогін: перевірено 5, Проблем 2",
        "Бот: нових постів 2; сайт: надіслано 1, помилок 1",
        "ШІ: $1.25 із $3.00",
        "Самоперевірка: ⚠️ Ключ ШІ: Ключ ШІ не працює; Сайт: Сайт повільний",
    ]


def test_empty_day_shows_only_published_zero(conn, data):
    assert report.build(conn, data, DAY).splitlines() == [
        "📊 Звіт за 21.10",
        "Шафа: опубліковано 0 (сесій 0)",
    ]


def test_late_report_says_yesterday(conn, data):
    assert report.build(conn, data, DAY, late=True).splitlines()[0] == "📊 Звіт за 21.10 (за вчора)"


def test_health_all_ok_and_stale_and_rehearsal_of_other_day(conn, data):
    now = datetime.now().astimezone()
    ok = [{"name": "Бот", "status": "ok", "text": "ok", "fix": ""}]
    write_json(data, "health-last.json", {"at": now.isoformat(), "checks": ok})
    write_json(data, "rehearse-last.json", {"at": at(DAY - timedelta(days=1), 3).isoformat(),
                                            "checked": 5, "problems": 0})  # fmt: skip
    text = report.build(conn, data, DAY)
    assert "Самоперевірка: все гаразд" in text and "Пробний прогін" not in text
    old = (now - timedelta(days=3)).isoformat()
    write_json(data, "health-last.json", {"at": old, "checks": ok})
    assert "Самоперевірка: давно не виконувалась" in report.build(conn, data, DAY)


def test_running_session_and_unknown_code(conn, data):
    item(conn, "9", "problem", code="щось_нове")
    item(conn, "10", "problem", code=None)
    with conn:
        conn.execute("INSERT INTO sessions (started_at) VALUES (?)", (stamp(DAY),))
    lines = report.build(conn, data, DAY).splitlines()
    assert lines[1] == "Шафа: опубліковано 0 (сесій 1); робот ще працює"
    assert lines[2] == "Проблеми за день: 2 (Інше ×1, Без коду ×1)"


def test_problems_top_five(conn, data):
    for number, code in enumerate(
        ["no_price", "no_text", "no_photos", "ai_error", "many_items", "no_section"], 1
    ):
        item(conn, str(number), "problem", code=code)
        item(conn, str(number + 50), "problem", code=code if number == 1 else "no_price")
    line = report.build(conn, data, DAY).splitlines()[2]
    assert line.startswith("Проблеми за день: 12 (Немає ціни ×")
    assert line.count("×") == 5


def test_text_fits_one_message_and_has_no_secrets(conn, data, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "5551234567")
    seed(conn)
    long = "Шлях C:\\Users\\Влад\\дані\\shafa.sqlite і /Users/nuskelo/shafa/x, id -1002394827824, "
    long += f"токен {SECRET}. " + "дуже довго " * 1000
    write_json(data, "health-last.json", {
        "at": datetime.now().astimezone().isoformat(),
        "checks": [{"name": "Бот", "status": "fail", "text": long, "fix": ""}],
    })  # fmt: skip
    text = report.build(conn, data, DAY)
    assert len(text) <= 4096 and text.endswith("…")
    for forbidden in (SECRET, "BOT-SECRET", "5551234567", "2394827824", "C:\\", "/Users/", "Влад"):
        assert forbidden not in text


# --- Коли слати ---


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def make_bot(conn, tg, root):
    def make(clock, owner=OWNER):
        settings = Settings(CHANNEL, SELLER, owner, root, GROUP)
        return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)

    return make


def reports(tg):
    return [t for t in tg.texts(OWNER) if t.startswith("📊")]


def test_sent_once_per_day_at_time(conn, data, tg, make_bot):
    clock = Clock(at(DAY, 20, 59))
    bot = make_bot(clock)
    bot.tick()
    assert reports(tg) == []  # ще немає 21:00
    clock.go(minutes=1)
    bot.tick()
    bot.tick()
    clock.go(hours=2)
    bot.tick()
    assert len(reports(tg)) == 1 and reports(tg)[0].startswith("📊 Звіт за 21.10\n")
    assert report.last_sent(data) == DAY
    make_bot(clock).tick()  # перезапуск бота в той самий день — вдруге не шле
    assert len(reports(tg)) == 1


def test_laptop_slept_through_evening_sends_at_first_tick_same_day(conn, data, tg, make_bot):
    clock = Clock(at(DAY, 20, 0))
    bot = make_bot(clock)
    bot.tick()
    clock.go(hours=3, minutes=30)  # 23:30: ноутбук прокинувся
    bot.tick()
    assert len(reports(tg)) == 1 and "(за вчора)" not in reports(tg)[0]


def test_missed_day_sends_yesterday_once_then_todays(conn, data, tg, make_bot):
    report.mark_sent(data, DAY - timedelta(days=1), at(DAY - timedelta(days=1), 21))
    clock = Clock(at(DAY + timedelta(days=1), 8))  # увесь день DAY ноутбук спав
    bot = make_bot(clock)
    bot.tick()
    bot.tick()
    assert [t.splitlines()[0] for t in reports(tg)] == ["📊 Звіт за 21.10 (за вчора)"]
    assert report.last_sent(data) == DAY
    clock.go(hours=13)  # 21:00 наступної доби
    bot.tick()
    assert [t.splitlines()[0] for t in reports(tg)] == [
        "📊 Звіт за 21.10 (за вчора)",
        "📊 Звіт за 22.10",
    ]


def test_first_run_does_not_send_yesterday(conn, data, tg, make_bot):
    clock = Clock(at(DAY, 8))
    make_bot(clock).tick()
    assert reports(tg) == []


@pytest.mark.parametrize("value", ["", "off", "OFF", "25:99", "ввечері"])
def test_switched_off(conn, data, tg, make_bot, monkeypatch, value):
    monkeypatch.setenv("SHAFA_REPORT_AT", value)
    make_bot(Clock(at(DAY, 23))).tick()
    assert reports(tg) == [] and report.last_sent(data) is None


def test_custom_time_from_env_file(conn, data, tg, make_bot):
    config.env_file().write_text("SHAFA_REPORT_AT=08:30\n", encoding="utf-8")
    clock = Clock(at(DAY, 8, 29))
    bot = make_bot(clock)
    bot.tick()
    assert reports(tg) == []
    clock.go(minutes=1)
    bot.tick()
    assert len(reports(tg)) == 1


def test_empty_value_in_env_file_switches_off(conn, data, tg, make_bot):
    config.env_file().write_text("SHAFA_REPORT_AT=\n", encoding="utf-8")
    make_bot(Clock(at(DAY, 23))).tick()
    assert reports(tg) == []


def test_no_owner_chat_sends_nothing_without_error(conn, data, tg, make_bot):
    make_bot(Clock(at(DAY, 23)), owner=None).tick()
    assert [m for m, _ in tg.calls if m == "sendMessage"] == []
    assert report.last_sent(data) is None


def test_telegram_error_not_marked_and_retried_in_ten_minutes(conn, data, tg, make_bot):
    clock = Clock(at(DAY, 21, 0))
    bot = make_bot(clock)
    tg.errors["sendMessage"] = TelegramError("мережа", code=502)
    bot.tick()
    assert report.last_sent(data) is None
    del tg.errors["sendMessage"]
    clock.go(minutes=9)
    bot.tick()
    assert len(reports(tg)) == 1 and report.last_sent(data) is None  # лише невдала спроба
    clock.go(minutes=1, seconds=1)
    bot.tick()
    assert len(reports(tg)) == 2 and report.last_sent(data) == DAY  # друга — вдала
    clock.go(minutes=30)
    bot.tick()
    assert len(reports(tg)) == 2


def test_broken_report_does_not_stop_the_bot(conn, data, tg, make_bot, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("зламалось")

    monkeypatch.setattr(report, "build", broken)
    clock = Clock(at(DAY, 22))
    make_bot(clock).tick()  # не кидає
    assert reports(tg) == []


def test_unwritable_marker_still_sends_once(conn, data, tg, make_bot, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("диск")

    monkeypatch.setattr(report, "mark_sent", fail)
    clock = Clock(at(DAY, 21, 0))
    bot = make_bot(clock)
    bot.tick()
    clock.go(minutes=30)
    bot.tick()
    assert len(reports(tg)) == 1


# --- /zvit ---


def command(update_id, chat_id, text="/zvit"):
    return {"update_id": update_id, "message": message(update_id, chat_id, text=text)}


def test_zvit_from_owner_sends_report_and_keeps_marker(conn, data, tg, make_bot):
    seed(conn)
    bot = make_bot(Clock(at(DAY, 10)))
    bot.handle(command(1, OWNER))
    bot.handle(command(2, OWNER, "/zvit@shafa_bot"))
    assert len(reports(tg)) == 2 and "опубліковано 3" in reports(tg)[0]
    assert report.last_sent(data) is None  # позначку не міняє
    bot.tick()
    clock_evening = Clock(at(DAY, 21))
    make_bot(clock_evening).tick()
    assert len(reports(tg)) == 3  # вечірній звіт усе одно прийшов


def test_zvit_from_seller_is_like_any_unknown_command(conn, data, tg, make_bot):
    bot = make_bot(Clock(at(DAY, 10)))
    bot.handle(command(1, SELLER))
    assert reports(tg) == [] and tg.texts(OWNER) == []
    assert len(tg.texts(SELLER)) == 1 and "📊" not in tg.texts(SELLER)[0]
    other = make_bot(Clock(at(DAY, 10)))
    other.handle(command(2, OWNER, "/zvitt"))
    assert reports(tg) == []  # схожа, але інша команда — довідка


def test_zvit_from_stranger_is_silent(conn, data, tg, make_bot):
    make_bot(Clock(at(DAY, 10))).handle(command(1, STRANGER))
    assert [m for m, _ in tg.calls if m == "sendMessage"] == []


# --- CLI ---


def test_cli_prints_and_sends_nothing(tmp_path, data, capsys, monkeypatch):
    path = data / "shafa.sqlite"
    yesterday = datetime.now().astimezone().date() - timedelta(days=1)
    conn = db.connect(path)
    seed(conn, yesterday)
    conn.close()
    sent = []
    monkeypatch.setattr("shafa.bot.telegram.Telegram.call", lambda *a, **k: sent.append(a))
    assert cli.main(["report", "--day", yesterday.isoformat()]) == 0
    out = capsys.readouterr().out
    assert f"📊 Звіт за {yesterday:%d.%m} (за вчора)" in out and "опубліковано 3 (сесій 2)" in out
    assert sent == []


def test_cli_today_and_bad_day_and_no_db(data, capsys):
    assert cli.main(["report"]) == 1 and "Бази даних ще немає" in capsys.readouterr().out
    db.connect(data / "shafa.sqlite").close()
    assert cli.main(["report"]) == 0
    today = datetime.now().astimezone().date()
    assert f"📊 Звіт за {today:%d.%m}\n" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["report", "--day", "вчора"])
