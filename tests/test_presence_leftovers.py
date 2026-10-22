"""BOT-014: копії нічної перевірки не лишаються в чаті Власника. Telegram — підставний."""

import json

import pytest

from shafa.bot import presence
from shafa.bot.presence import PRESENT, Presence
from shafa.bot.telegram import TelegramError

CHAT, CHANNEL = 111, 222


class FakeTg:
    def __init__(self):
        self.next_id = 500
        self.copies: set[int] = set()
        self.delete_errors: list[TelegramError] = []  # по одній на виклик deleteMessage
        self.deleted: list[int] = []

    def forward_message(self, chat_id, from_chat_id, message_id):
        self.next_id += 1
        self.copies.add(self.next_id)
        return {"message_id": self.next_id}

    def delete_message(self, chat_id, message_id):
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        self.copies.discard(message_id)
        self.deleted.append(message_id)


def too_many(after=7):
    return TelegramError("429", code=429, retry_after=after)


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(presence.events, "emit", lambda kind, text, **kw: sent.append(kind))
    return sent


def make(tg, tmp_path, now=1000.0):
    clock = {"now": now}
    return Presence(tg, CHANNEL, CHAT, log=lambda _: None, data_dir=tmp_path,
                    clock=lambda: clock["now"]), clock  # fmt: skip


def test_429_on_last_check_is_cleaned_after_night(tmp_path, told):
    tg = FakeTg()
    tg.delete_errors = [too_many(7), too_many(7)]
    pres, _ = make(tg, tmp_path)
    assert pres.check(1401) == PRESENT
    assert tg.copies  # копія лишилась
    naps = []
    assert pres.settle(sleep=naps.append) == 0
    assert not tg.copies
    assert naps == [7]  # retry_after Telegram, а не вигадана пауза
    assert told == []  # вдалося — тихо
    assert not (tmp_path / presence.LEFTOVERS_FILE).exists()


def test_restart_picks_up_leftover_from_file(tmp_path, told):
    tg = FakeTg()
    tg.delete_errors = [too_many()]
    first, _ = make(tg, tmp_path)
    first.check(1401)
    saved = json.loads((tmp_path / presence.LEFTOVERS_FILE).read_text(encoding="utf-8"))
    assert [e["chat"] for e in saved] == [CHAT]
    second, _ = make(tg, tmp_path)  # «перезапуск»: новий екземпляр, пам'ять порожня
    assert second.settle(sleep=lambda _: None) == 0
    assert not tg.copies


def test_other_instance_sees_leftover(tmp_path, told):
    """Бот і робот — різні екземпляри, але файл спільний."""
    tg = FakeTg()
    tg.delete_errors = [too_many()]
    bot, _ = make(tg, tmp_path)
    bot.check(1401)
    robot, _ = make(tg, tmp_path)
    robot.check(1402)  # `_clean` на початку перевірки добирає чужий залишок
    assert 501 in tg.deleted


def test_copy_older_than_48h_is_dropped_without_request(tmp_path, told):
    tg = FakeTg()
    tg.delete_errors = [too_many()]
    pres, clock = make(tg, tmp_path)
    pres.check(1401)
    clock["now"] += presence.TOO_OLD + 1
    tg.delete_errors = [AssertionError("запит не потрібен")]  # type: ignore[list-item]
    assert pres.settle(sleep=lambda _: None) == 0
    assert tg.deleted == []
    assert tg.delete_errors  # deleteMessage не викликали
    assert told == []


def test_all_attempts_fail_one_quiet_event(tmp_path, told):
    tg = FakeTg()
    tg.delete_errors = [too_many(60)] * 50
    pres, _ = make(tg, tmp_path)
    pres.check(1401)
    naps = []
    assert pres.settle(sleep=naps.append) == 1
    assert sum(naps) <= presence.SETTLE_BUDGET
    assert told == ["presence.leftover"]
    pres.settle(sleep=lambda _: None)  # наступна ніч — той самий залишок не повторюється
    assert told == ["presence.leftover"]


def test_event_kind_is_quiet():
    from shafa import events

    kind = events.info("presence.leftover")
    assert kind is not events.OTHER and not kind.loud


def test_without_data_dir_memory_only():
    tg = FakeTg()
    tg.delete_errors = [too_many()]
    pres = Presence(tg, CHANNEL, CHAT, log=lambda _: None)
    pres.check(1401)
    assert pres.settle(sleep=lambda _: None) == 0
