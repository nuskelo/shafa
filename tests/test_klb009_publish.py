"""KLB-009: «Пауза» / «Стоп» з екрана доходять до сесії Клубку (підставний Клубок, Chromium)."""

import json

from shafa.klubok import queue
from shafa.robot import klubok_publish as kp
from shafa.robot import publish
from test_klb001_page import chromium  # noqa: F401  (фікстура браузера на модуль)
from test_klb003_publish import cards, klubok_on, last_session, server, session  # noqa: F401


def run(conn, root, session, sleep, now_file=None):  # noqa: F811
    opts = kp.Options(limit=2, submit_wait=3.0, pause=(0, 0), page_pause=(0, 0))
    return kp.run(conn, root, session, opts, sleep=sleep, log=lambda line: None,
                  now_file=now_file)  # fmt: skip


def test_pause_waits_between_pairs_until_resume(conn, root, cards, server, session, tmp_path):  # noqa: F811
    seen = []

    def sleep(seconds):
        published = sum(
            queue.PUBLISHED == r[0] for r in conn.execute("SELECT state FROM klubok_ads")
        )
        if seconds == 0 and published == 1 and not seen:
            assert publish.request(conn, "pause", site="klubok")  # «Пауза» після першої пари
            seen.append("paused")
        elif seconds == kp.TICK:
            data = json.loads((tmp_path / "now.json").read_text(encoding="utf-8"))
            seen.append(data["stage"])
            assert published == 1  # друга пара не почалась, поки пауза
            publish.request(conn, None, site="klubok")  # «Продовжити»

    result = run(conn, root, session, sleep, now_file=tmp_path / "now.json")
    assert seen == ["paused", "paused"]
    assert result.code == kp.DONE and len(result.published) == 2
    assert last_session(conn)["command"] is None


def test_stop_finishes_current_pair_then_stops(conn, root, cards, server, session):  # noqa: F811
    def sleep(seconds):
        if seconds == 0:  # пауза після першої пари — «Стоп» з екрана
            publish.request(conn, "stop", site="klubok")

    result = run(conn, root, session, sleep)
    assert result.code == kp.STOPPED and len(result.published) == 1
    assert queue.QUEUED in {r[0] for r in conn.execute("SELECT state FROM klubok_ads")}
    ended = last_session(conn)
    assert ended["stop_code"] == kp.STOPPED and ended["command"] is None


def test_shafa_command_does_not_reach_klubok(conn, root, cards, server, session):  # noqa: F811
    with conn:
        conn.execute("INSERT INTO sessions (started_at) VALUES ('x')")  # відкрита сесія Шафи

    def sleep(seconds):
        publish.request(conn, "stop")  # «Стоп» Шафи

    result = run(conn, root, session, sleep)
    assert result.code == kp.DONE and len(result.published) == 2
