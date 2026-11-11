"""OPS-003: бот на сервері — вихідна черга для компа, команди Власника чергою, перехід з компа.
Лише підставний Telegram і тимчасові теки; мережі й сервера немає."""

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta

import pytest

from shafa import config, db
from shafa.__main__ import main
from shafa.bot import actions, move, outbox, report
from shafa.bot import run as bot_run
from shafa.bot.core import TEXTS, Bot, Settings
from shafa.robot import lock
from test_bot_core import (
    CHANNEL,
    GROUP,
    OWNER,
    SELLER,
    TEXT,
    Clock,
    FakeTelegram,
    Updates,
    chat,
    press,
    send_post,
)

SENT_AT = 1_791_960_000  # дата поста з підставного Telegram (`send_photo` у канал)


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
def bot(conn, tg, root, clock):
    settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
    return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)


@pytest.fixture
def server(conn, tg, root, clock):
    settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP, server=True)
    return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)


@pytest.fixture
def site_on(monkeypatch):
    monkeypatch.setenv("SITE_INTEGRATION_KEY", "sk-site-test")
    monkeypatch.setenv("SHAFA_SITE_SEND", "1")


def publish(bot, tg, upd, clock, draft_id=1, files=3, text=TEXT):
    send_post(bot, tg, upd, clock, files=files, text=text)
    press(upd, bot, tg, "pub", draft_id=draft_id)


def queue_rows(conn):
    return [
        {**dict(r), "data": json.loads(r["data"])}
        for r in conn.execute("SELECT seq, kind, at, post_id, data, ready FROM outbox ORDER BY seq")
    ]


def unzip(data: bytes) -> tuple[dict, dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        files = {n: zf.read(n) for n in zf.namelist() if n != outbox.INDEX}
        return json.loads(zf.read(outbox.INDEX)), files


def edit_update(upd, post_id, text, chat_id=CHANNEL, edit_date=SENT_AT + 60):
    post = {"message_id": post_id, "chat": {"id": chat_id, "type": "channel"}, "date": SENT_AT,
            "edit_date": edit_date, "caption": text}  # fmt: skip
    return upd(edited_channel_post=post)


# --- міграція 17 ---


def test_migration_16_to_17_keeps_data_and_second_start_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "v16.sqlite"
    with monkeypatch.context() as m:
        m.setattr(db, "SCHEMA_VERSION", 16)
        conn = db.connect(path)
        with conn:
            conn.execute("INSERT INTO posts VALUES (7001, '2026-11-01T10:00:00', 'т', 'bot', 'x')")
            conn.execute(
                "INSERT INTO bot_drafts (id, chat_id, state, created_at, last_message_at, "
                "updated_at) VALUES (4, 1, 'preview', 'a', 'b', 'c')"
            )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'outbox'").fetchone()
        conn.close()

    conn = db.connect(path)
    schema = [tuple(r) for r in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert {"outbox", "server_sync", "owner_cmds"} <= {r[0] for r in schema}
    assert [tuple(r) for r in conn.execute("SELECT post_id, text FROM posts")] == [(7001, "т")]
    assert [tuple(r) for r in conn.execute("SELECT id, state FROM bot_drafts")] == [(4, "preview")]
    conn.close()

    conn = db.connect(path)  # другий запуск: нічого не міняється
    assert [
        tuple(r) for r in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")
    ] == (schema)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_outbox_seq_never_reused_after_delete(conn):
    with conn:
        first = outbox.add(conn, "edit", 1, 5, {"text": "a"})
        conn.execute("DELETE FROM outbox")
        second = outbox.add(conn, "edit", 1, 5, {"text": "b"})
    assert second > first  # комп пам'ятає «після N» — номер не має повторитись


# --- серверний режим: пости, правки, «вже в черзі» ---


def test_server_publish_goes_to_outbox_not_to_site(conn, server, tg, upd, clock, root, site_on):
    publish(server, tg, upd, clock)

    assert len(tg.channel_posts) == 1  # у канал — як завжди
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0
    [row] = queue_rows(conn)
    assert row["kind"] == "post" and row["post_id"] == 1401 and row["ready"] == 1
    assert row["at"] == SENT_AT  # Unix-час: місцеву дату порахує комп
    assert row["data"] == {"text": row["data"]["text"], "source": "bot", "via": "published",
                           "photos": 3, "folder": "1401"}  # fmt: skip
    assert (root / "1401" / "3.jpg").is_file()


def test_ordinary_bot_writes_no_outbox(conn, bot, tg, upd, clock, site_on):
    publish(bot, tg, upd, clock)

    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 1
    assert queue_rows(conn) == []


def test_folder_not_written_blocks_queue_until_restart(conn, server, tg, upd, clock, root):
    def crash(post_id, photos):
        raise KeyboardInterrupt  # процес помер посеред запису фото

    server._save_folder = crash
    with pytest.raises(KeyboardInterrupt):
        publish(server, tg, upd, clock)
    with conn:
        outbox.add(conn, "edit", 1, 9, {"text": "пізніша правка"})

    [post, edit] = queue_rows(conn)
    assert post["kind"] == "post" and post["ready"] == 0
    assert outbox.give_bytes(conn, root, 0)[1].count == 0  # нічого — і правку після нього теж

    restarted = Bot(conn, tg, server.settings, clock=clock, log=lambda t: None)
    restarted.recover()

    given = outbox.give_bytes(conn, root, 0)[1]
    assert (given.count, given.last) == (2, edit["seq"])
    assert queue_rows(conn)[0]["data"]["folder"] is None
    assert any("1401" in t for t in tg.texts(OWNER))


def test_folder_failure_still_reaches_computer(conn, server, tg, upd, clock, root):
    def refuse(post_id, photos):
        raise OSError("диск повний")

    server._save_folder = refuse
    publish(server, tg, upd, clock)

    [row] = queue_rows(conn)
    assert row["ready"] == 1 and row["data"]["folder"] is None


def test_channel_edit_goes_to_outbox_not_to_cards_or_site(conn, server, upd, monkeypatch):
    from shafa import changes, site

    def forbidden(*args, **kwargs):
        raise AssertionError("на сервері Картки й сайт не чіпаються")

    monkeypatch.setattr(changes, "text_changed", forbidden)
    monkeypatch.setattr(site, "edited", forbidden)
    update = edit_update(upd, 7001, "Нова ціна 900")
    server.handle(update)
    server.handle(update)  # повторна доставка
    server.handle(edit_update(upd, 7002, "чужий канал", chat_id=-100555))

    [row] = queue_rows(conn)
    assert (row["kind"], row["post_id"], row["at"]) == ("edit", 7001, SENT_AT + 60)
    assert row["data"] == {"text": "Нова ціна 900"}


def test_known_post_on_queue_goes_to_outbox(conn, server, tg, upd, clock):
    send_post(server, tg, upd, clock)
    with conn:
        conn.execute("INSERT INTO posts VALUES (777, '2026-11-01T10:00:00', '', 'export', 'x')")
    draft = conn.execute("SELECT * FROM bot_drafts WHERE id = 1").fetchone()

    assert server._enqueue(draft, (777, SENT_AT, TEXT), quiet=True) == "known"
    [row] = queue_rows(conn)
    assert (row["kind"], row["post_id"], row["data"]) == ("known", 777, {"draft": 1})


def test_queued_post_goes_to_outbox_with_folder(conn, server, tg, upd, clock, root, site_on):
    send_post(server, tg, upd, clock, files=2)
    draft = conn.execute("SELECT * FROM bot_drafts WHERE id = 1").fetchone()

    assert server._enqueue(draft, (6900, SENT_AT - 86400, TEXT), quiet=True) == "queued"
    [row] = queue_rows(conn)
    assert row["kind"] == "post" and row["at"] == SENT_AT - 86400
    assert row["data"]["via"] == "queued" and row["data"]["folder"] == "6900"
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0


def test_server_tick_has_no_report_and_no_alerts(server, bot, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(Bot, "_daily_report", lambda self, now_: calls.append("report"))
    monkeypatch.setattr(Bot, "_alerts", lambda self, now_: calls.append("alerts"))

    server.tick()
    assert calls == []
    bot.tick()  # звичайний бот — як і був
    assert calls == ["report", "alerts"]


# --- команди й кнопки Власника ---


def owner_text(upd, text, chat_id=OWNER, msg_id=70):
    return upd(message={"message_id": msg_id, "chat": chat(chat_id), "from": {"id": chat_id},
                        "date": SENT_AT, "text": text})  # fmt: skip


def test_owner_commands_queued_for_computer(conn, server, tg, upd, monkeypatch):
    monkeypatch.setattr(report, "build", lambda *a, **k: pytest.fail("звіт складає комп"))
    update = owner_text(upd, "/zvit")
    server.handle(update)
    server.handle(update)  # повтор
    server.handle(owner_text(upd, "/detali", msg_id=71))

    rows = queue_rows(conn)
    assert [(r["kind"], r["data"]["cmd"]) for r in rows] == [
        ("owner_cmd", "zvit"), ("owner_cmd", "detali"),
    ]  # fmt: skip
    assert rows[0]["data"] == {"cmd": "zvit", "chat_id": OWNER, "message_id": 70}
    assert tg.texts(OWNER) == [TEXTS["owner_later"]] * 2


def test_seller_command_is_not_queued(conn, server, tg, upd):
    server.handle(owner_text(upd, "/zvit", chat_id=SELLER))

    assert queue_rows(conn) == []
    assert TEXTS["owner_later"] not in tg.texts(SELLER)


def button(upd, user, data="rc:5:ai_error"):
    keyboard = [[{"text": "🔁", "callback_data": data}]]
    query = {"id": "cb", "from": {"id": user}, "data": data,
             "message": {"message_id": 300, "chat": chat(user),
                         "reply_markup": {"inline_keyboard": keyboard}}}  # fmt: skip
    return upd(callback_query=query)


def test_owner_button_queued_and_answered(conn, server, tg, upd, monkeypatch):
    monkeypatch.setattr(actions, "handle", lambda *a: pytest.fail("кнопки виконує комп"))
    update = button(upd, OWNER)
    server.handle(update)
    server.handle(update)

    [row] = queue_rows(conn)
    assert row["kind"] == "owner_cmd"
    assert row["data"]["cmd"] == "button" and row["data"]["data"] == "rc:5:ai_error"
    assert (row["data"]["chat_id"], row["data"]["message_id"]) == (OWNER, 300)
    assert tg.answers() == [TEXTS["owner_later"]]


def test_button_from_someone_else_denied_and_not_queued(conn, server, tg, upd):
    server.handle(button(upd, SELLER))

    assert queue_rows(conn) == []
    assert tg.answers() == [actions.TEXTS["denied"]]


# --- віддача й підтвердження ---


def test_give_has_records_and_photos_and_repeat_is_identical(conn, server, tg, upd, clock, root):
    publish(server, tg, upd, clock)
    server.handle(edit_update(upd, 1401, "Знижка"))

    first, given = outbox.give_bytes(conn, root, 0)
    index, files = unzip(first)
    assert (given.count, given.last, given.more) == (2, 2, False)
    assert [r["kind"] for r in index["records"]] == ["post", "edit"]
    listed = index["records"][0]["files"]
    assert [f["name"] for f in listed] == ["1.jpg", "2.jpg", "3.jpg"]
    assert files["photos/1/1.jpg"] == (root / "1401" / "1.jpg").read_bytes()
    import hashlib

    assert listed[0]["sha256"] == hashlib.sha256(files["photos/1/1.jpg"]).hexdigest()
    assert index["bot"] is None or "heartbeat_at" in index["bot"]

    again, _ = outbox.give_bytes(conn, root, 0)  # обрив — комп просить ще раз
    assert unzip(again)[0]["records"] == index["records"] and unzip(again)[1] == files


def test_ack_only_what_was_given_and_repeat_is_harmless(conn, server, tg, upd, clock, root):
    publish(server, tg, upd, clock)
    with pytest.raises(outbox.OutboxError):
        outbox.ack(conn, 1, root)  # ще не віддавали
    outbox.give_bytes(conn, root, 0)

    assert outbox.ack(conn, 1, root).count == 1
    assert outbox.ack(conn, 1, root).count == 0
    with pytest.raises(outbox.OutboxError):
        outbox.ack(conn, 5, root)
    assert outbox.give_bytes(conn, root, 1)[1].count == 0


def test_give_in_portions(conn, root):
    with conn:
        for n in range(3):
            outbox.add(conn, "edit", n, 100 + n, {"text": str(n)})

    given = outbox.give_bytes(conn, root, 0, limit=2)[1]
    assert (given.count, given.last, given.more) == (2, 2, True)
    assert outbox.give_bytes(conn, root, 2, limit=2)[1].count == 1


def test_photos_pruned_only_long_after_ack(conn, server, tg, upd, clock, root):
    publish(server, tg, upd, clock)
    publish(server, tg, upd, clock, draft_id=2, text=TEXT.replace("Lowa", "Ecco"))
    outbox.give_bytes(conn, root, 0, limit=1)
    outbox.ack(conn, 1, root)  # лише перший пост
    later = datetime.now().astimezone() + timedelta(days=outbox.KEEP_DAYS + 1)

    assert outbox.prune(conn, root, datetime.now().astimezone()) == []
    assert outbox.prune(conn, root, later) == ["1401"]
    assert not (root / "1401").exists() and (root / "1402").is_dir()


def test_post_folder_refuses_outside_and_links(root, tmp_path):
    outside = tmp_path / "чуже"
    outside.mkdir()
    (root / "лінк").symlink_to(outside)

    assert outbox.post_folder(root, "../чуже") is None
    assert outbox.post_folder(root, "лінк") is None
    assert outbox.post_folder(root, None) is None


@pytest.mark.parametrize(
    "command, ok",
    [("give 0", ("give", 0)), ("ack 12", ("ack", 12)), ("give 1; rm -rf ~", None),
     ("scp -t .", None), ("", None), (None, None), ("give -1", None), ("give 1\n", None)],
)  # fmt: skip
def test_ssh_command_allows_only_two_actions(command, ok):
    if ok is None:
        with pytest.raises(outbox.OutboxError):
            outbox.parse_ssh(command)
    else:
        assert outbox.parse_ssh(command) == ok


def test_cli_give_prints_only_zip_and_ssh_refuses_others(tmp_path, root, monkeypatch, capfdbinary):
    path = tmp_path / "сервер.sqlite"
    conn = db.connect(path)
    with conn:
        outbox.add(conn, "edit", 1, 7001, {"text": "т"})
    conn.close()

    assert main(["outbox", "--db", str(path), "--root", str(root), "--give", "0"]) == 0
    assert zipfile.is_zipfile(io.BytesIO(capfdbinary.readouterr().out))

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "cat ~/.ssh/authorized_keys")
    assert main(["outbox", "--db", str(path), "--root", str(root), "--ssh"]) == 2
    assert capfdbinary.readouterr().out == b""
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "ack 1")
    assert main(["outbox", "--db", str(path), "--root", str(root), "--ssh"]) == 0
    assert capfdbinary.readouterr().out == b"ok 1\n"


# --- перехід з компа на сервер ---


@pytest.fixture
def mac(conn, bot, tg, upd, clock, root, tmp_path):
    """Комп перед переходом: опублікований пост з альбомом, що ще чекає, відкрита чернетка,
    старий пост з Експорту, пам'ять каналу."""
    publish(bot, tg, upd, clock)  # 3 фото → коментарі «waiting»
    send_post(bot, tg, upd, clock, text=TEXT.replace("Lowa", "Ecco"))
    with conn:
        conn.execute(
            "INSERT INTO posts VALUES (900, '2025-01-01T10:00:00', 'старий', 'export', 'x')"
        )
        conn.execute("INSERT INTO channel_posts VALUES (950, 1700000000, 'вручну', NULL, 'x')")
    return tmp_path / "база.sqlite"


def test_bot_pack_unpack_moves_bot_state(conn, mac, root, tmp_path, tg, clock):
    out = tmp_path / "бот.zip"
    packed = move.pack(mac, root, out)
    assert packed.folders == ["1401"] and packed.counts["bot_drafts"] == 2

    target, server_root = tmp_path / "сервер.sqlite", tmp_path / "сервер-шафа"
    server_root.mkdir()
    done = move.unpack(out, target, server_root)

    assert done.added["bot_drafts"] == 2 and done.folders == ["1401"]
    new = db.connect(target)
    try:
        for table in ("bot_updates", "bot_drafts", "bot_draft_parts", "channel_posts"):
            old_rows = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            assert [tuple(r) for r in new.execute(f"SELECT * FROM {table} ORDER BY rowid")] == (
                old_rows
            ), table
        posts = new.execute("SELECT post_id, text, source FROM posts ORDER BY post_id")
        assert [tuple(r) for r in posts] == [
            (900, "", "export"), (1401, "", "bot"),
        ]  # fmt: skip
        assert new.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert (server_root / "1401" / "3.jpg").read_bytes() == (
            root / "1401" / "3.jpg"
        ).read_bytes()

        # Telegram віддає серверу останню пачку, яку комп обробив, але не підтвердив
        last = new.execute("SELECT max(update_id) FROM bot_updates").fetchone()[0]
        parts = new.execute("SELECT count(*) FROM bot_draft_parts").fetchone()[0]
        settings = Settings(CHANNEL, SELLER, OWNER, server_root, GROUP, server=True)
        server = Bot(new, tg, settings, clock=clock, log=lambda t: None)
        calls = len(tg.calls)
        server.handle({"update_id": last, "message": {"message_id": 99, "chat": chat(SELLER),
                                                       "from": {"id": SELLER}, "text": "x"}})  # fmt: skip
        assert new.execute("SELECT count(*) FROM bot_draft_parts").fetchone()[0] == parts
        assert len(tg.calls) == calls
    finally:
        new.close()

    again = move.unpack(out, target, server_root)  # повтор — нічого нового
    assert not any(again.added.values()) and again.folders_kept == ["1401"]


def test_unpack_refuses_other_bot_base_and_changed_photos(mac, root, tmp_path):
    out = tmp_path / "бот.zip"
    move.pack(mac, root, out)
    target, server_root = tmp_path / "сервер.sqlite", tmp_path / "с"
    server_root.mkdir()
    other = db.connect(target)
    with other:
        other.execute(
            "INSERT INTO bot_drafts (id, chat_id, state, created_at, last_message_at, updated_at) "
            "VALUES (1, 42, 'preview', 'інша', 'b', 'c')"
        )
    other.close()
    with pytest.raises(move.MoveError, match="інша чернетка"):
        move.unpack(out, target, server_root)
    assert not (server_root / "1401").exists()  # до відмови нічого не записано

    (server_root / "1401").mkdir()
    (server_root / "1401" / "1.jpg").write_bytes(b"other")
    with pytest.raises(move.MoveError, match="іншими фото"):
        move.unpack(out, tmp_path / "нова.sqlite", server_root)


def test_unpack_refuses_strange_paths(tmp_path, root):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        index = {"format": 1, "schema": 17, "tables": {}, "folders": {"x": []}}
        zf.writestr(move.INDEX, json.dumps(index))
        zf.writestr("folders/../../evil/1.jpg", b"x")
    with pytest.raises(move.MoveError, match="підозрілий"):
        move.unpack(bad, tmp_path / "с.sqlite", root)
    assert not (tmp_path / "evil").exists()


def test_pack_refuses_while_bot_runs(mac, root, tmp_path):
    held = lock.acquire("bot", config.data_dir() / bot_run.LOCK_NAME)
    try:
        with pytest.raises(move.MoveError, match="Бот зараз працює"):
            move.pack(mac, root, tmp_path / "бот.zip")
    finally:
        held.release()


def test_unpacked_base_keeps_schema(mac, root, tmp_path):
    move.pack(mac, root, tmp_path / "бот.zip")
    move.unpack(tmp_path / "бот.zip", tmp_path / "с.sqlite", root)
    conn = sqlite3.connect(tmp_path / "с.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
