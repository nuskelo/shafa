"""KLB-007: після першої живої публікації — `used_id` рядком, вікно під екран, «підтвердити»."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shafa.__main__ import main
from shafa.klubok import queue
from shafa.robot import klubok_form as form
from shafa.robot import lock
from test_klb002_queue import add, row

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def reply(**data) -> str:
    return json.dumps({"status": "success", **data})


# --- _parse ---


@pytest.mark.parametrize("used_id", ["37977926", 37977926, "007"])
def test_parse_accepts_int_or_digit_string(used_id):
    sent = form.Session._parse(200, reply(used_id=used_id))
    assert sent.outcome == form.SUCCESS and sent.klubok_id == int(used_id)


@pytest.mark.parametrize("used_id", ["0", "", "12a", True, -5, "-5", " 12", "١٢", None, 1.5, [1]])
def test_parse_rejects_everything_else(used_id):
    sent = form.Session._parse(200, reply(used_id=used_id))
    assert sent.outcome == form.UNCERTAIN
    assert "used_id" in sent.detail and "відповідь" in sent.detail


def test_parse_uncertain_detail_is_short_and_has_type_and_value():
    sent = form.Session._parse(200, reply(used_id="12a", pad="x" * 1000))
    assert "str '12a'" in sent.detail and len(sent.detail) < 450


# --- вікно ---


def test_live_session_opens_without_fixed_viewport(monkeypatch, tmp_path):
    monkeypatch.setattr(form, "profile_dir", lambda: tmp_path / "klubok-profile")
    playwright = MagicMock()
    form.LiveSession(playwright)
    kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["no_viewport"] is True and "viewport" not in kwargs


# --- --klubok-confirm ---


def listing(conn, klubok_id=37977926, *, sold=0, read=NOW, url="https://klubok.com/item/37977926"):
    with conn:
        conn.execute(
            "INSERT INTO klubok_listings (id, name, price, sizes, url, sold, added_at) "
            "VALUES (?, 'Черевики', 1900, '[]', ?, ?, 'x')",
            (klubok_id, url, sold),
        )
        conn.execute(
            "INSERT OR REPLACE INTO klubok_listings_read VALUES (1, ?, 1)",
            (read.isoformat(timespec="seconds"),),
        )


def ad(conn, item_id, state="problem", code=queue.UNCERTAIN, klubok_id=None):
    with conn:
        conn.execute(
            "INSERT INTO klubok_ads (item_id, state, klubok_id, problem_code, problem_text, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'текст', 'x', 'x')",
            (item_id, state, klubok_id, code),
        )


def snap(conn):
    return (
        [tuple(r) for r in conn.execute("SELECT * FROM klubok_ads ORDER BY item_id")],
        [tuple(r) for r in conn.execute("SELECT * FROM klubok_listings ORDER BY id")],
    )


@pytest.fixture
def uncertain(conn, root):
    item = add(conn, root, 7233)
    ad(conn, item)
    listing(conn)
    return item


def test_confirm_publishes_card(conn, uncertain):
    text = queue.confirm(conn, 7233, 37977926, NOW)
    r = row(conn, uncertain)
    assert (r["state"], r["klubok_id"], r["url"]) == (
        "published", 37977926, "https://klubok.com/item/37977926",
    )  # fmt: skip
    assert r["published_at"] and r["problem_code"] is None and r["problem_text"] is None
    assert len(text.splitlines()) <= 5 and "37977926" in text


def refused(conn, post=7233, klubok_id=37977926, now=NOW):
    before = snap(conn)
    with pytest.raises(queue.ConfirmError) as error:
        queue.confirm(conn, post, klubok_id, now)
    assert snap(conn) == before
    return str(error.value)


def test_confirm_wrong_state(conn, root):
    item = add(conn, root, 7233)
    ad(conn, item, state="queued", code=None)
    listing(conn)
    assert "queued" in refused(conn)


def test_confirm_wrong_code(conn, root):
    item = add(conn, root, 7233)
    ad(conn, item, code="klubok_captcha")
    listing(conn)
    assert "klubok_captcha" in refused(conn)


def test_confirm_no_card(conn, root):
    listing(conn)
    assert "немає" in refused(conn)


def test_confirm_id_not_in_snapshot(conn, uncertain):
    assert "немає в знімку" in refused(conn, klubok_id=111)


def test_confirm_sold_listing(conn, root):
    ad(conn, add(conn, root, 7233))
    listing(conn, sold=1)
    assert "проданим" in refused(conn)


def test_confirm_id_belongs_to_another_card(conn, uncertain, root):
    other = add(conn, root, 7300)
    ad(conn, other, state="published", code=None, klubok_id=37977926)
    assert str(other) in refused(conn)


@pytest.mark.parametrize("age", [timedelta(hours=25), timedelta(hours=-1)])
def test_confirm_stale_snapshot(conn, root, age):
    ad(conn, add(conn, root, 7233))
    listing(conn, read=NOW - age)
    assert "--klubok-snapshot" in refused(conn)


def test_confirm_no_snapshot_at_all(conn, root):
    ad(conn, add(conn, root, 7233))
    assert "--klubok-snapshot" in refused(conn)


def test_confirm_two_cards_lists_both(conn, uncertain, root):
    second = add(conn, root, 7233, folder="7233~2")
    ad(conn, second)
    message = refused(conn)
    assert str(uncertain) in message and str(second) in message


def test_confirm_needs_state_in_update(conn, uncertain):
    """Умова на стан у самому UPDATE: рядок, що став іншим між перевіркою й записом, не чіпається."""
    with conn:
        conn.execute("UPDATE klubok_ads SET state = 'removed'")
    assert "removed" in refused(conn)


# --- CLI ---


@pytest.fixture
def cli(tmp_path, uncertain, conn, monkeypatch):
    conn.commit()
    monkeypatch.setattr(lock, "busy", lambda *a, **k: None)
    monkeypatch.setattr("shafa.klubok.queue.snapshot_fresh", lambda *a: True)
    return ["robot", "--klubok-confirm", "--db", str(tmp_path / "база.sqlite")]


def test_cli_confirms(cli, conn, uncertain, capsys):
    assert main([*cli, "--post", "7233", "--klubok-id", "37977926"]) == 0
    assert "опубліковано" in capsys.readouterr().out
    assert row(conn, uncertain)["state"] == "published"


def test_cli_refusal_exit_1_db_unchanged(cli, conn, uncertain, capsys):
    assert main([*cli, "--post", "7233", "--klubok-id", "5"]) == 1
    assert "немає в знімку" in capsys.readouterr().out
    assert row(conn, uncertain)["state"] == "problem"


def test_cli_refuses_during_live_session(cli, conn, uncertain, monkeypatch, capsys):
    monkeypatch.setattr(lock, "busy", lambda *a, **k: {"mode": "klubok-publish"})
    assert main([*cli, "--post", "7233", "--klubok-id", "37977926"]) == 2
    assert row(conn, uncertain)["state"] == "problem"


@pytest.mark.parametrize(
    "extra",
    [[], ["--post", "1"], ["--klubok-id", "5"], ["--post", "1", "--post", "2", "--klubok-id", "5"]],
)
def test_cli_needs_one_post_and_id(cli, extra, capsys):
    with pytest.raises(SystemExit) as stop:
        main([*cli, *extra])
    assert stop.value.code == 2


def test_klubok_id_only_with_confirm(capsys):
    with pytest.raises(SystemExit):
        main(["robot", "--login", "--klubok-id", "5"])
    assert "--klubok-id" in capsys.readouterr().err
