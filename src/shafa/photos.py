"""Фото для ШІ й мініатюри: поворот за EXIF, зменшення, JPEG. Оригінали не змінюються."""

import hashlib
import io
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

# Довга сторона, px. 4:3 → 1092×819 = 1170 токенів; бірки на такому розмірі читаються (CORE-003).
MAX_SIDE = 1092
JPEG_QUALITY = 85
PATCH = 28  # Claude рахує фото клітинками 28×28 px: ⌈w/28⌉ × ⌈h/28⌉ токенів


@dataclass(frozen=True)
class PreparedPhoto:
    file_name: str
    data: bytes  # JPEG
    width: int
    height: int

    @property
    def tokens(self) -> int:
        return image_tokens(self.width, self.height)


def image_tokens(width: int, height: int) -> int:
    """Вартість фото у вхідних токенах (docs: build-with-claude/vision, «Resolution and token cost»)."""
    return -(-width // PATCH) * -(-height // PATCH)


def prepare(path: Path, max_side: int = MAX_SIDE) -> PreparedPhoto:
    """Фото, повернуте за EXIF і зменшене до `max_side` по довгій стороні."""
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return PreparedPhoto(path.name, buffer.getvalue(), image.width, image.height)


# Мініатюри для екрана перевірки. Кеш — у теці даних програми (поруч із базою), не в папках з фото.
THUMB_SIDE = 320


def thumbnail(path: Path, side: int = THUMB_SIDE) -> bytes:
    """Мініатюра JPEG: поворот за EXIF; `draft` читає JPEG одразу зменшеним — у рази швидше."""
    with Image.open(path) as original:
        original.draft("RGB", (side, side))
        image = ImageOps.exif_transpose(original).convert("RGB")
    image.thumbnail((side, side))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def cached_thumbnail(cache_dir: Path, item_id: int, path: Path) -> bytes:
    """Мініатюра з кешу `cache_dir/<id Картки>/`; змінене фото (час, розмір) робиться знову."""
    stat = path.stat()
    key = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
    folder = cache_dir / str(item_id)
    target = folder / f"{key}-{stat.st_mtime_ns}-{stat.st_size}.jpg"
    if target.is_file():
        return target.read_bytes()
    data = thumbnail(path)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{key}-*.jpg"):
            old.unlink()
        temp = folder / f"{key}.{uuid.uuid4().hex}.tmp"  # кілька запитів одночасно — не заважають
        temp.write_bytes(data)
        temp.replace(target)
    except OSError:
        pass  # кеш не обов'язковий: мініатюру вже маємо
    return data


def drop_thumbnails(cache_dir: Path, item_id: int) -> None:
    shutil.rmtree(cache_dir / str(item_id), ignore_errors=True)
