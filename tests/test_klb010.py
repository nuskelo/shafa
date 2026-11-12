"""KLB-010: Клубок на екрані = Шафа — дублі, хвилі, Експорт іншого чату, черга завжди актуальна."""

import pytest

from shafa import events, review
from shafa.db import Status
from shafa.klubok import dup, queue, refresh, waves
from test_klb002_queue import TODAY, ad_row, add, row, snapshot, states
from test_klb008 import klubok_card


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def facts(name, brand=None, price=1200, sizes=()):
    return dup.Facts(name=name, sizes=tuple(sizes), brand=brand, price=price, group=None)


# --- F11 / N18: дублі ---


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        # різні номери моделі — інша пара
        (facts("Кросівки New Balance 237 44,5 розміру", "New Balance"),
         facts("Кросівки New Balance 530 оригінал в ідеальному стані"), False),
        (facts("Кросівки Asics Gel-1130 38 розміру", "Asics"),
         facts("Кросівки Asics 1130 38 розміру"), True),
        # без бренду: інший підтип — ні; той самий підтип і ціна ±25 % — так; ±33 % — ні
        (facts("Новий шкіряний рюкзак"), facts("Шкіряна сумка в ідеальному стані"), False),
        (facts("Шкіряна сумка в новому стані", price=1200),
         facts("Шкіряна сумка Італія", price=1300), True),
        (facts("Шкіряна сумка в новому стані", price=1200),
         facts("Шкіряна сумка Італія", price=1800), False),
        (facts("Шкіряні черевики в ідеальному стані 37 розміру", sizes=["37"]),
         facts("Шкіряна сумка 37"), False),
        # не звужуємо: «нові» ≈ «в ідеальному стані», 38,5 ≈ 38, з брендом ціна ±40 %
        (facts("Нові кросівки Pavers 45 розміру", "Pavers", sizes=["45"]),
         facts("Шкіряні кросівки Pavers в ідеальному стані 45 розміру", sizes=["45"]), True),
        (facts("Термо черевики Peter Storm 38,5 розміру", "Peter Storm", sizes=["38,5"]),
         facts("Термо черевики Peter Storm 38 розміру", sizes=["38"]), True),
        (facts("Черевики Ecco 38 розміру", "Ecco", price=1000, sizes=["38"]),
         facts("Черевики Ecco 38 розміру", price=1350, sizes=["38"]), True),
    ],
)  # fmt: skip
def test_duplicate_rule(ours, theirs, expected):
    assert dup.same(ours, theirs, brand_in_name=True) is expected


def test_model_numbers_skip_sizes_years_insole_and_price():
    title = "Кросівки Nike Air Max 90 38-39 розміру 2019 року, устілка 24,5 см, 1200грн"
    assert dup.model_numbers(title) == set()
    assert dup.model_numbers("New Balance 574 classic") == {"574"}


def test_other_model_of_same_brand_goes_to_queue(conn, root):
    item_id = add(
        conn, root, 7301, brand="New Balance", sizes=("44,5",), section="Чоловічий одяг / Взуття / "
        "Кросівки", title="Кросівки New Balance 237 44,5 розміру в ідеальному стані",
    )  # fmt: skip
    snapshot(conn, listings=[("Кросівки New Balance 530 оригінал 44,5 розміру", 1200)])
    assert states(queue.plan(conn, root, TODAY))[item_id] == (queue.QUEUED, None)


# --- F10: хвилі ---

MITTENS = "Нові зимові рукавиці Snowlife Швейцарія. На 6-10 років. Дуже теплі. 650грн"
SLIPPERS = (
    "Нові натуральні тапочки балетки. Вказаний розмір 4/5. Ідуть на 36. Всередині хутро. 700грн"
)


@pytest.mark.parametrize(
    ("text", "kind", "wave"),
    [
        (MITTENS, "одяг", None),
        (SLIPPERS, "домашнє", 4),
        ("Хутряні тапочки UGG в новому стані. 37 розмір", "домашнє", 4),
        ("Хутряна жилетка Zana Італія.", "одяг", None),
        ("Кросівки Nike в ідеальному стані, гарно під джинси. 41 розмір", "спорт", 4),
        ("Зимові черевики на хутрі Ecco. 38 розмір", "зима", 1),
        ("Шкіряна сумка з шарфом у подарунок", "сумки", 1),
    ],
)
def test_wave_kind(text, kind, wave):
    assert (waves.kind(text), waves.wave_of(waves.kind(text))) == (kind, wave)


def test_reclassify_moves_slippers_and_archives_mittens_once(conn, root, monkeypatch):
    said = []
    monkeypatch.setattr(events, "emit", lambda *a, **k: said.append((a, k)))
    mittens = klubok_card(conn, root, 3800, text=MITTENS)
    slippers = klubok_card(conn, root, 4045, text=SLIPPERS)

    done = refresh.reclassify(conn)

    assert done.dropped == [3800] and done.archived == [mittens]
    assert done.moved == [(4045, "зима/1", "домашнє/4")]
    card = conn.execute(
        "SELECT status, problem_code FROM items WHERE id = ?", (mittens,)
    ).fetchone()
    assert tuple(card) == (Status.ARCHIVED, review.NOT_FOR_KLUBOK)
    assert conn.execute("SELECT status FROM items WHERE id = ?", (slippers,)).fetchone()[0] == (
        Status.REVIEW
    )
    assert queue.wave(conn, 3800) is None and queue.wave(conn, 4045) == 4
    assert said[0][0][0] == "klubok.waves"
    assert refresh.reclassify(conn) == refresh.Reclassified()  # повтор — нічого


def test_reclassify_leaves_card_already_on_klubok(conn, root):
    mittens = klubok_card(conn, root, 3800, text=MITTENS, status=Status.APPROVED)
    ad_row(conn, mittens, queue.PUBLISHED, klubok_id=7)
    assert refresh.reclassify(conn).archived == []


# --- F3: черга завжди актуальна ---


def test_refresh_writes_queue_without_button(conn, root):
    item_id = add(conn, root, 7301)
    assert queue.unsynced(conn) == 1
    assert refresh.refresh(conn, root, TODAY) == 1
    assert queue.unsynced(conn) == 0 and row(conn, item_id)["state"] == queue.QUEUED


def test_refresh_does_nothing_to_queue_when_klubok_off(conn, root, monkeypatch):
    monkeypatch.delenv(queue.FLAG)
    add(conn, root, 7301)
    assert refresh.refresh(conn, root, TODAY) is None and queue.unsynced(conn) == 1


def test_background_refresh_merges_requests(conn, root, tmp_path):
    from shafa import db

    path = conn.execute("PRAGMA database_list").fetchone()[2]
    add(conn, root, 7301)
    bg = refresh.Background(lambda: db.connect(path), lambda: root, print)
    bg.start()
    bg.start()
    bg.wait()
    bg.wait()
    assert queue.unsynced(conn) == 0


# --- N1: Експорт іншого чату ---


def export_bytes(chat_id, name="Канал"):
    import json

    return json.dumps(
        {"name": name, "type": "private_channel", "id": chat_id, "messages": []}
    ).encode("utf-8")


@pytest.mark.parametrize("setting", ["2394827824", "-1002394827824"])
def test_export_of_channel_is_accepted(monkeypatch, tmp_path, setting):
    from shafa import export

    monkeypatch.setenv(export.CHANNEL_SETTING, setting)
    export.check_chat(export_bytes(2394827824), tmp_path)


def test_export_of_other_chat_is_refused_with_explanation(monkeypatch, tmp_path):
    from shafa import export

    monkeypatch.setenv(export.CHANNEL_SETTING, "2394827824")
    with pytest.raises(export.WrongChat, match="іншого чату.*Вікторія Шуз - фото.*3636774420"):
        export.check_chat(export_bytes(3636774420, "Вікторія Шуз - фото"), tmp_path)


def test_without_setting_first_export_chat_is_remembered(monkeypatch, tmp_path):
    from shafa import export

    monkeypatch.delenv(export.CHANNEL_SETTING, raising=False)
    export.check_chat(export_bytes(2394827824), tmp_path)
    export.check_chat(export_bytes(2394827824), tmp_path)
    with pytest.raises(export.WrongChat):
        export.check_chat(export_bytes(3636774420), tmp_path)


@pytest.mark.parametrize("command", ["klubok-import", "import-old", "scan"])
def test_cli_refuses_other_chat_and_writes_nothing(
    conn, root, tmp_path, monkeypatch, capsys, command
):
    from shafa import export
    from shafa.__main__ import main

    monkeypatch.setenv(export.CHANNEL_SETTING, "2394827824")
    source = tmp_path / "exp" / "result.json"
    source.parent.mkdir()
    source.write_bytes(export_bytes(3636774420, "Вікторія Шуз - фото"))
    numbers = tmp_path / "list.txt"
    numbers.write_text("41\n", encoding="utf-8")
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    args = (
        {
            "klubok-import": ["klubok-import", "--export", str(source), "--wave", "1"],
            "import-old": ["import-old", "--export", str(source), "--list", str(numbers)],
            "scan": ["scan", "--export", str(source)],
        }[command]
        + ["--db", db_path]
        + (["--root", str(root)] if command != "import-old" else [])
    )
    before = conn.execute("SELECT count(*) FROM posts").fetchone()[0]
    assert main(args) == 2
    assert "іншого чату" in capsys.readouterr().out
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == before


# --- F7: ціна на Клубку ---


def test_klubok_only_card_price_is_post_price_and_editable(conn, root):
    from shafa.klubok import fields

    item_id = klubok_card(conn, root, 301)
    card = review.load_card(conn, item_id)
    assert card.fields["price"].value == 1200  # у пості 1200 грн, без +15 % Шафи
    assert card.fields["price"].spec.label == "Ціна на Клубку, грн"
    assert fields.ad(card, root).price == 1200

    review.save(conn, card, {"price": ["1100"]}, None)
    assert fields.ad(review.load_card(conn, item_id), root).price == 1100


def test_shafa_card_keeps_shafa_price_and_klubok_takes_post_price(conn, root):
    from shafa.klubok import fields

    card = review.load_card(conn, add(conn, root, 7301))
    assert card.fields["price"].value == 1380  # 1200 +15 %
    assert card.fields["price"].spec.label == "Ціна на Шафі, грн"
    assert fields.klubok_price(card) == 1200


# --- екран: рішення дубля в рядку, панель, Сесії, 404 ---


@pytest.fixture
def app(conn, tmp_path, root):
    from shafa import web
    from test_web import PORT, TOKEN
    from test_web_robot import Spawner

    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=Spawner(), lock_file=tmp_path / "robot.lock",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("action", "state"), [("klubok_dup_yes", None), ("klubok_dup_other", "queued")]
)
def test_duplicate_decided_right_in_problems_row(conn, root, app, action, state):
    from test_web import get, html, location, post

    item_id = add(conn, root, 7302, brand="Geox", title="Кеди Geox 41 розміру", sizes=("41",))
    ad_row(conn, item_id, queue.PROBLEM, problem_code=queue.DUPLICATE, dup_name="Кеди Geox 41")
    page = html(get(app, "/klubok/decide"))
    assert f'action="/klubok/item/{item_id}"' in page and 'value="klubok_dup_yes"' in page
    assert "Кеди Geox 41" in page and "Інша пара — виставити" in page

    response = post(app, f"/klubok/item/{item_id}", action=action, stay="list",
                    **{"from": "/klubok/decide"})  # fmt: skip
    assert location(response).startswith("/klubok/decide?msg=")  # лишаємось у списку
    assert f"/klubok/item/{item_id}?" not in html(get(app, "/klubok/decide"))  # рядок зник
    if state:
        assert row(conn, item_id)["state"] == state


def test_login_stop_is_yellow_login_needed_until_window_closed(conn, root, tmp_path):
    from datetime import UTC, datetime, timedelta

    from shafa.robot import klubok_control, klubok_publish

    with conn:
        conn.execute(
            "INSERT INTO sessions (started_at, ended_at, site, stop_code) VALUES (?, ?, 'klubok', ?)",
            ("2026-11-12T10:00:00+00:00", "2026-11-12T10:01:00+00:00", klubok_publish.LOGIN),
        )
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    state = klubok_control.state(conn, tmp_path / "robot.lock", now)
    assert state.code == "login_needed" and not state.can_start
    assert "Увійти в Клубок" in state.why_not
    klubok_control.mark_login(now - timedelta(minutes=1))
    assert klubok_control.state(conn, tmp_path / "robot.lock", now).code == "stopped"


def test_started_over_limit_is_never_shown_over_limit(conn, root, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from shafa.robot import klubok_control

    monkeypatch.setenv(queue.LIMIT_SETTING, "2")
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    for n in range(3):
        item_id = add(conn, root, 7301 + n)
        ad_row(conn, item_id, queue.PUBLISHED, klubok_id=n + 1,
               publish_started_at=now.isoformat(), published_at=now.isoformat())  # fmt: skip
    state = klubok_control.state(conn, tmp_path / "robot.lock", now)
    assert (state.shown_started, state.day_limit) == (2, 2)
    assert "3 з 2" not in state.why_not and any("більше за ліміт" in d for d in state.details)


def test_klubok_sessions_look_like_shafa(conn, root, app):
    from test_web import get, html

    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    with conn:
        session = conn.execute(
            "INSERT INTO sessions (started_at, ended_at, site, published, stop_code) "
            "VALUES ('2026-11-12T10:00:00+00:00', '2026-11-12T10:20:00+00:00', 'klubok', 1, 'done')"
        ).lastrowid
    ad_row(conn, item_id, queue.PUBLISHED, klubok_id=9, session_id=session,
           published_at="2026-11-12T10:10:00+00:00")  # fmt: skip
    page = html(get(app, "/klubok/sessions"))
    assert f'href="/klubok/session/{session}"' in page and "Виставлено <b>1</b>" in page
    one = html(get(app, f"/klubok/session/{session}"))
    assert f'href="/klubok/item/{item_id}"' in one and 'href="/klubok/sessions"' in one
    assert get(app, f"/session/{session}").status == 404  # сесія Клубку — не в Шафі


@pytest.mark.parametrize("path", ["/nope", "/klubok/nope"])
def test_404_is_a_normal_page_with_header(app, path):
    from test_web import get

    response = get(app, path)
    page = response.body.decode("utf-8")
    assert response.status == 404 and '<header class="top">' in page and 'class="wrap"' in page


def test_spare_photo_note_only_for_shafa_etalon_and_no_verdict_when_published(conn, root, app):
    from test_web import get, html

    only = klubok_card(conn, root, 301)
    page = html(get(app, f"/klubok/item/{only}"))
    assert "фото Картки Шафи стерто" not in page and "Хвиля 1 · зима" in page  # N4, N3
    published = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, published, queue.PUBLISHED, klubok_id=5, url="https://klubok.com/item-x-5")
    page = html(get(app, f"/klubok/item/{published}"))
    assert "Не піде на Клубок" not in page and "Як буде на Клубку" not in page  # N5
    assert "Ціна на Клубку, грн" in page  # F7: Картка Шафи на вкладці Клубку


def test_shafa_crashed_at_daily_limit_does_not_say_press_start(conn, root, tmp_path, monkeypatch):
    """N9 (спільна кривизна): «зупинився посеред роботи» при вичерпаному ліміті — «завтра»."""
    from datetime import UTC, datetime

    from shafa.robot import control

    monkeypatch.setenv("SHAFA_DAILY_LIMIT", "1")
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    with conn:
        conn.execute(
            "UPDATE items SET publish_started_at = ?, shafa_id = 1 WHERE id = ?",
            (now.isoformat(), item_id),
        )
        conn.execute(
            "INSERT INTO sessions (started_at, site) VALUES (?, 'shafa')", (now.isoformat(),)
        )
    state = control.state(conn, tmp_path / "robot.lock", day_limit=1, now=now)
    assert state.code == "crashed"
    assert "завтра після 00:00" in state.text and "щоб продовжити" not in state.text
