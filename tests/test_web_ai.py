"""Екран: панель ШІ («Оновити», лічильники, збір результатів) і «ШІ, спробуй ще раз» (CORE-004)."""

import json
from datetime import UTC, datetime, timedelta

import anthropic
import httpx2
import pytest

from shafa import batch, web
from shafa.db import Status
from test_ai import GOOD, message
from test_batch import TODAY, line
from test_batch import FakeClient as BatchClient
from test_review import add_card, edits, status
from test_web import PORT, TOKEN, get, html, location, post


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Client(BatchClient):
    """Пакети (з test_batch) і звичайний запит `messages.create`."""

    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def client():
    return Client(message({**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(conn, tmp_path, root, client, clock):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        ai_client=lambda: client, run=lambda job: job(), clock=clock, today=lambda: TODAY,
    )  # fmt: skip


def no_key():
    from shafa import config

    return config.api_key()


def new_card(conn, root, folder="1342", post_id=1342, date="2025-03-01T10:00:00"):
    item_id = add_card(conn, root, folder=folder, post_id=post_id, status=Status.NEW, date=date)
    with conn:
        conn.execute("DELETE FROM blanks WHERE item_id = ?", (item_id,))
    return item_id


def update(app):
    return post(app, "/ai", action="update", back="/review")


def sent_hm(conn, today=TODAY):
    """Година відправки пакета так, як її бачить панель."""
    stamp = conn.execute("SELECT min(created_at) FROM ai_batches").fetchone()[0]
    return datetime.fromisoformat(stamp).astimezone(today.tzinfo).strftime("%H:%M")


def new_app(app, tmp_path, root, client, clock):
    """Той самий екран після перезапуску програми: пам'ять нова, база та сама."""
    return web.App(
        app.db_path, root, tmp_path / "thumbs", PORT, token=TOKEN,
        ai_client=lambda: client, run=lambda job: job(), clock=clock, today=lambda: TODAY,
    )  # fmt: skip


# --- Панель ---


def test_panel_counts(conn, app, root):
    new_card(conn, root)
    new_card(conn, root, folder="1349", post_id=1349, date="2026-10-12T10:00:00")
    page = html(get(app, "/review"))
    assert (
        "Готові йти на ШІ: 1" in page
        and "Ще не заповнені ШІ: 2" in page
        and "ШІ зараз заповнює" not in page
    )  # нулі не показуємо
    assert "через 7 днів після публікації в каналі (зараз таких: 1)" in page
    assert 'action="/ai"' in page and ">Знайти нові пости і відправити на ШІ</button>" in page
    assert 'name="action" value="update"' in page  # форма та сама, змінився лише підпис
    assert "Готові йти на ШІ: 1" in html(get(app, "/robot/panel?back=/review"))


def test_update_scans_sends_batch_and_reports(conn, app, root, client):
    new_card(conn, root)
    (root / "1349").mkdir()  # нова папка: пост уже є в базі з минулого Експорту
    (root / "1349" / "IMG_1.JPG").write_bytes((root / "1342" / "IMG_1.JPG").read_bytes())
    with conn:
        conn.execute(
            "INSERT INTO posts VALUES (1349, '2025-03-02T10:00:00', 'Кеди Ecco 38. 900грн', "
            "'export', 'x')"
        )

    assert location(update(app)) == "/review?msg=ai_started"

    ((_, requests),) = client.batches.created
    assert len(requests) == 2
    assert app.ai_note is None  # «що пішло» панель бере з бази, а не з примітки
    page = html(get(app, "/review"))
    assert "ШІ зараз заповнює: 2" in page
    assert f"ШІ заповнює 2 Картки (відправлено о {sent_hm(conn)})" in page
    assert "натискати нічого не треба" in page
    assert "Тримайте вікно програми відкритим" in page
    assert "ШІ працює… можна натиснути, щоб додати нові пости" in page
    assert app.ai_busy is None and not app.ai_lock.locked()


def test_update_while_busy_does_nothing(conn, app, root, client):
    new_card(conn, root)
    app.ai_lock.acquire()

    assert location(update(app)) == "/review?msg=ai_busy"
    assert client.batches.calls == []


def test_update_without_key_explains(conn, app, root):
    new_card(conn, root)
    app.ai_client = no_key

    update(app)

    assert app.ai_note == "ШІ не підключений — скажіть Власнику"
    assert not app.ai_lock.locked()


def test_results_collected_on_open_and_every_few_minutes(conn, app, root, client, clock):
    item_id = new_card(conn, root)
    update(app)
    calls = len(client.batches.calls)

    get(app, "/review")  # щойно перевіряли — не питаємо
    assert len(client.batches.calls) == calls

    clock.now += web.COLLECT_EVERY
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b1-i{item_id}")]
    page = html(get(app, "/review"))

    assert status(conn, item_id) == Status.REVIEW
    assert "(відправлено о" not in page and "Тримайте вікно" not in page
    assert "Готові до перевірки: 1" in page and 'href="/review"' in page
    assert "Готово о " in page and "1 Картка у «На перевірку»" in page
    clock.now += web.COLLECT_EVERY
    calls = len(client.batches.calls)
    get(app, "/review")  # незібраних пакетів немає — API не питаємо
    assert len(client.batches.calls) == calls


# --- UX-001: панель зрозумілими словами ---


def test_batch_note_survives_restart_and_disappears_when_collected(
    conn, app, root, client, clock, tmp_path
):
    item_id = new_card(conn, root)
    update(app)

    restarted = new_app(app, tmp_path, root, client, clock)
    page = html(get(restarted, "/review"))
    assert restarted.ai_note is None  # у пам'яті порожньо, а пакет видно з бази
    assert f"ШІ заповнює 1 Картка (відправлено о {sent_hm(conn)})" in page
    assert "Зазвичай готово до " in page

    clock.now += web.COLLECT_EVERY
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b1-i{item_id}")]
    page = html(get(restarted, "/review"))
    assert "(відправлено о" not in page and "ШІ зараз заповнює" not in page
    assert "Готово о " in page

    again = new_app(app, tmp_path, root, client, clock)
    assert "Готово о " in html(get(again, "/review"))


def test_checking_line_shows_last_check_time(conn, app, root, client):
    new_card(conn, root)
    update(app)
    page = html(get(app, "/review"))
    assert "Перевіряю, чи готово, кожні 3 хв · востаннє о 12:00 — ще не готово." in page


def test_old_finished_batch_is_not_shown_forever(conn, app, root, client, clock):
    item_id = new_card(conn, root)
    update(app)
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b1-i{item_id}")]
    clock.now += web.COLLECT_EVERY
    get(app, "/review")
    with conn:
        conn.execute("UPDATE ai_batches SET ended_at = '2026-10-01T10:00:00+00:00'")
    assert "Готово о " not in html(get(app, "/review"))


def test_note_is_replaced_not_stuck(conn, app, root, client):
    update(app)  # нічого відправляти
    first = html(get(app, "/review"))
    assert "12:00 · Немає нових товарів для ШІ" in first
    new_card(conn, root)
    update(app)
    assert app.ai_note is None
    assert "Немає нових товарів" not in html(get(app, "/review"))


def test_panel_without_anything_says_so(app):
    page = html(get(app, "/review"))
    assert "Зараз ШІ нічого не чекає й нічого не заповнює" in page
    assert (
        "Готові йти на ШІ" not in page
        and "Ще не заповнені ШІ" not in page
        and "Готово о" not in page
    )


def test_dot_is_calm_while_ai_works_and_warns_on_real_errors(conn, app, root, client):
    new_card(conn, root)
    update(app)
    context = app._panel_context(conn)
    assert context["ai"].pending == 1 and context["tones"]["ai"] == "ok"  # звичайна робота

    app.ai_client = no_key
    update(app)
    assert app.ai_note_error
    assert app._panel_context(conn)["tones"]["ai"] == "warn"
    assert "ШІ не підключений" in html(get(app, "/review"))


def test_info_note_does_not_turn_the_dot_yellow(conn, app):
    update(app)  # «Немає нових товарів для ШІ» — не помилка
    assert app.ai_note and not app.ai_note_error
    assert app._panel_context(conn)["tones"]["ai"] == "ok"


def test_no_cents_in_panel(conn, app, root, client):
    from pathlib import Path

    new_card(conn, root)
    update(app)
    page = html(get(app, "/review"))
    assert "¢" not in page and "≈ " not in page.split("Детальніше")[0].split("tile ai")[-1]
    assert "$" in page  # сума — лише під «Детальніше», у доларах
    for name in ("panel.html", "panel_mini.html"):
        assert "¢" not in (Path(web.__file__).parent / "templates" / name).read_text("utf-8")
    sent = batch.Sent(batch.Plan(planned=[]), batch_id=None)
    assert "¢" not in batch.sent_text(sent, batch.Limits(), panel=True)
    assert batch.approx_usd(1_360_000) == "≈ 1,36 $"


def test_held_note_hidden_when_everything_fits(conn, root, tmp_path, app, monkeypatch):
    from test_batch import old_cards

    monkeypatch.setenv("SHAFA_DAILY_OLD", "1")  # наперед — 3 Картки
    old_cards(conn, root, 3, tmp_path)
    assert "чекають своєї черги" not in html(get(app, "/review"))  # усі влізуть у пакет


def test_held_text_counts_old_beyond_ahead_days(conn, root, tmp_path, app, monkeypatch):
    from test_batch import old_cards

    monkeypatch.setenv("SHAFA_DAILY_OLD", "1")
    old_cards(conn, root, 5, tmp_path)
    page = html(get(app, "/review"))
    assert "Готові йти на ШІ: 5" in page
    assert "2 чекають своєї черги: ШІ готує Картки лише на 3 дні роботи робота наперед" in page
    assert "наступним пакетом" not in page


def test_sent_text_does_not_promise_next_batch_for_held_cards(conn, root, tmp_path, monkeypatch):
    from test_batch import old_cards

    old_cards(conn, root, 5, tmp_path)
    lim = batch.Limits(old_per_day=1)
    plan = batch.prepare(conn, root, TODAY, lim)
    assert (len(plan.planned), plan.held) == (3, 2)
    text = batch.sent_text(batch.Sent(plan, batch_id=1), lim, panel=True)
    assert text == "2 чекають своєї черги: ШІ готує Картки лише на 3 дні роботи робота наперед"
    assert "наступним пакетом" not in text


def test_new_bot_posts_hint_needs_the_button(conn, app):
    with conn:
        conn.execute("INSERT INTO posts VALUES (7001, '2026-10-30T10:00:00', 'Пост', 'bot', 'x')")
    page = html(get(app, "/review"))
    assert "Нових постів з бота: 1 — натисніть «Знайти нові пости і відправити на ШІ»" in page


def test_uncertain_and_busy_wording(app, conn, root):
    assert (
        batch.UNCERTAIN_TEXT
        == "ШІ не відповів вчасно — перевірю сам пізніше, нічого робити не треба"
    )
    assert batch.days_word(1) == "1 день" and batch.days_word(3) == "3 дні"
    assert batch.days_word(7) == "7 днів" and batch.days_word(11) == "11 днів"
    new_card(conn, root)
    app.ai_lock.acquire()
    app.ai_busy = "шукаю нові пости й відправляю…"
    page = html(get(app, "/review"))
    assert "ШІ: шукаю нові пости й відправляю…" in page
    assert 'name="action" value="update" disabled' in page


def test_rule_buttons_have_plain_words(conn, root, app):
    from test_core009 import stale_card

    stale_card(conn, root)
    page = html(get(app, "/review"))
    assert "Оновити Картки за новими правилами (1, безкоштовно)" in page
    assert "Програму оновили — натисніть, щоб Картки врахували зміни." in page
    assert 'name="action" value="recheck"' in page


def test_failing_job_frees_the_lock(conn, app, root):
    def broken():
        raise RuntimeError("зламалось")

    new_card(conn, root)
    app.ai_client = broken
    update(app)
    assert app.ai_note == "ШІ: щось пішло не так — скажіть Власнику"
    assert not app.ai_lock.locked()


def test_unknown_ai_action(app):
    assert post(app, "/ai", action="delete").status == 400


def test_ai_post_needs_token(conn, app, root, client):
    new_card(conn, root)
    assert post(app, "/ai", token=None, action="update").status == 403
    assert client.batches.calls == []


# --- «ШІ, спробуй ще раз» ---


@pytest.fixture
def review_card(conn, root):
    item_id = add_card(conn, root)
    with conn:
        conn.execute("INSERT INTO edits VALUES (?, 'brand', '\"Ecco\"', 'x')", (item_id,))
    return item_id


def retry(app, item_id):
    return post(app, f"/item/{item_id}", action="ai_retry")


def test_retry_button_only_for_review(conn, app, root, review_card):
    assert 'value="ai_retry"' in html(get(app, f"/item/{review_card}"))
    approved = add_card(conn, root, folder="1349", post_id=1349, status=Status.APPROVED)
    assert 'value="ai_retry"' not in html(get(app, f"/item/{approved}"))


def test_retry_refills_blank_keeps_edits(conn, app, review_card, client):
    response = retry(app, review_card)

    assert location(response) == f"/item/{review_card}?msg=ai_retry_done"
    (request,) = client.requests
    assert isinstance(request["system"], str)  # без кешу (D2)
    assert edits(conn, review_card) == {"brand": "Ecco"}
    title = conn.execute(
        "SELECT value FROM blanks WHERE item_id = ? AND field = 'title'", (review_card,)
    ).fetchone()[0]
    assert json.loads(title) == GOOD["title"]
    assert status(conn, review_card) == Status.REVIEW
    assert "ваші правки збережено" in html(get(app, f"/item/{review_card}?msg=ai_retry_done"))


def test_retry_error_keeps_card_and_explains(conn, app, review_card, client):
    client.reply = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x.test"))

    assert location(retry(app, review_card)).endswith("msg=ai_retry_failed")
    assert app.ai_note == "ШІ не заповнив Картку 1342: Немає зв'язку з ШІ — перевірте інтернет"
    assert status(conn, review_card) == Status.REVIEW


def test_retry_over_money_limit_sends_nothing(conn, app, review_card, client, monkeypatch):
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "0.01")

    assert location(retry(app, review_card)).endswith("msg=ai_budget")
    assert client.requests == []


def test_retry_counts_money_spent_today(conn, app, review_card, client):
    with conn:
        conn.execute(
            "INSERT INTO ai_requests (item_id, model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES (NULL, 'm', 0, 0, 2990000, ?)",
            (TODAY.astimezone(UTC).isoformat(timespec="seconds"),),
        )
    assert location(retry(app, review_card)).endswith("msg=ai_budget")
    with conn:  # учорашнє не рахується
        conn.execute(
            "UPDATE ai_requests SET created_at = ?",
            ((TODAY - timedelta(days=1)).astimezone(UTC).isoformat(timespec="seconds"),),
        )
    assert location(retry(app, review_card)).endswith("msg=ai_retry_done")


@pytest.mark.parametrize(
    ("setup", "message_code"),
    [
        (lambda app, conn, item: app.ai_lock.acquire(), "ai_busy"),
        (lambda app, conn, item: setattr(app, "ai_client", no_key), "ai_no_key"),
        (
            lambda app, conn, item: conn.execute(
                "UPDATE items SET status = 'approved' WHERE id = ?", (item,)
            ),
            "stale",
        ),
    ],
)
def test_retry_refused(conn, app, review_card, client, setup, message_code):
    with conn:
        setup(app, conn, review_card)

    assert location(retry(app, review_card)).endswith(f"msg={message_code}")
    assert client.requests == []


def test_retry_with_broken_photo(conn, app, root, review_card, client):
    (root / "1342" / "IMG_1.JPG").write_bytes(b"not a jpeg")
    assert location(retry(app, review_card)).endswith("msg=ai_photo")
    assert client.requests == []
    assert not app.ai_lock.locked()


def test_real_time_default_is_local(tmp_path, root):
    app = web.App(tmp_path / "б.sqlite", root, tmp_path / "t", PORT, token=TOKEN)
    assert app.today().tzinfo is not None
    assert abs(app.today() - datetime.now(UTC)) < timedelta(minutes=1)


def test_no_open_batches_no_background_work(conn, app, root, clock):
    """Без незібраних пакетів сторінки не запускають збору (і не займають замок ШІ)."""
    made = []
    app.ai_client = lambda: made.append(1)
    new_card(conn, root)
    clock.now += 10 * web.COLLECT_EVERY

    get(app, "/review")
    get(app, "/robot/panel?back=/review")

    assert made == [] and app.collected_at is None
