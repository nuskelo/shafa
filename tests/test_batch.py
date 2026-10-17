import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from shafa import ai, batch, fill
from shafa.db import Status
from test_ai import GOOD, message
from test_fill import add_item, item

KYIV = timezone(timedelta(hours=3))
TODAY = datetime(2026, 10, 13, 12, 0, tzinfo=KYIV)
LIMITS = batch.Limits()
_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/batches")


class FakeBatches:
    """Підставний Batch API: пакети в пам'яті, результати задає тест."""

    def __init__(self):
        self.created = []  # (id, requests)
        self.status = {}
        self.lines = {}
        self.listed = []  # для list(): найновіші першими
        self.create_error = None
        self.calls = []

    def create(self, requests):
        self.calls.append("create")
        if self.create_error:
            raise self.create_error
        batch_id = f"msgbatch_{len(self.created) + 1}"
        self.created.append((batch_id, requests))
        self.status[batch_id] = "in_progress"
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id):
        self.calls.append("retrieve")
        return SimpleNamespace(id=batch_id, processing_status=self.status[batch_id])

    def results(self, batch_id):
        self.calls.append("results")
        return iter(self.lines.get(batch_id, []))

    def list(self, limit):
        self.calls.append("list")
        return iter(self.listed)


class FakeClient:
    def __init__(self):
        self.batches = FakeBatches()
        self.messages = self
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self


def ok_answer():
    return {**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}


def line(cid, kind="succeeded", reply=None):
    result = SimpleNamespace(type=kind)
    if kind == "succeeded":
        result.message = reply or message(ok_answer(), usage=(9500, 370), cache=(0, 8800))
    if kind == "errored":
        result.error = SimpleNamespace(error=SimpleNamespace(type="invalid_request_error"))
    return SimpleNamespace(custom_id=cid, result=result)


def cards(conn, root, count):
    return [add_item(conn, root, folder=str(n), post_id=n) for n in range(1, count + 1)]


def set_post_date(conn, post_id, date):
    with conn:
        conn.execute("UPDATE posts SET date = ? WHERE post_id = ?", (date, post_id))


def states(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT item_id, state FROM ai_batch_items")}


def sent_requests(client):
    return [r for _, requests in client.batches.created for r in requests]


# --- Правило 7 днів (D4) ---


@pytest.mark.parametrize(
    ("date", "old"),
    [
        ("2026-10-06T12:00:00", True),  # рівно 7 днів
        ("2026-10-06T11:59:59", True),
        ("2026-10-06T12:00:01", False),  # на секунду молодший
        ("2026-10-10T09:00:00", False),
        ("2026-10-06T09:00:00+00:00", True),  # з поясом: 12:00 за Києвом
        ("2026-10-06T09:00:01+00:00", False),
        ("не дата", False),
    ],
)
def test_post_is_old_at_the_edge(date, old):
    assert batch.post_is_old(date, TODAY, 7) is old


def test_wait_days_is_a_setting(monkeypatch):
    assert batch.post_is_old("2026-10-12T12:00:00", TODAY, 1)
    monkeypatch.setenv("SHAFA_AI_WAIT_DAYS", "1")
    assert batch.limits().wait_days == 1


def test_queue_only_old_new_cards_not_in_batch(conn, root):
    old, _young, waiting, reviewed = cards(conn, root, 4)
    set_post_date(conn, 2, "2026-10-10T10:00:00")
    with conn:
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (reviewed,))
        conn.execute(
            "INSERT INTO ai_batches (id, status, created_at) VALUES (9, 'in_progress', 'x')"
        )
        conn.execute("INSERT INTO ai_batch_items VALUES (9, ?, 1, 'pending')", (waiting,))

    q = batch.queue(conn, TODAY, 7)

    assert [r["id"] for r in q.ready] == [old]
    assert (q.young, q.pending) == (1, 1)


# --- Відправка ---


def test_send_creates_batch_with_cached_requests(conn, root):
    ids = cards(conn, root, 2)
    client = FakeClient()

    sent = batch.send(conn, client, root, TODAY, LIMITS)

    assert sent.count == 2 and not sent.error and not sent.uncertain
    requests = sent_requests(client)
    assert [r["custom_id"] for r in requests] == [f"b{sent.batch_id}-i{i}" for i in ids]
    assert all(batch._CUSTOM_ID.fullmatch(r["custom_id"]) for r in requests)
    assert all(r["params"]["system"][0]["cache_control"] == ai.CACHE_CONTROL for r in requests)
    assert client.options == [{"max_retries": 0}]  # повтор SDK міг би створити другий пакет
    row = conn.execute("SELECT * FROM ai_batches").fetchone()
    assert (row["anthropic_id"], row["status"]) == ("msgbatch_1", "in_progress")
    assert states(conn) == {ids[0]: "pending", ids[1]: "pending"}
    assert all(item(conn, i)["status"] == Status.NEW for i in ids)
    estimates = [r[0] for r in conn.execute("SELECT estimate_micro_usd FROM ai_batch_items")]
    assert estimates == [p.estimate.cost_micro_usd for p in sent.plan.planned]
    assert all(e > 0 for e in estimates)


def test_card_in_unfinished_batch_is_not_sent_again(conn, root):
    cards(conn, root, 2)
    client = FakeClient()
    batch.send(conn, client, root, TODAY, LIMITS)

    again = batch.send(conn, client, root, TODAY, LIMITS)

    assert again.count == 0 and again.batch_id is None
    assert len(client.batches.created) == 1
    assert batch.sent_text(again, LIMITS) == "Немає нових товарів для ШІ"


def test_batch_limit_caps_cards(conn, root):
    ids = cards(conn, root, 3)
    client = FakeClient()

    sent = batch.send(conn, client, root, TODAY, batch.Limits(batch_limit=2))

    assert sent.count == 2 and sent.plan.left == 1
    assert list(states(conn)) == ids[:2]
    assert "ще 1 — наступним пакетом" in batch.sent_text(sent, LIMITS)


def one_estimate(conn, root):
    (row,) = batch.queue(conn, TODAY, 7).ready[:1]
    return fill.plan(conn, root, row, batch=True).estimate.cost_micro_usd


def test_money_limit_counts_estimate_spent_today_and_waiting_batches(conn, root):
    ids = cards(conn, root, 3)
    cost = one_estimate(conn, root)
    with conn:  # сьогодні вже витрачено (факт) і є незабраний пакет (оцінка)
        conn.execute(
            "INSERT INTO ai_requests (item_id, model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES (NULL, 'm', 0, 0, 1000, ?)",
            (TODAY.astimezone(UTC).isoformat(timespec="seconds"),),
        )
        conn.execute(
            "INSERT INTO ai_requests (item_id, model, input_tokens, output_tokens, cost_micro_usd, "
            "created_at) VALUES (NULL, 'm', 0, 0, 999999, ?)",
            ((TODAY - timedelta(days=1)).astimezone(UTC).isoformat(timespec="seconds"),),
        )
        conn.execute(
            "INSERT INTO ai_batches (id, status, created_at) VALUES (9, 'in_progress', 'x')"
        )
        conn.execute("INSERT INTO ai_batch_items VALUES (9, ?, 2000, 'pending')", (ids[2],))
    assert batch.spent_today(conn, TODAY) == 3000
    client = FakeClient()

    sent = batch.send(conn, client, root, TODAY, batch.Limits(daily_micro_usd=3000 + cost))

    assert sent.count == 1 and sent.plan.over_budget
    assert "ліміт витрат на ШІ за добу" in batch.sent_text(sent, LIMITS)


def test_over_money_limit_sends_nothing(conn, root):
    cards(conn, root, 1)
    cost = one_estimate(conn, root)
    client = FakeClient()

    sent = batch.send(conn, client, root, TODAY, batch.Limits(daily_micro_usd=cost - 1))

    assert sent.count == 0 and sent.plan.over_budget
    assert client.batches.calls == []
    assert conn.execute("SELECT count(*) FROM ai_batches").fetchone()[0] == 0
    assert batch.sent_text(sent, LIMITS) == "ліміт витрат на ШІ за добу ($3,00) — решта завтра"


def test_broken_photo_goes_to_problems_not_to_batch(conn, root):
    first, second = cards(conn, root, 2)
    (root / "1" / "IMG_1.JPG").write_bytes(b"not a jpeg")

    sent = batch.send(conn, FakeClient(), root, TODAY, LIMITS)

    assert list(states(conn)) == [second]
    row = item(conn, first)
    assert (row["status"], row["problem_code"], row["problem_text"]) == (
        Status.PROBLEM, "ai_error", "Фото не відкривається: IMG_1.JPG"
    )  # fmt: skip
    assert sent.count == 1


def _status_error(cls, status):
    response = httpx2.Response(status, request=_REQUEST, json={"error": {}})
    return cls("x", response=response, body=None)


def test_rejected_batch_returns_cards_to_queue(conn, root):
    ids = cards(conn, root, 1)
    client = FakeClient()
    client.batches.create_error = _status_error(anthropic.BadRequestError, 400)

    sent = batch.send(conn, client, root, TODAY, LIMITS)

    assert sent.error == "Помилка Claude API (400)"
    assert states(conn) == {ids[0]: "returned"}
    assert conn.execute("SELECT status FROM ai_batches").fetchone()[0] == "failed"
    client.batches.create_error = None
    assert batch.send(conn, client, root, TODAY, LIMITS).count == 1


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=_REQUEST),
        anthropic.APIConnectionError(request=_REQUEST),
        _status_error(anthropic.InternalServerError, 500),
    ],
)
def test_no_answer_on_create_keeps_cards_out_of_queue(conn, root, error):
    """Пакет міг створитися — Картки вдруге не відправляються, поки не з'ясуємо."""
    ids = cards(conn, root, 1)
    client = FakeClient()
    client.batches.create_error = error

    sent = batch.send(conn, client, root, TODAY, LIMITS)

    assert sent.uncertain
    assert states(conn) == {ids[0]: "pending"}
    assert conn.execute("SELECT status FROM ai_batches").fetchone()[0] == "uncertain"
    client.batches.create_error = None
    assert batch.send(conn, client, root, TODAY, LIMITS).count == 0


# --- Збір ---


def sent_batch(conn, root, client, count=1):
    ids = cards(conn, root, count)
    sent = batch.send(conn, client, root, TODAY, LIMITS)
    return ids, sent.batch_id


def test_collect_waits_for_unfinished_batch(conn, root):
    client = FakeClient()
    ids, _ = sent_batch(conn, root, client)

    report = batch.collect(conn, client, TODAY)

    assert report.waiting == 1
    assert states(conn) == {ids[0]: "pending"}
    assert "results" not in client.batches.calls


def test_partly_failed_batch(conn, root):
    client = FakeClient()
    ids, bid = sent_batch(conn, root, client, 5)
    ok, bad, expired, canceled, missing = ids
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [
        line(f"b{bid}-i{ok}"),
        line(f"b{bid}-i{bad}", "errored"),
        line(f"b{bid}-i{expired}", "expired"),
        line(f"b{bid}-i{canceled}", "canceled"),
        line("чужий-id"),
    ]

    report = batch.collect(conn, client, TODAY)

    assert (report.filled, report.errors, report.returned) == (1, 1, 3)
    assert item(conn, ok)["status"] == Status.REVIEW
    assert conn.execute("SELECT count(*) FROM blanks WHERE item_id = ?", (ok,)).fetchone()[0]
    row = item(conn, bad)
    assert (row["status"], row["problem_code"]) == (Status.PROBLEM, "ai_error")
    assert row["problem_text"] == "Помилка Claude API в пакеті (invalid_request_error)"
    for i in (expired, canceled, missing):  # знову в черзі, без запису вартості
        assert item(conn, i)["status"] == Status.NEW
    assert states(conn) == {
        ok: "done",
        bad: "error",
        expired: "returned",
        canceled: "returned",
        missing: "returned",
    }
    costs = {
        r["item_id"]: r
        for r in conn.execute("SELECT * FROM ai_requests WHERE batch_id = ?", (bid,))
    }
    assert set(costs) == {ok, bad}
    assert (costs[ok]["cache_read_tokens"], costs[ok]["cost_micro_usd"]) == (
        8800, 9500 + 370 * 5 + 440
    )  # fmt: skip
    assert costs[bad]["cost_micro_usd"] == 0
    assert conn.execute("SELECT status FROM ai_batches").fetchone()[0] == "ended"
    assert {r["id"] for r in batch.queue(conn, TODAY, 7).ready} == {expired, canceled, missing}


def test_collected_answer_without_section_is_problem(conn, root):
    client = FakeClient()
    (only,), bid = sent_batch(conn, root, client, 1)
    client.batches.status["msgbatch_1"] = "ended"
    reply = message({**ok_answer(), "section": ""}, usage=(9500, 370))
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{only}", reply=reply)]

    batch.collect(conn, client, TODAY)

    row = item(conn, only)
    assert (row["status"], row["problem_code"]) == (Status.PROBLEM, "no_section")


def test_collect_twice_changes_nothing(conn, root):
    client = FakeClient()
    ids, bid = sent_batch(conn, root, client)
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{ids[0]}")]
    batch.collect(conn, client, TODAY)
    with conn:  # навіть якщо пакет знову «в роботі» (збій посеред збору)
        conn.execute("UPDATE ai_batches SET status = 'in_progress'")

    report = batch.collect(conn, client, TODAY)

    assert report.filled == 0
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 1


def test_bad_answer_in_batch_is_problem_with_cost(conn, root):
    client = FakeClient()
    ids, bid = sent_batch(conn, root, client)
    client.batches.status["msgbatch_1"] = "ended"
    reply = message('{"title": "Шк', stop_reason="max_tokens", usage=(9500, 4000))
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{ids[0]}", reply=reply)]

    batch.collect(conn, client, TODAY)

    assert item(conn, ids[0])["problem_text"] == "Відповідь ШІ обрізана"
    cost = conn.execute("SELECT cost_micro_usd FROM ai_requests").fetchone()[0]
    assert cost == 9500 + 4000 * 5


def test_card_changed_while_waiting_keeps_its_state(conn, root):
    client = FakeClient()
    ids, bid = sent_batch(conn, root, client)
    with conn:  # папку тим часом прибрали — scan переніс Картку в «Проблеми»
        conn.execute("UPDATE items SET status = 'problem', problem_code = 'folder_missing'")
    client.batches.status["msgbatch_1"] = "ended"
    client.batches.lines["msgbatch_1"] = [line(f"b{bid}-i{ids[0]}")]

    batch.collect(conn, client, TODAY)

    assert item(conn, ids[0])["problem_code"] == "folder_missing"
    assert conn.execute("SELECT count(*) FROM blanks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 1  # заплачено


def test_results_not_collected_for_29_days_are_problems(conn, root):
    client = FakeClient()
    ids, _ = sent_batch(conn, root, client)
    later = datetime.now(UTC) + timedelta(days=29, hours=1)  # created_at — справжній час

    report = batch.collect(conn, client, later)

    assert report.lost == 1
    row = item(conn, ids[0])
    assert (row["status"], row["problem_code"], row["problem_text"]) == (
        Status.PROBLEM, "ai_error", batch.LOST_TEXT
    )  # fmt: skip
    assert client.batches.calls == ["create"]
    assert batch.open_batches(conn) == 0


def test_api_error_while_collecting_tries_later(conn, root):
    client = FakeClient()
    ids, _ = sent_batch(conn, root, client)

    def broken(batch_id):
        raise anthropic.APIConnectionError(request=_REQUEST)

    client.batches.retrieve = broken

    report = batch.collect(conn, client, TODAY)

    assert report.note == "Немає з'єднання з Claude API"
    assert states(conn) == {ids[0]: "pending"}
    assert batch.open_batches(conn) == 1


def uncertain_batch(conn, root, client):
    client.batches.create_error = anthropic.APITimeoutError(request=_REQUEST)
    ids, bid = sent_batch(conn, root, client)
    client.batches.create_error = None
    return ids, bid


def test_uncertain_batch_found_by_custom_id(conn, root):
    client = FakeClient()
    ids, bid = uncertain_batch(conn, root, client)
    created = datetime.fromisoformat(
        conn.execute("SELECT created_at FROM ai_batches").fetchone()[0]
    )
    client.batches.listed = [
        SimpleNamespace(id="msgbatch_x", created_at=created, processing_status="ended"),
        SimpleNamespace(id="msgbatch_old", created_at=created - timedelta(days=2),
                        processing_status="ended"),
    ]  # fmt: skip
    client.batches.status["msgbatch_x"] = "ended"
    client.batches.lines["msgbatch_x"] = [line(f"b{bid}-i{ids[0]}")]

    report = batch.collect(conn, client, TODAY)

    assert report.filled == 1
    assert item(conn, ids[0])["status"] == Status.REVIEW
    row = conn.execute("SELECT anthropic_id, status FROM ai_batches").fetchone()
    assert tuple(row) == ("msgbatch_x", "ended")


def test_uncertain_batch_not_found_in_a_day_returns_cards(conn, root):
    client = FakeClient()
    ids, _ = uncertain_batch(conn, root, client)

    batch.collect(conn, client, datetime.now(UTC) + timedelta(hours=1))
    assert states(conn) == {ids[0]: "pending"}  # ще може обробитись

    batch.collect(conn, client, datetime.now(UTC) + timedelta(hours=26))
    assert states(conn) == {ids[0]: "returned"}
    assert conn.execute("SELECT status FROM ai_batches").fetchone()[0] == "failed"


# --- Налаштування й тексти ---


def test_limits_from_settings(monkeypatch):
    assert batch.limits() == batch.Limits(7, 50, 3_000_000)
    monkeypatch.setenv("SHAFA_AI_BATCH_LIMIT", "20")
    monkeypatch.setenv("SHAFA_AI_DAILY_USD", "1,5")
    assert batch.limits() == batch.Limits(7, 20, 1_500_000)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SHAFA_AI_DAILY_USD", "0"),
        ("SHAFA_AI_DAILY_USD", "nan"),
        ("SHAFA_AI_DAILY_USD", "багато"),
        ("SHAFA_AI_DAILY_USD", "1000"),
        ("SHAFA_AI_BATCH_LIMIT", "0"),
        ("SHAFA_AI_WAIT_DAYS", "-1"),
    ],
)
def test_bad_settings(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(batch.SetupError):
        batch.limits()


def test_texts():
    plan = batch.Plan()
    assert batch.sent_text(batch.Sent(plan, error="Помилка Claude API (400)"), LIMITS) == (
        "Пакет не відправлено: Помилка Claude API (400). Спробуйте пізніше"
    )
    assert batch.collected_text(batch.Collected()) is None
    report = batch.Collected(filled=3, errors=1, lost=1, returned=2)
    assert batch.collected_text(report) == (
        "ШІ заповнило бланків: 3; у «Проблеми»: 2; повернуто в чергу: 2"
    )
    assert json.dumps(batch.cents(12345), ensure_ascii=False) == '"1,23 ¢"'


# --- Старі за списком і лише наперед (CORE-006) ---


def old_cards(conn, root, count, tmp_path):
    """Старі Картки `borh-N`: фото в теці даних. Позиція у списку — навпаки до дати поста."""
    import shutil

    from shafa.folders import old_root

    ids = []
    for n in range(1, count + 1):
        item_id = add_item(conn, root, folder=f"borh-{n}", post_id=n, photos=("1.jpg",))
        target = old_root() / str(n)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(root / f"borh-{n}", target)
        with conn:
            conn.execute("UPDATE items SET queue_pos = ? WHERE id = ?", (count - n + 1, item_id))
            conn.execute(
                "UPDATE posts SET date = ? WHERE post_id = ?", (f"2024-01-{n:02d}T10:00:00", n)
            )
        ids.append(item_id)
    return ids


def test_old_cards_queue_by_list_after_new(conn, root, tmp_path):
    old = old_cards(conn, root, 3, tmp_path)
    (new,) = [add_item(conn, root, folder="77", post_id=77)]
    set_post_date(conn, 77, "2026-01-01T10:00:00")

    q = batch.queue(conn, TODAY, 7)

    assert [r["id"] for r in q.ready] == [new, *reversed(old)]


def test_old_cards_filled_at_most_3_days_ahead(conn, root, tmp_path):
    old = old_cards(conn, root, 8, tmp_path)
    lim = batch.Limits(old_per_day=2)  # 3 × 2 = 6 наперед
    with conn:  # одна вже з готовим бланком — місце займає
        conn.execute("UPDATE items SET status = 'review' WHERE id = ?", (old[0],))

    plan = batch.prepare(conn, root, TODAY, lim)

    assert [p.item_id for p in plan.planned] == [old[7], old[6], old[5], old[4], old[3]]


def test_new_cards_not_limited_by_old_room(conn, root, tmp_path):
    old_cards(conn, root, 3, tmp_path)
    new = add_item(conn, root, folder="77", post_id=77)
    set_post_date(conn, 77, "2026-01-01T10:00:00")

    plan = batch.prepare(conn, root, TODAY, batch.Limits(old_per_day=0))

    assert [p.item_id for p in plan.planned] == [new]  # старих на день 0 — наперед нічого


def test_waiting_old_cards_in_batch_count_ahead(conn, root, tmp_path):
    old = old_cards(conn, root, 5, tmp_path)
    with conn:
        conn.execute(
            "INSERT INTO ai_batches (id, status, created_at) VALUES (9, 'in_progress', 'x')"
        )
        conn.execute("INSERT INTO ai_batch_items VALUES (9, ?, 1, 'pending')", (old[0],))
        conn.execute("INSERT INTO ai_batch_items VALUES (9, ?, 1, 'pending')", (old[1],))

    plan = batch.prepare(
        conn, root, TODAY, batch.Limits(old_per_day=1)
    )  # наперед 3, 2 вже в дорозі

    assert len(plan.planned) == 1


def test_seven_days_only_for_new_posts(conn, root, tmp_path):
    """CORE-007 (рішення Власника 17.10): старий пост віком 2 дні — готовий; новий — чекає."""
    (old,) = old_cards(conn, root, 1, tmp_path)
    new = add_item(conn, root, folder="7", post_id=7)
    set_post_date(conn, 1, "2026-10-11T10:00:00")  # обидва — 2 дні тому
    set_post_date(conn, 7, "2026-10-11T10:00:00")

    q = batch.queue(conn, TODAY, 7)

    assert [r["id"] for r in q.ready] == [old]
    assert q.young == 1  # «Нові чекають 7 днів» — лише новий
    assert new not in [r["id"] for r in q.ready]
