import io

import pytest
from PIL import Image

from shafa.photos import (
    THUMB_SIDE,
    cached_thumbnail,
    drop_thumbnails,
    image_tokens,
    prepare,
    thumbnail,
)

ORIENTATION = 0x0112


def make_photo(path, size, orientation=None, mode="RGB"):
    """Згенероване фото: лівий верхній кут червоний, решта синя."""
    image = Image.new(mode, size, "blue")
    image.paste("red", (0, 0, size[0] // 4, size[1] // 4))
    exif = Image.Exif()
    if orientation:
        exif[ORIENTATION] = orientation
    image.save(path, format="JPEG", exif=exif)
    return path


def opened(photo):
    return Image.open(io.BytesIO(photo.data))


def test_resizes_long_side_and_keeps_original(tmp_path):
    path = make_photo(tmp_path / "фото 1.JPG", (2400, 1800))
    before = path.read_bytes()

    photo = prepare(path)

    assert (photo.width, photo.height) == (1092, 819)
    assert opened(photo).size == (1092, 819)
    assert opened(photo).format == "JPEG"
    assert photo.file_name == "фото 1.JPG"
    assert path.read_bytes() == before


def test_exif_rotation_applied(tmp_path):
    # Orientation 6: камера тримали боком — у файлі 400×300, показувати 300×400.
    path = make_photo(tmp_path / "IMG_1.JPG", (400, 300), orientation=6)

    photo = prepare(path)

    assert (photo.width, photo.height) == (300, 400)
    # Червоний кут після повороту на 90° за годинниковою стрілкою — праворуч угорі.
    red, _, blue = opened(photo).getpixel((280, 20))
    assert red > 200 and blue < 80


def test_small_photo_not_enlarged(tmp_path):
    photo = prepare(make_photo(tmp_path / "a.jpg", (200, 150)))
    assert (photo.width, photo.height) == (200, 150)


def test_grayscale_converted_to_rgb(tmp_path):
    photo = prepare(make_photo(tmp_path / "a.jpg", (100, 100), mode="L"))
    assert opened(photo).mode == "RGB"


@pytest.mark.parametrize(
    ("size", "tokens"),
    [((1092, 819), 1170), ((1000, 1000), 1296), ((200, 200), 64), ((819, 1092), 1170)],
)
def test_image_tokens(size, tokens):
    assert image_tokens(*size) == tokens


def test_thumbnail_small_and_rotated(tmp_path):
    path = make_photo(tmp_path / "IMG_1.JPG", (2400, 1800), orientation=6)
    before = path.read_bytes()

    image = Image.open(io.BytesIO(thumbnail(path)))

    assert image.size == (THUMB_SIDE * 3 // 4, THUMB_SIDE)  # боком → стоячи
    assert path.read_bytes() == before


def test_thumbnail_cache_in_given_folder_and_refreshed(tmp_path):
    cache = tmp_path / "дані" / "thumbs"
    (tmp_path / "фото").mkdir()
    photo = make_photo(tmp_path / "фото" / "IMG_1.JPG", (800, 600))
    first = cached_thumbnail(cache, 7, photo)
    assert len(list((cache / "7").glob("*.jpg"))) == 1
    assert sorted(p.name for p in photo.parent.iterdir()) == ["IMG_1.JPG"]
    assert cached_thumbnail(cache, 7, photo) == first

    make_photo(photo, (600, 800))  # фото замінили
    second = cached_thumbnail(cache, 7, photo)

    assert Image.open(io.BytesIO(second)).size == (240, 320)
    assert len(list((cache / "7").glob("*.jpg"))) == 1  # стара мініатюра прибрана

    drop_thumbnails(cache, 7)
    assert not (cache / "7").exists()
