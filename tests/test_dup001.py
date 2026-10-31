"""DUP-001: ШІ порівнює фото й текст схожого оголошення. Підставна Шафа, підставна відповідь ШІ —
жодного живого запиту (ні до Шафи, ні до Claude)."""

import io
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from PIL import Image

from shafa import ai, dup, review, web
from shafa.db import Status
from shafa.robot import lock, publish
from shafa.robot.listings import Listing
from test_robot_publish import (  # noqa: F401 — autouse-фікстури двигуна
    SAME,
    Env,
    new_cards_allowed,
    quiet,
    row,
    state,
)
from test_web import PORT, TOKEN, get, html, post

PHOTO = "https://images.shafa.ua/1.jpg"
FOUND = Listing(**{**SAME.__dict__, "photo": PHOTO})
URL = SAME.url


def jpeg(size=(1092, 819)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "gray").save(buffer, format="JPEG")
    return buffer.getvalue()


def reply(same="no", why="У знайденого рожева підошва.", usage=(3400, 90)):
    """Відповідь API так, як її повертає `messages.create`."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps({"same": same, "why": why}))],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=usage[0],
            output_tokens=usage[1],
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )


class Client:
    """Підставний клієнт Claude: віддає задане, пам'ятає запити."""

    def __init__(self, result):
        self.result, self.requests = result, []
        self.messages = self

    def with_options(self, **options):
        return self

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def env(conn, root, tmp_path):
    return Env(conn, root, tmp_path)


def run(env, client, listing=FOUND, budget=1_000_000, body=None, judge=True):
    """Сесія робота з підставним ШІ. Фото знайденого — справжній JPEG, якщо не задано `body`."""
    env.listings = [listing]
    body = jpeg() if body is None else body
    with lock.hold("publish", env.tmp / "robot.lock") as held:
        return publish.Run(
            env.conn, env.root, env.session, env.make_driver, env.load, env.tmp / "екрани", held,
            log=env.lines.append, sleep=lambda s: None, rand=lambda a, b: 0.0,
            today=lambda: env.today, fetch_photo=lambda url: body,
            photos_dir=env.tmp / "duplicates",
            judge=dup.Judge(env.conn, lambda: client, lambda: budget) if judge else None,
        ).start(30, 30)  # fmt: skip


def card_of(env, folder="1342", post_id=1342, **kw):
    from test_robot_publish import add

    return add(env, folder, post_id, **kw)


def spent(conn):
    return conn.execute(
        "SELECT count(*), coalesce(sum(cost_micro_usd), 0) FROM ai_requests"
    ).fetchone()


# --- Рішення ---


def test_no_means_other_pair_published_with_note(env):
    item = card_of(env)
    client = Client(reply("no", "У знайденого рожева підошва."))
    report = run(env, client)
    assert state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)
    r = row(env.conn, item)
    assert r["problem_text"] == f"Схоже: {URL} — ШІ: інша пара (У знайденого рожева підошва.)"
    assert (r["duplicate_url"], r["duplicate_name"]) == (URL, SAME.name)
    assert [p for p, _ in report.published] == [1342]
    assert any("ШІ: інша пара" in note for note in report.notes)
    assert len(client.requests) == 1 and len(env.driver.submits) == 1


def test_yes_archives_card_without_publishing_and_without_touching_daily_limit(env):
    item = card_of(env)
    client = Client(reply("yes", "Та сама подряпина на носку."))
    report = run(env, client)
    r = row(env.conn, item)
    assert (r["status"], r["problem_code"]) == (Status.ARCHIVED, "duplicate")
    assert r["problem_text"] == f"Уже є на Шафі: {URL} — Та сама подряпина на носку."
    assert r["archived_at"] and r["duplicate_url"] == URL
    assert env.driver.submits == [] and report.published == []
    assert publish.started_today(env.conn, env.today) == 0  # ліміт доби не зачеплено
    assert any("Уже є на Шафі" in note for note in report.notes)
    assert (env.tmp / "duplicates" / f"{item}.jpg").is_file()  # фото для екрана


def test_restore_from_archive_marks_other_item_and_next_run_does_not_ask_ai_again(env):
    item = card_of(env)
    run(env, Client(reply("yes", "Та сама.")))
    assert review.act(env.conn, item, "restore") == "restored"
    r = row(env.conn, item)
    assert (r["status"], r["problem_code"], r["problem_text"]) == (Status.REVIEW, None, None)
    assert r["duplicate_ok_at"]  # людина повернула — це інший товар
    review.act(env.conn, item, "approve")
    client = Client(reply("yes", "Та сама."))
    run(env, client)
    assert client.requests == []
    assert state(env.conn, item) == (Status.PUBLISHED, None)


def test_unsure_stays_possible_duplicate_for_human(env):
    item = card_of(env)
    report = run(env, Client(reply("unsure", "Фото нечіткі.")))
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    text = row(env.conn, item)["problem_text"]
    assert URL in text and f"«{SAME.name}»" in text and "ШІ не впевнений: Фото нечіткі." in text
    assert env.driver is None or env.driver.submits == []
    assert report.published == []


# --- Усе, що не рішення ШІ, — як було ---


def test_network_error_is_possible_duplicate_and_request_is_logged(env):
    item = card_of(env)
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    run(env, Client(error))
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert "ШІ не порівняв: Немає з'єднання з Claude API" in row(env.conn, item)["problem_text"]


def test_refusal_logs_tokens_that_were_paid_for(env):
    item = card_of(env)
    refused = reply()
    refused.stop_reason = "refusal"
    run(env, Client(refused))
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    count, cost = spent(env.conn)
    assert count == 1 and cost == ai.Usage(3400, 90).cost_micro_usd
    assert env.conn.execute("SELECT error FROM ai_requests").fetchone()[0]


def test_no_photo_of_found_listing_does_not_ask_ai(env):
    item = card_of(env)
    client = Client(reply("no"))
    run(env, client, listing=SAME)  # у знімку немає адреси фото
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert client.requests == []
    assert "ШІ не порівняв: немає фото" in row(env.conn, item)["problem_text"]


def test_photo_that_is_not_an_image_does_not_ask_ai(env):
    item = card_of(env)
    client = Client(reply("no"))
    run(env, client, body=b"not a jpeg")
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert client.requests == [] and "фото не читається" in row(env.conn, item)["problem_text"]


def test_exhausted_daily_budget_does_not_ask_ai(env):
    item = card_of(env)
    client = Client(reply("no"))
    run(env, client, budget=1_000)
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert client.requests == [] and "бюджет" in row(env.conn, item)["problem_text"]


@pytest.mark.parametrize("answer", ['{"same": "maybe", "why": "x"}', '{"same": "no"}', "не json"])
def test_answer_not_by_schema_is_possible_duplicate(env, answer):
    item = card_of(env)
    message = reply()
    message.content = [SimpleNamespace(type="text", text=answer)]
    run(env, Client(message))
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")


def test_without_judge_behaves_as_before(env):
    item = card_of(env)
    run(env, None, judge=False)
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    assert "ШІ" not in row(env.conn, item)["problem_text"]


def test_human_already_checked_card_is_not_sent_to_ai(env):
    item = card_of(env)
    with env.conn:
        env.conn.execute("UPDATE items SET duplicate_ok_at = 'x' WHERE id = ?", (item,))
    client = Client(reply("yes"))
    run(env, client)
    assert client.requests == [] and state(env.conn, item) == (Status.PUBLISHED, None)


def test_nothing_similar_is_not_sent_to_ai(env):
    item = card_of(env)
    client = Client(reply("yes"))
    run(env, client, listing=Listing(1, "Сумка", "Gucci", "sumki/sumki", (), "https://x/1"))
    assert client.requests == [] and state(env.conn, item) == (Status.PUBLISHED, None)


# --- Обережне правило, гроші, запит ---


def test_conservative_rule_no_with_same_colors_and_insole_becomes_unsure(env):
    item = card_of(env)
    twin = Listing(**{**FOUND.__dict__, "name": FOUND.name + ", устілка 24,5 см",
                      "colors": ("Чорний",)})  # fmt: skip
    run(env, Client(reply("no", "Підошва ніби світліша.")), listing=twin)
    assert state(env.conn, item) == (Status.PROBLEM, "possible_duplicate")
    text = row(env.conn, item)["problem_text"]
    assert "Підошва ніби світліша." in text
    assert "збігаються: однакові кольори, однакова довжина устілки" in text


@pytest.mark.parametrize(
    ("colors", "name_tail"),
    [
        (("Чорний",), ""),  # устілка знайденого невідома
        ((), ", устілка 24,5 см"),  # кольору знайденого немає (TD-034)
        (("Білий",), ", устілка 24,5 см"),  # колір інший
        (("Чорний",), ", устілка 26 см"),  # устілка інша
    ],
)
def test_conservative_rule_needs_both_signs_known_and_equal(env, colors, name_tail):
    item = card_of(env)
    other = Listing(**{**FOUND.__dict__, "name": FOUND.name + name_tail, "colors": colors})
    run(env, Client(reply("no")), listing=other)
    assert state(env.conn, item) == (Status.PUBLISHED, dup.NOTE_CODE)


def test_conservative_rule_does_not_touch_yes_or_unsure():
    verdict = dup.Verdict(dup.YES, "та сама", asked=True)
    assert dup.conservative(None, None, verdict) is verdict


def test_request_has_both_photos_and_both_texts_and_no_description(env):
    item = card_of(env)
    client = Client(reply("no"))
    run(env, client, listing=Listing(**{**FOUND.__dict__, "price": 1150, "colors": ("Чорний",)}))
    request = client.requests[0]
    assert request["model"] == ai.MODEL and request["thinking"] == {"type": "between_tools"}
    assert request["output_config"]["format"]["schema"] == dup.SCHEMA
    content = request["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text", "image", "text"]
    text = content[-1]["text"]
    assert "Устілка 24,5 см" in text  # наш текст поста
    assert SAME.name in text and "1150 грн" in text and "Опису в знімку немає" in text
    assert "@" not in text and item  # контактів у запиті немає


def test_every_request_goes_to_ai_requests_and_to_daily_budget(env, conn):
    from datetime import datetime

    from shafa import batch

    item = card_of(env)
    run(env, Client(reply("no")))
    count, cost = spent(env.conn)
    assert count == 1 and cost == ai.Usage(3400, 90).cost_micro_usd
    assert env.conn.execute("SELECT item_id, error FROM ai_requests").fetchone()[:] == (item, None)
    today = datetime.now().astimezone()
    assert batch.spent_today(env.conn, today) == cost  # рахується в денний бюджет ($3/добу)


def test_cost_of_one_comparison_is_about_a_cent(env):
    """Замір: два фото 1092×819 (по 1170 токенів) + інструкція + тексти. Очікувано ≈1–2 ¢."""
    item = card_of(env)
    card = review.load_card(env.conn, item)
    ours = dup.photos.prepare_bytes(jpeg(), "1.jpg")
    request = dup.build_request(card, FOUND, ours, ours)
    usage = dup.estimate(request, ours, ours)
    assert usage.input_tokens < 6000
    assert 0 < usage.cost_micro_usd <= 20_000  # ≤ 2 ¢
    print(f"DUP-001: ≈{usage.input_tokens} токенів входу, ≈{usage.cost_micro_usd / 10_000:.2f} ¢")


# --- Екран ---


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def test_screen_published_card_shows_ai_note(env, app):
    item = card_of(env)
    run(env, Client(reply("no", "інша підошва")))
    assert f"Схоже: {URL} — ШІ: інша пара (інша підошва)" in html(get(app, f"/item/{item}"))
    shown = html(get(app, "/published"))  # UX-004: у списку — просто, слова ШІ — на Картці
    assert "Перевірено: на Шафі є схоже оголошення — це інша пара" in shown


def test_screen_archived_card_shows_ai_reason_and_can_be_restored(env, app):
    item = card_of(env)
    run(env, Client(reply("yes", "та сама подряпина")))
    assert f"Уже є на Шафі: {URL} — та сама подряпина" in html(get(app, f"/item/{item}"))
    assert "та сама подряпина" in html(get(app, "/archived"))
    post(app, f"/item/{item}", action="restore")
    assert row(env.conn, item)["status"] == Status.REVIEW


def test_screen_problem_card_shows_ai_reason_and_human_buttons(env, app):
    item = card_of(env)
    run(env, Client(reply("unsure", "фото нечіткі")))
    page = html(get(app, f"/item/{item}"))
    assert "ШІ не впевнений: фото нечіткі" in page
    assert 'value="other_item"' in page and 'value="to_archive"' in page  # людина вирішує
