import os

import pytest

from shafa import config, places, wizard

AI_SECRET = "sk-ant-test-key-XYZ"
BOT_SECRET = "123456:test-token-ABC"


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    home = tmp_path / "Вікторія"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    return home


class Script:
    """Відповіді майстру по черзі; усе, що він показує, — у `shown`."""

    def __init__(self, answers, secrets=(AI_SECRET, BOT_SECRET, ""), windows=False, desktop=None):
        self.answers, self.secrets = list(answers), list(secrets)
        self.shown, self.prompts, self.checks, self.links, self.logins = [], [], [], [], 0
        self.act = wizard.Actions(
            ask=self.ask, ask_secret=self.ask_secret, say=self.shown.append,
            check_ai=lambda key: self.checks.append(("ai", key)) or "Ключ ШІ працює",
            check_bot=lambda token, channel: self.checks.append(("bot", token, channel))
            or "Бот @shafa_bot: права в каналі є, коментарі — лише вона й бот",
            shortcut=lambda link, target, arguments="", icon=None: self.links.append(
                (link.name, target, arguments)),
            login=self.login, desktop=lambda: desktop, windows=windows,
        )  # fmt: skip

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def ask_secret(self, prompt):
        self.prompts.append(prompt)
        return self.secrets.pop(0)

    def login(self):
        self.logins += 1

    def everything(self):
        return "\n".join(self.shown + self.prompts)


IDS = ["2394827824", "2394827824", "111", "222"]


def test_full_setup_saves_privately_checks_and_never_shows_secrets(home):
    script = Script(["", *IDS, "так"])

    assert wizard.run(script.act) == 0

    root = home / "Шафа"
    assert root.is_dir()
    assert config.setting("SHAFA_ROOT_DIR") == str(root)
    assert config.setting("ANTHROPIC_API_KEY") == AI_SECRET
    assert config.setting("TELEGRAM_BOT_TOKEN") == BOT_SECRET
    assert config.setting("TELEGRAM_SELLER_CHAT_ID") == "111"
    assert script.checks == [("ai", AI_SECRET), ("bot", BOT_SECRET, "2394827824")]
    assert script.logins == 1
    assert "Ключ ШІ працює" in script.shown
    assert AI_SECRET not in script.everything() and BOT_SECRET not in script.everything()
    if os.name != "nt":
        assert config.env_file().stat().st_mode & 0o777 == 0o600


def test_onedrive_root_warns_and_offers_default(home, monkeypatch):
    cloud = home / "OneDrive"
    monkeypatch.setenv("OneDrive", str(cloud))
    script = Script([str(cloud / "Робочий стіл" / "Шафа"), "ні", "", *IDS, "ні"])

    wizard.run(script.act)

    warning = next(line for line in script.shown if "OneDrive" in line)
    assert warning.startswith("!!! УВАГА")
    assert config.setting("SHAFA_ROOT_DIR") == str(places.default_root())


def test_onedrive_root_can_still_be_chosen(home, monkeypatch):
    cloud = home / "OneDrive"
    monkeypatch.setenv("OneDrive", str(cloud))
    chosen = cloud / "Шафа"
    script = Script([str(chosen), "так", *IDS, "ні"])
    wizard.run(script.act)
    assert config.setting("SHAFA_ROOT_DIR") == str(chosen)


def test_rerun_keeps_secrets_on_enter(home):
    wizard.run(Script(["", *IDS, "ні"]).act)
    script = Script(["", "", "", "", "", "ні"], secrets=("", "", ""))

    wizard.run(script.act)

    assert config.setting("ANTHROPIC_API_KEY") == AI_SECRET
    assert config.setting("TELEGRAM_OWNER_CHAT_ID") == "222"
    assert any("(вже є; Enter — лишити)" in p for p in script.prompts)
    assert script.logins == 0


def test_without_secrets_says_what_will_not_work(home):
    script = Script(["", "", "", "", "", "ні"], secrets=("", "", ""))
    wizard.run(script.act)
    assert "Ключа ШІ немає — ШІ не працюватиме" in script.shown
    assert "Токена бота немає — бот не працюватиме" in script.shown
    assert script.checks == []


def test_windows_shortcuts_on_real_desktop(home, tmp_path):
    desktop = tmp_path / "OneDrive" / "Робочий стіл"
    script = Script(["", *IDS, "ні"], windows=True, desktop=desktop)
    wizard.run(script.act)
    names = [(name, args) for name, _, args in script.links]
    assert names == [("Шафа-помічник.lnk", "-m shafa app"), ("Шафа — фото товарів.lnk", "")]
    assert script.links[0][1].name == "pythonw.exe"
    assert script.links[1][1] == home / "Шафа"


@pytest.mark.skipif(os.name != "nt", reason="ярлик .lnk — лише Windows")
def test_real_shortcut_with_cyrillic(tmp_path):
    target = tmp_path / "Шафа"
    target.mkdir()
    link = tmp_path / "Шафа — фото товарів.lnk"
    wizard.shortcut(link, target)
    assert link.is_file() and link.stat().st_size > 0
    assert wizard.shortcut_target(link) == target  # кирилиця в назві й у цілі — без «????»


@pytest.mark.skipif(os.name != "nt", reason="ярлик .lnk — лише Windows")
def test_real_app_shortcut_with_arguments_and_icon(tmp_path):
    python = tmp_path / "Програма" / "pythonw.exe"
    python.parent.mkdir()
    python.write_bytes(b"MZ")
    icon = tmp_path / "Програма" / "shafa.ico"
    icon.write_bytes(b"\x00\x00\x01\x00")
    link = tmp_path / "Шафа-помічник.lnk"
    wizard.shortcut(link, python, "-m shafa app", icon)
    assert wizard.shortcut_target(link) == python


def test_check_ai_error_has_no_key(monkeypatch):
    import anthropic
    import httpx2

    def failing(self, **kwargs):
        request = httpx2.Request("GET", "https://api.anthropic.com/v1/models")
        response = httpx2.Response(401, request=request, json={"error": {"message": AI_SECRET}})
        raise anthropic.AuthenticationError(AI_SECRET, response=response, body=None)

    monkeypatch.setattr(anthropic.resources.models.Models, "list", failing)
    text = wizard.check_ai(AI_SECRET)
    assert text == "Ключ ШІ не працює: Ключ Claude API не прийнято (401)"
    assert AI_SECRET not in text


def test_site_key_optional_hidden_and_saved(home):
    script = Script(["", *IDS, "ні"], secrets=(AI_SECRET, BOT_SECRET, "sk-site-secret-key"))

    wizard.run(script.act)

    assert config.setting("SITE_INTEGRATION_KEY") == "sk-site-secret-key"
    assert "sk-site-secret-key" not in script.everything()
    assert any("Ключ сайту" in p and "необов'язково" in p for p in script.prompts)


# --- FIX-002: вставка в cmd і повторний запуск ---


def test_ctrl_v_and_line_breaks_are_cleaned_from_pasted_values(home):
    secrets = ("\x16" + AI_SECRET + "\r\n", " \x16" + BOT_SECRET + "\u200b", "")
    script = Script(["", " 2394827824\r", *IDS[1:], "ні"], secrets=secrets)

    wizard.run(script.act)

    assert config.setting("ANTHROPIC_API_KEY") == AI_SECRET
    assert config.setting("TELEGRAM_BOT_TOKEN") == BOT_SECRET
    assert config.setting("TELEGRAM_BOT_CHANNEL_ID") == "2394827824"
    assert script.checks[0] == ("ai", AI_SECRET)
    assert any("правою кнопкою миші, один раз" in p for p in script.prompts)


@pytest.mark.parametrize(
    "bad",
    [
        "\x16",  # Ctrl+V у cmd: лише керівний символ
        AI_SECRET + AI_SECRET,  # два правих кліки — дві копії
        "ключ-кирилицею",
    ],
)
def test_bad_secret_is_explained_and_asked_again_without_traceback(home, bad):
    script = Script(["", *IDS, "ні"], secrets=(bad, AI_SECRET, BOT_SECRET, ""))

    assert wizard.run(script.act) == 0

    assert (
        "Ключ/токен вставлено з помилкою — встав ще раз (вставка — правою кнопкою миші, один раз)"
        in script.shown
    )
    assert config.setting("ANTHROPIC_API_KEY") == AI_SECRET


def test_bad_token_format_asked_again(home):
    script = Script(["", *IDS, "ні"], secrets=(AI_SECRET, "123456 токен", BOT_SECRET, ""))
    wizard.run(script.act)
    assert config.setting("TELEGRAM_BOT_TOKEN") == BOT_SECRET


def test_bad_id_asked_again(home):
    script = Script(["", "\x16", "2394827824", *IDS[1:], "ні"])
    wizard.run(script.act)
    assert any(s.startswith("Id — лише цифри") for s in script.shown)
    assert config.setting("TELEGRAM_BOT_CHANNEL_ID") == "2394827824"


def test_broken_saved_secret_is_not_kept_on_enter(home):
    config.save_settings({"TELEGRAM_BOT_TOKEN": "\x16", "ANTHROPIC_API_KEY": "\x16" + AI_SECRET})
    script = Script(["", *IDS, "ні"], secrets=("", "", ""))

    wizard.run(script.act)

    assert config.setting("ANTHROPIC_API_KEY") == AI_SECRET  # почищене збережене
    assert any("Токен робочого бота" in s and "зіпсоване" in s for s in script.shown)
    assert not any("Токен робочого бота (вже є" in p for p in script.prompts)
    assert config.setting("TELEGRAM_BOT_TOKEN") is None  # зіпсоване не лишається
    assert "Токена бота немає — бот не працюватиме" in script.shown


def test_enter_keeps_current_root(home, tmp_path):
    current = tmp_path / "D" / "Шафа-фото"
    wizard.run(Script([str(current), *IDS, "ні"]).act)
    script = Script(["", "", "", "", "", "ні"], secrets=("", "", ""))

    wizard.run(script.act)

    assert config.setting("SHAFA_ROOT_DIR") == str(current)
    assert f"[Enter — {current}]" in script.prompts[0]
