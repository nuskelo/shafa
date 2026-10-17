"""Екран: панель ШІ («Оновити», лічильники, збір результатів) і «ШІ, спробуй ще раз» (CORE-004)."""

import json
from datetime import UTC, datetime, timedelta

import anthropic
import httpx2
import pytest

from shafa import web
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


# --- Панель ---


def test_panel_counts(conn, app, root):
    new_card(conn, root)
    new_card(conn, root, folder="1349", post_id=1349, date="2026-10-12T10:00:00")
    page = html(get(app, "/review"))
    assert "Готові до ШІ 1 · Чекають відповіді ШІ 0 · Нові чекають 7 днів після посту 1" in page
    assert 'action="/ai"' in page and ">Оновити</button>" in page
    assert "Готові до ШІ 1" in html(get(app, "/robot/panel?back=/review"))


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
    assert app.ai_note.startswith("Відправлено на ШІ 2 товарів, результати зазвичай до години")
    page = html(get(app, "/review"))
    assert "Чекають відповіді ШІ 2" in page
    assert app.ai_note in page
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

    assert app.ai_note == "Немає ключа ШІ (ANTHROPIC_API_KEY) — скажіть Власнику"
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
    assert "ШІ заповнило бланків: 1" in page
    clock.now += web.COLLECT_EVERY
    calls = len(client.batches.calls)
    get(app, "/review")  # незібраних пакетів немає — API не питаємо
    assert len(client.batches.calls) == calls


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
    assert app.ai_note == "«ШІ, спробуй ще раз» (1342): Немає з'єднання з Claude API"
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
