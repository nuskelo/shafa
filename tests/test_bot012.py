"""BOT-012: журнал подій — у бот приходить усе, що відбувається у фоні (ШІ, робот, довідники)."""

import json
import os
from datetime import timedelta

import pytest

from shafa import ai, batch, events, fill
from shafa.bot import alerts
from shafa.bot.core import Bot, Settings
from shafa.robot import browser, sync
from shafa.robot.form import FieldMissing
from shafa.robot.rehearse import Outcome, RehearsalReport
from test_ai import GOOD, FakeClient
from test_ai import message as ai_message
from test_batch import LIMITS, TODAY, line, sent_batch
from test_bot009 import DAY, Clock, at
from test_bot010 import session
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, FakeTelegram, message
from test_fill import add_item, planned_for
from test_robot_publish import Env, add


class QuietTelegram(FakeTelegram):
    """Окрім викликів, пам'ятає, які повідомлення були без звуку."""

    def __init__(self):
        super().__init__()
        self.flags = []  # (текст, без звуку)

    def send_message(self, chat_id, text, markup=None, silent=False):
        self.flags.append((text, silent))
        return super().send_message(chat_id, text, markup)


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
    return QuietTelegram()


@pytest.fixture
def make_bot(conn, tg, root):
    def make(clock):
        settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
        return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)

    return make


def journal(data):
    path = data / events.NAME
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def put(data, moment, kind, text, loud=False, extra=None):
    """Рядок журналу з відомим часом (бот читає файл, а не виклики `emit`)."""
    record = {"at": moment.isoformat(timespec="seconds"), "kind": kind, "text": text,
              "loud": loud, "data": extra or {}}  # fmt: skip
    with open(data / events.NAME, "ab") as stream:
        stream.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))


def started(tg, make_bot, hour=10):
    """Бот, який уже зробив свій перший цикл (запам'ятав стан) і нічого не надіслав."""
    clock = Clock(at(DAY, hour))
    bot = make_bot(clock)
    bot.tick()
    assert tg.flags == []
    return clock, bot


def step(clock, bot, minutes=1):
    clock.go(minutes=minutes)
    bot.tick()


def owner_texts(tg):
    return [(t, s) for t, s in tg.flags]


def command(update_id, chat_id, text):
    return {"update_id": update_id, "message": message(update_id, chat_id, text=text)}


# --- Журнал: emit ---


def test_emit_appends_one_json_line(data):
    events.emit("ai.start", "ШІ почав аналіз: 3 Картки", data={"n": 3})
    events.emit("robot.post", "6645 опубліковано", loud=True)
    first, second = journal(data)
    assert (first["kind"], first["text"], first["loud"], first["data"]) == (
        "ai.start", "ШІ почав аналіз: 3 Картки", False, {"n": 3}
    )  # fmt: skip
    assert second["loud"] is True and first["at"] and "T" in first["at"]
    assert "ШІ" in (data / events.NAME).read_bytes().decode("utf-8")  # кирилиця — UTF-8, не \u


def test_emit_failure_never_raises(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "файл"
    blocker.write_text("це не тека", encoding="utf-8")
    monkeypatch.setenv("SHAFA_DATA_DIR", str(blocker / "дані"))
    with caplog.at_level("WARNING"):
        events.emit("ai.start", "текст")  # тека не створюється — винятку немає
    assert "Журнал подій" in caplog.text


def test_emit_hides_secrets_and_chat_ids(data, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:BOT-SECRET-TOKEN")
    events.emit(
        "ai.stop", "збій 123456:BOT-SECRET-TOKEN у чаті 5550123456 файл /Users/vlad/x/y.png"
    )
    [record] = journal(data)
    assert "SECRET" not in record["text"] and "5550123456" not in record["text"]
    assert "/Users/vlad" not in record["text"]


def test_clean_url_drops_parameters():
    assert events.clean_url("https://shafa.ua/uk/item/1-x?utm=1&a=b#top") == (
        "https://shafa.ua/uk/item/1-x"
    )


def test_rotation_over_limit_replaces_old_copy(data, monkeypatch):
    monkeypatch.setattr(events, "MAX_BYTES", 200)
    (data / events.OLD_NAME).write_text("дуже стара копія\n", encoding="utf-8")
    for n in range(8):
        events.emit("ai.start", f"подія {n} " + "я" * 30)
    old = (data / events.OLD_NAME).read_text(encoding="utf-8")
    assert "дуже стара копія" not in old and "подія" in old
    assert len(journal(data)) >= 1


def test_reader_survives_rotation_without_loss_or_duplicates(data, monkeypatch):
    for n in range(3):
        events.emit("ai.start", f"до {n}")
    records, fresh = events.read_new(data, None)
    assert fresh and [r.text for r in records] == ["до 0", "до 1", "до 2"]
    last = records[-1]
    position = {"inode": last.inode, "offset": last.end}
    events.emit("ai.start", "дописано в старий")  # бот ще не прочитав цей рядок
    monkeypatch.setattr(events, "MAX_BYTES", 10)
    events.emit("ai.start", "у новому файлі")  # ротація: старий файл → events-old.jsonl
    assert (data / events.OLD_NAME).is_file()
    again, fresh = events.read_new(data, position)
    assert not fresh
    assert [r.text for r in again] == ["дописано в старий", "у новому файлі"]
    tail = again[-1]
    after, _ = events.read_new(data, {"inode": tail.inode, "offset": tail.end})
    assert after == []


def test_partial_last_line_waits_for_the_rest(data):
    events.emit("ai.start", "повний")
    with open(data / events.NAME, "ab") as stream:
        stream.write(b'{"at": "2026-10-21T10:00:00+03:00", "kind": "ai.sta')
    records, _ = events.read_new(data, None)
    assert [r.text for r in records] == ["повний"]


# --- Бот: перший запуск ---


def test_first_run_with_existing_journal_skips_old_events(conn, data, tg, make_bot):
    put(data, at(DAY, 8), "ai.done", "стара подія")
    put(data, at(DAY, 9, 55), "ai.done", "свіжа подія")
    clock, bot = started(tg, make_bot)
    step(clock, bot)
    step(clock, bot, 5)
    [(text, silent)] = owner_texts(tg)
    assert "свіжа подія" in text and "стара подія" not in text and silent
    saved = json.loads((data / "alerts-last.json").read_text(encoding="utf-8"))
    assert saved["events"]["offset"] > 0
    make_bot(clock).tick()
    step(clock, bot, 10)
    assert len(owner_texts(tg)) == 1  # перезапуск бота нічого не повторює


def test_state_file_from_bot010_still_loads(conn, data, tg, make_bot):
    old = {"session_id": 0, "problems": {}, "health_at": "", "health_sent": {}, "budget_day": "",
           "sent": {}, "hour": []}  # fmt: skip
    (data / "alerts-last.json").write_text(json.dumps(old), encoding="utf-8")
    state = alerts.load(data)
    assert state["detail"] is True and state["pending"] == [] and state["events"] is None


# --- Бот: тихі й гучні ---


def test_quiet_events_go_in_one_silent_message_every_five_minutes(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "robot.post", "6645 опубліковано · 690 грн · https://shafa.ua/uk/item/1")
    put(data, at(DAY, 10, 2), "robot.post", "6646 опубліковано · 450 грн")
    step(clock, bot)  # 10:01 — події прочитано, збірка чекає
    step(clock, bot, 4)  # 10:05
    assert tg.flags == []
    step(clock, bot)  # 10:06 — п'ять хвилин від першої
    [(text, silent)] = owner_texts(tg)
    assert silent is True
    assert text.splitlines() == [
        "10:00 ✅ 6645 опубліковано · 690 грн · https://shafa.ua/uk/item/1",
        "10:02 ✅ 6646 опубліковано · 450 грн",
    ]
    step(clock, bot, 10)
    assert len(owner_texts(tg)) == 1  # вдруге не шле


def test_fifteen_lines_flush_at_once(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    for n in range(15):
        put(data, at(DAY, 10), "robot.post", f"{n} опубліковано")
    step(clock, bot)
    [(text, silent)] = owner_texts(tg)
    assert silent and len(text.splitlines()) == 15


def test_loud_event_goes_at_once_without_silence_and_before_quiet(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "robot.post", "тихий")
    put(data, at(DAY, 10), "ai.stop", "ШІ не взявся за нові Картки: ліміт вичерпано")
    step(clock, bot)
    [(text, silent)] = owner_texts(tg)
    assert text == "⛔ ШІ не взявся за нові Картки: ліміт вичерпано" and silent is False
    step(clock, bot, 5)
    assert owner_texts(tg)[1][1] is True and "тихий" in owner_texts(tg)[1][0]


def test_loud_flag_of_a_single_event_beats_a_quiet_kind(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "ai.done", "важливе", loud=True)
    step(clock, bot)
    assert owner_texts(tg) == [("🤖 важливе", False)]


def test_unknown_kind_is_quiet_info(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "щось.нове", "дивина")
    step(clock, bot)
    step(clock, bot, 5)
    assert owner_texts(tg)[0][0].endswith("ℹ️ дивина")


def test_problem_events_of_robot_are_not_duplicated(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "robot.problem", "1342: Робот не знайшов поле «Сезон»")
    step(clock, bot)
    step(clock, bot, 5)
    assert tg.flags == []  # Проблему шле `_problem_events` з кнопками (BOT-011)


def test_telegram_failure_keeps_loud_event_without_duplicates(conn, data, tg, make_bot):
    from shafa.bot.telegram import TelegramError

    clock, bot = started(tg, make_bot)
    put(data, at(DAY, 10), "ai.stop", "перша")
    tg.errors["sendMessage"] = TelegramError("мережа")
    step(clock, bot)
    assert owner_texts(tg) == [("⛔ перша", False)]  # спроба була, не дійшла
    del tg.errors["sendMessage"]
    tg.flags.clear()
    step(clock, bot)
    step(clock, bot)
    assert tg.flags == [("⛔ перша", False)]


# --- Бот: антиспам ---


def test_loud_limit_twenty_per_hour_then_one_more_notice(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    for n in range(25):
        put(data, at(DAY, 10), "ai.stop", f"гучна {n}")
    step(clock, bot)
    texts = [t for t, _ in tg.flags]
    assert len(texts) == 20 and texts[-1] == "ℹ️ Ще 6 подій — подивись /zvit."
    assert not any(s for _, s in tg.flags)


def test_quiet_limit_twelve_batches_per_hour_then_notice(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    for _ in range(14):  # кожну хвилину по 15 рядків — збірка одразу
        for n in range(15):
            put(data, clock.now, "robot.post", f"{n} опубліковано")
        step(clock, bot)
    flags = tg.flags
    assert sum("опубліковано" in t for t, _ in flags) == 12
    notices = [t for t, _ in flags if t.startswith("ℹ️ Ще")]
    assert notices == ["ℹ️ Ще 15 подій — подивись /zvit."]
    assert all(s for _, s in flags)  # усе без звуку


# --- /detali ---


def test_detali_switches_between_detail_and_important_only(conn, data, tg, make_bot):
    bot = make_bot(Clock(at(DAY, 10)))
    bot.tick()
    bot.handle(command(1, OWNER, "/detali"))
    assert tg.texts(OWNER)[-1].startswith("Лише важливе")
    assert alerts.load(data)["detail"] is False
    bot.handle(command(2, OWNER, "/detali@shafa_bot"))
    assert tg.texts(OWNER)[-1].startswith("Детально")
    assert alerts.load(data)["detail"] is True


def test_important_only_ignores_the_journal_even_loud_events(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    bot.handle(command(1, OWNER, "/detali"))
    tg.flags.clear()
    put(data, at(DAY, 10), "ai.stop", "гучна")
    put(data, at(DAY, 10), "robot.post", "тиха")
    step(clock, bot)
    step(clock, bot, 6)
    assert tg.flags == []
    bot.handle(command(2, OWNER, "/detali"))  # знову «Детально»: минуле не вивалюється
    tg.flags.clear()
    step(clock, bot)
    step(clock, bot, 6)
    assert tg.flags == []
    put(data, clock.now, "ai.stop", "нова")
    step(clock, bot)
    assert [t for t, _ in tg.flags] == ["⛔ нова"]


def test_important_only_keeps_bot010_behavior(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    bot.handle(command(1, OWNER, "/detali"))
    tg.flags.clear()
    number = session(conn, "done", published=2, problems=1)
    put(data, at(DAY, 10), "robot.end", "Сесія 1: опубліковано 2", extra={"session": number})
    step(clock, bot)
    assert [t for t, _ in tg.flags] == ["✅ Сесія 1: опубліковано 2, у Проблемах 1"]


def test_detali_is_for_owner_only(conn, data, tg, make_bot):
    bot = make_bot(Clock(at(DAY, 10)))
    bot.tick()
    bot.handle(command(1, SELLER, "/detali"))
    assert alerts.load(data)["detail"] is True
    assert "Лише важливе" not in " ".join(tg.texts(SELLER))


def test_detali_when_alerts_are_off(conn, data, tg, make_bot, monkeypatch):
    monkeypatch.setenv("SHAFA_ALERTS", "off")
    make_bot(Clock(at(DAY, 10))).handle(command(1, OWNER, "/detali"))
    assert "вимкнено" in tg.texts(OWNER)[-1]


def test_alerts_off_sends_nothing_from_journal(conn, data, tg, make_bot, monkeypatch):
    monkeypatch.setenv("SHAFA_ALERTS", "off")
    put(data, at(DAY, 10), "ai.stop", "гучна")
    clock = Clock(at(DAY, 10))
    bot = make_bot(clock)
    for _ in range(3):
        step(clock, bot, 6)
    assert tg.flags == []


# --- Кінець сесії: журнал замість «✅ Сесія N» ---


def test_journal_end_replaces_calm_session_line(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "done", published=14, problems=1)
    put(
        data, at(DAY, 10), "robot.end",
        "Сесія 1: опубліковано 14, у Проблемах 1 · 38 хв · черга закінчилась",
        extra={"session": number},
    )  # fmt: skip
    step(clock, bot)
    step(clock, bot, 6)
    texts = [t for t, _ in tg.flags]
    assert len(texts) == 1 and "✅ Сесія" not in texts[0]
    assert texts[0].endswith(
        "⏹ Сесія 1: опубліковано 14, у Проблемах 1 · 38 хв · черга закінчилась"
    )


def test_calm_session_without_journal_line_is_still_announced(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    session(conn, "done", published=2, problems=0)
    step(clock, bot)
    assert [t for t, _ in tg.flags] == ["✅ Сесія 1: опубліковано 2, у Проблемах 0"]


def test_stop_of_session_is_loud_once_with_journal_too(conn, data, tg, make_bot):
    clock, bot = started(tg, make_bot)
    number = session(conn, "login", published=1, problems=0)
    put(data, at(DAY, 10), "robot.stop", "Сесія 1: зупинка", loud=True, extra={"session": number})
    step(clock, bot)
    step(clock, bot, 6)
    [(text, silent)] = owner_texts(tg)
    assert text.startswith("⛔ Робот зупинився") and silent is False


# --- ШІ ---


def many(conn, root, count):
    return [add_item(conn, root, folder=str(n), post_id=n) for n in range(1, count + 1)]


def test_ai_run_of_ten_cards_has_start_and_end_with_numbers(conn, root, data):
    many(conn, root, 10)
    rows = fill.pick_items(conn, None, 10)
    planned = [fill.plan(conn, root, r) for r in rows]
    answer = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}
    client = FakeClient(ai_message(answer, usage=(9000, 500)))
    results = fill.fill_all(conn, client, planned)
    start, done = journal(data)
    assert (start["kind"], start["text"]) == ("ai.start", "ШІ почав аналіз: 10 Карток")
    cents = round(sum(r.usage.cost_micro_usd for r in results) / 10_000)
    assert done["kind"] == "ai.done"
    assert done["text"].startswith("ШІ закінчив: 10 Карток — ")
    assert f"· {cents} ¢ (сьогодні " in done["text"] and done["text"].endswith(" з 3 $)")
    ready = sum(1 for r in results if not r.yellow)
    yellow = len(results) - ready
    assert f"{ready} готові до перевірки" in done["text"] or ready == 0
    assert (f"{yellow} з жовтим" in done["text"]) == bool(yellow)


def test_ai_run_with_problem_counts_it(conn, root, data):
    many(conn, root, 3)
    planned = [fill.plan(conn, root, r) for r in fill.pick_items(conn, None, 10)]
    bad = {**GOOD, "section": "", "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}
    fill.fill_all(conn, FakeClient(ai_message(bad, usage=(9000, 500))), planned)
    done = journal(data)[-1]
    assert "3 у Проблемах" in done["text"] and "готові" not in done["text"]


def test_single_card_ai_run_is_one_short_line(conn, root, data):
    add_item(conn, root)
    planned = [planned_for(conn, root)]
    answer = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}
    results = fill.fill_all(conn, FakeClient(ai_message(answer, usage=(9000, 500))), planned)
    [only] = journal(data)
    cents = round(results[0].usage.cost_micro_usd / 10_000)
    assert only["kind"] == "ai.done"
    assert only["text"].startswith("ШІ: 1342 готова до перевірки")
    assert only["text"].endswith(f"· {cents} ¢")


def test_ai_run_where_every_card_fails_is_a_loud_stop(conn, root, data):
    many(conn, root, 2)
    planned = [fill.plan(conn, root, r) for r in fill.pick_items(conn, None, 10)]
    error = ai.AiError("Ключ ШІ не приймається — скажіть Власнику", None)
    fill.fill_all(conn, FakeClient(error), planned)
    last = journal(data)[-1]
    assert last["kind"] == "ai.stop" and "Ключ ШІ не приймається" in last["text"]
    assert events.info(last["kind"]).loud


def test_retry_of_one_card_is_one_line(conn, root, data):
    item_id = add_item(conn, root)
    with conn:
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (item_id,))
    planned = planned_for_review(conn, root, item_id)
    answer = {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}
    fill.retry(conn, FakeClient(ai_message(answer, usage=(9000, 500))), planned)
    [only] = journal(data)
    assert only["text"].startswith("ШІ: 1342 готова до перевірки")


def planned_for_review(conn, root, item_id):
    row = fill.retry_row(conn, item_id)
    result = fill.plan(conn, root, row)
    assert isinstance(result, fill.Planned)
    return result


def test_batch_has_start_when_many_and_end_when_collected(conn, root, data):
    from test_batch import FakeClient as FakeBatchClient

    client = FakeBatchClient()
    ids, bid = sent_batch(conn, root, client, 2)
    [start] = journal(data)
    assert start["text"].startswith("ШІ почав аналіз: 2 Картки (пакет, ≈ ")
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{i}") for i in ids]
    batch.collect(conn, client, TODAY)
    done = journal(data)[-1]
    assert done["kind"] == "ai.done" and done["text"].startswith("ШІ закінчив: 2 Картки — ")
    assert "¢ (сьогодні" in done["text"]


def test_batch_of_one_card_says_nothing_at_start(conn, root, data):
    from test_batch import FakeClient as FakeBatchClient

    client = FakeBatchClient()
    ids, bid = sent_batch(conn, root, client, 1)
    assert journal(data) == []
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{ids[0]}")]
    batch.collect(conn, client, TODAY)
    [only] = journal(data)
    assert only["text"].startswith("ШІ: 1 готова до перевірки")


def test_collect_with_nothing_ready_says_nothing(conn, root, data):
    from test_batch import FakeClient as FakeBatchClient

    client = FakeBatchClient()
    sent_batch(conn, root, client, 1)
    batch.collect(conn, client, TODAY)  # пакет ще в роботі
    assert journal(data) == []


def test_batch_budget_stop_is_loud(conn, root, data, monkeypatch):
    from test_batch import FakeClient as FakeBatchClient

    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "0.001")
    many(conn, root, 1)
    batch.send(conn, FakeBatchClient(), root, TODAY, batch.limits())
    [stop] = journal(data)
    assert stop["kind"] == "ai.stop" and "ліміт витрат на добу" in stop["text"]


def test_batch_not_created_is_loud(conn, root, data):
    import anthropic
    import httpx2

    from test_batch import FakeClient as FakeBatchClient

    client = FakeBatchClient()
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    client.batches.create_error = anthropic.AuthenticationError(
        "x", response=httpx2.Response(401, request=request), body=None
    )
    many(conn, root, 2)
    batch.send(conn, client, root, TODAY, LIMITS)
    [stop] = journal(data)
    assert stop["kind"] == "ai.stop" and "Ключ ШІ не приймається" in stop["text"]


def test_rules_only_runs_are_short_events(conn, root, data):
    item_id = add_item(conn, root)
    with conn:
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (item_id,))
    assert fill.mark_refill(conn, [item_id]) == 1
    [only] = journal(data)
    assert only["kind"] == "ai.start" and "Перезаповнити: 1 Картка у черзі ШІ" in only["text"]


def test_precheck_and_sizes_dry_runs_write_nothing(conn, root, data):
    fill.precheck_approved(conn, root, dry_run=True)
    fill.recheck_sizes(conn, dry_run=True)
    assert journal(data) == []


# --- Робот ---


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setenv("SHAFA_DAILY_NEW", "30")  # межа «нових на день» тут не заважає
    monkeypatch.setenv("SHAFA_DAILY_OLD", "0")


@pytest.fixture
def env(conn, root, tmp_path, no_pause):
    return Env(conn, root, tmp_path)


def other_item(env, item_id):
    with env.conn:
        env.conn.execute("UPDATE items SET duplicate_ok_at = 'x' WHERE id = ?", (item_id,))


def test_robot_session_start_posts_and_end(env, data):
    newer = add(env, "1400", 1400, "2025-05-01T10:00:00")
    other_item(env, newer)
    add(env, "1342", 1342, "2025-03-01T10:00:00")
    env.run()
    records = journal(data)
    kinds = [r["kind"] for r in records]
    assert kinds == ["robot.start", "robot.post", "robot.post", "robot.end"]
    start, first, second, end = records
    assert start["text"] == "Сесія 1 почалась: у черзі 2 (старих 0, нових 2), ліміт сьогодні ще 30"
    assert first["text"].startswith("1342 опубліковано · ") and " грн · " in first["text"]
    assert first["text"].endswith("https://shafa.ua/uk/item/556-shafa")
    assert second["text"].startswith("1400 опубліковано")
    assert end["text"].startswith("Сесія 1: опубліковано 2, у Проблемах 0 · ")
    assert end["text"].endswith("черга закінчилась") and end["data"]["session"] == 1
    assert not end["loud"] and not events.info("robot.end").loud


def test_robot_post_link_has_no_parameters(env, data):
    add(env)
    env.run()
    post = next(r for r in journal(data) if r["kind"] == "robot.post")
    assert "?" not in post["text"] and "#" not in post["text"]


def test_robot_problem_is_logged_but_the_bot_skips_it(env, data):
    add(env)
    env.raises = FieldMissing("Додати річ")
    env.run()
    problems = [r for r in journal(data) if r["kind"] == "robot.problem"]
    assert problems and events.info("robot.problem").skip
    last = journal(data)[-1]
    assert last["kind"] == "robot.stop" and events.info("robot.stop").skip


def test_robot_stop_with_reason_is_not_calm(env, data):
    add(env)
    env.session.logins = [False]
    env.run()
    last = journal(data)[-1]
    assert last["kind"] == "robot.stop"
    assert last["data"]["session"] == 1 and last["data"]["code"]


# --- Пробний прогін і довідники ---


def report(*outcomes, code="done", reason="перевірено все, що було в списку"):
    return RehearsalReport(list(outcomes), reason, code)


def test_rehearsal_all_good_is_quiet_summary():
    kind, text = __import__("shafa.robot.rehearse", fromlist=["x"])._event(
        report(Outcome(1, 1, "ok"), Outcome(2, 2, "ok"), Outcome(3, 3, "ok"))
    )
    assert (kind, text) == ("rehearse.done", "Пробний прогін: 3 з 3 добре")
    assert not events.info(kind).loud


def test_rehearsal_problem_is_loud_and_says_what():
    from shafa.robot import rehearse

    bad = Outcome(2, 2, "error", "robot_field_missing", "Робот не знайшов поле «Сезон»", moved=True)
    kind, text = rehearse._event(report(Outcome(1, 1, "ok"), bad))
    assert kind == "rehearse.bad" and events.info(kind).loud
    assert text == (
        "Пробний прогін: добре 1 з 2, не пройшли 1 (пости 2); Схвалених серед них — 1, вони в "
        "«Проблемах». Перша: Робот не знайшов поле «Сезон»"
    )
    assert "robot_field_missing" not in text  # без службових кодів


def test_rehearsal_stop_reason_is_in_text():
    from shafa.robot import rehearse

    kind, text = rehearse._event(report(code="login", reason="немає входу на Шафу"))
    assert kind == "rehearse.bad" and text.endswith("Зупинка: немає входу на Шафу")


def test_sync_catalog_events_start_done_and_error(data, tmp_path, monkeypatch):
    snapshot = tmp_path / "shafa-catalog.json"
    snapshot.write_text(json.dumps({"sections": {"А": {"x": 1}, "Б": {"x": 1}}, "brands": ["a"]}),
                        encoding="utf-8")  # fmt: skip
    monkeypatch.setattr(sync, "SNAPSHOT", snapshot)
    fresh = {"sections": {"А": {"x": 2}, "В": {"x": 1}}, "brands": ["a", "b"]}
    monkeypatch.setattr(sync, "crawl", lambda session, cache, log=print: fresh)
    sync.sync_and_save(None, tmp_path / "кеш")
    assert (
        json.loads(snapshot.read_text(encoding="utf-8")) == fresh
    )  # у підмінений файл, не в репозиторій
    start, done = journal(data)
    assert start["kind"] == "catalog.start"
    assert done["text"] == (
        "Довідники Шафи оновлено: розділів 2: нових 1, зниклих 1, змінених 1, брендів змінилось 1"
    )

    def broken(session, cache, log=print):
        raise RuntimeError("Немає входу на Шафу")

    monkeypatch.setattr(sync, "crawl", broken)
    with pytest.raises(RuntimeError):
        sync.sync_and_save(None, tmp_path / "кеш")
    error = journal(data)[-1]
    assert error["kind"] == "catalog.error" and "Немає входу" in error["text"]
    assert events.info("catalog.error").loud


# --- Таблиця видів і Telegram ---


def test_every_kind_has_emoji_and_known_flags():
    assert events.KINDS and all(k.emoji for k in events.KINDS.values())
    assert events.info("не-існує") is events.OTHER and not events.OTHER.loud
    assert [events.cards(n) for n in (1, 2, 5, 11, 12, 21, 25)] == [
        "1 Картка", "2 Картки", "5 Карток", "11 Карток", "12 Карток", "21 Картка", "25 Карток"
    ]  # fmt: skip


def test_telegram_silent_sets_disable_notification():
    import httpx2

    from shafa.bot.telegram import Telegram

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg = Telegram("1:T", httpx2.Client(transport=httpx2.MockTransport(handler)))
    tg.send_message(1, "тихо", silent=True)
    tg.send_message(1, "гучно")
    assert seen[0]["disable_notification"] is True and "disable_notification" not in seen[1]


def test_journal_files_are_utf8(data):
    events.emit("ai.start", "Кирилиця: щ, ї, є, ґ")
    raw = (data / events.NAME).read_bytes()
    assert "Кирилиця: щ, ї, є, ґ".encode() in raw
    assert os.path.getsize(data / events.NAME) == len(raw)
    assert timedelta(minutes=10) == alerts.FRESH
