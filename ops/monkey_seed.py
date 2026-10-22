"""QA-004: тестові дані для «мавпи» — база й фото-заглушки в тимчасовій теці (не в репозиторії).

Усе вигадане: тексти, фото (кольорові прямокутники), номери. Жодних справжніх даних Продавчині.
Картки всіх статусів і всіх кодів Проблем; багато Схвалених для робота; кілька «пасток»:
HTML у тексті, порожнє й відсутнє фото, дуже довгий текст, кирилиця в назві файлу.
"""

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from shafa import ai, db, fill, review
from shafa.db import Status
from shafa.robot import listings as listings_mod
from shafa.robot.publish import card_listing

BOOTS = "Жіночий одяг / Взуття / Черевики"
SNEAKERS = "Чоловічий одяг / Взуття / Кросівки"
BRANDS = ("Ecco", "Geox", "Rieker", "Nike", "Adidas", "Clarks", "Timberland", "Salomon")
HTML_TRAP = '<script>window.__xss=1</script> "><img src=x onerror="window.__xss=2"> & \' <b>'

# Усі коди Проблем, які знає екран (`web.PROBLEM_KINDS`), роботові (`robot_*`) і невідомий.
PROBLEM_CODES = (
    "bad_folder_name", "duplicate_post", "post_not_found", "no_text", "no_price", "many_items",
    "size_gone", "no_section", "no_photos", "too_many_photos", "folder_missing", "ai_error",
    "stale_rules", "possible_duplicate", "already_published", "publish_uncertain",
    "publish_error", "shafa_declined", "photos_missing", "robot_photos", "post_deleted",
    "robot_field_missing", "robot_slow", "robot_not_sent", "robot_logged_out",
    "robot_brand_missing", "robot_not_ready", "robot_retry", "robot_publish_blocked",
    "some_future_code",
)  # fmt: skip
WITH_SHAFA_ID = ("shafa_declined", "already_published")  # у таких Картки вже є товар на Шафі
NOTE_CODES = review.NOTE_CODES  # примітки на Картці «На перевірку»

ENV = """# QA-004: порожні налаштування — без ключа ШІ, токена бота, ключа сайту
SHAFA_DAILY_LIMIT=100
SHAFA_DAILY_NEW=30
SHAFA_DAILY_OLD=0
SHAFA_ROOT_DIR={root}
"""


def blank(title: str, section: str = BOOTS, brand: str = "Ecco", size: str = "38") -> list:
    """Бланк, який робот приймає (як `tests/test_robot_plan.BLANK`)."""
    return [
        ai.Field("title", title),
        ai.Field("section", section),
        ai.Field("sizes", [size]),
        ai.Field("colors", ["Чорний"]),
        ai.Field("brand", brand),
        ai.Field("material_upper", ["Шкіра"]),
        ai.Field("material_lining", ["Хутро"]),
        ai.Field("season", ["Зима"]),
        ai.Field("type", "Челсі"),
        ai.Field("style", "Повсякденний"),
        ai.Field("keywords", ["черевики", "черевики жіночі", f"черевики {brand}", "черевики 38"]),
        ai.Field("photo_order", ["IMG_1.JPG", "IMG_2.JPG"]),
    ]


def text_for(brand: str, size: str, price: int = 1200, extra: str = "") -> str:
    return (
        f"Шкіряні черевики {brand} {size} розміру. На хутрі. Устілка 24,5 см. "
        f"В ідеальному стані. {price}грн {extra}".strip()
    )


def make_photo(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")


class Seed:
    """Будує базу й Головну папку. Ідентифікатори постів ростуть з 5000."""

    def __init__(self, data_dir: Path, root: Path, seed: int) -> None:
        self.data_dir, self.root = data_dir, root
        self.random = random.Random(seed)
        self.conn = db.connect(data_dir / "shafa.sqlite")
        self.post = 5000
        self.items: dict[str, list[int]] = {}
        self.corrupt = 0

    def close(self) -> None:
        self.conn.close()

    def add(
        self,
        status: str,
        *,
        text: str | None = None,
        fields: list | None = None,
        photos: tuple[str, ...] = ("IMG_1.JPG", "IMG_2.JPG"),
        code: str | None = None,
        reason: str | None = None,
        shafa_id: int | None = None,
        days_ago: int = 20,
        folder: str | None = None,
        broken: tuple[str, ...] = (),
    ) -> int:
        """Картка з постом, фото в папці (`broken` — порожній файл), бланком, правками."""
        self.post += 1
        post_id = self.post
        folder = folder or str(post_id)
        brand = BRANDS[post_id % len(BRANDS)]
        size = str(36 + post_id % 9)
        text = text or text_for(brand, size)
        stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        for number, name in enumerate(photos):
            target = self.root / folder / name
            if name in broken:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"")
            else:
                shade = 60 + (post_id * 7 + number * 40) % 150
                make_photo(target, (64 + number * 16, 48), (shade, 90, 200 - shade // 2))
        with self.conn:
            self.conn.execute(
                "INSERT INTO posts VALUES (?, ?, ?, 'export', ?)", (post_id, stamp, text, stamp)
            )
            item_id = self.conn.execute(
                "INSERT INTO items (folder_name, post_id, status, problem_code, problem_text, "
                "created_at, updated_at, shafa_id, shafa_url, published_at, publish_started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    folder, post_id, status, code, reason, stamp, stamp, shafa_id,
                    f"https://shafa.ua/uk/women/obuv/{shafa_id}" if shafa_id else None,
                    stamp if status == Status.PUBLISHED else None,
                    stamp if status == Status.PUBLISHED else None,
                ),
            ).lastrowid  # fmt: skip
            self.conn.executemany(
                "INSERT INTO photos VALUES (?, ?, ?)",
                [(item_id, i, n) for i, n in enumerate(photos, start=1)],
            )
            if fields is None and status not in (Status.NEW,):
                fields = blank(f"Шкіряні черевики {brand} {size} розміру", brand=brand, size=size)
            if fields:
                fill.save_blank(self.conn, item_id, fields)
        self.items.setdefault(str(status), []).append(item_id)
        return item_id

    def sessions(self) -> None:
        """Сесії з різними причинами зупинки (екран «Сесії», колір за `stop_code`)."""
        rows = [
            ("done", "черга закінчилась"), ("limit", "ліміт доби (30) вичерпано"),
            ("stop", "Стоп від користувача"), ("login", "немає входу на Шафу"),
            ("streak", "3 Картки поспіль з помилкою"), ("shafa_balance", "від'ємний баланс"),
            ("error", "збій: TimeoutError"), (None, None),
        ]  # fmt: skip
        with self.conn:
            for number, (code, why) in enumerate(rows):
                start = datetime.now(UTC) - timedelta(days=number + 1, hours=1)
                self.conn.execute(
                    "INSERT INTO sessions (started_at, ended_at, published, problems, stop_reason, "
                    "stop_code) VALUES (?, ?, 0, 0, ?, ?)",
                    (
                        start.isoformat(timespec="seconds"),
                        (start + timedelta(minutes=20)).isoformat(timespec="seconds"),
                        why, code,
                    ),
                )  # fmt: skip

    def build(self, approved: int) -> None:
        """Усе, що екран має вміти показати."""
        # На перевірку: звичайні, жовті, з правками й «пастками».
        for _ in range(6):
            self.add(Status.REVIEW)
        yellow = blank("Кеди Nike 42")
        yellow = [
            ai.Field(f.name, f.value, True, "ШІ не впевнене") if f.name in ("brand", "type") else f
            for f in yellow
        ]
        self.add(Status.REVIEW, fields=yellow)
        trap = self.add(
            Status.REVIEW,
            text=text_for("Ecco", "38", extra=HTML_TRAP),
            fields=blank(f"Черевики {HTML_TRAP}"),
        )
        self.add(Status.REVIEW, text=text_for("Geox", "39", extra="Довгий опис. " * 300))
        self.add(Status.REVIEW, folder="Фото Ольги 5", photos=("світлина-1.JPG", "Ф2.jpg"))
        self.corrupt = self.add(Status.REVIEW, broken=("IMG_2.JPG",))  # порожній файл фото
        self.add(Status.REVIEW, photos=("IMG_1.JPG",) + tuple(f"IMG_{n}.JPG" for n in range(2, 11)))
        gone = self.add(Status.REVIEW)
        for note in NOTE_CODES:  # примітки «пост змінено», «сезон змінено» на Картці
            self.add(Status.REVIEW, code=note, reason=f"Примітка {note}: було → стало")
        with self.conn:
            self.conn.execute(
                "INSERT INTO edits VALUES (?, 'title', ?, 'x')",
                (trap, json.dumps('Правка назви <b>та</b> "лапки"')),
            )
            self.conn.execute("DELETE FROM photos WHERE item_id = ?", (gone,))  # Картка без фото
        # Нові (чекають ШІ), без бланка.
        for _ in range(3):
            self.add(Status.NEW, fields=[])
        # Проблеми — усі коди; з бланком і без.
        for number, code in enumerate(PROBLEM_CODES):
            self.add(
                Status.PROBLEM,
                code=code,
                reason=f'Текст проблеми {code}: <i>не HTML</i> & "лапки"',
                fields=[] if number % 2 else None,
                shafa_id=800_000_000 + number if code in WITH_SHAFA_ID else None,
            )
        self.add(Status.PROBLEM, code=None, reason=None, fields=[])  # Проблема без коду
        # Схвалені: невеликий хвіст для екрана + пул для робота (потім `pool`).
        self.add(Status.APPROVED, fields=blank("Кросівки Nike 42", SNEAKERS, "Nike", "42"))
        for _ in range(approved):
            self.add(Status.APPROVED)
        # Опубліковані, архів, «publishing» (обрив посеред публікації).
        for number in range(8):
            self.add(Status.PUBLISHED, shafa_id=700_000_000 + number, days_ago=number)
        self.add(Status.PUBLISHED, shafa_id=700_000_100, code="duplicate_checked",
                 reason="Схоже: https://shafa.ua/uk/x/1 — ШІ: інша пара")  # fmt: skip
        for number in range(3):
            archived = self.add(Status.ARCHIVED, code="possible_duplicate" if number else None)
            with self.conn:
                self.conn.execute(
                    "UPDATE items SET archived_at = ? WHERE id = ?",
                    ((datetime.now(UTC) - timedelta(days=number)).isoformat(), archived),
                )
        self.add(Status.PUBLISHING)
        self.sessions()

    def pool(self, similar: int = 4) -> list[int]:
        """Схвалені для робота: «Це інший товар» (щоб однакові назви не були дублями), крім
        `similar` Карток — їм у знімку «Мої речі» стоїть такий самий товар (`possible_duplicate`)."""
        approved = self.items[str(Status.APPROVED)][1:]
        stamp = db.now()
        picked = approved[:: max(1, len(approved) // max(similar, 1))][:similar]
        with self.conn:
            for item_id in approved:
                if item_id not in picked:
                    self.conn.execute(
                        "UPDATE items SET duplicate_ok_at = ? WHERE id = ?", (stamp, item_id)
                    )
        snapshot(self.conn, picked)
        return picked


def snapshot(conn, picked: list[int]) -> None:
    """Знімок «Мої речі»: у кожної з `picked` уже є такий самий товар. Без знімка робот читав би
    кабінет, якого в підставній Шафі немає — тож знімок свіжий (а «Перечитати зараз» його стирає)."""
    found = []
    for number, item_id in enumerate(picked):
        card = review.load_card(conn, item_id)
        found.append(card_listing(card, 600_000_000 + number, f"https://shafa.ua/x/{number}"))
    listings_mod.store(conn, found or [_stub_listing()])


def _stub_listing() -> listings_mod.Listing:
    return listings_mod.Listing(1, "Чужа річ", "Zara", "zhenskaya-odezhda", ("S",), "https://x/1")


def prepare(base: Path, seed: int, approved: int = 140) -> tuple[Path, Path, dict]:
    """Тека даних, Головна папка й база в `base`. Повертає (data_dir, root, зведення)."""
    data_dir, root = base / "data", base / "Шафа"
    data_dir.mkdir(parents=True)
    root.mkdir()
    (data_dir / ".env").write_text(ENV.format(root=root), encoding="utf-8")
    seeded = Seed(data_dir, root, seed)
    try:
        seeded.build(approved)
        picked = seeded.pool()
        info = {
            "items": {status: len(ids) for status, ids in seeded.items.items()},
            "similar": picked,
            "corrupt": seeded.corrupt,
        }
    finally:
        seeded.close()
    return data_dir, root, info
