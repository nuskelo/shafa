"""Головна папка: папки товарів і фото в них."""

import re
from dataclasses import dataclass
from pathlib import Path

from shafa import config

PHOTO_SUFFIXES = {".jpg", ".jpeg"}
PUBLISHED_DIR = "Опубліковано"  # зарезервована назва: сюди робот переносить опубліковане (D1)
# Картки старих постів з Експорту (CORE-005): одне фото поста, скопійоване в теку даних
# `<дані>/borh/<номер>/`, а не папка в Головній. Назва Картки — `borh-<номер>`.
OLD_PREFIX = "borh-"
OLD_DIR = "borh"
# Картки «лише Клубок» (KLB-008): старий пост з Експорту, якого немає на Шафі. Одне фото поста й
# текст — у `<дані>/klubok-old/<номер>/`, у Головну нічого не кладемо. Назва Картки — `klubok-<номер>`.
# Та сама тека — запасне фото еталона Шафи, чиї фото вже стерто (R5).
KLUBOK_PREFIX = "klubok-"
KLUBOK_DIR = "klubok-old"
SPARE_PHOTO = "1.jpg"  # одне фото поста з Експорту в цій теці
# Картка-варіант одного розміру з поста з кількома розмірами (CORE-014): `<папка>~<розмір>`
# (`3654~43`, `borh-3655~36`). Фото — спільна папка поста, назва без суфікса.
VARIANT_SEP = "~"
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


def is_old(folder_name: str) -> bool:
    """Картка старого поста з Експорту (фото в теці даних)."""
    return folder_name.startswith(OLD_PREFIX)


def is_klubok(folder_name: str) -> bool:
    """Картка «лише Клубок» (KLB-008, фото в теці даних)."""
    return folder_name.startswith(KLUBOK_PREFIX)


def in_data_dir(folder_name: str) -> bool:
    """Фото Картки — у теці даних програми, не в Головній (старий пост Шафи чи «лише Клубок»)."""
    return is_old(folder_name) or is_klubok(folder_name)


def base_name(folder_name: str) -> str:
    """Папка з фото Картки: для варіанта — без `~<розмір>`."""
    return folder_name.split(VARIANT_SEP, 1)[0]


def variant_size(folder_name: str) -> str | None:
    """Розмір Картки-варіанта («43», «37,5») або None — звичайна Картка."""
    _, sep, size = folder_name.partition(VARIANT_SEP)
    return size if sep else None


def variant_name(folder_name: str, size: str) -> str:
    return f"{base_name(folder_name)}{VARIANT_SEP}{size}"


def old_folder_name(post_id: int) -> str:
    return f"{OLD_PREFIX}{post_id}"


def old_root() -> Path:
    return config.data_dir() / OLD_DIR


def klubok_folder_name(post_id: int) -> str:
    return f"{KLUBOK_PREFIX}{post_id}"


def klubok_root() -> Path:
    return config.data_dir() / KLUBOK_DIR


def klubok_spare(post_id: int) -> Path:
    """Тека з одним фото поста з Експорту для Клубку (Картка «лише Клубок» чи запасне еталона)."""
    return klubok_root() / str(post_id)


def item_folder(root: Path, folder_name: str) -> Path:
    """Де лежать фото Картки: папка в Головній або (старий пост, «лише Клубок») тека в даних
    програми. Варіанти одного поста (CORE-014) ділять одну папку."""
    folder_name = base_name(folder_name)
    if is_old(folder_name):
        return old_root() / folder_name.removeprefix(OLD_PREFIX)
    if is_klubok(folder_name):
        return klubok_root() / folder_name.removeprefix(KLUBOK_PREFIX)
    return root / folder_name


def photo_folder(root: Path, folder_name: str) -> Path:
    """Фото Картки для Клубку (KLB-002): `item_folder`, а якщо такої теки немає й це не старий пост —
    `<Головна>/Опубліковано/<папка>` (робот Шафи переносить туди опубліковане)."""
    folder = item_folder(root, folder_name)
    if folder.is_dir() or in_data_dir(base_name(folder_name)):
        return folder
    return root / PUBLISHED_DIR / base_name(folder_name)


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
    """Папки товарів першого рівня, у порядку «як у провіднику».

    Теку «Опубліковано» і назви `borh-…`/`klubok-…` (зарезервовані для старих постів) пропускає.
    """
    folders = [
        Folder(p.name, post_id_from_name(p.name, channel_id), list_photos(p))
        for p in root.iterdir()
        if p.is_dir() and not _is_hidden(p) and p.name != PUBLISHED_DIR and not in_data_dir(p.name)
    ]
    return sorted(folders, key=lambda f: natural_key(f.name))
