"""SITE-001: нові пари з бота → сайт. Лише підставний сервер сайту (`MockTransport`), без мережі."""

import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from PIL import Image

from shafa import config, db, folders, site
from shafa.site import Notify, Site, Worker

KEY = "sk-site-SECRET-123"
BASE = "https://site.test/api/integration/v1"
POSTED = "2026-10-17T12:30:00+03:00"
TEXT = (
    "Шкіряні черевики Ecco 38 розміру в ідеальному стані. Устілка 24,5см. 1450грн\n"
    "#VS_38_розмір\n\nПишіть сюди 👉 @viktoriaviktoriatg\n\nВсі фото в коментарях ⬇️"
)
SUBS = [
    {"id": "sub-women-winter", "name": "Зимове взуття", "categoryName": "Жіноче взуття"},
    {"id": "sub-kids-summer", "name": "Літнє взуття", "categoryName": "Дитяче взуття"},
]


def jpeg(size=(60, 40), color="red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def pass_(self, **delta):
        self.now += timedelta(**delta)


class FakeSite:
    """Підставний сайт: пари й фото в пам'яті, відповіді на замовлення (`plan`)."""

    def __init__(self):
        self.requests = []  # (метод, шлях, заголовок ключа)
        self.plan = []  # (метод, шлях, відповідь або клас винятку httpx2)
        self.products = {}
        self.photos = {}
        self.warnings = []
        self.forget = False  # пара «зникає» одразу після створення (404 на фото завжди)
        self.sold_api = True  # API-023 розгорнуто; False — 404 на маршрути «Продано» і списку
        self.listed = set()  # старі пари сайту (не наші), номери постів
        self.sold = set()

    def fail(self, method, path, outcome):
        self.plan.append((method, path, outcome))

    def __call__(self, request):
        path = request.url.path.removeprefix("/api/integration/v1")
        self.requests.append((request.method, path, request.headers.get("X-Integration-Key")))
        for i, (method, wanted, outcome) in enumerate(self.plan):
            if request.method == method and path == wanted:
                del self.plan[i]
                if isinstance(outcome, type):
                    raise outcome("зв'язку немає", request=request)
                return outcome
        if path == "/subcategories":
            return httpx2.Response(200, json=SUBS)
        if path == "/products/post-ids" or path.endswith("/sold"):
            return self.sold_route(request.method, path)
        parts = path.split("/")  # ['', 'products', 'by-post', '1401', 'photos', '0']
        post = int(parts[3])
        if len(parts) == 4:
            created = post not in self.products
            self.products.setdefault(post, json.loads(request.content))
            if self.forget:
                del self.products[post]
            body = {"id": "x", "postId": post, "status": "Hidden", "created": created,
                    "warnings": self.warnings}  # fmt: skip
            return httpx2.Response(201 if created else 200, json=body)
        if post not in self.products:
            return httpx2.Response(404, json={})
        index = int(parts[5])
        replaced = (post, index) in self.photos
        self.photos[(post, index)] = request.content
        return httpx2.Response(200, json={"index": index, "path": "/i.jpg", "replaced": replaced})

    def sold_route(self, method, path):
        if not self.sold_api:
            return httpx2.Response(404)  # маршруту немає — без тіла
        if path == "/products/post-ids":
            ids = sorted((self.listed | set(self.products)) - self.sold)
            return httpx2.Response(200, json={"postIds": ids, "count": len(ids)})
        assert method == "POST"
        post = int(path.split("/")[3])
        if post not in self.listed and post not in self.products:
            return httpx2.Response(404, json={"postId": post, "error": "not_found"})
        changed = post not in self.sold
        self.sold.add(post)
        body = {"id": "x", "postId": post, "status": "Sold", "changed": changed}
        return httpx2.Response(200, json=body)

    def paths(self):
        return [f"{m} {p}" for m, p, _ in self.requests]

    def sent(self):
        """Запити про пари (без нічного списку пар сайту)."""
        return [p for p in self.paths() if p != "GET /products/post-ids"]


class FakeAi:
    """Підставний клієнт Claude: `answer` — JSON-відповідь, `error` — виняток на запит."""

    def __init__(self, answer=None):
        self.answer = answer or {
            "subcategoryId": "sub-women-winter", "brand": "Ecco", "sizes": ["38"],
            "size_system": "EU",
        }  # fmt: skip
        self.error = None
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        usage = SimpleNamespace(
            input_tokens=900, output_tokens=60, cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )  # fmt: skip
        text = SimpleNamespace(type="text", text=json.dumps(self.answer, ensure_ascii=False))
        return SimpleNamespace(stop_reason="end_turn", content=[text], usage=usage)


@pytest.fixture
def fake():
    return FakeSite()


@pytest.fixture
def ai_fake():
    return FakeAi()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def said():
    return {"owner": [], "seller": [], "log": []}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(site.KEY, KEY)
    monkeypatch.setenv(site.SEND_SETTING, "1")
    monkeypatch.setenv(site.WAIT_SETTING, "0")  # одразу, як до SITE-002; чекання — test_site_wait


def make_worker(conn, root, fake, ai_fake, clock, said):
    notify = Notify(said["owner"].append, said["seller"].append)
    return Worker(
        conn, root, notify,
        site_for=lambda key: Site(key, httpx2.Client(transport=httpx2.MockTransport(fake)), BASE),
        ai_client=lambda: ai_fake, clock=clock, log=said["log"].append,
    )  # fmt: skip


@pytest.fixture
def worker(conn, root, fake, ai_fake, clock, said):
    return make_worker(conn, root, fake, ai_fake, clock, said)


def add_post(conn, root, post_id=1401, photos=3, text=TEXT):
    folder = root / str(post_id)
    folder.mkdir()
    for i in range(1, photos + 1):
        (folder / f"{i}.jpg").write_bytes(jpeg(color=("red", "green", "blue", "white")[i % 4]))
    assert site.enqueue(conn, post_id, folder.name, POSTED, text)


def job(conn, post_id=1401):
    return conn.execute("SELECT * FROM site_jobs WHERE post_id = ?", (post_id,)).fetchone()


def run_until_idle(worker, steps=30):
    for _ in range(steps):
        if worker.step() == site.IDLE:
            return


# --- Поля ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Черевики в ідеальному стані", "Ідеальний"),
        ("Кеди у відмінному стані", "Відмінний"),
        ("Кросівки нові, з бірками", "Новий"),
        ("Туфлі в стані нових", "Новий"),
        ("Чоботи в хорошому стані", "Хороший"),
        ("Стан: ідеальний", "Ідеальний"),
        ("Кеди Nike 38 розмір", None),
        ("Відправлю Новою поштою. Нова пошта", None),
        ("Ідеально підходять до джинсів", None),
    ],
)
def test_condition_only_from_text(text, expected):
    assert site.condition(text) == expected


def test_code_fields_price_as_in_telegram_insole_and_text_without_service_lines():
    fields = site.code_fields(TEXT, POSTED)

    assert fields == {
        "title": None, "description": TEXT.split("\n\nПишіть")[0], "postedAt": POSTED,
        "price": 1450, "lengthSm": "24.5", "condition": "Ідеальний",
    }  # fmt: skip


@pytest.mark.parametrize("site_line", ["🛍 Більше взуття на сайті 👈", "🛍 Більше взуття на сайті"])
@pytest.mark.parametrize(
    "comments",
    ["Додаткові фото в коментарях ⬇️", "Більше фото в коментарях ⬇️", "Всі фото в коментарях ⬇️"],
)
def test_site_description_drops_site_and_comments_lines(site_line, comments):
    """TD-049: підпис поста бота не потрапляє в опис для сайту."""
    text = f"Кеди Nike 38 розмір. 800грн\n#VS_38_розмір\n\nПишіть сюди 👉 @x\n\n{site_line}\n\n{comments}"

    assert (
        site.code_fields(text, POSTED)["description"]
        == "Кеди Nike 38 розмір. 800грн\n#VS_38_розмір"
    )


def test_code_fields_without_price_and_state_sends_nothing_for_them():
    fields = site.code_fields("Кеди Nike 38 розмір", POSTED)

    assert set(fields) == {"title", "description", "postedAt"}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"subcategoryId": "sub-women-winter", "brand": "Ecco", "sizes": ["38,5"],
          "size_system": "EU"}, {"subcategoryId": "sub-women-winter", "brand": "Ecco",
                                 "size": "38.5"}),
        ({"subcategoryId": "вигадана", "brand": None, "sizes": [], "size_system": "EU"}, {}),
        ({"subcategoryId": None, "brand": "Дуже довга назва бренду тут", "sizes": ["38"],
          "size_system": "EU"}, {"size": "38"}),
        ({"subcategoryId": "sub-women-winter", "brand": None, "sizes": ["7.5"],
          "size_system": "US"}, {"subcategoryId": "sub-women-winter", "size": "38"}),
        ({"subcategoryId": "sub-women-winter", "brand": None, "sizes": ["8"],
          "size_system": "US"}, {"subcategoryId": "sub-women-winter"}),  # діапазон — не шлемо
        ({"subcategoryId": None, "brand": None, "sizes": ["7.5"], "size_system": "US"}, {}),
    ],
)  # fmt: skip
def test_ai_answer_checked_by_code(answer, expected):
    assert site.check_ai(answer, SUBS) == expected


# --- Черга: перемикач, ключ, лише пости бота ---


def test_switch_off_by_default_posts_do_not_queue(conn, monkeypatch):
    monkeypatch.setenv(site.KEY, KEY)

    assert site.enqueue(conn, 1401, "1401", POSTED, TEXT) is False
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0


def test_without_key_integration_off(conn, monkeypatch, worker, fake, said):
    monkeypatch.setenv(site.SEND_SETTING, "1")

    assert site.enqueue(conn, 1401, "1401", POSTED, TEXT) is False
    for _ in range(3):
        assert worker.step() == site.IDLE

    assert fake.requests == []
    assert said["log"] == [f"Сайт: немає {site.KEY} — інтеграція вимкнена"]  # один рядок


def test_switched_off_with_queue_waits_and_loses_nothing(conn, root, on, monkeypatch, worker, fake):
    add_post(conn, root)
    monkeypatch.setenv(site.SEND_SETTING, "0")

    for _ in range(5):
        assert worker.step() == site.IDLE
    assert fake.requests == []
    assert job(conn)["state"] == "fields"

    monkeypatch.setenv(site.SEND_SETTING, "1")
    run_until_idle(worker)
    assert job(conn)["state"] == "done"


def test_switch_from_settings_file(conn, monkeypatch):
    monkeypatch.setenv(site.KEY, KEY)
    config.save_settings({site.SEND_SETTING: "1"})

    assert site.enqueue(conn, 1401, "1401", POSTED, TEXT) is True


def test_repeat_enqueue_does_not_duplicate(conn, root, on):
    add_post(conn, root)
    assert site.enqueue(conn, 1401, "1401", POSTED, "інший текст")

    assert conn.execute("SELECT count(*), max(text) FROM site_jobs").fetchone()[:] == (1, TEXT)


# --- Відправка ---


def test_fields_then_photos_one_by_one_in_channel_order(conn, root, on, worker, fake, ai_fake):
    add_post(conn, root, photos=3)

    run_until_idle(worker)

    assert fake.paths() == [
        "GET /subcategories", "PUT /products/by-post/1401", "PUT /products/by-post/1401/photos/0",
        "PUT /products/by-post/1401/photos/1", "PUT /products/by-post/1401/photos/2",
    ]  # fmt: skip
    assert all(key == KEY for _, _, key in fake.requests)
    assert fake.products[1401] == {
        "title": None, "description": TEXT.split("\n\nПишіть")[0], "postedAt": "2026-10-17T09:00:00+00:00",
        "price": 1450, "lengthSm": "24.5", "condition": "Ідеальний",
        "subcategoryId": "sub-women-winter",
        "brand": "Ecco", "size": "38",
    }  # fmt: skip
    for i in range(3):  # 0 — головне = 1.jpg (перше в каналі)
        assert (root / "1401" / f"{i + 1}.jpg").read_bytes() in fake.photos[(1401, i)]
    assert job(conn)["state"] == "done"
    assert len(ai_fake.requests) == 1
    request = ai_fake.requests[0]
    assert request["thinking"] == {"type": "between_tools"}
    assert [b["type"] for b in request["messages"][0]["content"]] == ["image", "text"]
    assert "sub-women-winter" in request["messages"][0]["content"][1]["text"]
    cost = conn.execute("SELECT item_id, input_tokens, error FROM ai_requests").fetchall()
    assert [tuple(r) for r in cost] == [(None, 900, None)]


def test_requests_spaced_for_shared_rate_limit(conn, root, on, worker):
    add_post(conn, root, photos=2)

    assert worker.step() >= 1.0  # ≤ 60 запитів за хвилину разом з імпортом сайту


def test_subcategories_cached_for_a_day(conn, root, on, worker, fake, clock):
    add_post(conn, root, 1401, photos=1)
    run_until_idle(worker)
    clock.pass_(hours=5)
    add_post(conn, root, 1402, photos=1)
    run_until_idle(worker)
    clock.pass_(hours=20)
    add_post(conn, root, 1403, photos=1)
    run_until_idle(worker)

    assert fake.paths().count("GET /subcategories") == 2


def test_folder_moved_to_published_still_found(conn, root, on, worker, fake):
    add_post(conn, root, photos=2)
    (root / folders.PUBLISHED_DIR).mkdir()
    (root / "1401").rename(root / folders.PUBLISHED_DIR / "1401")

    run_until_idle(worker)

    assert job(conn)["state"] == "done"
    assert (1401, 1) in fake.photos


def test_several_days_without_network_then_delivered_after_restart(
    conn, root, on, worker, fake, clock, ai_fake, said, tmp_path
):
    add_post(conn, root, photos=2)
    waits = []
    for _ in range(40):  # три дні без зв'язку: сайт мовчить, черга чекає
        fake.fail("GET", "/subcategories", httpx2.ConnectError)
        worker.step()
        row = job(conn)
        if row["next_at"]:
            waits.append(datetime.fromisoformat(row["next_at"]) - clock())
            clock.now = datetime.fromisoformat(row["next_at"])
    assert job(conn)["state"] == "fields"
    assert waits[0] < waits[3] < waits[-1] == timedelta(minutes=30)  # пауза зростає
    assert said["owner"] == said["seller"] == []

    worker.close()  # «перезапуск програми»: нове з'єднання, новий потік
    fake.plan.clear()
    again = make_worker(db.connect(tmp_path / "база.sqlite"), root, fake, ai_fake, clock, said)
    run_until_idle(again)

    assert job(again.conn)["state"] == "done"
    assert (1401, 1) in fake.photos


def test_break_in_middle_of_photos_continues_from_same_index(conn, root, on, worker, fake, clock):
    add_post(conn, root, photos=3)
    worker.step()  # поля (із запитом підкатегорій)
    worker.step()  # фото 0
    fake.fail("PUT", "/products/by-post/1401/photos/1", httpx2.ReadError)

    worker.step()
    assert (job(conn)["state"], job(conn)["next_photo"]) == ("photos", 1)
    clock.pass_(minutes=1)
    run_until_idle(worker)

    photos = [p for p in fake.paths() if "/photos/" in p]
    assert photos == [
        "PUT /products/by-post/1401/photos/0", "PUT /products/by-post/1401/photos/1",
        "PUT /products/by-post/1401/photos/1", "PUT /products/by-post/1401/photos/2",
    ]  # fmt: skip
    assert job(conn)["state"] == "done"


@pytest.mark.parametrize(
    ("status", "path"), [(429, "/products/by-post/1401"), (503, "/products/by-post/1401/photos/0")]
)
def test_429_and_503_wait_retry_after(conn, root, on, worker, fake, clock, status, path):
    add_post(conn, root, photos=1)
    if "photos" in path:
        worker.step()  # поля
    fake.fail("PUT", path, httpx2.Response(status, headers={"Retry-After": "45"}))

    assert worker.step() == 45
    before = len(fake.requests)
    clock.pass_(seconds=44)
    worker.step()
    assert len(fake.requests) == before  # до кінця паузи — жодного запиту

    clock.pass_(seconds=2)
    run_until_idle(worker)
    assert job(conn)["state"] == "done"


def test_5xx_retried_later(conn, root, on, worker, fake, clock):
    add_post(conn, root, photos=1)
    worker.step()
    fake.fail("PUT", "/products/by-post/1401/photos/0", httpx2.Response(500))

    worker.step()
    assert job(conn)["state"] == "photos"
    assert job(conn)["next_at"] > clock().isoformat()
    clock.pass_(minutes=1)
    run_until_idle(worker)
    assert job(conn)["state"] == "done"


def test_401_stops_queue_with_one_message_until_new_key(
    conn, root, on, worker, fake, said, monkeypatch
):
    add_post(conn, root, photos=1)
    fake.fail("GET", "/subcategories", httpx2.Response(401))

    for _ in range(10):
        worker.step()

    assert fake.paths() == ["GET /subcategories"]  # не крутимо в циклі
    assert len(said["owner"]) == 1 and "401" in said["owner"][0]
    assert said["seller"] == []

    monkeypatch.setenv(site.KEY, KEY + "-new")
    run_until_idle(worker)
    assert job(conn)["state"] == "done"


def test_404_on_everything_stops_queue_with_one_message(conn, root, on, worker, fake, said):
    add_post(conn, root, photos=1)
    fake.fail("PUT", "/products/by-post/1401", httpx2.Response(404))

    for _ in range(10):
        worker.step()

    assert fake.paths().count("PUT /products/by-post/1401") == 1
    assert len(said["owner"]) == 1 and "404" in said["owner"][0]
    assert job(conn)["state"] == "fields"  # нічого не загублено


def test_404_on_photo_sends_fields_again_once(conn, root, on, worker, fake):
    add_post(conn, root, photos=1)
    worker.step()
    fake.products.clear()  # пари на сайті немає

    run_until_idle(worker)

    assert fake.paths()[-3:] == [
        "PUT /products/by-post/1401/photos/0", "PUT /products/by-post/1401",
        "PUT /products/by-post/1401/photos/0",
    ]  # fmt: skip
    assert job(conn)["state"] == "done"


@pytest.mark.parametrize("status", [409, 400])
def test_409_and_400_not_retried_owner_and_seller_told(conn, root, on, worker, fake, said, status):
    add_post(conn, root, photos=1)
    body = {"errors": {"$.postedAt": ["текст помилки сайту"]}} if status == 400 else {}
    fake.fail("PUT", "/products/by-post/1401", httpx2.Response(status, json=body))

    for _ in range(5):
        worker.step()

    assert fake.paths().count("PUT /products/by-post/1401") == 1
    assert job(conn)["state"] == "failed"
    assert len(said["owner"]) == 1 and "1401" in said["owner"][0]
    assert len(said["seller"]) == 1 and "1401" in said["seller"][0]
    if status == 400:
        assert "postedAt" in said["owner"][0]
        assert "текст помилки" not in said["owner"][0]


def test_warnings_logged_and_owner_told_code_and_field(conn, root, on, worker, fake, said):
    fake.warnings = [{"field": "brand", "code": "dropped_too_long"}]
    add_post(conn, root, photos=1)

    run_until_idle(worker)

    assert said["owner"] == ["Сайт, пост 1401: brand — dropped_too_long"]
    assert any("dropped_too_long" in line for line in said["log"])


def test_ai_unsure_or_failing_fields_still_go(conn, root, on, worker, fake, ai_fake):
    ai_fake.error = anthropic.AuthenticationError(
        "ні", response=httpx2.Response(401, request=httpx2.Request("POST", "https://a.test")),
        body=None,
    )  # fmt: skip
    add_post(conn, root, photos=1)

    run_until_idle(worker)

    assert "subcategoryId" not in fake.products[1401]
    assert fake.products[1401]["price"] == 1450
    assert job(conn)["state"] == "done"


def test_ai_without_connection_retried_later_not_skipped(
    conn, root, on, worker, fake, ai_fake, clock
):
    ai_fake.error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://a.test"))
    add_post(conn, root, photos=1)

    worker.step()
    assert fake.paths() == ["GET /subcategories"]
    assert job(conn)["fields"] is None
    ai_fake.error = None
    clock.pass_(minutes=1)
    run_until_idle(worker)
    assert fake.products[1401]["subcategoryId"] == "sub-women-winter"


def test_ai_fields_computed_once(conn, root, on, worker, fake, ai_fake, clock):
    add_post(conn, root, photos=1)
    fake.fail("PUT", "/products/by-post/1401", httpx2.Response(500))
    worker.step()
    clock.pass_(minutes=1)
    run_until_idle(worker)

    assert len(ai_fake.requests) == 1


def test_missing_folder_owner_told_not_seller(conn, root, on, worker, fake, said):
    add_post(conn, root, photos=1)
    worker.step()
    (root / "1401" / "1.jpg").unlink()
    (root / "1401").rmdir()

    run_until_idle(worker)

    assert job(conn)["state"] == "failed"
    assert len(said["owner"]) == 1 and said["seller"] == []


def test_big_photo_shrunk_copy_within_site_limits():
    big = Image.new("RGB", (6000, 4300), "white")  # 25,8 Мп
    out = io.BytesIO()
    big.save(out, "JPEG")

    data = site.for_site(out.getvalue())

    with Image.open(io.BytesIO(data)) as image:
        assert image.width * image.height <= site.PHOTO_MAX_PIXELS
    assert len(data) <= site.PHOTO_MAX_BYTES
    small = jpeg()
    assert site.for_site(small) is small  # влазить — як є


def test_key_never_in_logs_messages_errors_or_db(conn, root, on, worker, fake, said, clock):
    add_post(conn, root, photos=1)
    fake.fail("GET", "/subcategories", httpx2.ConnectError)
    worker.step()
    clock.pass_(minutes=1)
    fake.fail("GET", "/subcategories", httpx2.Response(401))
    worker.step()

    dump = "\n".join(said["log"] + said["owner"] + said["seller"])
    dump += json.dumps([tuple(r) for r in conn.execute("SELECT * FROM site_jobs")])
    assert KEY not in dump

    client = Site(KEY, httpx2.Client(transport=httpx2.MockTransport(fake)), BASE)
    fake.fail("GET", "/subcategories", httpx2.ConnectError)
    with pytest.raises(site.SiteError) as error:
        client.subcategories()
    assert KEY not in str(error.value) and error.value.__cause__ is None


def test_thread_survives_crashing_step(conn, root, on, worker, said):
    import threading

    stop = threading.Event()
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        raise RuntimeError("секрет не друкуємо")

    worker.step = boom
    site.run(worker, stop, sleep=lambda s: None)

    assert len(calls) == 3
    assert all("секрет" not in line for line in said["log"])


def test_only_bot_puts_posts_in_site_queue(conn, root, on, export_path):
    """Ручні пости каналу (Експорт, папки) і старі (`borh`) на сайт не йдуть: у чергу ставить
    лише бот після своєї публікації."""
    from pathlib import Path

    from shafa.export import read_export
    from shafa.folders import list_folders
    from shafa.scan import scan

    (root / "10").mkdir()
    (root / "10" / "1.jpg").write_bytes(jpeg())
    scan(conn, read_export(export_path), list_folders(root, None))

    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] >= 1
    assert conn.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0
    src = Path(site.__file__).parent
    callers = sorted(
        p.relative_to(src).as_posix() for p in src.rglob("*.py")
        if "site.enqueue(" in p.read_text(encoding="utf-8")
    )  # fmt: skip
    assert callers == ["bot/core.py"]


def test_404_on_photo_forever_stops_after_one_resend(conn, root, on, worker, fake, said):
    fake.forget = True
    add_post(conn, root, photos=1)

    for _ in range(10):
        worker.step()

    assert fake.paths().count("PUT /products/by-post/1401") == 2  # поля ще раз — лише один раз
    assert fake.paths().count("PUT /products/by-post/1401/photos/0") == 2
    assert len(said["owner"]) == 1 and "404" in said["owner"][0]


def test_429_pauses_whole_queue_not_just_one_post(conn, root, on, worker, fake, clock):
    add_post(conn, root, 1401, photos=1)
    add_post(conn, root, 1402, photos=1)
    fake.fail("PUT", "/products/by-post/1401", httpx2.Response(429, headers={"Retry-After": "45"}))

    assert worker.step() == 45
    before = len(fake.requests)
    for _ in range(3):
        worker.step()  # пост 1402 готовий, але ліміт спільний — чекаємо всі

    assert len(fake.requests) == before
