import os

import pytest

from shafa import places


@pytest.fixture(autouse=True)
def no_onedrive(monkeypatch):
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)


def test_default_root_outside_onedrive_with_cyrillic(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "Вікторія"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Вікторія"))
    root = places.default_root()
    assert root == tmp_path / "Вікторія" / "Шафа"
    assert not places.in_onedrive(root)
    root.mkdir(parents=True)
    (root / "1342").mkdir()
    (root / "1342" / "Фото 1.jpg").write_bytes(b"\xff\xd8")
    assert [p.name for p in (root / "1342").iterdir()] == ["Фото 1.jpg"]


@pytest.mark.parametrize("var", ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"])
def test_onedrive_by_environment(tmp_path, monkeypatch, var):
    cloud = tmp_path / "Хмара Вікторії"
    monkeypatch.setenv(var, str(cloud))
    assert places.in_onedrive(cloud / "Робочий стіл" / "Шафа")
    assert places.in_onedrive(cloud)
    assert not places.in_onedrive(tmp_path / "Шафа")


@pytest.mark.parametrize(
    "parts", [("OneDrive", "Desktop", "Шафа"), ("OneDrive - Компанія", "Робочий стіл", "Шафа")]
)
def test_onedrive_by_folder_name(tmp_path, parts):
    assert places.in_onedrive(tmp_path.joinpath(*parts))


def test_desktop_exists():
    desktop = places.desktop()
    assert desktop.is_absolute()
    if os.name == "nt":  # справжній робочий стіл Windows (Known Folder), де б він не був
        assert desktop.is_dir()
