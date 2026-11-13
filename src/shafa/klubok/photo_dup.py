"""KLB-011: відбиток фото (dHash 64 біти) і фото її оголошень на Клубку.

Її фото в знімку (`klubok_listings.photo`) — мініатюра 158×158, вирізана з середини фото (Фаза 0).
Тому наше фото обрізаємо до центрального квадрата, її — беремо як є. Відстань ≤ `T1` — те саме фото
(перестиснуте, зменшене, повернуте за EXIF); більша нічого не доводить — вона могла поставити інше.
На копії 12.11: 11 з 84 — відстань 0–1; 19 050 пар «Картка × чуже оголошення» — мінімум 6.

Для ШІ — велике фото без обрізки (`/l/`, 1024×768): адреса з `/m/` лише за точним шаблоном;
не вдалось — ШІ отримує мініатюру. Завантаження — лише `https://img.klubok.com/`, GET без cookies,
≤ 5 МБ, `image/*`, пауза між запитами; 429/403 — `FetchStop` (завдання зупиняється). Кеш —
`<тека>/<id>.jpg` і `<id>-l.jpg`. Браузер і guard не потрібні: робот не запускається.
"""

import io
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from shafa import config
from shafa.folders import photo_folder
from shafa.klubok import fields
from shafa.review import Card

T1 = 4  # бітів із 64: виміряно на копії 12.11 (0 хибних у контролі)
HOST = "https://img.klubok.com/"
_SMALL = re.compile(r"(https://img\.klubok\.com/(?:[\w.-]+/)+)m/(\d+_\d+\.jpg)")
TIMEOUT = 15
MAX_BYTES = 5 * 1024 * 1024
PAUSE = (0.5, 1.0)
CACHE_DIR = "klubok-photos"


class FetchStop(Exception):
    """Клубок відповів 429/403 — зупинити все завдання (подія), спробувати пізніше."""


class FetchFailed(Exception):
    """Фото не отримано. `permanent` — не той хост, немає (404), не картинка, завелике;
    інакше (мережа, 5xx) — тимчасово."""

    def __init__(self, why: str, *, permanent: bool) -> None:
        super().__init__(why)
        self.permanent = permanent


# --- відбиток ---


def dhash(image: Image.Image) -> int:
    """dHash 64 біти: сірий → 9×8 → чи світліший піксель за правого сусіда."""
    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = gray.tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y * 9 + x] > px[y * 9 + x + 1])
    return bits


def square(image: Image.Image) -> Image.Image:
    """Центральний квадрат — так Клубок робить мініатюру."""
    w, h = image.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return image.crop((left, top, left + side, top + side))


def ours_hash(path: Path) -> int:
    """Наше фото: поворот за EXIF → центральний квадрат → відбиток."""
    with Image.open(path) as image:
        return dhash(square(ImageOps.exif_transpose(image)))


def theirs_hash(data: bytes) -> int:
    """Її мініатюра як є (вже квадрат)."""
    with Image.open(io.BytesIO(data)) as image:
        return dhash(ImageOps.exif_transpose(image))


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def our_photos(card: Card, root: Path) -> list[Path]:
    """Усі фото Картки (порядок Картки, ≤ 15, лише наявні) + запасне фото поста з Експорту."""
    folder = photo_folder(root, card.folder_name)
    found = [folder / p.file_name for p in card.photos][: fields.PHOTOS_MAX]
    found = [p for p in found if p.is_file()]
    spare = fields.spare_photo(card.post_id)
    if spare is not None and spare not in found:
        found.append(spare)
    return found


@dataclass(frozen=True)
class Hashed:
    path: Path
    hash: int


def hashed(paths: list[Path]) -> list[Hashed]:
    """Відбитки файлів; файл не читається — пропускаємо."""
    out = []
    for path in paths:
        try:
            out.append(Hashed(path, ours_hash(path)))
        except (OSError, UnidentifiedImageError, ValueError):
            continue
    return out


def closest(ours: list[Hashed], theirs: list[int]) -> tuple[int, Path] | None:
    """Найменша відстань і наше фото, що її дало; порожньо — None."""
    pairs = [(distance(o.hash, t), o.path) for o in ours for t in theirs]
    return min(pairs, key=lambda p: p[0]) if pairs else None


# --- завантаження ---


def large_url(url: str) -> str | None:
    """`…/m/<id>_<N>.jpg` → `…/l/<id>_<N>.jpg`; інший шаблон — None."""
    match = _SMALL.fullmatch(url)
    return f"{match[1]}l/{match[2]}" if match else None


Get = Callable[[str], tuple[int, str, bytes]]


def http_get(url: str) -> tuple[int, str, bytes]:
    """GET без cookies і без переходів; (код, content-type, тіло ≤ MAX_BYTES + 1)."""
    import httpx2

    try:
        with (
            httpx2.Client(timeout=TIMEOUT, follow_redirects=False) as client,
            client.stream("GET", url) as response,
        ):
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            return response.status_code, response.headers.get("content-type", ""), body
    except httpx2.HTTPError as error:
        raise FetchFailed(f"мережа: {type(error).__name__}", permanent=False) from None


def cache_dir() -> Path:
    return config.data_dir() / CACHE_DIR


class Photos:
    """Фото її оголошень з кешем і паузою між запитами. `get` і `sleep` — ззовні (тести)."""

    def __init__(
        self,
        cache: Path,
        get: Get | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache, self.get, self.sleep = cache, get or http_get, sleep
        self.requests = 0

    def small(self, listing_id: int, url: str | None) -> bytes:
        """Мініатюра для відбитка (`<id>.jpg`)."""
        if not url:
            raise FetchFailed("у знімку немає фото оголошення", permanent=True)
        return self._cached(self.cache / f"{listing_id}.jpg", url)

    def large(self, listing_id: int, url: str | None) -> bytes:
        """Велике фото для ШІ (`<id>-l.jpg`); не вдалось (окрім 429/403) — мініатюра."""
        big = large_url(url or "")
        if big is not None:
            try:
                return self._cached(self.cache / f"{listing_id}-l.jpg", big)
            except FetchFailed:
                pass
        return self.small(listing_id, url)

    def _cached(self, path: Path, url: str) -> bytes:
        if path.is_file():
            return path.read_bytes()
        data = self._fetch(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass  # кеш — не обов'язково
        return data

    def _fetch(self, url: str) -> bytes:
        if not url.startswith(HOST):
            raise FetchFailed("фото не з img.klubok.com", permanent=True)
        if self.requests:
            self.sleep(random.uniform(*PAUSE))
        self.requests += 1
        code, kind, body = self.get(url)
        if code in (403, 429):
            raise FetchStop(f"Клубок відповів {code}")
        if code in (404, 410):
            raise FetchFailed(f"фото оголошення немає ({code})", permanent=True)
        if code != 200:
            raise FetchFailed(f"Клубок відповів {code}", permanent=False)
        if not kind.startswith("image/"):
            raise FetchFailed("це не картинка", permanent=True)
        if len(body) > MAX_BYTES:
            raise FetchFailed("фото більше 5 МБ", permanent=True)
        return body
