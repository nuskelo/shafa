import pytest

from conftest import CHANNEL_ID, make_folder
from shafa.folders import list_folders, natural_key, post_id_from_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1342", 1342),
        ("7", 7),
        ("1234567", 1234567),
        (CHANNEL_ID + "1342", 1342),
        ("12345678", None),  # 8 цифр і не починається з id каналу
        (CHANNEL_ID, None),
        (CHANNEL_ID + "12345678", None),
        ("1342 сукня", None),
        ("Сукня", None),
        ("", None),
    ],
)
def test_post_id_from_name(name, expected):
    assert post_id_from_name(name, CHANNEL_ID) == expected


def test_post_id_without_channel_id():
    assert post_id_from_name("1342", None) == 1342
    assert post_id_from_name(CHANNEL_ID + "1342", None) is None


def test_natural_sort():
    names = ["IMG_10.JPG", "img_2.jpg", "IMG_9.JPG", "IMG_1.jpeg"]
    assert sorted(names, key=natural_key) == ["IMG_1.jpeg", "img_2.jpg", "IMG_9.JPG", "IMG_10.JPG"]


def test_list_folders(root):
    make_folder(
        root,
        CHANNEL_ID + "13",
        [
            "IMG_10.JPG",
            "IMG_9.JPG",
            "фото 2.jpeg",
            "a.Jpg",
            "Thumbs.db",
            "desktop.ini",
            ".DS_Store",
            "._IMG_9.JPG",
            "відео.mp4",
            "знімок.png",
        ],
    )
    make_folder(root, "10", ["1.jpg"])
    make_folder(root, "Сукня", [])
    make_folder(root, ".приховано", ["1.jpg"])
    (root / "нотатка.txt").write_text("x", encoding="utf-8")
    (root / "10" / "вкладена").mkdir()

    folders = list_folders(root, CHANNEL_ID)

    assert [(f.name, f.post_id) for f in folders] == [
        ("10", 10),
        (CHANNEL_ID + "13", 13),
        ("Сукня", None),
    ]
    assert folders[0].photos == ("1.jpg",)
    assert folders[1].photos == ("a.Jpg", "IMG_9.JPG", "IMG_10.JPG", "фото 2.jpeg")
