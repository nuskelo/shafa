"""BOT-010: тривоги Власнику одразу — зупинка робота, нові Проблеми, самоперевірка, бюджет ШІ."""

import json
from datetime import timedelta

import pytest

from shafa.bot import alerts
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import TelegramError
from test_bot009 import DAY, Clock, at, item, stamp, write_json
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, FakeTelegram

SECRET_CHAT = "5550123456"


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "дані"
    folder.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "3")
    monkeypatch.delenv("SHAFA_ALERTS", raising=False)
    return folder


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def make_bot(conn, tg, root):
    def make(clock, owner=OWNER):
        settings = Settings(CHANNEL, SELLER, owner, root, GROUP)
        return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)

    return make


def sent(tg):
    return tg.texts(OWNER)


def session(conn, code="done", published=2, problems=0, reason=None, hour=10):
    with conn:
        cursor = conn.execute(
            "INSERT INTO sessions (started_at, ended_at, published, problems, stop_code, "
            "stop_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (stamp(DAY, hour - 1), stamp(DAY, hour), published, problems, code, reason),
        )
    return cursor.lastrowid


def problem(conn, name, code="no_price", text=None, hour=11):
    item(conn, name, "problem", DAY, code)
    with conn:
        conn.execute("UPDATE items SET problem_text = ? WHERE folder_name = ?", (text, name))


def health(data, *fails, hour=11, warn=()):
    checks = [
        {"name": n, "status": "fail", "text": f"{n} не працює", "fix": "Перезапусти"} for n in fails
    ]
    checks += [{"name": n, "status": "warn", "text": "увага", "fix": ""} for n in warn]
    checks.append({"name": "База", "status": "ok", "text": "ок", "fix": ""})
    write_json(
        data,
        "health-last.json",
        {"at": at(DAY, hour).isoformat(timespec="seconds"), "checks": checks},
    )


def spend(conn, micro, hour=11):
    with conn:
        conn.execute(
            "INSERT INTO ai_requests (model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES ('m', 1, 1, ?, ?)",
            (micro, stamp(DAY, hour)),
        )


def started(conn, data, tg, make_bot, hour=10):
    """Бот, який уже зробив свій перший цикл (запам'ятав стан)."""
    clock = Clock(at(DAY, hour))
    bot = make_bot(clock)
    bot.tick()
    assert sent(tg) == []
    return clock, bot


def step(clock, bot, minutes=1):
    clock.go(minutes=minutes)
    bot.tick()


# --- Перший запуск ---


def test_first_run_remembers_state_and_sends_nothing(conn, data, tg, make_bot):
    session(conn, "crash", 0, 0)
    problem(conn, "201")
    health(data, "Ключ ШІ")
    spend(conn, 2_700_000)
    clock = Clock(at(DAY, 12))
    bot = make_bot(clock)
    bot.tick()
    step(clock, bot)
    assert sent(tg) == []
    saved = json.loads((data / "alerts-last.json").read_text(encoding="utf-8"))
    assert saved["session_id"] == 1 and saved["problems"] and saved["budget_day"] == DAY.isoformat()
    make_bot(clock).tick()  # перезапуск бота — минуле не повторюється
    assert sent(tg) == []


# --- Сесії робота ---


def test_stop_of_session_sends_one_message_once(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    session(conn, "login", published=3, problems=1)
    step(clock, bot)
    step(clock, bot)
    step(clock, bot)
    [text] = sent(tg)
    assert text.startswith("⛔ Робот зупинився: Робот вийшов із Шафи.")
    assert "Опубліковано 3, у Проблемах 1." in text
    make_bot(clock).tick()  # перезапуск
    assert len(sent(tg)) == 1


def test_unknown_stop_code_uses_reason_text(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    session(conn, "weird", 0, 0, reason="щось нове")
    step(clock, bot)
    assert "Робот зупинився: щось нове." in sent(tg)[0]


@pytest.mark.parametrize("code", ["done", "limit", "stop"])
def test_normal_end_is_one_calm_line(conn, data, tg, make_bot, code):
    clock, bot = started(conn, data, tg, make_bot)
    number = session(conn, code, published=5, problems=2)
    step(clock, bot)
    [text] = sent(tg)
    assert text.startswith(f"✅ Сесія {number}: опубліковано 5, у Проблемах 2")
    assert "\n" not in text and "⛔" not in text


def test_running_session_is_not_announced_until_it_ends(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    with conn:
        conn.execute("INSERT INTO sessions (started_at) VALUES (?)", (stamp(DAY, 10),))
    step(clock, bot)
    assert sent(tg) == []
    with conn:
        conn.execute("UPDATE sessions SET ended_at = ?, stop_code = 'slow'", (stamp(DAY, 11),))
    step(clock, bot)
    assert len(sent(tg)) == 1 and "Шафа довго не відповідала" in sent(tg)[0]


# --- Проблеми ---


def test_three_problems_in_a_minute_is_one_message(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201", "no_price", "У пості немає ціни")
    problem(conn, "202", "robot_field_missing", "Поле «Колір»")
    problem(conn, "203", "many_items")
    step(clock, bot)
    [text] = sent(tg)
    assert text.startswith("⚠️ Нові Проблеми (3):")
    assert "• Пост 201: Немає ціни — У пості немає ціни" in text
    assert "Пост 202: Робот не знайшов поле на формі — Поле «Колір»" in text
    assert "Пост 203: Кілька товарів у пості" in text
    step(clock, bot)
    step(clock, bot)
    assert len(sent(tg)) == 1


def test_single_problem_is_one_line_and_old_ones_are_not_repeated(conn, data, tg, make_bot):
    problem(conn, "100")  # була до запуску
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201", "no_section", "Немає розділу")
    step(clock, bot)
    assert sent(tg) == ["⚠️ Пост 201: Немає розділу на Шафі — Немає розділу"]


def test_more_than_ten_problems_are_cut(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    for n in range(300, 313):
        problem(conn, str(n))
    step(clock, bot)
    [text] = sent(tg)
    assert text.count("• ") == 10 and "…і ще 3" in text
    assert text.endswith("Кнопки — для перших 5, решту — у програмі.")  # BOT-011


def test_problem_that_was_fixed_and_came_back_is_new_again(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201")
    step(clock, bot)
    with conn:
        conn.execute("UPDATE items SET status = 'review' WHERE folder_name = '201'")
    step(clock, bot)
    with conn:
        conn.execute("UPDATE items SET status = 'problem' WHERE folder_name = '201'")
    clock.go(hours=1)
    bot.tick()
    assert len(sent(tg)) == 2


def test_resaving_the_same_problem_does_not_repeat(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201")
    step(clock, bot)
    with conn:
        conn.execute("UPDATE items SET updated_at = ? WHERE folder_name = '201'", (stamp(DAY, 15),))
    clock.go(hours=2)
    bot.tick()
    assert len(sent(tg)) == 1


def test_scrub_removes_path_and_chat_id(conn, data, tg, make_bot, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", SECRET_CHAT)
    clock, bot = started(conn, data, tg, make_bot)
    problem(
        conn, "201", "publish_error", rf"Файл C:\Users\Влада\Шафа\201\1.jpg і чат {SECRET_CHAT}"
    )
    step(clock, bot)
    [text] = sent(tg)
    assert "Users" not in text and "Влада" not in text and SECRET_CHAT not in text


# --- Самоперевірка ---


def test_health_fail_repeats_not_more_often_than_six_hours(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    health(data, "Ключ ШІ", hour=11)
    step(clock, bot)
    [text] = sent(tg)
    assert text == "🔴 Ключ ШІ: Ключ ШІ не працює. Перезапусти."
    clock.go(hours=1)
    health(data, "Ключ ШІ", hour=12)
    bot.tick()
    assert len(sent(tg)) == 1  # година — нічого
    clock.go(hours=6)
    health(data, "Ключ ШІ", hour=19)
    bot.tick()
    assert len(sent(tg)) == 2  # 7 годин — знову


def test_health_warn_is_not_sent_and_same_file_is_not_reread(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    health(data, warn=("Диск",))
    step(clock, bot)
    assert sent(tg) == []
    health(data, "Бот", warn=("Диск",), hour=12)
    step(clock, bot)
    assert len(sent(tg)) == 1 and "Диск" not in sent(tg)[0]
    step(clock, bot)
    step(clock, bot)
    assert len(sent(tg)) == 1


def test_two_fails_are_one_message(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    health(data, "Сайт", "Бот")
    step(clock, bot)
    [text] = sent(tg)
    assert text.count("🔴") == 2


# --- Бюджет ШІ ---


def test_budget_80_percent_once_a_day(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    spend(conn, 2_000_000)  # 67 %
    step(clock, bot)
    assert sent(tg) == []
    spend(conn, 500_000)  # 83 %
    step(clock, bot)
    [text] = sent(tg)
    assert text.startswith("💸 ШІ: сьогодні витрачено $2.50 із $3.00 (83%)")
    spend(conn, 400_000)
    step(clock, bot)
    step(clock, bot)
    assert len(sent(tg)) == 1
    clock.go(days=1)
    with conn:
        conn.execute(
            "INSERT INTO ai_requests (model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES ('m', 1, 1, ?, ?)",
            (2_900_000, stamp(DAY + timedelta(days=1), 11)),
        )
    bot.tick()
    assert len(sent(tg)) == 2  # наступної доби — знову


# --- Збої Telegram, налаштування, ліміт повідомлень ---


def test_telegram_down_retries_next_cycle_without_duplicates(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    session(conn, "login")
    problem(conn, "201")
    tg.errors["sendMessage"] = TelegramError("мережа", code=502)
    step(clock, bot)
    assert len(sent(tg)) == 1  # одна невдала спроба (FakeTelegram записує й її), решта чекає
    del tg.errors["sendMessage"]
    step(clock, bot)
    delivered = sent(tg)[1:]
    assert len(delivered) == 2  # і сесія, і Проблема — нічого не загублено
    assert delivered[0].startswith("⛔") and delivered[1].startswith("⚠️")
    step(clock, bot)
    step(clock, bot)
    assert len(sent(tg)) == 3


def test_checked_once_a_minute_not_on_every_poll(conn, data, tg, make_bot, monkeypatch):
    clock, bot = started(conn, data, tg, make_bot)
    calls = []
    monkeypatch.setattr(alerts, "run", lambda *a, **k: calls.append(1))
    for _ in range(5):
        clock.go(seconds=5)
        bot.tick()
    assert calls == []
    clock.go(seconds=40)
    bot.tick()
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["off", "OFF", "ні"])
def test_switched_off_sends_nothing_and_on_again_is_a_first_run(
    conn, data, tg, make_bot, monkeypatch, value
):
    clock, bot = started(conn, data, tg, make_bot)
    monkeypatch.setenv("SHAFA_ALERTS", value)
    session(conn, "login")
    problem(conn, "201")
    step(clock, bot)
    assert sent(tg) == [] and not (data / "alerts-last.json").exists()
    monkeypatch.delenv("SHAFA_ALERTS")
    step(clock, bot)
    step(clock, bot)
    assert sent(tg) == []  # що було за час «off» — не наздоганяємо


def test_off_in_env_file(conn, data, tg, make_bot, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SHAFA_ALERTS=off\n", encoding="utf-8")
    monkeypatch.setattr("shafa.config.env_file", lambda: env)
    assert alerts.enabled() is False
    env.write_text("SHAFA_ALERTS=\n", encoding="utf-8")
    assert alerts.enabled() is True


def test_no_owner_chat_sends_nothing(conn, data, tg, make_bot):
    clock = Clock(at(DAY, 10))
    bot = make_bot(clock, owner=None)
    bot.tick()
    session(conn, "login")
    step(clock, bot)
    assert [m for m, _ in tg.calls if m == "sendMessage"] == []
    assert not (data / "alerts-last.json").exists()


def test_twenty_per_hour_then_one_summary(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    for _ in range(25):
        session(conn, "login")
    step(clock, bot)
    texts = sent(tg)
    assert len(texts) == 20
    assert texts[-1] == "ℹ️ Ще 6 подій — подивись /zvit."
    step(clock, bot)
    assert len(sent(tg)) == 20  # те, що не влізло, не повертається
    clock.go(hours=1, minutes=1)
    session(conn, "login")
    bot.tick()
    assert len(sent(tg)) == 21  # нова година — знову можна


def test_broken_alerts_do_not_stop_the_bot(conn, data, tg, make_bot, monkeypatch):
    clock, bot = started(conn, data, tg, make_bot)

    def broken(*args, **kwargs):
        raise RuntimeError("поламалось")

    monkeypatch.setattr(alerts, "run", broken)
    step(clock, bot)  # не кидає виняток
    assert sent(tg) == []


def test_corrupt_state_file_is_a_first_run(conn, data, tg, make_bot):
    (data / "alerts-last.json").write_text("{не json", encoding="utf-8")
    session(conn, "login")
    clock = Clock(at(DAY, 12))
    bot = make_bot(clock)
    bot.tick()
    step(clock, bot)
    assert sent(tg) == []
    assert json.loads((data / "alerts-last.json").read_text(encoding="utf-8"))["session_id"] == 1


def test_timedelta_constants_are_as_in_ticket():
    assert alerts.SAME == timedelta(hours=1) and alerts.HEALTH_REPEAT == timedelta(hours=6)
    assert alerts.PER_HOUR == 20 and alerts.BUDGET_SHARE == 80
