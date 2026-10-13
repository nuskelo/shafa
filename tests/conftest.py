from pathlib import Path

import pytest

from shafa import db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHANNEL_ID = "1234567890"


def make_folder(root: Path, name: str, photos: list[str]) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for photo in photos:
        (folder / photo).write_bytes(b"")
    return folder


@pytest.fixture
def export_path() -> Path:
    return FIXTURES / "export.json"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "Робочий стіл" / "Шафа"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "база.sqlite")
    yield connection
    connection.close()
