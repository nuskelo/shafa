"""QA-005 (частина B): `alerts-preview` шле зразок кожного виду повідомлень, нічого не чіпаючи."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from shafa import __main__ as cli
from shafa import config
from shafa.bot import preview, sessions
from test_bot_core import FakeTelegram

CHAT = 777


@pytest.fixture
def real_data(tmp_path, monkeypatch):
    """«Справжня» тека даних Власника: після перегляду в ній має бути порожньо."""
    folder = tmp_path / "справжні-дані"
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setattr(sessions, "LIVE_EVERY", timedelta(0))  # у тесті без справжніх 25 с
    return folder


@pytest.fixture
def fake():
    tg = FakeTelegram()
    tg.close = lambda: None
    return tg


def run(tg, **kw):
    return preview.run(
        "123:fake-token", CHAT, telegram=lambda token: tg, sleep=lambda s: None,
        out=lambda text: None, **kw,
    )  # fmt: skip


def sent(tg):
    return [(m, p) for m, p in tg.calls if m in ("sendMessage", "editMessageText")]


def texts(tg, method="sendMessage"):
    return [p["text"] for m, p in tg.calls if m == method]


def find(tg, needle, method="sendMessage"):
    found = [p for m, p in tg.calls if m == method and needle in p["text"]]
    assert found, needle
    return found[0]


def test_every_kind_of_message_is_sent_to_the_one_chat(real_data, fake):
    run(fake)

    assert {p["chat_id"] for _, p in fake.calls} == {CHAT}  # лише в названий чат
    labels = [t for t in texts(fake) if t[:2] in {f"{n}/" for n in range(1, 10)}]
    assert [t[:2] for t in labels] == [f"{n}/" for n in range(1, 10)]  # усі 9 зразків, по порядку

    start = find(fake, "сесія 1 почалась")  # початок сесії Шафи: тихо, з відокремлювачем
    assert start["silent"] is True and start["text"].startswith("━━━━")
    assert find(fake, "сесія 3 почалась")["silent"] is True  # і Клубку

    live = find(fake, "сесія 1 іде")  # живе: одне повідомлення без звуку, далі лише правки
    assert live["silent"] is True
    edits = [p for m, p in fake.calls if m == "editMessageText" and "сесія 1 іде" in p["text"]]
    assert len(edits) == 3 and all(e["message_id"] for e in edits)
    assert len([t for t in texts(fake) if "сесія 1 іде" in t]) == 1  # нових не слали

    short = find(fake, "сесія 1 завершена")  # підсумок: гучно, з розгортаємими блоками
    assert short["silent"] is False
    assert {"expandable_blockquote", "text_link"} <= {e["type"] for e in short["entities"]}
    assert "Відкладено" in short["text"] or any(
        "Відкладено" in p["text"] for m, p in fake.calls if m == "sendMessage"
    )


def test_long_summary_is_split_under_the_limit(real_data, fake):
    run(fake)
    parts = [
        p for m, p in fake.calls if m == "sendMessage" and "Проблеми (продовження)" in p["text"]
    ]
    head = find(fake, "сесія 2 завершена")
    assert len(parts) >= 1
    for part in [head, *parts]:
        assert sessions.u16(part["text"]) <= 4096


def test_stops_and_new_problem_and_background(real_data, fake):
    run(fake)

    for needle in (
        "⛔ Робот зупинився: Робот вийшов із Шафи",
        "⛔ Клубок зупинився: Клубок не прийняв публікацію",
        "⛔ Робот зупинився: Telegram не відповідає",
    ):
        stop = find(fake, needle)
        assert stop["silent"] is False  # зупинка — завжди гучно, окремим повідомленням
        assert "Що робити" in stop["text"] or "Старт" in stop["text"]  # з порадою (BOT-019)

    album = [p for m, p in fake.calls if m == "sendMediaGroup"]
    assert len(album[0]["photos"]) == 2  # наше фото і фото схожого — заглушки
    problem = find(fake, "⚠️ Пост 1301")
    assert fake.calls.index(("sendMediaGroup", album[0])) < fake.calls.index(
        ("sendMessage", problem)
    )  # фото — перед повідомленням
    assert problem["markup"] and problem["silent"] is False  # кнопки рішень — Власнику

    assert find(fake, "ШІ закінчив")["silent"] is False  # кінець ШІ — гучно, з порадою
    background = find(fake, "🕓 Фонові події\n")
    assert background["silent"] is True and "ШІ почав аналіз" in background["text"]


def test_seller_view_has_no_buttons_and_no_codes(real_data, fake):
    run(fake, owner=False)
    assert all(p.get("markup") is None for m, p in fake.calls if m == "sendMessage")
    assert not any("(код:" in t for t in texts(fake))


def test_nothing_real_is_touched_and_environment_is_restored(real_data, fake, monkeypatch):
    monkeypatch.setenv("SHAFA_ALERTS_TO", "both")
    before = dict(os.environ)
    run(fake)
    assert not real_data.exists()  # події, стан і `robot-now.json` ішли в тимчасову теку
    assert dict(os.environ) == before and config.data_dir() == real_data
    assert not any("123:fake-token" in str(call) for call in fake.calls)  # токен ніде не лунає


def test_pause_shorter_than_telegram_throttle_is_refused(real_data, fake, monkeypatch):
    monkeypatch.setattr(sessions, "LIVE_EVERY", timedelta(seconds=20))
    with pytest.raises(preview.PreviewError, match="20 с"):
        run(fake, pause=5)
    assert fake.calls == []


def test_cli_takes_token_only_from_environment_not_from_dotenv(real_data, monkeypatch, capsys):
    real_data.mkdir(parents=True)
    (real_data / ".env").write_text("TELEGRAM_BOT_TOKEN=999:from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv(preview.TOKEN, raising=False)
    called = []
    monkeypatch.setattr(preview, "Telegram", lambda token: called.append(token))

    assert cli.main(["alerts-preview", "--chat", "5"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err and called == []


# --- Правки за живим переглядом (тестовий бот) ---

import re

from shafa import fill, hints
from shafa.bot.telegram import NO_PREVIEW


def lines_of(tg):
    return [line for text in texts(tg) for line in text.split("\n")]


def test_no_problem_line_repeats_itself(real_data, fake):
    """Було: «…не знайдено на формі Шафи, скріншот збережено» тричі в одному рядку."""
    run(fake)
    for line in lines_of(fake):
        words = line.split()
        grams = [tuple(words[i : i + 4]) for i in range(len(words) - 3)]
        assert len(grams) == len(set(grams)), line


def blocks(message):
    """(текст блоку, чи він цілий усередині `expandable_blockquote`) для кожного блоку після шапки."""
    text, entities = message["text"], message.get("entities") or []
    quotes = [(e["offset"], e["offset"] + e["length"]) for e in entities
              if e["type"] == "expandable_blockquote"]  # fmt: skip
    found, offset = [], 0
    for number, chunk in enumerate(text.split("\n\n")):
        if number:  # перший шматок — шапка
            start = offset
            found.append((chunk, any(a <= start and start + sessions.u16(chunk) <= b
                                     for a, b in quotes)))  # fmt: skip
        offset += sessions.u16(chunk) + 2
    return found


def test_every_block_of_every_message_is_expandable_and_entities_fit(real_data, fake):
    """Було: «Проблеми (24)» — суцільний текст у першому повідомленні, бо після 120 посилань
    «Виставлених» Telegram губив сутності понад 100."""
    run(fake)
    long = [p for m, p in fake.calls if m == "sendMessage" and "сесія 2 завершена" in p["text"]]
    assert long
    parts = [p for m, p in fake.calls if m == "sendMessage" and p["text"].startswith("Проблеми")]
    for message in [*long, *parts]:
        assert len(message["entities"]) <= 100 and sessions.u16(message["text"]) <= 4096
        for chunk, inside in blocks(message):
            assert inside, chunk[:60]
    assert len([*long, *parts]) >= 2  # довгий підсумок справді розбито


def test_pack_keeps_entities_under_telegram_limit_with_many_links():
    links = [
        [
            f"• Пост {n}",
        ]
        for n in range(0)
    ]
    section = sessions.Section(
        "Виставлені (120)",
        [["• ", (f"Пост {n}", f"https://shafa.ua/uk/item/{n}")] for n in range(120)],
    )
    problems = sessions.Section("Проблеми (3)", [[f"• рядок {n}"] for n in range(3)])
    messages = sessions.pack("шапка", [section, problems])
    assert links == [] and len(messages) >= 2
    for message in messages:
        assert len(message.entities) <= sessions.MAX_ENTITIES + 1
        assert {e["type"] for e in message.entities} >= {"expandable_blockquote"}
    assert sum(1 for m in messages for e in m.entities if e["type"] == "text_link") == 120


def test_links_in_bot_messages_never_open_a_preview():
    import httpx2

    from shafa.bot.telegram import Telegram

    seen = []

    def handler(request):
        import json

        seen.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg = Telegram("1:x", httpx2.Client(transport=httpx2.MockTransport(handler)))
    tg.send_message(1, "https://shafa.ua/uk/item/1")
    tg.edit_text(1, 2, "https://klubok.com/item-1")
    assert [(m, body["link_preview_options"]) for m, body in seen] == [
        ("sendMessage", NO_PREVIEW), ("editMessageText", NO_PREVIEW),
    ]  # fmt: skip
    assert NO_PREVIEW == {"is_disabled": True}


def stopped(conn, site, code, reason="x"):
    with conn:
        cursor = conn.execute(
            "INSERT INTO sessions (started_at, ended_at, stop_code, stop_reason, site) "
            "VALUES ('2026-11-26T10:00:00+00:00', '2026-11-26T10:30:00+00:00', ?, ?, ?)",
            (code, reason, site),
        )
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (cursor.lastrowid,)).fetchone()


def test_every_stop_of_shafa_and_klubok_says_what_to_do(conn):
    """Було: Клубок/капча — без «Що робити». Тепер кожна зупинка (не спокійна), що має кроки в
    `hints.py`, каже їх і в ⛔, і в підсумку."""
    from datetime import datetime

    checked = 0
    for site, table in (("shafa", hints.STOPS), ("klubok", hints.KLUBOK_STOPS)):
        for code, hint in table.items():
            row = stopped(conn, site, code)
            if sessions.is_calm(row) or not hint.do:
                continue
            for owner in (True, False):
                assert "Що робити" in sessions.stop_text(row, owner), (site, code)
            summary = sessions.summary(conn, row, datetime.now().astimezone(), None, True)[0].text
            assert "Що робити" in summary, (site, code)
            checked += 1
    assert checked >= 8  # пройшли справжні таблиці, а не порожні цикли


def test_stop_reason_is_taken_as_is_with_its_capital_letter(conn):
    from datetime import datetime

    for site, code, expected in (
        ("shafa", "telegram_busy", "причина: Telegram не відповідає"),
        ("klubok", "klubok_captcha", "причина: Клубок не прийняв"),
    ):
        row = stopped(conn, site, code)
        text = sessions.summary(conn, row, datetime.now().astimezone(), None, False)[0].text
        assert expected in text, text


def test_ai_today_includes_what_was_just_spent(conn):
    tally = fill.Tally(ready=5, cost_micro=410_000)
    # запити ще не лягли в базу — «сьогодні» все одно не менше за щойно витрачене
    assert "витрачено 0,41 $ · сьогодні 0,41 $ з 3 $" in fill.tally_text(conn, tally)
    # лягли — рахуємо один раз, не двічі
    with conn:
        conn.execute(
            "INSERT INTO ai_requests (model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES ('m', 1, 1, 410000, ?)",
            (datetime.now(UTC).isoformat(timespec="seconds"),),
        )
    text = fill.tally_text(conn, tally)
    assert "витрачено 0,41 $ · сьогодні 0,41 $ з 3 $" in text and re.search(r"0,41", text)
