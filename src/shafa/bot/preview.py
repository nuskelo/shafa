"""QA-005: `alerts-preview` — по зразку кожного виду повідомлень бота в ОДИН тестовий чат.

Влад запускає її сам із токеном тестового бота (`TELEGRAM_BOT_TOKEN` — лише зі змінної середовища,
`.env` пілоту не читається) і дивиться на телефоні, як це виглядає в Telegram. Дані вигадані: тимчасова
тека даних і база, тимчасова Головна папка, жодної мережі, крім Telegram; живу базу, її тривоги й стан
(`alerts-last*.json`) не чіпаємо. Тексти будує той самий код, що й справжній бот: `bot/alerts.run`
читає журнал подій і базу, `bot/sessions` складає підсумки — тут лише вигадані рядки в базі.
"""

import contextlib
import io
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageDraw

from shafa import db, events, fill
from shafa.bot import actions, alerts, sessions
from shafa.bot.telegram import Telegram, TelegramError
from shafa.robot import listings, lock, publish

TOKEN = "TELEGRAM_BOT_TOKEN"  # лише змінна середовища
PAUSE = 25.0  # с між правками живого повідомлення (Telegram: не частіше за `sessions.LIVE_EVERY`)
EDITS = 3  # скільки разів живе повідомлення оновлюється
PHOTOS_FOR_DUPLICATE = 1301  # номер поста Картки-дубля


FIELDS = (
    "Матеріал підкладки", "Довжина устілки", "Сезон", "Тип підошви", "Висота підборів",
    "Застібка", "Стиль", "Колір", "Бренд", "Розмір", "Стан", "Мембрана", "Призначення",
    "Матеріал верху", "Країна виробник", "Висота халяви", "Тип каблука", "Платформа",
    "Принт", "Декор", "Вік", "Стать", "Ширина колодки", "Назва",
)  # fmt: skip
# Довгі й різні тексти Проблем (кожен — власне поле, без повторів у рядку): підсумок > 4096 знаків
LONG_REASONS = [
    f"Не знайдено поле «{name}» на формі Шафи: схоже, форма змінилась; зупиніться й покажіть "
    "знімок екрана Власнику"
    for name in FIELDS
]


class PreviewError(Exception):
    """Неможливо зробити перегляд: немає токена чи чата."""


class Shim:
    """Те, чого потребують `actions.deliver` і `sessions.Live` від бота: `say`, `tg`, `conn`, `settings`."""

    def __init__(self, tg: Telegram, conn: sqlite3.Connection, root: Path, log: Callable) -> None:
        self.tg, self.conn, self.log = tg, conn, log
        self.settings = SimpleNamespace(root=root)

    def say(
        self,
        chat_id: int | None,
        text: str,
        markup: dict | None = None,
        silent: bool = False,
        entities: list[dict] | None = None,
    ) -> dict | None:
        try:
            return self.tg.send_message(
                chat_id, text, markup, silent=silent, entities=entities or None
            )
        except TelegramError as error:
            self.log(f"Не вдалося надіслати: {error}")
            return None


@contextlib.contextmanager
def isolated_environment(data_dir: Path, owner: bool) -> Iterator[None]:
    """Тека даних — тимчасова (інакше `events.emit`, `alerts`, `robot-now.json` пішли б у справжню);
    тривоги — як для Власника чи для Вікторії; ліміт ШІ — відомий."""
    names = ("SHAFA_DATA_DIR", "SHAFA_ALERTS", "SHAFA_ALERTS_TO", "SHAFA_AI_DAILY_USD")
    saved = {name: os.environ.get(name) for name in names}
    os.environ["SHAFA_DATA_DIR"] = str(data_dir)
    os.environ.pop("SHAFA_ALERTS", None)
    os.environ["SHAFA_ALERTS_TO"] = "owner" if owner else "both"
    os.environ["SHAFA_AI_DAILY_USD"] = "3"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def placeholder(label: str, color: tuple[int, int, int]) -> bytes:
    """Фото-заглушка: кольоровий прямокутник із підписом (справжніх фото тут немає й не буде)."""
    image = Image.new("RGB", (640, 480), color)
    ImageDraw.Draw(image).text((40, 220), label, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


class Preview:
    def __init__(
        self,
        tg: Telegram,
        chat: int,
        conn: sqlite3.Connection,
        data_dir: Path,
        root: Path,
        *,
        owner: bool,
        pause: float,
        sleep: Callable[[float], None],
        out: Callable[[str], None],
    ) -> None:
        self.tg, self.chat, self.conn, self.data_dir, self.root = tg, chat, conn, data_dir, root
        self.owner, self.pause, self.sleep, self.out = owner, pause, sleep, out
        self.shim = Shim(tg, conn, root, out)
        self.live = sessions.Live(self.shim, chat)
        self.post = 7000

    # --- обв'язка ---

    def say(self, text: str) -> None:
        """Підпис зразка: тихо, щоб не плутати з повідомленнями бота."""
        self.tg.send_message(self.chat, text, silent=True)

    def step(self) -> int:
        """Один цикл тривог для чату — те саме, що бот робить раз на 20 с."""

        def send(text: str, markup=None, album_of=None, silent=False, entities=None) -> bool:
            return actions.deliver(self.shim, self.chat, text, markup, album_of, silent, entities)

        return alerts.run(
            self.conn, self.data_dir, datetime.now().astimezone(), send, live=self.live,
            owner=self.owner,
        )  # fmt: skip

    def seen(self) -> None:
        """Проблеми, що вже лежать у базі, — «відомі»: окремого повідомлення про них не буде
        (їх покаже підсумок сесії); нове повідомлення про Проблему — лише в зразку «дубль»."""
        state = alerts.load(self.data_dir) or {}
        if state:
            state["problems"] = alerts._problems_now(self.conn)
            alerts.save(self.data_dir, state)

    # --- вигадані рядки бази ---

    def number(self) -> int:
        self.post += 1
        return self.post

    def session(self, site: str = "shafa", **fields: Any) -> int:
        row = {"started_at": db.now(), "site": site, **fields}
        names = ", ".join(row)
        with self.conn:
            cursor = self.conn.execute(
                f"INSERT INTO sessions ({names}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
        return int(cursor.lastrowid or 0)

    def item(self, status: str, session: int | None = None, **fields: Any) -> int:
        post = self.number()
        row = {
            "folder_name": str(post), "post_id": post, "status": status, "created_at": db.now(),
            "updated_at": db.now(), "session_id": session, **fields,
        }  # fmt: skip
        with self.conn:
            cursor = self.conn.execute(
                f"INSERT INTO items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
        return int(cursor.lastrowid or 0)

    def published(self, session: int, count: int) -> None:
        for _ in range(count):
            post = self.number()
            self.item(
                "published", session, folder_name=str(post), post_id=post,
                shafa_url=f"https://shafa.ua/uk/item/{post}-zrazok", published_at=db.now(),
            )  # fmt: skip

    def problems(self, session: int, codes: list[tuple[str, str]]) -> None:
        for code, text in codes:
            self.item("problem", session, problem_code=code, problem_text=text)

    def end(self, session: int, code: str, reason: str, published: int, problems: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sessions SET ended_at = ?, stop_code = ?, stop_reason = ?, published = ?, "
                "problems = ? WHERE id = ?",
                (db.now(), code, reason, published, problems, session),
            )

    # --- зразки ---

    def shafa_live(self) -> int:
        """Початок сесії Шафи й живе повідомлення: надсилається тихо, далі лише правиться."""
        self.say("1/9 · Початок сесії Шафи і живе повідомлення (3 правки з паузою)")
        session = self.session()
        events.emit(
            "robot.start",
            "Сесія почалась",
            data={"session": session, "queue": 30, "old": 10, "left": 30},
        )
        with lock.hold("publish"):
            for handled, post in ((3, 7431), (9, 7437), (15, 7443), (22, 7450)):
                publish.write_now(
                    session, "fill", plan=30, handled=handled, post=post,
                    title="Шкіряні черевики Ecco",
                )  # fmt: skip
                self.step()
                if handled != 22:
                    self.sleep(self.pause)
            self.out("   живе повідомлення оновлено 3 рази")
        publish.clear_now()
        self._summary_session(session)
        return session

    def _summary_session(self, session: int) -> None:
        self.say("2/9 · Підсумок сесії Шафи: розгортаємі блоки, посилання, «на що глянути»")
        self.published(session, 12)
        self.problems(
            session,
            [
                ("robot_field_missing", "Не знайшов поле «Матеріал підкладки»"),
                ("possible_duplicate", "Схоже на вже виставлене оголошення"),
            ],
        )
        self.seen()
        for _ in range(2):  # продане: пост видалено з каналу за час сесії
            self.item(
                "archived", session=None, problem_code="post_deleted",
                archived_at=db.now(), problem_text="Пост видалено з каналу — продано",
            )  # fmt: skip
        self.end(session, "done", "черга закінчилась", 12, 2)
        events.emit(
            "robot.end", "Сесія закінчилась",
            data={"session": session, "deferred": [7432], "notes": ["Прибрано фото старих постів: 4"]},
        )  # fmt: skip
        self.step()

    def long_summary(self) -> None:
        self.say("3/9 · Довгий підсумок (> 4096 знаків): ділиться на кілька повідомлень")
        session = self.session()
        self.published(session, 130)
        self.problems(session, [("robot_field_missing", why) for why in LONG_REASONS])
        self.seen()
        self.end(session, "limit", "ліміт доби", 130, 24)
        events.emit("robot.end", "Сесія закінчилась", data={"session": session})
        self.step()

    def klubok(self) -> None:
        self.say("4/9 · Клубок: початок сесії і підсумок")
        session = self.session("klubok")
        events.emit("klubok.start", "Клубок почав", data={"session": session, "limit": 10})
        self.step()
        for number in range(5):
            item = self.item("published")
            post = self.number()
            with self.conn:
                self.conn.execute("UPDATE items SET post_id = ? WHERE id = ?", (post, item))
                self.conn.execute(
                    "INSERT INTO klubok_ads (item_id, state, created_at, updated_at, session_id, "
                    "published_at, url, klubok_id) VALUES (?, 'published', ?, ?, ?, ?, ?, ?)",
                    (item, db.now(), db.now(), session, db.now(),
                     f"https://klubok.com/item-zrazok-{number}", 37_000_000 + number),
                )  # fmt: skip
        problem = self.item("published")
        with self.conn:
            self.conn.execute(
                "INSERT INTO klubok_ads (item_id, state, created_at, updated_at, session_id, "
                "problem_code, problem_text) VALUES (?, 'problem', ?, ?, ?, 'klubok_field_missing', "
                "'У списку категорій немає «Детское»')",
                (problem, db.now(), db.now(), session),
            )
        self.seen()
        self.end(session, "done", "черга закінчилась", 5, 1)
        events.emit("klubok.end", "Клубок закінчив", data={"session": session})
        self.step()

    def stops(self) -> None:
        for number, (site, code, reason, published) in enumerate(
            (
                ("shafa", "login", "Шафа розлогінила", 3),
                ("klubok", "klubok_captcha", "Клубок просить пройти перевірку (капча)", 2),
                ("shafa", "telegram_busy", "Telegram не відповідає більше 20 хв", 1),
            ),
            start=5,
        ):
            self.say(f"{number}/9 · ⛔ Зупинка: {code}")
            session = self.session(site)
            if site == "shafa":
                self.published(session, published)
            self.end(session, code, reason, published, 0)
            events.emit(f"{'robot' if site == 'shafa' else 'klubok'}.stop", reason,
                        data={"session": session})  # fmt: skip
            self.step()

    def duplicate(self) -> None:
        self.say("8/9 · Нова Проблема: дубль із фото (кнопки рішень — лише для Власника)")
        item = self.item(
            "problem", None, problem_code="possible_duplicate",
            problem_text="Схоже: https://shafa.ua/uk/item/12345-zrazok — ШІ не впевнений",
            folder_name=str(PHOTOS_FOR_DUPLICATE), post_id=PHOTOS_FOR_DUPLICATE,
        )  # fmt: skip
        folder = self.root / str(PHOTOS_FOR_DUPLICATE)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "1.jpg").write_bytes(placeholder("ФОТО-ЗАГЛУШКА · наша пара", (90, 60, 120)))
        with self.conn:
            self.conn.execute(
                "INSERT INTO photos (item_id, position, file_name) VALUES (?, 1, '1.jpg')", (item,)
            )
        other = self.data_dir / listings.PHOTOS_DIR
        other.mkdir(parents=True, exist_ok=True)
        (other / f"{item}.jpg").write_bytes(
            placeholder("ФОТО-ЗАГЛУШКА · схоже оголошення", (30, 90, 70))
        )
        self.step()

    def background(self) -> None:
        self.say("9/9 · ШІ почав/закінчив і «🕓 Фонові події» (тихі події збираються в одне)")
        planned = [SimpleNamespace(folder_name=str(i), estimate=SimpleNamespace(cost_micro_usd=52_000))
                   for i in range(30)]  # fmt: skip
        planned[:10] = [SimpleNamespace(folder_name=f"borh-{i}", estimate=planned[0].estimate)
                        for i in range(10)]  # fmt: skip
        events.emit("ai.start", fill.start_text(planned))  # type: ignore[arg-type]
        events.emit("post.new", "Новий пост 7027: 5 фото, розмір 38 → чекає ШІ")
        events.emit("post.changed", "1342: текст змінено → знову «На перевірку»")
        events.emit("site.sent", "7020: відправлено на сайт (поля + 5 фото)")
        events.emit("site.night", "Ніч: перевірено 61, зникло з каналу 2 (6801, 6806)")
        events.emit("photos.purged", "Прибрано фото 4 постів (≈ 12 МБ)")
        for number in range(7030, 7040):
            events.emit("post.new", f"Новий пост {number}: 3 фото, розмір 39 → чекає ШІ")
        tally = fill.Tally(ready=7, yellow=2, problems=1, cost_micro=410_000)
        events.emit("ai.done", fill.tally_text(self.conn, tally), loud=True)
        self.step()


def run(
    token: str,
    chat: int,
    *,
    telegram: Callable[[str], Telegram] = Telegram,
    owner: bool = True,
    pause: float = PAUSE,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    """Надіслати в `chat` зразки всіх повідомлень. Повертає, скільки секунд це зайняло."""
    if not token.strip():
        raise PreviewError(f"Немає {TOKEN} у змінній середовища (токен тестового бота).")
    if pause < sessions.LIVE_EVERY.total_seconds():
        raise PreviewError("Пауза між правками живого повідомлення — не менше 20 с.")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="shafa-preview-") as tmp:
        data_dir, root = Path(tmp) / "дані", Path(tmp) / "Шафа"
        data_dir.mkdir()
        root.mkdir()
        tg = telegram(token)
        with isolated_environment(data_dir, owner):
            conn = db.connect(data_dir / "preview.sqlite")
            try:
                show = Preview(
                    tg, chat, conn, data_dir, root, owner=owner, pause=pause, sleep=sleep, out=out
                )
                tg.send_message(
                    chat,
                    "🧪 Перегляд повідомлень бота: усе нижче — вигадані дані, нічого справжнього "
                    f"не відбувається. Вид: {'Власник' if owner else 'Вікторія'}. "
                    "Кнопки під Проблемою не працюють (бот не слухає).",
                    silent=True,
                )
                show.step()  # перший цикл лише запам'ятовує стан (як у справжньому боті)
                show.shafa_live()
                show.long_summary()
                show.klubok()
                show.stops()
                show.duplicate()
                show.background()
                show.say("✅ Перегляд закінчено.")
            finally:
                conn.close()
                with contextlib.suppress(Exception):
                    tg.close()
    return int(time.monotonic() - started)
