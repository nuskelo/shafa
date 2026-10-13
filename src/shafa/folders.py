"""Головна папка: папки товарів і фото в них."""

import re
from dataclasses import dataclass
from pathlib import Path

PHOTO_SUFFIXES = {".jpg", ".jpeg"}
PUBLISHED_DIR = "Опубліковано"  # зарезервована назва: сюди робот переносить опубліковане (D1)
_POST_NUMBER = re.compile(r"\d{1,7}")
_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True)
class Folder:
    name: str
    post_id: int | None  # None — назва папки не номер поста
    photos: tuple[str, ...]


def post_id_from_name(name: str, channel_id: str | None) -> int | None:
    """`1342` → 1342; `<id каналу>1342` → 1342; інакше None."""
    if _POST_NUMBER.fullmatch(name):
        return int(name)
    if channel_id and name.startswith(channel_id):
        rest = name[len(channel_id) :]
        if _POST_NUMBER.fullmatch(rest):
            return int(rest)
    return None


def natural_key(name: str) -> tuple:
    """Ключ сортування «як у провіднику»: `IMG_9.JPG` перед `IMG_10.JPG`."""
    return tuple(
        int(part) if i % 2 else part.casefold() for i, part in enumerate(_DIGITS.split(name))
    )


def _is_hidden(path: Path) -> bool:
    # `.DS_Store`, `._IMG_1.JPG` (macOS) — службові, ігноруються мовчки.
    return path.name.startswith(".")


def list_photos(folder: Path) -> tuple[str, ...]:
    names = [
        p.name
        for p in folder.iterdir()
        if p.is_file() and not _is_hidden(p) and p.suffix.lower() in PHOTO_SUFFIXES
    ]
    return tuple(sorted(names, key=natural_key))


def list_folders(root: Path, channel_id: str | None) -> list[Folder]:
    """Папки товарів першого рівня, у порядку «як у провіднику». Теку «Опубліковано» пропускає."""
    folders = [
        Folder(p.name, post_id_from_name(p.name, channel_id), list_photos(p))
        for p in root.iterdir()
        if p.is_dir() and not _is_hidden(p) and p.name != PUBLISHED_DIR
    ]
    return sorted(folders, key=lambda f: natural_key(f.name))
