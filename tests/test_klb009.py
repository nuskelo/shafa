"""KLB-009: вкладка «Клубок» — списки, Картка, схвалення, рішення, панель робота (без браузера)."""

import json
from datetime import UTC, datetime

import pytest

from shafa import events, review, web
from shafa.db import Status
from shafa.klubok import queue
from shafa.robot import control, klubok_control, klubok_publish, lock
from test_klb002_queue import OTHER_PAIR, ad_row, add, row, snapshot
from test_klb008 import klubok_card
from test_web import PORT, TOKEN, get, html, location, post
from test_web_robot import Process, Spawner

NOW = datetime.now().astimezone()


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


@pytest.fixture
def lock_file(tmp_path):
    return tmp_path / "robot.lock"


@pytest.fixture
def spawner():
    return Spawner()


@pytest.fixture
def app(conn, tmp_path, root, spawner, lock_file):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        spawner=spawner, lock_file=lock_file,
    )  # fmt: skip


def status_of(conn, item_id):
    return conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()[0]


def ads(conn):
    return conn.execute("SELECT count(*) FROM klubok_ads").fetchone()[0]


def klubok_session(conn, **cols):
    names = ["started_at", "site", *cols]
    with conn:
        return conn.execute(
            f"INSERT INTO sessions ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            ("2026-11-12T10:00:00+00:00", "klubok", *cols.values()),
        ).lastrowid


# --- перемикач і вкладки ---


def test_switch_on_both_sites_and_klubok_tabs(conn, root, app):
    shafa = html(get(app, "/review"))
    assert 'href="/klubok/review"' in shafa and ">Клубок</a>" in shafa
    page = html(get(app, "/klubok/review"))
    for key, title in (("review", "На перевірку"), ("queue", "Схвалені"),
                       ("decide", "Проблеми"), ("published", "Опубліковані"),
                       ("archive", "Архів"), ("sessions", "Сесії")):  # fmt: skip  # KLB-010: як у Шафи
        assert f'href="/klubok/{key}"' in page and title in page
    assert location(get(app, "/klubok/skipped")) == "/klubok/archive"  # стара адреса «Не піде»
    assert location(get(app, "/klubok")) == "/klubok/review"
    assert get(app, "/klubok/nonsense").status == 404


def test_review_tab_shows_only_klubok_cards_shafa_does_not(conn, root, app):
    only = klubok_card(conn, root, 301, title="Черевики Ecco 38 розміру старі")
    shafa_card = add(conn, root, 7301, status=Status.REVIEW, **OTHER_PAIR)
    page = html(get(app, "/klubok/review"))
    assert f"/klubok/item/{only}?" in page and "Черевики Ecco 38 розміру старі" in page
    assert f"/klubok/item/{shafa_card}?" not in page
    assert "Хвиля 1 · зима" in page and "1200 грн" in page  # ціна Клубку — з поста
    assert "Черевики Ecco 38 розміру старі" not in html(get(app, "/review"))  # Шафа без змін


def test_search_like_shafa(conn, root, app):
    klubok_card(conn, root, 301, title="Черевики Ecco 38 розміру")
    klubok_card(conn, root, 302, **OTHER_PAIR)
    page = html(get(app, "/klubok/review?q=geox"))
    assert "Кеди Geox" in page and "Черевики Ecco" not in page


def test_get_pages_write_nothing(conn, root, app):
    klubok_card(conn, root, 301)
    add(conn, root, 7301)
    for tab in ("review", "queue", "decide", "published", "archive", "sessions"):
        html(get(app, f"/klubok/{tab}"))
    html(get(app, "/klubok/panel"))
    assert ads(conn) == 0


# --- Картка «лише Клубок»: поля, схвалення, «Не виставляти» ---


def test_card_shows_how_it_will_be_on_klubok(conn, root, app):
    item_id = klubok_card(conn, root, 301)
    page = html(get(app, f"/klubok/item/{item_id}"))
    assert "Як буде на Клубку" in page and "1200 грн" in page
    assert "Женское" in page  # шлях категорії Клубку
    assert 'value="approve_klubok"' in page and "Схвалити і далі" in page  # KLB-010: як у Шафи
    assert "Попросити ШІ заповнити заново" in page and "Не виставляти (в Архів)" in page
    assert f'action="/klubok/item/{item_id}"' in page
    assert 'value="approve"' not in page  # кнопок Шафи немає
    assert get(app, f"/item/{item_id}").status == 404  # Шафа її як і раніше не показує


def test_approve_klubok_goes_to_queue_with_wave(conn, root, app):
    item_id = klubok_card(conn, root, 301)
    response = post(app, f"/klubok/item/{item_id}", action="approve_klubok",
                     **{"from": "/klubok/review"})  # fmt: skip
    assert "msg=klubok_approved" in location(response)
    assert status_of(conn, item_id) == Status.APPROVED
    assert row(conn, item_id)["state"] == queue.QUEUED
    page = html(get(app, "/klubok/queue"))
    assert f"/klubok/item/{item_id}?" in page and "хвиля 1" in page and "№ 1" in page


def test_save_edits_on_klubok_card(conn, root, app):
    item_id = klubok_card(conn, root, 301)
    post(app, f"/klubok/item/{item_id}", action="save", title="Нова назва черевиків 38 розміру")
    card = review.load_card(conn, item_id)
    assert card.fields["title"].value == "Нова назва черевиків 38 розміру"
    assert card.status == Status.REVIEW


def test_reject_klubok_archived_not_in_queue(conn, root, app):
    item_id = klubok_card(conn, root, 301)
    post(app, f"/klubok/item/{item_id}", action="reject_klubok")
    assert status_of(conn, item_id) == Status.ARCHIVED
    queue.sync(conn, root, NOW)
    assert row(conn, item_id) is None
    assert f"/klubok/item/{item_id}?" not in html(get(app, "/klubok/review"))


def test_approve_refused_when_klubok_would_skip(conn, root, app):
    item_id = klubok_card(conn, root, 301, text="Черевики Ecco 38 розміру")  # без ціни
    page = html(get(app, f"/klubok/item/{item_id}"))
    # KLB-010 (F6): одна причина, одним реченням, без «піде після схвалення» поруч
    assert '<p class="robot-error">Схвалити не можна: у пості немає ціни</p>' in page
    assert "Не піде на Клубок" not in page and "немає ціни в пості" not in page
    assert "піде після схвалення" not in page
    assert "disabled" in page.split('value="approve_klubok"')[1].split(">")[0]
    post(app, f"/klubok/item/{item_id}", action="approve_klubok")
    assert status_of(conn, item_id) == Status.REVIEW


def test_approve_refused_with_yellow_section(conn, root, app):
    item_id = klubok_card(conn, root, 301, section="")
    response = post(app, f"/klubok/item/{item_id}", action="approve_klubok")
    assert "msg=no_section" in location(response)
    assert status_of(conn, item_id) == Status.REVIEW


def test_shafa_card_is_read_only_on_klubok_page(conn, root, app):
    item_id = add(conn, root, 7301)
    page = html(get(app, f"/klubok/item/{item_id}"))
    assert "Як буде на Клубку" in page and f'href="/item/{item_id}"' in page
    assert 'value="approve_klubok"' not in page
    for action in ("approve_klubok", "save", "reject_klubok"):
        post(app, f"/klubok/item/{item_id}", action=action)
    assert status_of(conn, item_id) == Status.APPROVED


def test_next_card_after_approve(conn, root, app):
    first = klubok_card(conn, root, 301)
    second = klubok_card(conn, root, 302, **OTHER_PAIR)
    response = post(app, f"/klubok/item/{first}", action="approve_klubok",
                    **{"from": "/klubok/review"})  # fmt: skip
    assert location(response).startswith(f"/klubok/item/{second}?")


# --- рішення: дубль, «перевірте, чи опубліковано» ---


def test_duplicate_buttons_on_klubok_card(conn, root, app):
    item_id = add(conn, root, 7301)
    snapshot(conn, listings=[("Черевики Ecco 38 розміру", 1200)])
    queue.sync(conn, root, NOW)
    assert row(conn, item_id)["problem_code"] == queue.DUPLICATE
    assert f"/klubok/item/{item_id}?" in html(get(app, "/klubok/decide"))
    page = html(get(app, f"/klubok/item/{item_id}"))
    assert 'value="klubok_dup_other"' in page and "Черевики Ecco 38 розміру" in page
    post(app, f"/klubok/item/{item_id}", action="klubok_dup_other")
    assert row(conn, item_id)["state"] == queue.QUEUED


def test_retry_only_from_uncertain(conn, root, monkeypatch):
    said = []
    monkeypatch.setattr(events, "emit", lambda kind, text, **kw: said.append((kind, text)))
    uncertain = add(conn, root, 7301)
    ad_row(conn, uncertain, queue.PROBLEM, problem_code=queue.UNCERTAIN, problem_text="x")
    others = {
        add(conn, root, 7302, **OTHER_PAIR): (queue.PROBLEM, queue.DUPLICATE),
        add(conn, root, 7303, brand="Rieker", title="Туфлі Rieker 39 розміру"): (
            queue.PROBLEM, "klubok_captcha"),
        add(conn, root, 7304, brand="Clarks", title="Туфлі Clarks 40 розміру"): (
            queue.PUBLISHED, None),
        add(conn, root, 7305, brand="Lasocki", title="Туфлі Lasocki 37 розміру"): (
            queue.QUEUED, None),
    }  # fmt: skip
    for item_id, (state, code) in others.items():
        ad_row(conn, item_id, state, problem_code=code)

    assert queue.retry(conn, uncertain)
    assert (
        row(conn, uncertain)["state"] == queue.QUEUED
        and row(conn, uncertain)["problem_code"] is None
    )
    assert said == [("klubok.retry", "Клубок: пост 7301 — «не опубліковано», повернуто в чергу")]
    assert not queue.retry(conn, uncertain)  # удруге — вже не Проблема
    for item_id, (state, code) in others.items():
        assert not queue.retry(conn, item_id)
        assert (row(conn, item_id)["state"], row(conn, item_id)["problem_code"]) == (state, code)


def test_retry_and_confirm_from_screen(conn, root, app, lock_file):
    item_id = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, item_id, queue.PROBLEM, problem_code=queue.UNCERTAIN, problem_text="x")
    page = html(get(app, f"/klubok/item/{item_id}"))
    assert 'value="klubok_confirm"' in page and 'value="klubok_retry"' in page

    with lock.hold("klubok-publish", lock_file):  # робот працює — нічого не міняємо
        response = post(app, f"/klubok/item/{item_id}", action="klubok_retry")
    assert "msg=klubok_robot_busy" in location(response)
    assert row(conn, item_id)["state"] == queue.PROBLEM

    snapshot(conn, at=datetime.now(UTC), listings=[("Інше оголошення", 900)])  # id 900
    bad = html(post(app, f"/klubok/item/{item_id}", action="klubok_confirm", klubok_id="12345"))
    assert "немає в знімку" in bad and row(conn, item_id)["state"] == queue.PROBLEM
    response = post(app, f"/klubok/item/{item_id}", action="klubok_confirm",
                    klubok_id="https://klubok.com/item-cherevyky-900")  # fmt: skip
    assert "msg=klubok_confirmed" in location(response)
    assert (row(conn, item_id)["state"], row(conn, item_id)["klubok_id"]) == (queue.PUBLISHED, 900)


def test_retry_from_screen_back_to_queue(conn, root, app):
    item_id = add(conn, root, 7301)
    ad_row(conn, item_id, queue.PROBLEM, problem_code=queue.UNCERTAIN, problem_text="x")
    assert "msg=klubok_retry" in location(
        post(app, f"/klubok/item/{item_id}", action="klubok_retry")
    )
    assert row(conn, item_id)["state"] == queue.QUEUED


@pytest.mark.parametrize(
    ("raw", "number"),
    [("37977926", 37977926), (" 37977926 ", 37977926),
     ("https://klubok.com/item-zhinochi-cherevyky-37977926", 37977926),
     ("https://evil.com/item-1-37977926", None), ("abc", None), ("0", None), ("", None)],
)  # fmt: skip
def test_klubok_number(raw, number):
    assert web.klubok_number(raw) == number


# --- «Виставлено», «Не піде» ---


def test_published_tab_links_only_to_klubok(conn, root, app):
    good = add(conn, root, 7301, status=Status.PUBLISHED)
    ad_row(conn, good, queue.PUBLISHED, url="https://klubok.com/item-x-5", klubok_id=5,
           published_at="2026-11-09T10:00:00+00:00")  # fmt: skip
    evil = add(conn, root, 7302, status=Status.PUBLISHED, **OTHER_PAIR)
    ad_row(conn, evil, queue.PUBLISHED, url="javascript:alert(1)", klubok_id=6)
    page = html(get(app, "/klubok/published"))
    assert "виставлено 09.11.2026" in page and "javascript:" not in page
    assert 'href="https://klubok.com/item-x-5"' in html(get(app, f"/klubok/item/{good}"))
    assert "javascript:" not in html(get(app, f"/klubok/item/{evil}"))


def test_skipped_grouped_by_reason(conn, root, app):
    for n, code in ((7301, "klubok_no_price"), (7302, "klubok_no_price"), (7303, queue.POST_GONE)):
        item_id = add(conn, root, n, title=f"Черевики {n} 38 розміру")
        ad_row(conn, item_id, queue.SKIPPED, problem_code=code)
    page = html(get(app, "/klubok/archive"))
    assert "у пості немає ціни · 2" in page and "пост видалено з каналу — пару продано · 1" in page
    assert page.index("у пості немає ціни") < page.index("пост видалено з каналу")


def test_no_sync_button_queue_is_always_fresh(conn, root, app):
    """KLB-010 (F3): ні кнопки «Перерахувати чергу», ні «+N» — черга пишеться у фоні."""
    add(conn, root, 7301)
    page = html(get(app, "/klubok/queue"))
    assert "Перерахувати чергу" not in page and "схвалених пар не перераховано" not in page
    assert "<small>+" not in html(get(app, "/klubok/panel"))
    assert post(app, "/klubok/sync", back="/klubok/queue").status == 404


def test_safe_klubok_list_and_next():
    assert web.safe_klubok_list("/klubok/queue?q=ecco&x=1") == "/klubok/queue?q=ecco"
    for bad in ("/review", "//evil.com/klubok/queue", "https://evil.com/klubok/queue",
                "/klubok/item/5", "/klubok/queue\\x"):  # fmt: skip
        assert web.safe_klubok_list(bad) is None
    assert web.safe_klubok_next("/klubok/item/5?from=/klubok/review") == (
        "/klubok/item/5?from=%2Fklubok%2Freview"
    )
    assert web.safe_klubok_next("/item/5") is None


# --- панель робота Клубку ---


def test_start_spawns_klubok_publish_with_rest_of_daily_limit(conn, root, app, spawner, tmp_path):
    item_id = add(conn, root, 7301)
    ad_row(conn, item_id, queue.QUEUED)
    done = add(conn, root, 7302, **OTHER_PAIR)
    ad_row(conn, done, queue.PUBLISHED, publish_started_at=datetime.now(UTC).isoformat())
    response = post(app, "/klubok/robot", action="start", back="/klubok/queue")
    assert "msg=klubok_started" in location(response)
    assert spawner.calls == [[
        "--klubok-publish", "--db", str(tmp_path / "база.sqlite"), "--root", str(root),
        "--limit", "9",
    ]]  # fmt: skip
    assert "--human-click" not in spawner.calls[0]


def test_start_fake_mode_and_login_snapshot(conn, root, tmp_path, spawner, lock_file):
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
                  spawner=spawner, lock_file=lock_file, fake_shafa=True)  # fmt: skip
    ad_row(conn, add(conn, root, 7301), queue.QUEUED)
    post(app, "/klubok/robot", action="start", back="/klubok/queue")
    assert spawner.calls[-1][-1] == "--fake-klubok"
    app.klubok_process = Process(0)
    assert "msg=klubok_fake_login" in location(post(app, "/klubok/robot", action="login"))
    post(app, "/klubok/robot", action="snapshot")
    assert spawner.calls[-1] == ["--klubok-snapshot", "--db", str(tmp_path / "база.sqlite"),
                                 "--fake-klubok"]  # fmt: skip


def test_login_spawns_klubok_login(conn, root, app, spawner):
    assert "msg=klubok_login" in location(post(app, "/klubok/robot", action="login"))
    assert spawner.calls == [["--klubok-login"]]


def test_start_refused_while_shafa_robot_runs_and_vice_versa(conn, root, app, spawner, lock_file):
    ad_row(conn, add(conn, root, 7301), queue.QUEUED)
    with lock.hold("publish", lock_file):
        page = html(get(app, "/klubok/panel"))
        assert "Зараз працює робот Шафи" in page and 'value="start"' not in page
        response = post(app, "/klubok/robot", action="start")
        assert "msg=klubok_cannot_start" in location(response)
        assert "msg=klubok_robot_busy" in location(post(app, "/klubok/robot", action="snapshot"))
    with lock.hold("klubok-publish", lock_file):
        shafa = control.state(conn, lock_file)
        assert shafa.code == "busy_klubok" and not shafa.can_start and "start" not in shafa.buttons
        assert "Зараз працює робот Клубку" in html(get(app, "/review"))
    assert spawner.calls == []


@pytest.mark.parametrize("why", ["off", "limit", "empty"])
def test_start_disabled_with_reason(conn, root, app, spawner, monkeypatch, why):
    item_id = add(conn, root, 7301)
    ad_row(conn, item_id, queue.QUEUED)
    if why == "off":
        monkeypatch.delenv(queue.FLAG)
        text = "Увімкніть Клубок у Налаштуваннях"
    elif why == "limit":
        monkeypatch.setenv(queue.LIMIT_SETTING, "1")
        with conn:
            conn.execute("UPDATE klubok_ads SET publish_started_at = ?",
                         (datetime.now(UTC).isoformat(),))  # fmt: skip
        text = "На сьогодні все: почато 1 з 1"
    else:
        with conn:
            conn.execute("UPDATE klubok_ads SET state = 'skipped'")
        with conn:
            conn.execute("UPDATE items SET status = 'review'")
        text = "Схвалених для Клубку немає"
    state = klubok_control.state(conn, app.lock_file, NOW)
    assert not state.can_start and text in state.why_not
    assert text in html(get(app, "/klubok/review"))
    post(app, "/klubok/robot", action="start")
    assert spawner.calls == []


def test_pause_resume_stop_reach_only_klubok_session(conn, root, app):
    shafa_sid = conn.execute("INSERT INTO sessions (started_at) VALUES ('x')").lastrowid
    conn.commit()
    sid = klubok_session(conn)

    def command(session_id):
        return conn.execute("SELECT command FROM sessions WHERE id = ?", (session_id,)).fetchone()[
            0
        ]

    assert "msg=klubok_paused" in location(post(app, "/klubok/robot", action="pause"))
    assert (command(sid), command(shafa_sid)) == ("pause", None)
    post(app, "/klubok/robot", action="resume")
    assert command(sid) is None
    post(app, "/klubok/robot", action="stop")
    assert (command(sid), command(shafa_sid)) == ("stop", None)
    with conn:
        conn.execute("UPDATE sessions SET ended_at = 'y' WHERE id = ?", (sid,))
    assert "msg=klubok_not_running" in location(post(app, "/klubok/robot", action="pause"))


def test_running_panel_shows_progress_and_stage(conn, root, tmp_path, lock_file):
    sid = klubok_session(conn)
    item_id = add(conn, root, 7301)
    ad_row(conn, item_id, queue.PUBLISHED, session_id=sid)
    now_file = tmp_path / "klubok-now.json"
    until = NOW.timestamp() + 150
    now_file.write_text(json.dumps({"session": sid, "stage": "pause", "limit": 8,
                                    "until": until, "last": {"post": 7301, "ok": True}}),
                        encoding="utf-8")  # fmt: skip
    with lock.hold("klubok-publish", lock_file):
        state = klubok_control.state(conn, lock_file, NOW, now_file=now_file)
        assert state.code == "running" and state.buttons == ["pause", "stop"]
        assert state.progress == "Виставлено 1 з 8 у цій сесії"
        assert state.doing == "Пауза між парами (як людина) — наступна за ~3 хв"
        assert state.previous == "Останній: пост 7301 — виставлено ✓"
        with conn:
            conn.execute("UPDATE sessions SET command = 'pause' WHERE id = ?", (sid,))
        paused = klubok_control.state(conn, lock_file, NOW, now_file=now_file)
        assert paused.code == "paused" and paused.buttons == ["resume", "stop"]
        assert klubok_control.state(conn, lock_file, NOW, now_file=now_file).doing == ""


def test_last_session_reason_in_plain_words(conn):
    klubok_session(conn, ended_at="2026-11-12T11:00:00+00:00", stop_code=queue.EMPTY,
                   stop_reason="у черзі Клубку нічого немає", published=3, problems=1)  # fmt: skip
    klubok_session(conn, ended_at="2026-11-12T12:00:00+00:00", stop_code=klubok_publish.LOGIN,
                   stop_reason=klubok_publish.LOGIN_TEXT, published=0)  # fmt: skip
    state = klubok_control.state(conn, None, NOW)
    assert "натисніть «Увійти в Клубок»" in state.last and "python -m" not in state.last
    assert klubok_control.reason_of(queue.EMPTY, "x") == "черга закінчилась"


def test_crashed_klubok_session_and_listings_line(conn, root):
    klubok_session(conn)
    with conn:
        conn.execute("INSERT INTO klubok_listings_read VALUES (1, ?, 1787)",
                     (datetime.now(UTC).isoformat(),))  # fmt: skip
    state = klubok_control.state(conn, None, NOW)
    assert state.code == "crashed" and "start" in state.buttons
    assert state.listings.startswith("Ваших оголошень на Клубку: 1787 · перечитано сьогодні")
