"""QA-005 (частина B): `alerts-preview` шле зразок кожного виду повідомлень, нічого не чіпаючи."""

import os
from datetime import timedelta

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
