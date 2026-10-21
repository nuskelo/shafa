"""BOT-011: кнопки під тривогами — вирішувати Проблеми прямо в Telegram."""

import pytest
from PIL import Image

from shafa import review
from shafa.bot import actions, report
from shafa.bot.core import Bot, Settings
from shafa.db import Status
from test_bot009 import DAY, stamp
from test_bot010 import problem, session, started, step
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, STRANGER, FakeTelegram

SHAFA = "https://shafa.ua/uk/product/5551234-kedy"
counter = {"update": 9000}


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


def one(conn, name):
    return conn.execute("SELECT * FROM items WHERE folder_name = ?", (name,)).fetchone()


def alert(conn, tg, make_bot, data, *specs):
    """Бот, який побачив нові Проблеми `(назва, код, текст)`, і що він надіслав Власнику."""
    clock, bot = started(conn, data, tg, make_bot)
    for name, code, text in specs:
        problem(conn, name, code, text)
    tg.calls.clear()
    step(clock, bot)
    return bot


def last_markup(tg):
    return [p["markup"] for m, p in tg.calls if m == "sendMessage" and p["chat_id"] == OWNER][-1]


def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def tap(bot, tg, data_, text="⚠️ alert", user=OWNER, chat=OWNER, chat_type="private", markup=None):
    counter["update"] += 1
    message = {
        "message_id": 700,
        "chat": {"id": chat, "type": chat_type},
        "text": text,
        "reply_markup": markup or {"inline_keyboard": []},
    }
    query = {"id": "cb1", "from": {"id": user}, "message": message, "data": data_}
    tg.calls.clear()
    bot.handle({"update_id": counter["update"], "callback_query": query})
    return counter["update"]


def edits(tg):
    return [p for m, p in tg.calls if m == "editMessageText"]


def final_text(tg):
    [edit] = edits(tg)
    return edit["text"]


def jpg(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 40), color).save(path, "JPEG")


# --- Кнопки під тривогою ---


def test_single_problem_gets_recheck_button_in_limit(conn, data, tg, make_bot):
    alert(conn, tg, make_bot, data, ("201", "no_price", "Немає ціни"))
    [button] = buttons(last_markup(tg))
    item = one(conn, "201")
    assert button["text"] == "🔁 Перевірити ще раз"
    assert button["callback_data"] == f"rc:{item['id']}:no_price"
    assert len(button["callback_data"].encode()) <= 64


def test_every_known_problem_code_fits_callback_data():
    longest = max([*report.PROBLEMS, "x" * 30], key=len)
    row = {"id": 123456, "problem_code": longest}
    assert len(f"{actions.ARCHIVE_YES}:{row['id']}:{longest}".encode()) <= 64


def test_publish_problems_have_no_buttons_for_the_owner_to_misuse(conn, data, tg, make_bot):
    alert(conn, tg, make_bot, data, ("201", "publish_uncertain", "Невідомо"))
    assert last_markup(tg) is None


def test_duplicate_alert_sends_album_and_two_buttons(conn, data, tg, make_bot, root):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201", "possible_duplicate", "Схоже")
    item = one(conn, "201")
    jpg(root / "201" / "1.jpg", "red")
    with conn:
        conn.execute("INSERT INTO photos VALUES (?, 0, '1.jpg')", (item["id"],))
    jpg(data.parent / "duplicates" / f"{item['id']}.jpg", "blue")
    tg.calls.clear()
    step(clock, bot)
    methods = [m for m, p in tg.calls if p.get("chat_id") == OWNER]
    assert methods == ["sendMediaGroup", "sendMessage"]  # альбом, потім текст із кнопками
    [(_, files, reply_to)] = tg.albums
    assert len(files) == 2 and reply_to is None
    assert [b["text"] for b in buttons(last_markup(tg))] == ["✅ Різні пари", "🗄 Дубль"]


def test_duplicate_without_photos_still_has_buttons(conn, data, tg, make_bot):
    alert(conn, tg, make_bot, data, ("201", "possible_duplicate", None))
    assert tg.albums == []
    assert len(buttons(last_markup(tg))) == 2


def test_declined_has_link_and_archive(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201", "shafa_declined", "Відхилено")
    with conn:
        conn.execute("UPDATE items SET shafa_id = 5551234, shafa_url = ? ", (SHAFA,))
    step(clock, bot)
    link, archive = buttons(last_markup(tg))
    assert link == {"text": "🔗 Відкрити на Шафі", "url": SHAFA}
    assert archive["text"] == "🗄 В архів"


def test_shafa_card_with_other_problem_has_no_buttons(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201", "no_price", None)
    with conn:
        conn.execute("UPDATE items SET shafa_id = 5551234")
    step(clock, bot)
    assert last_markup(tg) is None


def test_many_problems_buttons_for_first_five_only(conn, data, tg, make_bot):
    specs = [(str(300 + n), "no_price", None) for n in range(7)]
    alert(conn, tg, make_bot, data, *specs)
    keyboard = buttons(last_markup(tg))
    assert [b["text"] for b in keyboard] == [f"📂 Пост {300 + n}" for n in range(5)]
    [text] = [p["text"] for m, p in tg.calls if m == "sendMessage"]
    assert "решту — у програмі" in text


def test_open_button_sends_the_card_and_removes_its_row(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "no_price", None), ("202", "no_price", None))
    markup = last_markup(tg)
    first = buttons(markup)[0]
    tap(bot, tg, first["callback_data"], "⚠️ Нові Проблеми (2)", markup=markup)
    [(_, card)] = [(m, p) for m, p in tg.calls if m == "sendMessage"]
    assert card["text"] == "⚠️ Пост 201: Немає ціни"
    assert buttons(card["markup"])[0]["callback_data"].startswith("rc:")
    [edit] = edits(tg)
    assert [b["text"] for b in buttons(edit["markup"])] == ["📂 Пост 202"]


def test_open_button_of_duplicate_sends_album(conn, data, tg, make_bot, root):
    bot = alert(
        conn, tg, make_bot, data, ("201", "possible_duplicate", None), ("202", "no_price", None)
    )
    markup = last_markup(tg)
    item = one(conn, "201")
    jpg(root / "201" / "1.jpg", "red")
    with conn:
        conn.execute("INSERT INTO photos VALUES (?, 0, '1.jpg')", (item["id"],))
    jpg(data.parent / "duplicates" / f"{item['id']}.jpg", "blue")
    tap(bot, tg, buttons(markup)[0]["callback_data"], markup=markup)
    assert len(tg.albums) == 1 and len(tg.albums[0][1]) == 2


def test_session_stop_has_what_happened_button_and_no_start(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    sid = session(conn, "site", published=1, problems=1)
    problem(conn, "201", "robot_field_missing", "Поле «Колір»")
    with conn:
        conn.execute("UPDATE items SET updated_at = ?", (stamp(DAY, 10),))
    tg.calls.clear()
    step(clock, bot)
    keyboards = [p["markup"] for m, p in tg.calls if m == "sendMessage" and p["markup"]]
    stop = next(k for k in keyboards if buttons(k)[0]["text"] == "📋 Що сталось")
    assert [b["callback_data"] for b in buttons(stop)] == [f"ws:{sid}"]
    assert all("Старт" not in b["text"] for k in keyboards for b in buttons(k))
    tap(bot, tg, f"ws:{sid}", "⛔ Робот зупинився")
    text = final_text(tg)
    assert "Пост 201: Робот не знайшов поле на формі — Поле «Колір»" in text
    assert edits(tg)[0]["markup"] is None


def test_calm_session_end_has_no_buttons(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    session(conn, "done")
    tg.calls.clear()
    step(clock, bot)
    assert last_markup(tg) is None


# --- Перевірити ще раз ---


@pytest.mark.parametrize(
    ("code", "status", "line"),
    [
        ("robot_field_missing", Status.APPROVED, "Пост 201 → Схвалені"),
        ("robot_slow", Status.APPROVED, "Пост 201 → Схвалені"),
        ("photos_missing", Status.REVIEW, "Пост 201 → На перевірку"),
    ],
)
def test_recheck_does_what_the_screen_does(conn, data, tg, make_bot, code, status, line):
    bot = alert(conn, tg, make_bot, data, ("201", code, "Було"))
    item = one(conn, "201")
    tap(bot, tg, f"rc:{item['id']}:{code}")
    assert one(conn, "201")["status"] == status
    assert one(conn, "201")["problem_code"] is None
    assert final_text(tg).endswith(line)
    assert edits(tg)[0]["markup"] is None  # кнопки зникли
    assert tg.answers() == [line]


def test_recheck_same_result_as_review_recheck(conn, tg, root):
    for name in ("201", "202"):
        problem(conn, name, "robot_logged_out", "x")
    expected = review.recheck(conn, one(conn, "201")["id"], root, None)
    assert expected == review.Recheck("robot_retry", Status.APPROVED)


def test_recheck_without_folder_stays_problem(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "no_price", "У пості немає ціни"))
    item = one(conn, "201")
    tap(bot, tg, f"rc:{item['id']}:no_price")
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert final_text(tg).endswith("Пост 201 → досі Проблема: Папку не знайдено")


def test_recheck_does_not_touch_publish_problems(conn, tg, root):
    problem(conn, "201", "publish_uncertain", "?")
    item = one(conn, "201")
    assert review.recheck(conn, item["id"], root, None) == review.Recheck("sticky", Status.PROBLEM)
    assert one(conn, "201")["problem_code"] == "publish_uncertain"


def test_forged_recheck_for_publish_problem_does_nothing(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "publish_uncertain", "?"))
    item = one(conn, "201")
    tap(bot, tg, f"rc:{item['id']}:publish_uncertain")
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert actions.TEXTS["no_action"] in final_text(tg)


# --- В архів / не дубль ---


def duplicate_bot(conn, tg, make_bot, data, code="possible_duplicate"):
    bot = alert(conn, tg, make_bot, data, ("201", code, "Схоже"))
    return bot, one(conn, "201")["id"]


def test_archive_needs_confirmation(conn, data, tg, make_bot):
    bot, item_id = duplicate_bot(conn, tg, make_bot, data)
    tap(bot, tg, f"ar:{item_id}:possible_duplicate")
    assert one(conn, "201")["status"] == Status.PROBLEM  # одна кнопка нічого не ламає
    [edit] = edits(tg)
    assert [b["text"] for b in buttons(edit["markup"])] == ["Так, в архів", "Ні, назад"]
    tap(bot, tg, f"an:{item_id}:possible_duplicate")
    assert [b["text"] for b in buttons(edits(tg)[0]["markup"])] == ["✅ Різні пари", "🗄 Дубль"]
    assert one(conn, "201")["status"] == Status.PROBLEM
    tap(bot, tg, f"ay:{item_id}:possible_duplicate")
    assert one(conn, "201")["status"] == Status.ARCHIVED
    assert "Пост 201 → Архів" in final_text(tg)
    assert edits(tg)[0]["markup"] is None


def test_different_pairs_is_other_item_like_on_screen(conn, data, tg, make_bot):
    bot, item_id = duplicate_bot(conn, tg, make_bot, data)
    tap(bot, tg, f"od:{item_id}:possible_duplicate")
    row = one(conn, "201")
    assert row["status"] == Status.APPROVED and row["duplicate_ok_at"]
    assert "Пост 201 → Схвалені" in final_text(tg)


def test_declined_archive(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "201", "shafa_declined", "Відхилено")
    with conn:
        conn.execute("UPDATE items SET shafa_id = 5551234, shafa_url = ?", (SHAFA,))
    step(clock, bot)
    item_id = one(conn, "201")["id"]
    tap(bot, tg, f"ay:{item_id}:shafa_declined")
    assert one(conn, "201")["status"] == Status.ARCHIVED
    assert (
        one(conn, "201")["shafa_id"] == 5551234
    )  # `shafa_id` лишається: Картка в чергу не повернеться


def test_shafa_card_cannot_be_archived_unless_declined(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "no_price", None))
    with conn:
        conn.execute("UPDATE items SET shafa_id = 5551234")
    item_id = one(conn, "201")["id"]
    for action in ("ar", "ay", "rc", "od"):
        tap(bot, tg, f"{action}:{item_id}:no_price")
        assert actions.TEXTS["no_action"] in final_text(tg)
    assert one(conn, "201")["status"] == Status.PROBLEM


def test_no_button_can_approve():
    assert "approve" not in " ".join(actions.ACTIONS)
    assert set(actions.ALLOWED) <= set(actions.ACTIONS)


# --- Безпека ---


@pytest.mark.parametrize(
    ("user", "chat", "chat_type"),
    [
        (STRANGER, STRANGER, "private"),
        (SELLER, SELLER, "private"),  # Продавчиня користується ботом, але не тривогами
        (STRANGER, OWNER, "private"),  # чужий користувач у чаті Власника
        (OWNER, OWNER, "group"),
        (OWNER, -1001234, "supergroup"),
    ],
)
def test_only_owner_can_press(conn, data, tg, make_bot, user, chat, chat_type):
    bot = alert(conn, tg, make_bot, data, ("201", "robot_field_missing", "x"))
    item_id = one(conn, "201")["id"]
    tap(bot, tg, f"rc:{item_id}:robot_field_missing", user=user, chat=chat, chat_type=chat_type)
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert tg.answers() == ["Немає доступу"] and edits(tg) == []


def test_no_owner_configured_nobody_can_press(conn, data, tg, make_bot):
    clock, _ = started(conn, data, tg, make_bot)
    bot = make_bot(clock, owner=None)
    problem(conn, "201", "robot_field_missing", "x")
    item_id = one(conn, "201")["id"]
    tap(bot, tg, f"rc:{item_id}:robot_field_missing")
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert tg.answers() == ["Немає доступу"]


def test_already_solved_on_screen_is_not_touched(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "robot_field_missing", "x"))
    item_id = one(conn, "201")["id"]
    with conn:  # вирішили на екрані: Картка вже Схвалена
        conn.execute("UPDATE items SET status = 'approved', problem_code = NULL")
    tap(bot, tg, f"rc:{item_id}:robot_field_missing")
    assert one(conn, "201")["status"] == Status.APPROVED
    assert final_text(tg).endswith("Уже вирішено")
    assert edits(tg)[0]["markup"] is None


def test_problem_changed_to_another_code_is_not_touched(conn, data, tg, make_bot):
    bot, item_id = duplicate_bot(conn, tg, make_bot, data)
    with conn:
        conn.execute("UPDATE items SET problem_code = 'no_price'")
    tap(bot, tg, f"ay:{item_id}:possible_duplicate")
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert final_text(tg).endswith("Уже вирішено")


def test_card_in_robot_queue_is_not_touched(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "robot_field_missing", "x"))
    item_id = one(conn, "201")["id"]
    with conn:
        conn.execute("UPDATE items SET status = 'publishing'")
    tap(bot, tg, f"rc:{item_id}:robot_field_missing")
    assert one(conn, "201")["status"] == Status.PUBLISHING
    assert final_text(tg).endswith("Робот зараз працює з цією Карткою")


def test_missing_card_and_garbage_data_do_nothing(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "no_price", None))
    for raw in ("rc:99999:no_price", "rc:abc:x", "rc", "od:", "ws:77", "ws:x"):
        tap(bot, tg, raw)
        assert final_text(tg).endswith(("Уже вирішено", actions.TEXTS["no_action"]))
    assert one(conn, "201")["status"] == Status.PROBLEM


def test_same_update_is_not_handled_twice(conn, data, tg, make_bot):
    bot, item_id = duplicate_bot(conn, tg, make_bot, data)
    update_id = tap(bot, tg, f"od:{item_id}:possible_duplicate")
    assert len(edits(tg)) == 1
    tg.calls.clear()
    message = {"message_id": 700, "chat": {"id": OWNER, "type": "private"}, "text": "x"}
    query = {"id": "cb2", "from": {"id": OWNER}, "message": message, "data": f"od:{item_id}:x"}
    bot.handle({"update_id": update_id, "callback_query": query})
    assert tg.calls == []


def test_edit_failure_does_not_break_the_button(conn, data, tg, make_bot):
    from shafa.bot.telegram import TelegramError

    bot = alert(conn, tg, make_bot, data, ("201", "robot_field_missing", "x"))
    item_id = one(conn, "201")["id"]
    tg.errors["editMessageText"] = TelegramError("збій")
    tap(bot, tg, f"rc:{item_id}:robot_field_missing")
    assert one(conn, "201")["status"] == Status.APPROVED  # дію зроблено
    assert any(m == "editMessageReplyMarkup" for m, _ in tg.calls)  # хоч кнопки прибрано


def test_other_problem_that_the_action_would_also_accept_is_not_touched(conn, data, tg, make_bot):
    """Тривога була про дубль; Картка тим часом стала «уже опубліковано» — архівувати за старою
    кнопкою не можна, навіть якщо `to_archive` таке прийняв би."""
    bot, item_id = duplicate_bot(conn, tg, make_bot, data)
    with conn:
        conn.execute("UPDATE items SET problem_code = 'already_published'")
    tap(bot, tg, f"ay:{item_id}:possible_duplicate")
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert final_text(tg).endswith("Уже вирішено")


def test_recheck_with_old_code_is_not_done_for_new_problem(conn, data, tg, make_bot):
    bot = alert(conn, tg, make_bot, data, ("201", "robot_field_missing", "x"))
    item_id = one(conn, "201")["id"]
    with conn:
        conn.execute("UPDATE items SET problem_code = 'robot_slow'")
    tap(bot, tg, f"rc:{item_id}:robot_field_missing")
    assert one(conn, "201")["status"] == Status.PROBLEM
    assert final_text(tg).endswith("Уже вирішено")


def test_locked_database_does_not_crash_the_bot(conn, data, tg, make_bot, monkeypatch):
    import sqlite3

    from shafa.robot import publish

    bot, item_id = duplicate_bot(conn, tg, make_bot, data)

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(publish, "other_item", locked)
    tap(bot, tg, f"od:{item_id}:possible_duplicate")
    assert final_text(tg).endswith(actions.TEXTS["failed"])
    assert one(conn, "201")["status"] == Status.PROBLEM
