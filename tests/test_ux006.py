"""UX-006: бот для Продавчині — прості тексти й зрозумілі кнопки. Підставний Telegram, тека tmp_path."""

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shafa import site
from shafa.bot import core
from shafa.bot.core import Bot
from shafa.bot.telegram import TelegramError
from test_bot_core import (
    CHANNEL,
    GROUP,
    OWNER,
    SELLER,
    Clock,
    FakeTelegram,
    Settings,
    Updates,
    comments,
    forwarded,
    message,
    press,
    published,
    send_post,
    state,
)

LINK = "https://t.me/c/2394827824/1401"


class Telegram(FakeTelegram):
    """+ збої на замовлення: відповідь каналу невизначена, файл фото не віддається."""

    def __init__(self):
        super().__init__()
        self.channel_error = None
        self.files_down = False

    def send_photo(self, chat_id, photo, caption, markup=None, entities=None):
        if chat_id == CHANNEL and self.channel_error:
            self._record("sendPhoto", chat_id=chat_id)
            raise self.channel_error
        return super().send_photo(chat_id, photo, caption, markup, entities)

    def get_file(self, file_id):
        if self.files_down:
            raise TelegramError("файл не віддається")
        return super().get_file(file_id)


@pytest.fixture
def tg():
    return Telegram()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def upd():
    return Updates()


@pytest.fixture
def bot(conn, tg, root, clock):
    settings = Settings(CHANNEL, SELLER, OWNER, root, GROUP)
    return Bot(conn, tg, settings, clock=clock, log=lambda text: None, sleep=lambda s: None)


def preview_markup(tg):
    return next(p["markup"] for m, p in tg.calls if m == "sendPhoto" and p["markup"])


# --- C3: кнопки прев'ю ---


def test_preview_buttons_new_labels_same_callbacks(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    rows = preview_markup(tg)["inline_keyboard"]
    assert [[(b["text"], b["callback_data"]) for b in row] for row in rows] == [
        [("✅ Опублікувати в канал", "pub:1")],
        [("Редагувати", "edit:1"), ("Скасувати", "cancel:1")],
        [("📦 Пост уже в каналі — лише на Шафу", "queue:1")],
    ]
    assert core.PUBLISH == "pub" and core.QUEUE == "queue" and core.CANCEL == "cancel"


def test_preview_text_tells_about_already_in_channel(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    text = tg.texts(SELLER)[-1]
    assert "Пост уже виклала в канал сама? Тоді «📦 Пост уже в каналі»." in text


def test_old_callback_still_works(conn, bot, tg, upd, clock):
    """Прев'ю зі старими підписами кнопок (callback_data той самий) публікується."""
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg, "pub")
    assert state(conn) == "published" and len(tg.channel_posts) == 1


# --- C30: невпевнено опубліковано ---


def test_uncertain_tells_what_to_do(conn, bot, tg, upd, clock):
    tg.channel_error = TelegramError("x", uncertain=True)
    send_post(bot, tg, upd, clock)
    press(upd, bot, tg)
    sent = tg.texts(SELLER)[-1]
    assert state(conn) == "uncertain"
    assert "Власник уже знає." in sent
    assert "Пост є — нічого не роби. Поста немає — надішли його мені ще раз." in sent


# --- C31, C37: «Опубліковано» ---


def test_done_has_shafa_date_from_wait_days(conn, bot, tg, upd, clock, monkeypatch):
    monkeypatch.setenv("SHAFA_AI_WAIT_DAYS", "3")
    published(bot, tg, upd, clock)
    sent = tg.texts(SELLER)[-1]
    posted = conn.execute("SELECT date FROM posts WHERE post_id = 1401").fetchone()[0]
    until = (datetime.fromisoformat(posted) + timedelta(days=3)).strftime("%d.%m.%Y")
    assert sent.endswith(f"На Шафу — не раніше {until}.")


def test_done_without_folder_says_photos_not_saved(conn, bot, tg, upd, clock, monkeypatch):
    def broken(self, post_id, photos):
        raise OSError("диск")

    monkeypatch.setattr(Bot, "_save_folder", broken)
    published(bot, tg, upd, clock)
    sent = tg.texts(SELLER)[-1]
    assert sent == (
        f"Опубліковано в каналі: {LINK}\n⚠️ Фото для Шафи зберегти не вдалося — "
        "Власник уже знає. Пост повторно не надсилай."
    )
    assert "Збережено фото для Шафи: 0" not in sent
    assert any("папку не створено" in t for t in tg.texts(OWNER))


# --- C32: фото не підійшло / не завантажилось ---


def test_bad_photo_asks_for_regular_photo(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    tg.files["f2"] = b"not a jpeg"
    press(upd, bot, tg)
    sent = tg.texts(SELLER)[-1]
    assert "як звичайне фото (не файлом)" in sent and "JPG" not in sent


def test_download_failure_says_retry_in_a_minute(conn, bot, tg, upd, clock):
    send_post(bot, tg, upd, clock)
    tg.files_down = True  # Telegram не віддає файл
    press(upd, bot, tg)
    sent = tg.texts(SELLER)[-1]
    assert state(conn) == "failed" and tg.channel_posts == []
    assert "спробуй ще раз за хвилину" in sent


# --- C33: задовгий текст ---


@pytest.mark.parametrize(
    ("over", "word"), [(1, "1 символ"), (3, "3 символи"), (11, "11 символів"), (25, "25 символів")]
)
def test_too_long_says_how_much_to_cut(conn, bot, tg, upd, clock, over, word):
    extra = core.caption_length(f"\n#VS_38_розмір\n\n{core.SIGNATURE}\n\n{core.SITE_TEXT}")
    start = "Кеди Nike 38 розмір. 800грн "
    length = core.CAPTION_MAX + over
    send_post(bot, tg, upd, clock, files=1, text=start + "я" * (length - extra - len(start)))
    sent = tg.texts(SELLER)[-1]
    assert f"скороти ще на {word} (" in sent
    assert "«Пишіть сюди…», «Більше взуття…», які я додаю сам" in sent


def test_symbols_plural():
    assert [core.symbols(n) for n in (1, 2, 5, 11, 12, 21, 22, 100, 101)] == [
        "1 символ", "2 символи", "5 символів", "11 символів", "12 символів", "21 символ",
        "22 символи", "100 символів", "101 символ",
    ]  # fmt: skip


# --- C34: фото в коментарях так і не додались ---


def test_comments_never_came_seller_and_owner_told_once(conn, bot, tg, upd, clock):
    published(bot, tg, upd, clock)
    clock.pass_(minutes=3)
    bot.tick()
    clock.pass_(hours=24)
    bot.tick()
    bot.tick()
    assert comments(conn) == "failed"
    assert tg.texts(SELLER)[-1] == (
        f"Фото в коментарі до поста {LINK} так і не додались — додай їх руками в коментарях до "
        "поста."
    )
    assert sum("так і не додались" in t for t in tg.texts(SELLER)) == 1
    assert sum("так і не додались" in t for t in tg.texts(OWNER)) == 1
    bot.handle(upd(message=forwarded(77, 1401)))
    assert tg.albums == []


# --- C35, C36: пост не знайдено, довідка ---


def test_queue_none_points_to_new_preview_button(conn, bot):
    assert "а під новим прев'ю натисни «📦 Пост уже в каналі»" in core.TEXTS["queue_none"]
    assert "а під новими прев'ю натисни «📦 Пост уже в каналі»" in core.TEXTS["queue_all_missing"]


def test_help_text(conn, bot, tg, upd, clock, monkeypatch):
    monkeypatch.setenv("SHAFA_AI_WAIT_DAYS", "10")
    bot.handle(upd(message=message(5, text="/start")))
    sent = tg.texts(SELLER)[-1]
    assert sent.startswith("Надішли фото (до 10) і текст пари — покажу, як буде в каналі.")
    assert "«✅ Опублікувати в канал»" in sent and "«📦 Пост уже в каналі»" in sent
    assert "без хештега" in sent and "Прев'ю живе 24 год." in sent
    assert "приблизно через 10 дн." in sent


def test_help_week_default(conn, bot):
    assert bot._wait_phrase() == "тиждень"


# --- C38: сайт ---


def test_site_giveup_text_for_seller():
    source = Path(site.__file__).read_text(encoding="utf-8")
    assert "Сайт Viktoria Shoes не прийняв пару з поста" in source
    assert "додай її в адмінці сайту" not in source and "кабінет сайту" in source


# --- Нема технічних слів у текстах для неї ---

TECH = re.compile(r"токен|вебхук|webhook|API|JSON|getUpdates|borh|адмінк|Bot API", re.IGNORECASE)
OWNER_ONLY = {"detail_on", "detail_off"}  # команда /detali — лише Власнику


def test_no_technical_words_in_seller_texts():
    bad = {k: v for k, v in core.TEXTS.items() if k not in OWNER_ONLY and TECH.search(v)}
    assert bad == {}


def test_every_text_still_formats():
    """Усі шаблони мають лише ті поля, які їм дає код (жоден не зник і не зламався)."""
    for key, text in core.TEXTS.items():
        names = set(re.findall(r"{(\w+)}", text))
        assert names <= {
            "n",
            "k",
            "m",
            "max",
            "wait",
            "over",
            "link",
            "why",
            "until",
            "date",
            "time",
            "tag",
            "comments",
            "numbers",
            "names",
            "done",
            "left",
        }, key
