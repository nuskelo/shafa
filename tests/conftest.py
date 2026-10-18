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
    # FIX-001: справжніх місць (тека даних, `.env` у корені проєкту) не бачить ні тест, ні процес,
    # який він запускає (змінні оточення успадковуються); звернення до них — помилка.
    monkeypatch.setenv("SHAFA_TESTING", "1")
    monkeypatch.setenv("SHAFA_LEGACY_ENV", str(tmp_path_factory.mktemp("legacy") / ".env"))
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "SHAFA_ROOT_DIR",
        "SHAFA_DAILY_LIMIT",
        "SHAFA_DAILY_OLD",
        "SHAFA_DAILY_NEW",
        "SITE_INTEGRATION_KEY",
        "SHAFA_SITE_SEND",
        "SHAFA_SITE_WAIT_DAYS",
        "SHAFA_SOLD_CHECK",
    ):
        monkeypatch.delenv(name, raising=False)
    # Тека даних (замок робота, журнал) — тимчасова: тести не бачать справжньої.
    monkeypatch.setenv("SHAFA_DATA_DIR", str(tmp_path_factory.mktemp("дані")))


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


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Жоден тест не відкриває справжній Chrome робота (і не ходить на Шафу), навіть через помилку."""
    from shafa.robot import browser

    def forbidden(*args, **kwargs):
        raise AssertionError("тест спробував відкрити справжній браузер робота")

    monkeypatch.setattr(browser, "launch", forbidden)
    monkeypatch.setattr(browser, "wait_for_manual_login", forbidden)
