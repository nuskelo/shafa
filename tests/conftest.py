from pathlib import Path

import pytest

from shafa import config, db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHANNEL_ID = "1234567890"


def make_folder(root: Path, name: str, photos: list[str]) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for photo in photos:
        (folder / photo).write_bytes(b"")
    return folder


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path_factory, monkeypatch):
    """Тести не бачать справжнього `.env` і ключів середовища — лише те, що задали самі."""
    monkeypatch.setattr(config, "ENV_FILE", tmp_path_factory.mktemp("env") / ".env")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SHAFA_ROOT_DIR", "SHAFA_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)


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


@pytest.fixture(autouse=True)
def shafa_snapshot(monkeypatch):
    """Тести бачать маленький знімок довідників Шафи з `fixtures/`, а не справжній."""
    from shafa import catalog

    monkeypatch.setattr(catalog, "SNAPSHOT", FIXTURES / "shafa-catalog.json")
    catalog.snapshot.cache_clear()
    yield
    catalog.snapshot.cache_clear()
