"""KLB-012: фото пар бота з Експорту групи обговорення → `<дані>/klubok-old/<N>/` (без мережі)."""

import hashlib
import json

import pytest

from shafa import db, folders
from shafa.__main__ import main
from shafa.db import Status
from shafa.klubok import chatphotos, fields, queue
from test_klb002_queue import TEXT, add
from test_review import make_photo

CHANNEL = 5550001111  # вигаданий id каналу
GROUP = 7770002222
HER = "user100"  # її акаунт: відповідає на більшість пересланих постів
BUYER = "user200"
BASE = "2026-11-01T10:00:00"  # час поста; пересланий приходить за секунди


@pytest.fixture(autouse=True)
def klubok_on(monkeypatch):
    monkeypatch.setenv(queue.FLAG, "1")


def text_of(n):
    return f"{TEXT} Пара {n}."


def channel_file(tmp_path, posts, chat_id=CHANNEL):
    folder = tmp_path / "канал"
    folder.mkdir(exist_ok=True)
    messages = [
        {"id": n, "type": "message", "date": date, "text": text}
        for n, (date, text) in posts.items()
    ]
    (folder / "result.json").write_text(
        json.dumps({"name": "К", "type": "private_channel", "id": chat_id, "messages": messages}),
        encoding="utf-8",
    )
    return folder


class Chat:
    """Підставний Експорт групи: пересланий пост і відповіді; кожне фото — справжній файл."""

    def __init__(self, tmp_path, kind="private_supergroup"):
        self.folder = tmp_path / "чат"
        (self.folder / "photos").mkdir(parents=True)
        self.kind, self.messages, self.next_id = kind, [], 1

    def photo(self, color):
        name = f"photos/{len(list((self.folder / 'photos').iterdir())) + 1}.jpg"
        make_photo(self.folder / name, (60 + color, 40))
        return name

    def add(self, date, from_id, **extra):
        self.next_id += 1
        self.messages.append(
            {"id": self.next_id, "type": "message", "date": date, "from_id": from_id,
             "from": from_id, "text": "", **extra}
        )  # fmt: skip
        return self.next_id

    def forward(self, date, text, color, origin=f"channel{CHANNEL}"):
        return self.add(
            date, f"channel{CHANNEL}", forwarded_from_id=origin, text=text, photo=self.photo(color)
        )

    def reply(self, to, date, color, who=HER):
        return self.add(date, who, reply_to_message_id=to, photo=self.photo(color))

    def save(self):
        (self.folder / "result.json").write_text(
            json.dumps(
                {"name": "Група", "type": self.kind, "id": GROUP, "messages": self.messages}
            ),
            encoding="utf-8",
        )
        return self.folder


def bare(conn, root, post_id):
    """Опублікована Картка без фото на диску; рядок Клубку — `skipped klubok_no_photos`."""
    item_id = add(conn, root, post_id, status=Status.PUBLISHED, text=text_of(post_id), photos=False)
    queue.sync(conn, root)
    return item_id


def ad_state(conn, item_id):
    return tuple(
        conn.execute(
            "SELECT state, problem_code FROM klubok_ads WHERE item_id = ?", (item_id,)
        ).fetchone()
    )


def spare_bytes(post_id):
    return [p.read_bytes() for p in fields.spare_photos(post_id)]


def run(tmp_path, root, channel, chat, *flags):
    return main(
        ["klubok-photos", "--chat", str(chat), "--channel", str(channel), "--db",
         str(tmp_path / "база.sqlite"), "--root", str(root), *flags]
    )  # fmt: skip


def setup_pair(tmp_path, conn, root, n=7301):
    item_id = bare(conn, root, n)
    channel = channel_file(tmp_path, {n: (BASE, text_of(n))})
    chat = Chat(tmp_path)
    first = chat.forward("2026-11-01T10:00:05", text_of(n), 1)
    return item_id, channel, chat, first


def test_found_by_text_copied_in_time_order_and_queue_refreshed(tmp_path, conn, root, capsys):
    item_id, channel, chat, first = setup_pair(tmp_path, conn, root)
    assert ad_state(conn, item_id) == ("skipped", "klubok_no_photos")
    chat.reply(first, "2026-11-01T10:01:00", 3)
    chat.reply(first, "2026-11-01T10:00:30", 2)
    chat.reply(first, "2026-11-01T10:02:00", 3)  # те саме фото ще раз — один раз
    chat.messages[-1]["photo"] = chat.messages[-2]["photo"]
    folder = chat.save()

    code = run(tmp_path, root, channel, folder)

    out = capsys.readouterr().out
    assert code == 0 and "7301:3" in out and "Черга Клубку перерахована" in out
    files = fields.spare_photos(7301)
    assert [p.name for p in files] == ["1.jpg", "2.jpg", "3.jpg"]
    sources = [(folder / m["photo"]).read_bytes() for m in sorted(
        (m for m in chat.messages if m.get("photo")), key=lambda m: m["date"])][:3]  # fmt: skip
    assert [p.read_bytes() for p in files] == sources  # пересланий, 10:00:30, 10:01:00
    verify = db.connect(tmp_path / "база.sqlite")
    assert ad_state(verify, item_id) == ("queued", None)
    verify.close()


def test_two_similar_forwards_take_nearest_in_time(tmp_path, conn, root):
    bare(conn, root, 7301)
    channel = channel_file(tmp_path, {7301: (BASE, text_of(7301))})
    chat = Chat(tmp_path)
    far = chat.forward(
        "2026-11-01T10:08:00", text_of(7301), 5
    )  # ≤ 10 хв, але далі (і першим у файлі)
    late = chat.forward("2026-11-01T10:20:00", text_of(7301), 6)  # поза вікном
    near = chat.forward("2026-11-01T10:00:05", text_of(7301), 1)
    chat.reply(far, "2026-11-01T10:09:00", 7)
    chat.reply(late, "2026-11-01T10:21:00", 8)
    chat.reply(near, "2026-11-01T10:01:00", 9)
    folder = chat.save()

    assert run(tmp_path, root, channel, folder) == 0

    mine = [m for m in chat.messages if m["id"] == near or m.get("reply_to_message_id") == near]
    assert spare_bytes(7301) == [(folder / m["photo"]).read_bytes() for m in mine]
    assert len(mine) == 2


def test_forward_before_post_or_other_text_not_matched(tmp_path, conn, root, capsys):
    bare(conn, root, 7301)
    bare(conn, root, 7302)
    channel = channel_file(tmp_path, {7301: (BASE, text_of(7301)), 7302: (BASE, text_of(7302))})
    chat = Chat(tmp_path)
    chat.forward("2026-11-01T09:59:00", text_of(7301), 1)  # раніше за пост
    chat.forward("2026-11-01T10:00:05", text_of(7399), 2)  # чужий текст

    assert run(tmp_path, root, channel, chat.save()) == 0

    out = capsys.readouterr().out
    assert "пропущено 2 — в обговоренні не знайдено пересланого поста: 7301 7302" in out
    assert fields.spare_photos(7301) == [] and fields.spare_photos(7302) == []


def test_buyer_reply_with_photo_is_never_taken(tmp_path, conn, root, capsys):
    _item_id, channel, chat, first = setup_pair(tmp_path, conn, root)
    other = chat.forward("2026-11-01T10:00:09", text_of(9999), 4)  # інший пост: відповідь її
    chat.reply(other, "2026-11-01T10:05:00", 5)
    mine = chat.reply(first, "2026-11-01T10:01:00", 2)
    chat.reply(first, "2026-11-01T10:02:00", 3, who=BUYER)  # покупець
    folder = chat.save()

    assert run(tmp_path, root, channel, folder) == 0

    assert "7301:2" in capsys.readouterr().out
    expected = [
        (folder / m["photo"]).read_bytes() for m in chat.messages if m["id"] in (first, mine)
    ]
    assert spare_bytes(7301) == expected


def test_group_itself_counts_as_author(tmp_path, conn, root):
    _item_id, channel, chat, first = setup_pair(tmp_path, conn, root)
    chat.reply(first, "2026-11-01T10:01:00", 2, who=f"channel{GROUP}")

    assert run(tmp_path, root, channel, chat.save()) == 0

    assert len(fields.spare_photos(7301)) == 2


@pytest.mark.parametrize("case", ["channel", "not_group", "other_forward", "no_forward"])
def test_wrong_discussion_refused_nothing_written(tmp_path, conn, root, capsys, case):
    _item_id, channel, chat, _first = setup_pair(tmp_path, conn, root)
    if case == "not_group":
        chat.kind = "private_channel"
    if case == "other_forward":  # автопересилання з іншого каналу
        chat.messages.clear()
        chat.forward("2026-11-01T10:00:05", text_of(7301), 1, origin="channel42")
    if case == "no_forward":  # переслано вручну (від людини), а не автоматично
        chat.messages[0]["from_id"] = HER
    if case == "channel":  # Експорт каналу замість обговорення
        chat.kind = "private_channel"
    before = hashlib.sha256((tmp_path / "база.sqlite").read_bytes()).hexdigest()

    code = run(tmp_path, root, channel, chat.save())

    assert code == 2
    out = capsys.readouterr().out
    assert "група обговорення" in out or "автопереслано" in out
    assert fields.spare_photos(7301) == []
    assert hashlib.sha256((tmp_path / "база.sqlite").read_bytes()).hexdigest() == before


def test_wrong_channel_export_refused(tmp_path, conn, root, capsys):
    _item_id, channel, chat, _first = setup_pair(tmp_path, conn, root)
    assert run(tmp_path, root, channel, chat.save()) == 0  # id каналу запам'ятався
    folder = tmp_path / "інший"
    folder.mkdir()
    (folder / "result.json").write_text(
        json.dumps({"name": "Не той", "type": "private_channel", "id": 424242, "messages": []}),
        encoding="utf-8",
    )

    assert run(tmp_path, root, folder, chat.folder) == 2


def test_dry_run_writes_nothing(tmp_path, conn, root, capsys):
    _item_id, channel, chat, first = setup_pair(tmp_path, conn, root)
    chat.reply(first, "2026-11-01T10:01:00", 2)
    folder = chat.save()
    conn.close()
    before = hashlib.sha256((tmp_path / "база.sqlite").read_bytes()).hexdigest()

    assert run(tmp_path, root, channel, folder, "--dry-run") == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Пробний прогін") and any("7301:2" in line for line in out)
    assert len(out) <= 40
    assert fields.spare_photos(7301) == []
    assert not folders.klubok_spare(7301).exists()
    assert hashlib.sha256((tmp_path / "база.sqlite").read_bytes()).hexdigest() == before


def test_existing_photos_are_not_touched(tmp_path, conn, root, capsys):
    _item_id, channel, chat, first = setup_pair(tmp_path, conn, root)
    chat.reply(first, "2026-11-01T10:01:00", 2)
    mine = folders.klubok_spare(7301) / "1.jpg"
    mine.parent.mkdir(parents=True)
    make_photo(mine, (10, 10))
    before = mine.read_bytes()

    assert run(tmp_path, root, channel, chat.save()) == 0

    assert mine.read_bytes() == before and fields.spare_photos(7301) == [mine]
    assert "7301:" not in capsys.readouterr().out  # вже не кандидат


def test_post_missing_in_channel_export_is_reported(tmp_path, conn, root, capsys):
    bare(conn, root, 7301)
    channel = channel_file(tmp_path, {})  # поста немає (продано)
    chat = Chat(tmp_path)
    chat.forward("2026-11-01T10:00:05", text_of(7301), 1)

    assert run(tmp_path, root, channel, chat.save()) == 0

    assert "пропущено 1 — поста немає в Експорті каналу: 7301" in capsys.readouterr().out


def test_photo_limit_15(tmp_path, conn, root):
    _item_id, channel, chat, first = setup_pair(tmp_path, conn, root)
    for i in range(20):
        chat.reply(first, f"2026-11-01T10:{1 + i // 2:02d}:{i % 2 * 30:02d}", 10 + i)

    assert run(tmp_path, root, channel, chat.save()) == 0

    assert len(list(folders.klubok_spare(7301).glob("*.jpg"))) == chatphotos.PHOTOS_MAX == 15


def test_spare_photos_numeric_order_and_limit(tmp_path, conn, root):
    folder = folders.klubok_spare(7305)
    folder.mkdir(parents=True)
    for n in (10, 2, 1, 11, *range(3, 10), 12, 13, 14, 15, 16, 17):
        make_photo(folder / f"{n}.jpg", (20 + n, 20))
    (folder / "text.txt").write_text("x", encoding="utf-8")

    names = [p.name for p in fields.spare_photos(7305)]

    assert names == [f"{n}.jpg" for n in range(1, 16)]
    assert fields.spare_photos(None) == [] and fields.spare_photos(7399) == []


def test_ad_takes_all_spare_photos_when_card_photos_are_gone(tmp_path, conn, root):
    item_id = add(conn, root, 7301, status=Status.PUBLISHED, photos=False)
    folders.klubok_spare(7301).mkdir(parents=True)
    for n in (1, 2, 3):
        make_photo(folders.klubok_spare(7301) / f"{n}.jpg", (20 + n, 20))
    from shafa.review import load_card

    result = fields.ad(load_card(conn, item_id), root)

    assert [p.name for p in result.photos] == ["1.jpg", "2.jpg", "3.jpg"]
