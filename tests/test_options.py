import pytest

from shafa import batch, config, options, web
from shafa.robot import publish
from test_web import PORT, TOKEN, get, html, post


@pytest.fixture
def app(conn, tmp_path, root):
    return web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)


def form(root, **changes):
    values = {
        "SHAFA_DAILY_LIMIT": "25", "SHAFA_AI_DAILY_USD": "2,5", "SHAFA_AI_WAIT_DAYS": "5",
        "SHAFA_AI_BATCH_LIMIT": "40", "SHAFA_ROOT_DIR": str(root), "SHAFA_DELETE_PUBLISHED": "1",
    }  # fmt: skip
    values.update(changes)
    return {k: v for k, v in values.items() if v is not None}


def test_page_shows_defaults_and_no_secrets(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-секрет-не-показувати")
    page = html(get(app, "/settings"))
    assert 'name="SHAFA_DAILY_LIMIT" value="30"' in page
    assert 'name="SHAFA_AI_DAILY_USD" value="3"' in page
    assert 'name="SHAFA_AI_WAIT_DAYS" value="7"' in page
    assert 'name="SHAFA_DELETE_PUBLISHED" value="1">' in page  # типово вимкнено
    assert "sk-секрет" not in page and "ANTHROPIC" not in page
    assert ">Налаштування</a>" in html(get(app, "/review"))


def test_save_is_used_by_robot_and_ai(app, root):
    page = html(post(app, "/settings", **form(root)))
    assert "Збережено" in page
    assert publish.daily_limit() == 25
    assert publish.delete_enabled()
    assert batch.limits() == batch.Limits(5, 40, 2_500_000)
    assert config.setting("SHAFA_ROOT_DIR") == str(root)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SHAFA_DAILY_LIMIT", "0"), ("SHAFA_DAILY_LIMIT", "101"), ("SHAFA_DAILY_LIMIT", "багато"),
        ("SHAFA_AI_DAILY_USD", "0"), ("SHAFA_AI_DAILY_USD", "nan"), ("SHAFA_AI_WAIT_DAYS", "-1"),
        ("SHAFA_AI_BATCH_LIMIT", "1000"), ("SHAFA_ROOT_DIR", "Z:/немає/такої"),
    ],
)  # fmt: skip
def test_bad_value_saves_nothing(app, root, name, value):
    page = html(post(app, "/settings", **form(root, **{name: value})))
    assert "Не збережено" in page
    assert f'value="{value}"' in page  # її введене лишається у формі
    assert config.setting("SHAFA_DAILY_LIMIT") is None


def test_flag_off_and_root_changes_screen_at_once(app, root, tmp_path):
    other = tmp_path / "Інша Шафа"
    other.mkdir()
    post(app, "/settings", **form(other, SHAFA_DELETE_PUBLISHED=None))
    assert not publish.delete_enabled()
    assert app.root == other


def test_onedrive_root_warns_but_saves(app, tmp_path, monkeypatch):
    cloud = tmp_path / "OneDrive"
    (cloud / "Шафа").mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(cloud))
    page = html(post(app, "/settings", **form(cloud / "Шафа")))
    assert "Головна папка в OneDrive" in page
    assert config.setting("SHAFA_ROOT_DIR") == str(cloud / "Шафа")


def test_settings_post_needs_token(app, root):
    assert post(app, "/settings", token=None, **form(root)).status == 403
    assert config.setting("SHAFA_DAILY_LIMIT") is None


def test_options_have_no_secrets():
    names = {o.name for o in options.OPTIONS}
    assert not names & {"ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"}
