import pytest

from shafa import config
from shafa.__main__ import main
from shafa.bot import run
from shafa.bot.core import Bot, Settings
from shafa.bot.telegram import TelegramError
from shafa.robot import lock
from test_bot_core import CHANNEL, OWNER, SELLER, TEXT, FakeTelegram, photo

TOKEN = "123456:ТЕСТ-секрет-AAAbbb"


@pytest.mark.parametrize(
    ("raw", "value"), [("2394827824", -1002394827824), ("-1002394827824", -1002394827824)]
)
def test_channel_id_forms(raw, value):
    assert run.channel_id(raw) == value


@pytest.mark.parametrize("raw", [None, "", "@kanal", "-2394827824", "12a"])
def test_bad_channel_id(raw):
    with pytest.raises(run.SetupError):
        run.channel_id(raw or None)


def test_settings_need_someone_to_listen(monkeypatch, root):
    monkeypatch.setenv("TELEGRAM_BOT_CHANNEL_ID", "2394827824")
    with pytest.raises(run.SetupError, match="нікого не слухатиме"):
        run.settings(root)
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "111")
    assert run.settings(root) == Settings(-1002394827824, 111, None, root)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "двісті")
    with pytest.raises(run.SetupError, match="TELEGRAM_OWNER_CHAT_ID — число"):
        run.settings(root)


def test_bot_channel_is_separate_from_folder_channel(monkeypatch, root):
    """Її канал (назви папок) не стає каналом публікації сам собою."""
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "2394827824")
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", "111")
    with pytest.raises(run.SetupError, match="TELEGRAM_BOT_CHANNEL_ID"):
        run.settings(root)


class RightsTelegram:
    def __init__(self, member=None, error=None):
        self.member, self.error = member, error

    def get_me(self):
        return {"id": 42, "username": "shafa_test_bot"}

    def get_chat_member(self, chat_id, user_id):
        assert (chat_id, user_id) == (CHANNEL, 42)
        if self.error:
            raise self.error
        return self.member


@pytest.mark.parametrize(
    "member",
    [{"status": "creator"}, {"status": "administrator", "can_post_messages": True}],
)
def test_rights_ok(member):
    assert run.check_rights(RightsTelegram(member), CHANNEL) == "shafa_test_bot"


@pytest.mark.parametrize(
    ("member", "error", "text"),
    [
        ({"status": "administrator", "can_post_messages": False}, None, "не адміністратор"),
        ({"status": "member"}, None, "не адміністратор"),
        ({"status": "left"}, None, "не адміністратор"),
        (None, TelegramError("x", code=400), "не бачить каналу"),
        (None, TelegramError("x", code=403), "не бачить каналу"),
        (
            None,
            TelegramError("Немає з'єднання з Telegram (getChatMember)"),
            "Не вдалося перевірити",
        ),
    ],
)
def test_no_rights(member, error, text):
    with pytest.raises(run.SetupError, match=text):
        run.check_rights(RightsTelegram(member, error), CHANNEL)


class PollingTelegram(FakeTelegram):
    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.polls = []

    def get_updates(self, offset, timeout):
        self.polls.append((offset, timeout))
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def polling(conn, root, batches):
    tg = PollingTelegram(batches)
    bot = Bot(conn, tg, Settings(CHANNEL, SELLER, OWNER, root), log=lambda t: None)
    return tg, bot


def test_poll_confirms_handled_updates_and_polls_fast_while_collecting(conn, root):
    first = [
        {"update_id": 8, "message": photo(12, "f2")},
        {"update_id": 7, "message": photo(11, "f1", caption=TEXT)},
    ]
    tg, bot = polling(conn, root, [first, []])
    sleeps = []

    run.poll(bot, tg, conn, stop=lambda: not tg.batches, sleep=sleeps.append)

    assert tg.polls == [(None, run.POLL), (9, run.BUSY_POLL)]
    parts = conn.execute("SELECT message_id FROM bot_draft_parts ORDER BY rowid").fetchall()
    assert [r[0] for r in parts] == [11, 12]  # оброблено за update_id
    assert conn.execute("SELECT heartbeat_at, error FROM bot_status").fetchone()[1] is None


def test_poll_waits_after_network_error(conn, root):
    error = TelegramError("Немає з'єднання з Telegram (getUpdates)", retry_after=None)
    flood = TelegramError("Telegram відмовив (getUpdates, 429)", code=429, retry_after=17)
    tg, bot = polling(conn, root, [error, flood, []])
    sleeps = []

    run.poll(bot, tg, conn, stop=lambda: not tg.batches, sleep=sleeps.append)

    assert sleeps == [run.PAUSE, 17]
    assert conn.execute("SELECT error FROM bot_status").fetchone()[0] is None  # відновився


@pytest.mark.parametrize(("code", "text"), [(409, "Інший процес"), (401, "токен")])
def test_poll_stops_on_conflict_or_bad_token(conn, root, code, text):
    tg, bot = polling(conn, root, [TelegramError("x", code=code)])
    with pytest.raises(run.SetupError, match=text):
        run.poll(bot, tg, conn, stop=lambda: False, sleep=lambda s: None)


# --- Команда ---


@pytest.fixture
def db_path(conn, tmp_path):
    return tmp_path / "база.sqlite"


def bot_args(db_path, root, *extra):
    return ["bot", "--db", str(db_path), "--root", str(root), *extra]


@pytest.fixture
def env(monkeypatch):
    config.ENV_FILE.write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_BOT_CHANNEL_ID=2394827824\n"
        "TELEGRAM_SELLER_CHAT_ID=111\n",
        encoding="utf-8",
    )


@pytest.fixture
def no_network(monkeypatch):
    from shafa.bot import telegram

    made = []

    class Fake(RightsTelegram):
        def __init__(self, token):
            made.append(token)
            super().__init__({"status": "administrator", "can_post_messages": True})

        def close(self):
            pass

    monkeypatch.setattr(telegram, "Telegram", Fake)
    return made


def test_bot_check_prints_no_token(db_path, root, env, no_network, capsys):
    assert main(bot_args(db_path, root, "--check")) == 0
    out = capsys.readouterr()
    assert out.out == "Бот @shafa_test_bot: права в каналі є\n"
    assert TOKEN not in out.out + out.err
    assert no_network == [TOKEN]


def test_bot_without_token_explains(db_path, root, capsys):
    config.ENV_FILE.write_text("TELEGRAM_BOT_CHANNEL_ID=1\nTELEGRAM_OWNER_CHAT_ID=2\n", "utf-8")
    with pytest.raises(SystemExit):
        main(bot_args(db_path, root, "--check"))
    assert "Немає TELEGRAM_BOT_TOKEN у .env" in capsys.readouterr().err


def test_second_bot_does_nothing(db_path, root, env, no_network, capsys):
    with lock.hold("bot", config.data_dir() / run.LOCK_NAME):
        assert main(bot_args(db_path, root)) == 2
    assert capsys.readouterr().out == "Бот уже працює — другий запуск нічого не чіпає\n"
    assert no_network == []


def test_bot_without_rights_stops_with_status(db_path, root, env, monkeypatch, capsys):
    from shafa.bot import telegram

    class NoRights(RightsTelegram):
        def __init__(self, token):
            super().__init__({"status": "member"})

        def close(self):
            pass

    monkeypatch.setattr(telegram, "Telegram", NoRights)
    assert main(bot_args(db_path, root)) == 1
    assert "не адміністратор" in capsys.readouterr().out
    from shafa import db

    conn = db.connect(db_path)
    assert "не адміністратор" in conn.execute("SELECT error FROM bot_status").fetchone()[0]
    conn.close()
