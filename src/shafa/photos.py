"""Фото для ШІ: поворот за EXIF, зменшення, JPEG у пам'яті. Оригінали не змінюються."""

import io
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
