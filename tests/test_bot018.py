"""BOT-018: бот для Вікторії — сесії окремими блоками, тихо/гучно, повний підсумок сесії.

Підставний Telegram; обидва чати (Вікторія й Власник) бачать те саме; тихі — `silent`, гучні — ні."""

import json
from datetime import UTC, timedelta

import pytest

from shafa import events
from shafa.bot import alerts, sessions
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import TelegramError
from shafa.robot import lock, publish
from test_bot009 import DAY, Clock, at, item, stamp
from test_bot010 import problem
from test_bot012 import put
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, FakeTelegram


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "дані"
    folder.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "3")
    monkeypatch.setenv("SHAFA_ALERTS_TO", "both")  # BOT-018: типово обидва чати
    monkeypatch.delenv("SHAFA_ALERTS", raising=False)
    return folder


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def make_bot(conn, tg, root):
    def make(clock):
        settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
        return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)

    return make


def sent(tg, chat):
    """Усе, що надіслано в чат: словники параметрів `sendMessage`."""
    return [p for m, p in tg.calls if m == "sendMessage" and p["chat_id"] == chat]


def edits(tg, chat):
    return [p for m, p in tg.calls if m == "editMessageText" and p["chat_id"] == chat]


def started(tg, make_bot):
    clock = Clock(at(DAY, 10))
    bot = make_bot(clock)
    bot.tick()  # перший цикл: стан кожного чату запам'ятано
    assert sent(tg, OWNER) == [] and sent(tg, SELLER) == []
    return clock, bot


def step(clock, bot, seconds=30):
    clock.go(seconds=seconds)
    bot.tick()


def session(conn, code="done", published=0, problems=0, site="shafa", ended=True, reason=None):
    with conn:
        cursor = conn.execute(
            "INSERT INTO sessions (started_at, ended_at, published, problems, stop_code, "
            "stop_reason, site) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (stamp(DAY, 9), stamp(DAY, 10) if ended else None, published, problems,
             code if ended else None, reason, site),
        )  # fmt: skip
    return cursor.lastrowid


def published(conn, number, session_id, url=None):
    item(conn, str(number), "published", DAY, published=True)
    with conn:
        conn.execute(
            "UPDATE items SET session_id = ?, shafa_url = ? WHERE post_id = ?",
            (session_id, url or f"https://shafa.ua/uk/item/{number}", number),
        )


def stamp_at(hour, minute):
    return at(DAY, hour, minute).astimezone(UTC).isoformat(timespec="seconds")


def utf16_slice(text, offset, length):
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le")


# --- Кому ---


def test_both_chats_get_the_same_summary_loud(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=2)
    published(conn, 7431, number)
    published(conn, 7432, number)
    step(clock, bot)
    [owner], [seller] = sent(tg, OWNER), sent(tg, SELLER)
    assert owner["text"] == seller["text"]
    assert owner["text"].startswith("✅ 🛍 Шафа · сесія 1 завершена")
    assert owner["silent"] is False and seller["silent"] is False  # кінець — гучно


def test_alerts_to_owner_leaves_seller_silent(conn, data, tg, make_bot, monkeypatch):
    monkeypatch.setenv("SHAFA_ALERTS_TO", "owner")
    clock, bot = started(tg, make_bot)
    session(conn, "done", published=1)
    step(clock, bot)
    assert len(sent(tg, OWNER)) == 1 and sent(tg, SELLER) == []
    assert not (data / "alerts-last-seller.json").exists()


def test_each_chat_has_its_own_state_file(conn, data, tg, make_bot):
    started(tg, make_bot)
    assert (data / "alerts-last.json").is_file() and (data / "alerts-last-seller.json").is_file()


def test_failure_of_one_chat_does_not_duplicate_to_the_other(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    session(conn, "done", published=1)
    real = tg.send_message

    def flaky(chat_id, *args, **kwargs):
        if chat_id == SELLER:
            raise TelegramError("мережа", code=502)
        return real(chat_id, *args, **kwargs)

    tg.send_message = flaky
    step(clock, bot)
    step(clock, bot)
    tg.send_message = real
    step(clock, bot)
    step(clock, bot)
    assert len(sent(tg, OWNER)) == 1  # Власнику — один раз, хоч Вікторії довелось повторювати
    assert len(sent(tg, SELLER)) == 1


def test_seller_gets_no_decision_buttons_owner_does(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    session(conn, "login", published=1)
    problem(conn, "201")
    step(clock, bot)
    owner_stop = sent(tg, OWNER)[0]
    seller_stop = sent(tg, SELLER)[0]
    assert owner_stop["markup"] and not seller_stop["markup"]  # «Що сталось» — лише Власнику
    owner_problem = next(p for p in sent(tg, OWNER) if p["text"].startswith("⚠️"))
    seller_problem = next(p for p in sent(tg, SELLER) if p["text"].startswith("⚠️"))
    assert owner_problem["markup"] and not seller_problem["markup"]
    assert owner_problem["text"] == seller_problem["text"]


def test_detali_stays_owner_only(conn, data, tg, make_bot):
    _, bot = started(tg, make_bot)
    bot.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"},
                                            "from": {"id": OWNER}, "text": "/detali"}})  # fmt: skip
    assert alerts.load(data)["detail"] is False
    assert alerts.load(data, "seller")["detail"] is True  # Вікторії це не змінює


# --- Початок сесії ---


def test_start_is_a_silent_block_with_separator(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "robot.start", "Сесія 80 почалась", extra={
        "session": 80, "queue": 30, "old": 10, "left": 30})  # fmt: skip
    step(clock, bot)
    for chat in (OWNER, SELLER):
        [start] = sent(tg, chat)
        assert start["text"].startswith(sessions.SEPARATOR + "\n")
        assert "🛍 Шафа · сесія 80 почалась · у черзі 30 (нових 20, старих 10)" in start["text"]
        assert "ліміт на сьогодні ще 30" in start["text"]
        assert start["silent"] is True


def test_klubok_start_has_its_own_icon(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "klubok.start", "x", extra={"session": 5, "limit": 8})
    step(clock, bot)
    [start] = sent(tg, SELLER)
    assert "🧶 Клубок · сесія 5 почалась · виставить до 8" in start["text"] and start["silent"]


def test_per_post_robot_lines_do_not_go_into_the_quiet_batch(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "robot.post", "6645 опубліковано · 690 грн")
    put(data, at(DAY, 10), "klubok.post", "Клубок: пост 1 виставлено")
    step(clock, bot)
    step(clock, bot, 400)
    assert sent(tg, OWNER) == []  # це бачить живе повідомлення й підсумок


# --- Живе повідомлення ---


@pytest.fixture
def running(conn, data, monkeypatch):
    """Робот «працює»: замок узято, сесія відкрита, `robot-now.json` з планом."""
    monkeypatch.setattr(lock, "busy", lambda path=None: {"mode": "publish"})
    number = session(conn, ended=False)

    def now(handled, post, stage="fill", **fields):
        publish.write_now(number, stage, plan=30, handled=handled, post=post, **fields)
        with conn:
            conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (at(DAY, 10).astimezone().isoformat(), number),
            )

    return number, now


def test_live_message_is_sent_silently_then_edited_not_resent(conn, data, tg, make_bot, running):
    number, now = running
    clock, bot = started(tg, make_bot)
    now(12, 7431)
    step(clock, bot)
    for chat in (OWNER, SELLER):
        [live] = sent(tg, chat)
        assert live["silent"] is True
        assert f"🛍 Шафа · сесія {number} іде" in live["text"]
        assert "Публікує: 12 з 30 на сьогодні" in live["text"]
        assert "Зараз: пост 7431 — заповнює форму" in live["text"]
    now(13, 7432)
    step(clock, bot, 5)  # від попереднього оновлення < 20 с
    assert edits(tg, OWNER) == []
    step(clock, bot, 25)
    for chat in (OWNER, SELLER):
        [edit] = edits(tg, chat)
        assert "Публікує: 13 з 30" in edit["text"] and "пост 7432" in edit["text"]
        assert len(sent(tg, chat)) == 1  # нове повідомлення не слали — лише правка (без звуку)


def test_live_shows_waiting_for_telegram_and_listings_in_panel_words(
    conn, data, tg, make_bot, running
):
    _, now = running
    clock, bot = started(tg, make_bot)
    now(3, 7431, "wait_channel", asked=True, until=(at(DAY, 10) + timedelta(minutes=9)).isoformat())
    step(clock, bot)
    assert "Telegram просить зачекати" in sent(tg, SELLER)[0]["text"]
    now(3, None, "listings", read=820, expected=1650, since=at(DAY, 10).isoformat())
    step(clock, bot, 25)
    assert "Перечитую ваші оголошення на Шафі" in edits(tg, SELLER)[-1]["text"]


def test_live_counts_problems_of_this_session(conn, data, tg, make_bot, running):
    number, now = running
    problem(conn, "201")
    with conn:
        conn.execute("UPDATE items SET session_id = ?", (number,))
    clock, bot = started(tg, make_bot)
    now(5, 7431)
    step(clock, bot)
    text = next(p for p in sent(tg, OWNER) if "іде" in p["text"])["text"]
    assert "Проблем 1" in text


def test_live_message_is_closed_and_summary_follows_when_session_ends(
    conn, data, tg, make_bot, running, monkeypatch
):
    number, now = running
    clock, bot = started(tg, make_bot)
    now(12, 7431)
    step(clock, bot)
    published(conn, 7431, number)
    with conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, stop_code = 'done', published = 1 WHERE id = ?",
            (stamp(DAY, 11), number),
        )
    monkeypatch.setattr(lock, "busy", lambda path=None: None)  # робот вийшов
    step(clock, bot)
    for chat in (OWNER, SELLER):
        assert "підсумок нижче" in edits(tg, chat)[-1]["text"]
        summary = sent(tg, chat)[-1]
        assert summary["text"].startswith(f"✅ 🛍 Шафа · сесія {number} завершена")
        assert summary["silent"] is False
    step(clock, bot)
    step(clock, bot)
    assert len(sent(tg, OWNER)) == 2  # живе + підсумок, більше нічого


def test_important_only_mode_has_no_start_and_no_live_but_the_summary(
    conn, data, tg, make_bot, running
):
    number, now = running
    clock, bot = started(tg, make_bot)
    state = alerts.load(data)
    state["detail"] = False
    alerts.save(data, state)
    put(data, at(DAY, 10), "robot.start", "x", extra={"session": number, "queue": 3, "old": 0})
    now(1, 7431)
    step(clock, bot)
    assert sent(tg, OWNER) == []


# --- Підсумок ---


def test_summary_has_expandable_blocks_with_links(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=2, problems=1)
    published(conn, 7431, number)
    published(conn, 7432, number, url="https://evil.example/x")  # чужого хоста — не посилання
    problem(conn, "201", text="Немає ціни в тексті")
    with conn:
        conn.execute("UPDATE items SET session_id = ? WHERE folder_name = '201'", (number,))
    item(conn, "7210", "archived", DAY, "post_deleted")
    with conn:
        conn.execute(
            "UPDATE items SET archived_at = ? WHERE folder_name = '7210'", (stamp_at(9, 30),)
        )
    step(clock, bot)
    summary = sent(tg, OWNER)[0]
    text, entities = summary["text"], summary["entities"]
    assert "виставлено 2 · Проблем 1 · продано 1" in text and "причина: Черга закінчилась" in text
    quotes = [e for e in entities if e["type"] == "expandable_blockquote"]
    titles = [utf16_slice(text, e["offset"], e["length"]).split("\n")[0] for e in quotes]
    assert titles == ["Виставлені (2)", "Проблеми (1)", "Продано, в Архів (1)"]
    links = [e for e in entities if e["type"] == "text_link"]
    assert [utf16_slice(text, e["offset"], e["length"]) for e in links] == ["Пост 7431"]
    assert links[0]["url"] == "https://shafa.ua/uk/item/7431"
    assert "Немає ціни" in text or "ціни" in text


def test_summary_lists_deferred_and_notes_from_the_journal(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=0)
    put(data, at(DAY, 10), "robot.end", "x", extra={
        "session": number, "deferred": [7300, 7301],
        "notes": ["Пост 7302: бренду немає ніде", "Прибрано фото старих постів: 4"]})  # fmt: skip
    step(clock, bot)
    text = sent(tg, SELLER)[0]["text"]
    assert "Відкладено" in text and "Пост 7300" in text and "Пост 7301" in text
    assert "На що глянути (1)" in text and "бренду немає ніде" in text
    assert "Прибрано фото" not in text  # службове — не для людини


def test_long_summary_is_split_into_messages_under_the_limit(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=0, problems=80)
    for n in range(7000, 7080):
        problem(conn, str(n), text="подробиці " * 25)
        with conn:
            conn.execute("UPDATE items SET session_id = ? WHERE folder_name = ?", (number, str(n)))
    step(clock, bot)
    parts = [
        p for p in sent(tg, OWNER) if not p["text"].startswith("⚠️")
    ]  # без тривоги про Проблеми
    assert len(parts) >= 3 and all(p["entities"] for p in parts)
    assert all(sessions.u16(p["text"]) <= 4096 for p in parts)
    joined = "\n".join(p["text"] for p in parts)
    assert "Проблеми (80)" in joined and "(продовження)" in joined
    assert "Пост 7000:" in joined and "Пост 7079:" in joined  # нічого не загубилось
    assert [p["silent"] for p in parts] == [False] * len(parts)  # кінець — гучно, кожна частина
    for p in parts:  # зсуви — у UTF-16, кожен блок цілий усередині тексту
        for e in p["entities"]:
            assert e["offset"] + e["length"] <= sessions.u16(p["text"])


def test_pack_splits_a_huge_section_and_counts_emoji_as_two_units():
    lines = [[f"• 🛍 {n} " + "я" * 280] for n in range(100)]
    messages = sessions.pack("шапка", [sessions.Section("Блок (100)", lines)])
    assert len(messages) >= 8 and all(sessions.u16(m.text) <= 4096 for m in messages)
    for m in messages:
        [quote] = m.entities
        inside = utf16_slice(m.text, quote["offset"], quote["length"])
        assert inside.split("\n")[0].startswith("Блок") and m.text.endswith(inside)


def test_summary_caps_a_long_list_with_a_pointer_to_the_program(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=300)
    for n in range(7000, 7300):
        published(conn, n, number)
    step(clock, bot)
    joined = "\n".join(p["text"] for p in sent(tg, OWNER))
    assert "Виставлені (300)" in joined and "… і ще 180 — вкладка «Сесії» в програмі" in joined


def test_error_stop_is_loud_message_then_silent_summary(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    session(conn, "login", published=3, problems=1)
    step(clock, bot)
    stop, summary = sent(tg, SELLER)
    assert stop["text"].startswith("⛔ Робот зупинився") and stop["silent"] is False
    assert summary["text"].startswith("⛔ 🛍 Шафа · сесія 1 зупинена") and summary["silent"] is True
    assert "(код: login)" not in summary["text"]  # коди — лише для Власника
    owner_summary = sent(tg, OWNER)[1]
    assert "(код: login)" in owner_summary["text"]


def test_klubok_session_is_its_own_block(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=1, site="klubok")
    item(conn, "7500", "approved", DAY)
    with conn:
        item_id = conn.execute("SELECT id FROM items WHERE folder_name = '7500'").fetchone()[0]
        conn.execute(
            "INSERT INTO klubok_ads (item_id, state, klubok_id, url, session_id, published_at, "
            "created_at, updated_at) VALUES (?, 'published', 99, 'https://klubok.com/item/99', ?, "
            "?, ?, ?)",
            (item_id, number, stamp_at(9, 30), stamp(DAY), stamp(DAY)),
        )
    step(clock, bot)
    [text] = [p["text"] for p in sent(tg, SELLER)]
    assert text.startswith("✅ 🧶 Клубок · сесія 1 завершена") and "Виставлені (1)" in text


def test_klubok_stop_is_sent_once_not_twice(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "klubok_login", site="klubok")
    events.emit("klubok.stop", "⛔ Клубок · сесія: потрібен вхід", loud=True,
                data={"session": number, "code": "klubok_login"})  # fmt: skip
    step(clock, bot)
    texts = [p["text"] for p in sent(tg, SELLER)]
    assert sum(t.startswith("⛔ Клубок зупинився") for t in texts) == 1
    assert not any("потрібен вхід" in t for t in texts)  # журнал його не дублює


def test_old_state_file_does_not_replay_old_klubok_sessions(conn, data, tg, make_bot):
    session(conn, "done", site="klubok")
    (data / "alerts-last.json").write_text(
        json.dumps(
            {
                "session_id": 0,
                "problems": {},
                "health_at": "",
                "health_sent": {},
                "budget_day": "",
                "sent": {},
                "hour": [],
            }
        ),  # fmt: skip  (стан до BOT-018)
        encoding="utf-8",
    )
    clock = Clock(at(DAY, 10))
    make_bot(clock).tick()
    assert sent(tg, OWNER) == []
    assert alerts.load(data)["session_id"] == 1


# --- Тихі події ---


def test_quiet_batch_has_a_header_and_blank_lines(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "site.sent", "6645 відправлено на сайт")
    put(data, at(DAY, 10, 2), "post.new", "Новий пост 7027")
    step(clock, bot)
    step(clock, bot, 400)
    for chat in (OWNER, SELLER):
        [batch] = sent(tg, chat)
        assert batch["silent"] is True
        assert batch["text"].splitlines()[:3] == [
            "🕓 Фонові події",
            "",
            "10:00 🌐 6645 відправлено на сайт",
        ]
        assert batch["text"].splitlines()[3] == ""


def test_texts_have_no_internal_codes_for_seller(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    session(conn, "klubok_login", site="klubok")
    step(clock, bot)
    assert not any("klubok_login" in p["text"] for p in sent(tg, SELLER))
    assert any("klubok_login" in p["text"] for p in sent(tg, OWNER))


def test_live_edit_is_throttled_inside_run_itself(conn, data, running):
    """Не частіше за `LIVE_EVERY` — і тоді, коли `run` кличуть частіше (а не лише завдяки опитуванню)."""
    _, now = running
    alerts.save(data, alerts.snapshot(conn, data, at(DAY, 10)))
    calls = []

    class Stub:
        def send(self, text):
            calls.append(("send", text))
            return 7

        def edit(self, message_id, text):
            calls.append(("edit", text))
            return "ok"

    now(1, 7431)
    alerts.run(conn, data, at(DAY, 10), lambda *a, **k: True, live=Stub())
    now(2, 7432)
    alerts.run(conn, data, at(DAY, 10) + timedelta(seconds=5), lambda *a, **k: True, live=Stub())
    assert [c[0] for c in calls] == ["send"]
    alerts.run(conn, data, at(DAY, 10) + timedelta(seconds=25), lambda *a, **k: True, live=Stub())
    assert [c[0] for c in calls] == ["send", "edit"] and "2 з 30" in calls[1][1]
