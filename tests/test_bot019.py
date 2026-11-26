"""BOT-019: один довідник «що сталось → що робити» — екран і бот говорять однаково, кожен код
із коду має підказку, повідомлення ШІ мають цифри (початок тихо, кінець гучно)."""

import re
from pathlib import Path

import pytest

from shafa import ai, batch, events, fill, hints, review, web
from shafa.bot import actions, report, sessions
from shafa.bot.core import Bot, Settings
from shafa.klubok import queue as klubok_queue
from shafa.klubok import snapshot
from shafa.robot import control, klubok_control, klubok_publish, listings, publish
from test_ai import GOOD, FakeClient
from test_ai import message as ai_message
from test_bot010 import problem, sent, session, started, step
from test_bot012 import journal, put
from test_bot_core import CHANNEL, GROUP, OWNER, SELLER, FakeTelegram
from test_fill import add_item, planned_for


@pytest.fixture
def data(tmp_path, monkeypatch):
    folder = tmp_path / "дані"
    folder.mkdir()
    monkeypatch.setenv("SHAFA_DATA_DIR", str(folder))
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "3")
    monkeypatch.delenv("SHAFA_ALERTS", raising=False)
    monkeypatch.setenv("SHAFA_ALERTS_TO", "owner")
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


SRC = Path(__file__).resolve().parent.parent / "src" / "shafa"


def literals(pattern: str, *files: str) -> set[str]:
    found: set[str] = set()
    for name in files:
        found |= set(re.findall(pattern, (SRC / name).read_text(encoding="utf-8")))
    return found


# --- Кожен код з коду має підказку ---


def test_every_shafa_stop_code_has_a_hint():
    codes = literals(r'Stop\(\s*"(\w+)"', "robot/publish.py", "robot/rehearse.py")
    codes |= set(control.LAST) | {"photos", "slow", "crash"}
    assert codes - {"shafa_balance:"} <= set(hints.STOPS), codes - set(hints.STOPS)
    assert {"login", "shafa_balance", "telegram_busy", "no_form", "limit", "stop"} <= codes


def test_every_klubok_stop_code_has_a_hint():
    codes = set(klubok_publish.STOPS) | set(klubok_publish.LOUD) | set(klubok_control.LAST)
    codes |= {klubok_queue.EMPTY, klubok_queue.LIMIT_REACHED, snapshot.TOO_MANY}
    assert codes <= set(hints.KLUBOK_STOPS), codes - set(hints.KLUBOK_STOPS)


def test_every_shafa_problem_code_has_a_hint():
    from shafa import scan

    codes = {*scan.STICKY, *publish.ARCHIVABLE, *review.NO_RESTORE, listings.DECLINED_CODE}
    codes |= {*fill.REFILL_PROBLEMS, *fill.RECHECK_PROBLEMS, *web.FOLDER_CODES, *web.LINK_CODES}
    codes |= literals(r"problem_code = '(\w+)'", "fill.py", "scan.py", "review.py", "batch.py")
    codes |= literals(r'"(robot_\w+)"', "robot/publish.py", "robot/rehearse.py")
    codes |= {"post_deleted", "duplicate", "possible_duplicate", "publish_error", "robot_slow"}
    codes -= {"photos_changed", "post_changed", "season_changed", "duplicate_checked"}
    missing = {code for code in codes if hints.problem(code) is None}
    assert not missing, missing
    assert {code for code in codes if code not in hints.PROBLEMS} <= {
        c for c in codes if c.startswith("robot_")
    }, "кожен код Шафи — окремий запис (`robot_…` без запису — загальний «Робот»)"


def test_every_klubok_problem_code_has_a_hint():
    codes = {klubok_queue.DUPLICATE, klubok_queue.UNCERTAIN, *klubok_queue.BEFORE_CLICK}
    codes |= {klubok_publish.CAPTCHA, klubok_publish.NOT_SENT}
    missing = {code for code in codes if hints.problem(code, "klubok") is None}
    assert not missing, missing


def test_events_with_hints_point_to_real_entries():
    for name, kind in events.KINDS.items():
        assert not kind.hint or kind.hint in hints.EVENTS, name
    for name in ("shafa.login", "klubok.login", "klubok.renew_failed", "klubok.sold_failed"):
        assert events.KINDS[name].hint in hints.EVENTS, name
    assert {"ai_budget", "ai_budget_out", "ai_key", "klubok_sold_asked"} <= set(hints.EVENTS)


def test_steps_name_only_buttons_and_places_that_exist():
    """«…» у кроках — кнопка чи сторінка, яку людина справді бачить у програмі."""
    ui = "\n".join(p.read_text(encoding="utf-8") for p in (SRC / "templates").glob("*.html"))
    ui += (SRC / "web.py").read_text(encoding="utf-8")
    ui += (SRC / "klubok" / "screen.py").read_text(encoding="utf-8")
    ui += (SRC / "robot" / "control.py").read_text(encoding="utf-8")
    ui += (SRC / "robot" / "klubok_control.py").read_text(encoding="utf-8")
    outside = {"Мій баланс", "Мої оголошення", "Обновить все", "Обновить"}  # сторінки сайтів
    tables = [hints.STOPS, hints.KLUBOK_STOPS, hints.PROBLEMS, hints.KLUBOK_PROBLEMS, hints.EVENTS]
    bad = []
    for table in tables:
        for code, hint in table.items():
            for name in re.findall(r"«([^»]+)»", " ".join(hint.do)):
                if name not in outside and name not in ui:
                    bad.append((code, name))
    assert not bad, bad


def test_steps_are_short():
    for table in (hints.STOPS, hints.KLUBOK_STOPS, hints.PROBLEMS, hints.KLUBOK_PROBLEMS):
        for code, hint in table.items():
            assert hint.title and hint.what and len(hint.do) <= 4, code


# --- Екран і бот — одні слова ---


def test_screen_and_bot_use_the_same_words():
    login = hints.STOPS["login"]
    assert control.LAST["login"] == hints.inline(login)
    assert web.PROBLEM_HINTS["possible_duplicate"] == hints.card_text(
        hints.PROBLEMS["possible_duplicate"]
    )
    for code, hint in hints.PROBLEMS.items():
        assert web.PROBLEM_KINDS[code] == (hint.title, "; ".join(hint.do))
        assert report.problem_name(code) == hint.title
    assert klubok_control.LAST["klubok_captcha"] == hints.inline(
        hints.KLUBOK_STOPS["klubok_captcha"]
    ).replace("Клубок не", "клубок не", 1).replace("Chrome", "Chrome")


def test_login_stop_in_bot_says_what_to_press(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    session(conn, "login", published=3, problems=1)
    step(clock, bot)
    text, _summary = sent(tg)
    assert text.startswith("⛔ Робот зупинився: Робот вийшов із Шафи.")
    for button in ("«Увійти на Шафу»", "закрийте це вікно", "«Старт»"):
        assert button in text
    assert "Що робити: 1) у програмі натисніть «Увійти на Шафу»; 2)" in text
    assert text == sessions.stop_text(
        conn.execute("SELECT * FROM sessions").fetchone(), owner=True
    ), "те саме повідомлення будує `sessions.stop_text`"


def test_calm_stop_has_no_advice(conn, data, tg, make_bot):
    row = conn.execute("SELECT 1").fetchone()
    assert row
    session(conn, "limit", published=30)
    stop = conn.execute("SELECT * FROM sessions").fetchone()
    assert sessions.advice_of(stop) == ""


def test_new_problem_in_bot_has_advice(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "7440", "possible_duplicate", "Схоже: https://shafa.ua/uk/my/1")
    step(clock, bot)
    (text,) = sent(tg)
    assert text.startswith("⚠️ Пост 7440: Схожий товар уже є на Шафі")
    assert "Що робити: 1) відкрийте Картку в «Проблемах» і порівняйте фото" in text
    assert "«Це інший товар»" in text and "«В архів»" in text
    assert text.endswith(".") and "\n" in text


def test_several_new_problems_get_one_advice_per_code(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    for name in ("301", "302"):
        problem(conn, name, "no_price")
    problem(conn, "303", "robot_photos")
    step(clock, bot)
    (text,) = sent(tg)
    assert text.count("→ Нема ціни:") == 1 and text.count("→ Фото не завантажились:") == 1
    assert "Що робити:" in text


def test_problem_without_steps_adds_nothing(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    problem(conn, "401", "some_unknown_code")
    step(clock, bot)
    (text,) = sent(tg)
    assert "Що робити" not in text


def test_session_summary_has_advice_block(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    sid = session(conn, "done", published=1, problems=1)
    problem(conn, "501", "no_price")
    with conn:
        conn.execute("UPDATE items SET session_id = ? WHERE folder_name = '501'", (sid,))
    step(clock, bot)
    (summary,) = [t for t in sent(tg) if "Проблеми (1)" in t]
    assert "→ Нема ціни. Що робити: 1) допишіть ціну в пост" in summary


def test_what_button_shows_hint(conn):
    assert hints.advice(hints.STOPS["telegram_busy"]) == "Що робити: спробуйте «Старт» за годину."
    assert actions.advice_text([]) == ""


def test_loud_journal_event_gets_advice(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    put(data, clock.now, "shafa.login", "Вхід на Шафу злетів", loud=True)
    put(data, clock.now, "klubok.sold_failed", "Не підтвердилось", loud=True)
    put(data, clock.now, "ai.done", "тиха подія без поради")
    step(clock, bot)
    login, sold = sent(tg)[:2]
    assert "Що робити: 1) у програмі натисніть «Увійти на Шафу»" in login
    assert "Що робити: 1) відкрийте оголошення на Клубку" in sold


# --- ШІ ---


def many(conn, root, count):
    for n in range(count):
        add_item(conn, root, str(9000 + n), 9000 + n)


def test_ai_start_text_counts_new_and_old():
    from types import SimpleNamespace

    def card(name):
        return SimpleNamespace(folder_name=name, estimate=SimpleNamespace(cost_micro_usd=51_666))

    planned = [card(f"borh-{n}") for n in range(10)] + [card(str(n)) for n in range(20)]
    assert fill.start_text(planned, " · зазвичай до години") == (
        "ШІ почав аналіз: 30 Карток (нових 20, старих 10) · оцінка ≈ 1,55 $ · зазвичай до години"
    )


def test_ai_start_and_done_texts(conn, root, data):
    many(conn, root, 5)
    rows = fill.pick_items(conn, None, 5)
    planned = [fill.plan(conn, root, r) for r in rows]
    old = 0
    client = FakeClient(
        ai_message({**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}, usage=(9000, 500))
    )
    results = fill.fill_all(conn, client, planned)
    start, done = journal(data)
    assert start["kind"] == "ai.start" and not start["loud"]
    assert re.fullmatch(
        rf"ШІ почав аналіз: 5 Карток \(нових {5 - old}, старих {old}\) · оцінка ≈ \d+(,\d+)? \$",
        start["text"],
    ), start["text"]
    assert done["kind"] == "ai.done" and done["loud"] is True
    first, _, tail = done["text"].partition("\n")
    assert re.fullmatch(
        r"ШІ закінчив: 5 Карток — .+ · витрачено \d+,\d\d \$ · сьогодні \d+,\d\d \$ з 3 \$", first
    ), first
    assert tail == "Відкрийте «На перевірку»."
    yellow = sum(1 for r in results if r.yellow)
    assert (f"{yellow} з жовтим (перевірте жовті поля)" in first) == bool(yellow)


def test_single_card_ai_result_stays_quiet(conn, root, data):
    many(conn, root, 1)
    planned = planned_for(conn, root)
    fill.fill_all(conn, FakeClient(ai_message(GOOD, usage=(9000, 500))), [planned])
    (done,) = journal(data)
    assert done["kind"] == "ai.done" and not done["loud"]
    assert done["text"].startswith("ШІ: 9000 ")


def test_tally_text_numbers(conn):
    tally = fill.Tally()
    for n in range(15):
        tally.card(f"1{n}", cost=22_000)
    for n in range(15):
        tally.card(f"2{n}", yellow=True, cost=0)
    text = fill.tally_text(conn, tally)
    assert text.startswith("ШІ закінчив: 30 Карток — 15 готові, 15 з жовтим (перевірте жовті поля)")
    assert " · витрачено 0,33 $ · сьогодні 0,00 $ з 3 $" in text or "витрачено 0,33 $" in text
    assert text.endswith("\nВідкрийте «На перевірку».")


def test_tally_with_problems_only_has_no_review_hint(conn):
    tally = fill.Tally()
    tally.card("1", problem=True)
    tally.card("2", problem=True)
    text = fill.tally_text(conn, tally)
    assert "2 у Проблемах (відкрийте «Проблеми»)" in text and "«На перевірку»" not in text


def test_ai_stop_for_key_and_budget_get_advice(conn, data, tg, make_bot):
    clock, bot = started(conn, data, tg, make_bot)
    put(data, clock.now, "ai.stop", f"ШІ не відповів (3 Картки): {ai.KEY_TEXT} — скажіть Власнику",
        extra={"hint": "ai_key"})  # fmt: skip
    put(data, clock.now, "ai.stop", "ШІ не взявся за нові Картки: ліміт вичерпано",
        extra={"hint": "ai_budget_out"})  # fmt: skip
    step(clock, bot)
    key, budget = sent(tg)[:2]
    assert "Що робити: скажіть Власнику — ключ треба замінити" in key
    assert "Що робити: 1) нічого не треба — нові Картки підуть на ШІ завтра" in budget


def test_budget_warning_has_advice(conn):
    assert "Що робити:" in hints.advice(hints.EVENTS["ai_budget"])
    assert batch.dollars(1_000_000) == "1,00 $"


def test_duplicate_buttons_are_named_as_in_the_hint(conn, root):
    """Кнопки Власника в боті = назви з кроків підказки `possible_duplicate` (і з екрана)."""
    steps_names = re.findall(r"«([^»]+)»", " ".join(hints.PROBLEMS["possible_duplicate"].do))
    row = {"id": 1, "problem_code": "possible_duplicate", "shafa_id": None, "shafa_url": None}
    names = [b["text"] for line in actions.buttons(row) for b in line]
    assert names == ["Це інший товар", "В архів"]
    assert set(names) <= set(steps_names)
