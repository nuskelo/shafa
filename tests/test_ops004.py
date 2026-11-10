"""OPS-004: клієнт на компі — обмін із сервером через ssh, без опитування Telegram."""

import io
import json
import sys
import threading
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from shafa import app as app_mod
from shafa import config, db, health, site, transfer, web
from shafa.__main__ import main
from shafa.bot import actions, client, outbox, report
from shafa.bot import run as bot_run
from shafa.bot.core import Bot, Settings
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, TEXT, Clock, FakeTelegram, Updates
from test_ops003 import button, edit_update, owner_text, publish

POST = 1401  # номер першого поста з підставного Telegram


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def upd():
    return Updates()


@pytest.fixture
def sconn(tmp_path):
    connection = db.connect(tmp_path / "сервер.sqlite")
    yield connection
    connection.close()


@pytest.fixture
def sroot(tmp_path):
    path = tmp_path / "сервер-Шафа"
    path.mkdir()
    return path


@pytest.fixture
def server(sconn, tg, sroot, clock):
    settings = Settings(CHANNEL, SELLER, OWNER, sroot, GROUP, server=True)
    return Bot(sconn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)


@pytest.fixture
def site_on(monkeypatch):
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")


@pytest.fixture
def on_server(monkeypatch):
    monkeypatch.setenv(client.SERVER, "ubuntu@203.0.113.7")


@pytest.fixture
def chats(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_CHANNEL_ID", str(CHANNEL))
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", str(OWNER))
    monkeypatch.setenv("TELEGRAM_SELLER_CHAT_ID", str(SELLER))


def blob(sconn, sroot, after):
    return outbox.give_bytes(sconn, sroot, after)[0]


def apply(conn, root, data, same_channel=False):
    return client.apply(
        conn, root, data, data_dir=config.data_dir(), same_channel=same_channel, log=lambda t: None
    )


def pull(sconn, sroot, conn, root):
    return apply(conn, root, blob(sconn, sroot, client.last_seq(conn)))


def rezip(data, change):
    """Той самий zip, але з іншим `outbox.json` / фото (пошкодження, інший порядок)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        index = json.loads(zf.read(outbox.INDEX))
        files = {n: zf.read(n) for n in zf.namelist() if n != outbox.INDEX}
    index, files = change(index, files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(outbox.INDEX, json.dumps(index, ensure_ascii=False))
        for name, body in files.items():
            zf.writestr(name, body)
    return out.getvalue()


def folders(root):
    return sorted(p.name for p in root.iterdir())


def photos_of(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir()}


# --- застосування черги ---


def test_post_from_server_lands_in_base_folder_and_site(conn, root, sconn, sroot, server, tg,
                                                        upd, clock, site_on):  # fmt: skip
    publish(server, tg, upd, clock, files=3)

    applied = pull(sconn, sroot, conn, root)

    assert applied.posts == [POST] and client.last_seq(conn) == applied.last == 1
    row = conn.execute("SELECT date, text, source FROM posts WHERE post_id = ?", (POST,)).fetchone()
    assert row["source"] == "bot" and row["text"].startswith(TEXT[:20])
    assert row["date"] == datetime.fromtimestamp(1_791_960_000, UTC).astimezone().strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    assert folders(root) == [str(POST)]
    assert photos_of(root / str(POST)) == photos_of(sroot / str(POST))
    job = conn.execute("SELECT folder_name, state FROM site_jobs WHERE post_id = ?", (POST,))
    assert tuple(job.fetchone()) == (str(POST), "waiting")


def test_same_zip_twice_changes_nothing(conn, root, sconn, sroot, server, tg, upd, clock, site_on):
    publish(server, tg, upd, clock, files=2)
    server.handle(edit_update(upd, POST, "Нова ціна 900"))
    data = blob(sconn, sroot, 0)
    apply(conn, root, data)
    before = conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0]

    again = apply(conn, root, data)  # обрив після застосування, до підтвердження

    assert again.posts == [] and again.edits == [] and again.last == 2
    assert folders(root) == [str(POST)]
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == before == 1


def test_folder_written_before_crash_is_reused_not_duplicated(conn, root, sconn, sroot, server,
                                                              tg, upd, clock):  # fmt: skip
    publish(server, tg, upd, clock, files=3)
    data = blob(sconn, sroot, 0)
    # перший раз упали після перейменування теки, до запису в базу; ще й лишилась тимчасова тека
    client.place_folder(root, POST, 1, photos_of(sroot / str(POST)))
    (root / f"{client.TEMP}1-{POST}-бот").mkdir()

    apply(conn, root, data)

    assert str(POST) in folders(root) and f"{POST}-бот" not in folders(root)
    assert conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (POST,)).fetchone()


def test_half_written_temp_folder_replaced(conn, root, sconn, sroot, server, tg, upd, clock):
    publish(server, tg, upd, clock, files=3)
    temp = root / f"{client.TEMP}1-{POST}"
    temp.mkdir()
    (temp / "1.jpg").write_bytes(b"half")  # обрив посеред запису

    apply(conn, root, blob(sconn, sroot, 0))

    assert folders(root) == [str(POST)]  # тимчасової теки більше немає
    assert photos_of(root / str(POST)) == photos_of(sroot / str(POST))


def test_existing_folder_with_other_photos_gets_bot_suffix(conn, root, sconn, sroot, server, tg,
                                                           upd, clock):  # fmt: skip
    (root / str(POST)).mkdir()
    (root / str(POST) / "1.jpg").write_bytes(b"her own photo")
    publish(server, tg, upd, clock, files=2)

    apply(conn, root, blob(sconn, sroot, 0))

    assert folders(root) == [str(POST), f"{POST}-бот"]
    assert photos_of(root / str(POST)) == {"1.jpg": b"her own photo"}
    assert photos_of(root / f"{POST}-бот") == photos_of(sroot / str(POST))


def test_records_applied_by_seq_even_if_listed_out_of_order(conn, root, sconn, sroot, server, tg,
                                                            upd, clock, site_on):  # fmt: skip
    publish(server, tg, upd, clock, files=2)
    server.handle(edit_update(upd, POST, "Нова ціна 900"))
    data = rezip(blob(sconn, sroot, 0), lambda i, f: ({**i, "records": i["records"][::-1]}, f))

    applied = apply(conn, root, data)

    assert applied.posts == [POST] and applied.edits == [POST] and applied.last == 2
    text = conn.execute("SELECT text FROM posts WHERE post_id = ?", (POST,)).fetchone()[0]
    assert text == "Нова ціна 900"  # правка — після поста, не до нього
    job = conn.execute("SELECT text FROM site_jobs WHERE post_id = ?", (POST,)).fetchone()
    assert job[0] == "Нова ціна 900"


def test_edit_in_later_exchange_updates_site_and_post(conn, root, sconn, sroot, server, tg, upd,
                                                      clock, site_on):  # fmt: skip
    publish(server, tg, upd, clock, files=2)
    pull(sconn, sroot, conn, root)
    server.handle(edit_update(upd, POST, "Продано не все: ціна 700"))

    applied = pull(sconn, sroot, conn, root)

    assert applied.edits == [POST] and applied.posts == []
    assert conn.execute("SELECT text FROM site_jobs").fetchone()[0] == "Продано не все: ціна 700"


def test_damaged_photo_writes_nothing_and_keeps_number(conn, root, sconn, sroot, server, tg, upd,
                                                       clock):  # fmt: skip
    publish(server, tg, upd, clock, files=2)
    data = rezip(
        blob(sconn, sroot, 0),
        lambda i, f: (i, {n: (b"bad" if n.endswith("1.jpg") else b) for n, b in f.items()}),
    )

    with pytest.raises(client.SyncError, match="sha256"):
        apply(conn, root, data)

    assert folders(root) == [] and client.last_seq(conn) == 0
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0


def test_cut_zip_is_an_error_not_a_partial_apply(conn, root, sconn, sroot, server, tg, upd, clock):
    publish(server, tg, upd, clock, files=2)
    data = blob(sconn, sroot, 0)

    with pytest.raises(client.SyncError, match="пошкоджений"):
        apply(conn, root, data[: len(data) // 2])

    assert folders(root) == [] and client.last_seq(conn) == 0


def test_unknown_kind_stops_before_it(conn, root, sconn, sroot, server, tg, upd, clock):
    publish(server, tg, upd, clock, files=2)
    future = {"seq": 2, "kind": "future", "at": 1, "post_id": None, "data": {}, "files": []}
    data = rezip(blob(sconn, sroot, 0), lambda i, f: ({**i, "records": i["records"] + [future]}, f))

    with pytest.raises(client.SyncError, match="оновіть програму"):
        apply(conn, root, data)

    assert client.last_seq(conn) == 1  # пост застосовано, невідоме — ні


def test_other_format_refused(conn, root, sconn, sroot):
    data = rezip(blob(sconn, sroot, 0), lambda i, f: ({**i, "format": 99}, f))

    with pytest.raises(client.SyncError, match="формат"):
        apply(conn, root, data)


# --- команди Власника ---


def sender(conn, root, tg):
    return client.sender(conn, root, tg, lambda t: None)


def test_owner_report_command_runs_once_on_computer(conn, root, sconn, sroot, server, tg, upd,
                                                    monkeypatch, chats):  # fmt: skip
    server.handle(owner_text(upd, "/zvit"))
    pull(sconn, sroot, conn, root)
    monkeypatch.setattr(report, "build", lambda *a, **k: "Звіт за сьогодні")
    home = FakeTelegram()

    assert client.run_commands(conn, sender(conn, root, home)) == 1
    assert client.run_commands(conn, sender(conn, root, home)) == 0

    assert home.texts(OWNER) == ["Звіт за сьогодні"]
    row = conn.execute("SELECT cmd, done_at, result FROM owner_cmds").fetchone()
    assert row["cmd"] == "zvit" and row["done_at"] and row["result"] == "виконано"


def test_owner_command_repeated_in_queue_stored_once(conn, root, sconn, sroot, server, upd):
    server.handle(owner_text(upd, "/detali"))
    data = blob(sconn, sroot, 0)
    apply(conn, root, data)
    with conn:
        conn.execute("UPDATE server_sync SET last_seq = 0")  # навіть якщо номер «відкотився»

    apply(conn, root, data)

    assert conn.execute("SELECT count(*) FROM owner_cmds").fetchone()[0] == 1


def test_owner_button_handled_on_computer_and_keeps_alert_text(conn, root, sconn, sroot, server,
                                                               upd, chats):  # fmt: skip
    update = button(upd, OWNER)
    update["callback_query"]["message"]["text"] = "⚠️ Нова Проблема: пост 7015"
    server.handle(update)
    pull(sconn, sroot, conn, root)
    home = FakeTelegram()

    client.run_commands(conn, sender(conn, root, home))

    [(_, params)] = [c for c in home.calls if c[0] == "editMessageText"]
    assert (params["chat_id"], params["message_id"]) == (OWNER, 300)
    assert params["text"].startswith("⚠️ Нова Проблема: пост 7015")
    assert actions.TEXTS["gone"] in params["text"]  # Картки 5 немає — «Уже вирішено»
    assert not [c for c in home.calls if c[0] == "answerCallbackQuery"]


def test_command_not_from_owner_skipped(conn, root, chats):
    with conn:
        conn.execute(
            "INSERT INTO owner_cmds (seq, cmd, data, at, received_at) VALUES (1, 'zvit', ?, 1, 'x')",
            (json.dumps({"cmd": "zvit", "chat_id": SELLER}),),
        )
    home = FakeTelegram()

    client.run_commands(conn, sender(conn, root, home))

    assert home.calls == []
    assert "пропущено" in conn.execute("SELECT result FROM owner_cmds").fetchone()[0]


def test_send_only_telegram_cannot_poll():
    tg = client.SendOnly("123:abc")
    try:
        with pytest.raises(RuntimeError, match="не опитує"):
            tg.get_updates(None, 25)
    finally:
        tg.close()


# --- обмін: підставний ssh ---


class FakeSsh:
    """Сервер у тому ж процесі; `fail` — на якій дії обірвати зв'язок."""

    def __init__(self, sconn, sroot):
        self.sconn, self.sroot, self.fail, self.calls = sconn, sroot, None, []

    def give(self, after):
        self.calls.append(f"give {after}")
        if self.fail == "give":
            raise client.SyncError("ssh: код 255: Connection refused")
        return blob(self.sconn, self.sroot, after)

    def ack(self, upto):
        self.calls.append(f"ack {upto}")
        if self.fail == "ack":
            raise client.SyncError("ssh: код 255: Connection reset")
        outbox.ack(self.sconn, upto, self.sroot)


def exchange(conn, root, ssh):
    return client.exchange(
        conn, root, ssh, data_dir=config.data_dir(), same_channel=False, log=lambda t: None
    )


def acked(sconn):
    return [r[0] for r in sconn.execute("SELECT seq FROM outbox WHERE acked_at IS NOT NULL")]


def test_exchange_applies_acks_and_records_link(conn, root, sconn, sroot, server, tg, upd, clock):
    publish(server, tg, upd, clock, files=2)
    ssh = FakeSsh(sconn, sroot)

    exchange(conn, root, ssh)
    exchange(conn, root, ssh)  # нового нічого — підтверджувати нічого

    assert ssh.calls == ["give 0", "ack 1", "give 1"]
    assert acked(sconn) == [1]
    state = client.link(conn)
    assert state.ok_at is not None and state.error is None


def test_exchange_down_records_error_then_recovers(conn, root, sconn, sroot, server, tg, upd,
                                                   clock):  # fmt: skip
    publish(server, tg, upd, clock, files=2)
    ssh = FakeSsh(sconn, sroot)
    ssh.fail = "give"

    with pytest.raises(client.SyncError):
        exchange(conn, root, ssh)
    assert client.link(conn).error.startswith("ssh: код 255") and client.link(conn).ok_at is None

    ssh.fail = None
    exchange(conn, root, ssh)
    assert client.link(conn).error is None and acked(sconn) == [1]


def test_lost_ack_is_repeated_next_time(conn, root, sconn, sroot, server, tg, upd, clock):
    publish(server, tg, upd, clock, files=2)
    ssh = FakeSsh(sconn, sroot)
    ssh.fail = "ack"
    with pytest.raises(client.SyncError):
        exchange(conn, root, ssh)
    assert client.last_seq(conn) == 1 and acked(sconn) == []  # застосовано, не підтверджено

    ssh.fail = None
    exchange(conn, root, ssh)

    assert acked(sconn) == [1] and folders(root) == [str(POST)]


def test_exchange_takes_several_rounds_when_more(conn, root, sconn, sroot, server, tg, upd, clock,
                                                 monkeypatch):  # fmt: skip
    for n in range(3):
        server.handle(owner_text(upd, "/zvit", msg_id=70 + n))
    monkeypatch.setattr(outbox, "LIMIT", 1)  # по одному запису за віддачу
    ssh = FakeSsh(sconn, sroot)
    ssh.give = lambda after: (ssh.calls.append(f"give {after}"),
                              outbox.give_bytes(sconn, sroot, after, limit=1)[0])[1]  # fmt: skip

    applied = exchange(conn, root, ssh)

    assert applied.last == 3 and applied.commands == 3
    assert ssh.calls == ["give 0", "ack 1", "give 1", "ack 2", "give 2", "ack 3"]


STUB = """
import os, subprocess, sys
command = sys.argv[-1]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
mode = os.environ.get("STUB_MODE", "")
if mode == "down":
    sys.stderr.write("ssh: connect to host 203.0.113.7 port 22: Connection refused\\n")
    sys.exit(255)
done = subprocess.run(
    [sys.executable, "-m", "shafa", "outbox", "--db", os.environ["STUB_DB"],
     "--root", os.environ["STUB_ROOT"], "--ssh"],
    env={**os.environ, "SSH_ORIGINAL_COMMAND": command}, capture_output=True,
)
out = done.stdout
if mode == "cut" and command.startswith("give"):
    out = out[: len(out) // 2]
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(done.stderr)
sys.exit(done.returncode)
"""


@pytest.fixture
def stub_ssh(tmp_path, sconn, monkeypatch):
    """Підставний `ssh`: окремий процес, що віддає команду `outbox --ssh` серверної бази."""
    script = tmp_path / "ssh-stub.py"
    script.write_text(STUB, encoding="utf-8")
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_DB", str(tmp_path / "сервер.sqlite"))
    monkeypatch.setenv("STUB_ROOT", str(tmp_path / "сервер-Шафа"))
    ssh = client.Ssh(
        "ubuntu@203.0.113.7", tmp_path / "key", tmp_path / "known_hosts",
        program=[sys.executable, str(script)], timeout=60,
    )  # fmt: skip
    return ssh, log


def test_real_ssh_transport_through_stub(conn, root, sconn, sroot, server, tg, upd, clock,
                                         stub_ssh, monkeypatch):  # fmt: skip
    ssh, log = stub_ssh
    publish(server, tg, upd, clock, files=2)
    sconn.commit()

    monkeypatch.setenv("STUB_MODE", "down")
    with pytest.raises(client.SyncError, match="Connection refused"):
        exchange(conn, root, ssh)
    monkeypatch.setenv("STUB_MODE", "cut")
    with pytest.raises(client.SyncError, match="пошкоджений"):
        exchange(conn, root, ssh)
    assert folders(root) == [] and client.last_seq(conn) == 0
    monkeypatch.setenv("STUB_MODE", "")
    exchange(conn, root, ssh)

    assert folders(root) == [str(POST)] and acked(sconn) == [1]
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[-1].endswith("ubuntu@203.0.113.7 ack 1")
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"):
        assert option in lines[-1]


def test_missing_ssh_program_is_a_plain_error(tmp_path):
    ssh = client.Ssh("h", tmp_path / "k", tmp_path / "kh", program=[str(tmp_path / "no-ssh")])

    with pytest.raises(client.SyncError, match="немає програми ssh"):
        ssh.give(0)


def test_transport_key_from_setting_or_data_dir(monkeypatch, on_server, tmp_path):
    assert client.transport().key == config.data_dir() / client.KEY_NAME
    monkeypatch.setenv(client.KEY, str(tmp_path / "мій-ключ"))
    ssh = client.transport()
    assert ssh.key == tmp_path / "мій-ключ" and ssh.host == "ubuntu@203.0.113.7"
    assert ssh.known_hosts == config.data_dir() / client.HOSTS_NAME


# --- фоновий потік ---


class StopAfterGive(FakeSsh):
    def __init__(self, sconn, sroot, stop):
        super().__init__(sconn, sroot)
        self.stop = stop

    def give(self, after):
        data = super().give(after)
        self.stop.set()
        return data


def test_client_thread_exchanges_and_runs_owner_commands(conn, root, sconn, sroot, server, upd,
                                                         tmp_path, monkeypatch, chats,
                                                         on_server):  # fmt: skip
    monkeypatch.setenv(bot_run.TOKEN, "123:abc")
    server.handle(owner_text(upd, "/zvit"))
    monkeypatch.setattr(report, "build", lambda *a, **k: "Звіт")
    home = FakeTelegram()
    home.close = lambda: None
    stop = threading.Event()
    worker = client.Client(
        tmp_path / "база.sqlite", root, ssh=StopAfterGive(sconn, sroot, stop),
        telegram=lambda token: home, log=lambda t: None, start_site=False,
    )  # fmt: skip

    worker.run(stop)

    assert home.texts(OWNER) == ["Звіт"]
    assert not [c for c in home.calls if c[0] == "getUpdates"]
    assert conn.execute("SELECT done_at FROM owner_cmds").fetchone()[0]


def test_start_does_nothing_without_server_setting(tmp_path, root):
    assert client.start(tmp_path / "база.sqlite", root) is None


# --- програма: бота на компі не запускаємо ніколи ---


def test_app_with_server_never_spawns_bot(conn, root, tmp_path, monkeypatch, on_server):
    monkeypatch.setenv(bot_run.TOKEN, "123:abc")
    spawned = []
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", bot_spawner=spawned.append)

    assert app.start_bot() == "bot_on_server"
    assert spawned == []


def test_app_without_server_still_spawns_bot(conn, root, tmp_path, monkeypatch):
    monkeypatch.setenv(bot_run.TOKEN, "123:abc")
    spawned = []
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", bot_spawner=spawned.append,
                  bot_lock=tmp_path / "bot.lock")  # fmt: skip

    assert app.start_bot() == "bot_started" and len(spawned) == 1


def test_quit_all_does_not_look_for_bot_lock(monkeypatch, on_server):
    looked = []
    monkeypatch.setattr(app_mod, "stop_process", lambda name, kill: looked.append(name) or False)

    app_mod.quit_all(lambda pid, sig: None)

    assert bot_run.LOCK_NAME not in looked


def test_poll_refuses_on_computer_with_server(conn, root, tg, on_server):
    bot = Bot(conn, tg, Settings(CHANNEL, SELLER, OWNER, root, GROUP), log=lambda t: None)
    tg.get_updates = lambda *a: pytest.fail("комп не опитує Telegram")

    with pytest.raises(bot_run.SetupError, match="на сервері"):
        bot_run.poll(bot, tg, conn, stop=lambda: False)


def test_bot_command_refuses_on_computer_with_server(tmp_path, root, on_server, capsys):
    db.connect(tmp_path / "база.sqlite").close()

    code = main(["bot", "--db", str(tmp_path / "база.sqlite"), "--root", str(root)])

    assert code == 2 and "на сервері" in capsys.readouterr().out


def test_server_setting_not_transferred():
    assert not transfer.transferable(client.SERVER, "ubuntu@203.0.113.7")
    assert not transfer.transferable(client.KEY, "/x/key")


# --- панель і самоперевірка ---


def set_ok(conn, moment):
    with conn:
        conn.execute(
            "INSERT INTO server_sync (id, last_seq, ok_at) VALUES (1, 0, ?) "
            "ON CONFLICT (id) DO UPDATE SET ok_at = excluded.ok_at",
            (moment.isoformat(timespec="seconds"),),
        )


def test_panel_shows_server_link_age(conn, root, tmp_path, on_server):
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs")
    assert app.bot_state(conn).code == "server_lost"  # обміну ще не було

    set_ok(conn, datetime.now().astimezone() - timedelta(minutes=7))
    state = app.bot_state(conn)
    assert state.code == "server" and "на зв'язку 7 хв тому" in state.text
    assert not state.can_start and web.BOT_TONES[state.code] == "ok"

    set_ok(conn, datetime.now().astimezone() - timedelta(hours=2))
    state = app.bot_state(conn)
    assert state.code == "server_lost" and web.BOT_TONES[state.code] == "warn"


def test_panel_warns_when_server_bot_heartbeat_old(conn, root, tmp_path, on_server):
    set_ok(conn, datetime.now().astimezone())
    old = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
    client._save_bot(config.data_dir(), {"heartbeat_at": old, "error": None})
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs")

    state = app.bot_state(conn)

    assert state.code == "server_lost" and "не відповідає" in state.text


def ctx(conn, moment):
    deps = health.Deps(lambda k: None, lambda t, c: "shafa_bot", lambda k: None, now=lambda: moment)
    return health._Ctx(config.data_dir(), config.data_dir(), deps, conn)


def test_health_bot_on_server_by_last_exchange(conn, on_server):
    moment = datetime.now().astimezone()
    assert health._server_beat(ctx(conn, moment), "shafa_bot").status == health.WARN
    set_ok(conn, moment - timedelta(minutes=12))
    assert health._server_beat(ctx(conn, moment), "shafa_bot").status == health.OK
    late = health._server_beat(ctx(conn, moment + timedelta(hours=2)), "shafa_bot")
    assert late.status == health.WARN and "зв'язку немає" in late.text


def test_site_enqueue_respects_switch(conn, root, sconn, sroot, server, tg, upd, clock):
    publish(server, tg, upd, clock, files=2)  # сайт вимкнено — черги сайту немає

    pull(sconn, sroot, conn, root)

    assert site.enabled() is False
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0
