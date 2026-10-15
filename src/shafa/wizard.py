"""Майстер першого налаштування на її ноутбуці (WIN-001): запускає Власник наживо, у консолі.

Кроки: Головна папка (типово поза OneDrive, попередження, якщо в OneDrive) і ярлик до неї на
робочому столі → секрети (введення приховане, на екран і в журнали не потрапляють) і id чатів →
перевірка ключа ШІ (перелік моделей — безкоштовно) і бота (права в каналі, закрита група
коментарів) → вхід на Шафу (вона вводить пароль сама) → ярлик «Шафа-помічник».
Усе, що ходить у мережу чи Windows, — підставні функції `Actions`: тести без мережі.
"""

import getpass
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shafa import config, places

APP_LINK = "Шафа-помічник.lnk"
ROOT_LINK = "Шафа — фото товарів.lnk"
SECRETS = (
    ("ANTHROPIC_API_KEY", "Ключ Claude API для неї (окремий, з Console)"),
    ("TELEGRAM_BOT_TOKEN", "Токен робочого бота (з @BotFather)"),
)
IDS = (
    ("TELEGRAM_BOT_CHANNEL_ID", "Id каналу, куди бот публікує (її канал — лише з твого дозволу)"),
    ("TELEGRAM_CHANNEL_ID", "Id її каналу для назв папок (як у t.me/c/<id>/…)"),
    ("TELEGRAM_SELLER_CHAT_ID", "Її chat id"),
    ("TELEGRAM_OWNER_CHAT_ID", "Твій chat id"),
)
YES = ("так", "т", "yes", "y", "1")


def check_ai(key: str) -> str:
    """Ключ ШІ: перелік моделей — безкоштовний запит. Повертає текст для людини."""
    import anthropic

    from shafa import ai

    try:
        anthropic.Anthropic(api_key=key, max_retries=1).models.list(limit=1)
    except anthropic.AnthropicError as error:
        return f"Ключ ШІ не працює: {ai.error_text(error)}"
    return "Ключ ШІ працює"


def check_bot(token: str, channel: str | None) -> str:
    from shafa.bot import run
    from shafa.bot.telegram import Telegram

    try:
        tg = Telegram(token)
        try:
            name, _ = run.check_rights(tg, run.channel_id(channel))
        finally:
            tg.close()
    except run.SetupError as error:
        return f"Бот: {error}"
    return f"Бот @{name}: права в каналі є, коментарі — лише вона й бот"


def shortcut(link: Path, target: Path, arguments: str = "", icon: Path | None = None) -> None:
    """Ярлик Windows (.lnk) через WScript.Shell. Шляхи — у змінних середовища (кирилиця, лапки)."""
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHAFA_LNK);"
        "$s.TargetPath=$env:SHAFA_TARGET;$s.Arguments=$env:SHAFA_ARGS;"
        "$s.WorkingDirectory=$env:SHAFA_WD;"
        "if($env:SHAFA_ICON){$s.IconLocation=$env:SHAFA_ICON};$s.Save()"
    )
    env = {
        **os.environ, "SHAFA_LNK": str(link), "SHAFA_TARGET": str(target),
        "SHAFA_ARGS": arguments, "SHAFA_WD": str(target.parent), "SHAFA_ICON": str(icon or ""),
    }  # fmt: skip
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, check=False, capture_output=True,
    )  # fmt: skip
    if result.returncode != 0:
        why = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise OSError(f"Ярлик «{link.name}» не створено: {why[-500:]}")


def shafa_login() -> None:
    subprocess.run([sys.executable, "-m", "shafa", "robot", "--login"], check=False)


@dataclass
class Actions:
    """Що майстер робить назовні (у тестах — підставне)."""

    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print
    check_ai: Callable[[str], str] = check_ai
    check_bot: Callable[[str, str | None], str] = check_bot
    shortcut: Callable[..., None] = shortcut
    login: Callable[[], None] = shafa_login
    desktop: Callable[[], Path] = places.desktop
    windows: bool = os.name == "nt"
    made: list[str] = field(default_factory=list)


def _yes(answer: str) -> bool:
    return answer.strip().lower() in YES


def choose_root(act: Actions) -> Path:
    default = places.default_root()
    while True:
        answer = act.ask(f"Головна папка з фото товарів [Enter — {default}]: ").strip()
        root = Path(answer.strip('"')) if answer else default
        if not places.in_onedrive(root):
            return root
        act.say(
            "!!! УВАГА: ця папка в OneDrive. Тисячі фото поїдуть у хмару (може скінчитись місце),"
            " а програма читатиме їх повільно."
        )
        act.say(f"    Краще: {default}")
        if _yes(act.ask("Все одно взяти цю папку? (так/ні): ")):
            return root


def run(act: Actions | None = None) -> int:
    act = act or Actions()
    act.say("Налаштування «Шафа-помічник». Секрети на екрані не показуються.")
    act.say(f"Налаштування зберігаються тут: {config.env_file()}")

    root = choose_root(act)
    root.mkdir(parents=True, exist_ok=True)
    values: dict[str, str] = {"SHAFA_ROOT_DIR": str(root)}

    for name, title in SECRETS:
        has = config.setting(name) is not None
        hint = " (вже є; Enter — лишити)" if has else ""
        secret = act.ask_secret(f"{title}{hint}: ").strip()
        if secret:
            values[name] = secret
    for name, title in IDS:
        current = config.setting(name)
        answer = act.ask(f"{title}{f' [Enter — {current}]' if current else ''}: ").strip()
        if answer:
            values[name] = answer
    config.save_settings(values)
    act.say("Збережено (лише для її користувача Windows).")

    key = config.setting("ANTHROPIC_API_KEY")
    act.say(act.check_ai(key) if key else "Ключа ШІ немає — ШІ не працюватиме")
    token = config.setting("TELEGRAM_BOT_TOKEN")
    if token:
        act.say(act.check_bot(token, config.setting("TELEGRAM_BOT_CHANNEL_ID")))
    else:
        act.say("Токена бота немає — бот не працюватиме")

    if _yes(act.ask("Відкрити вікно входу на Шафу (вона вводить пароль сама)? (так/ні): ")):
        act.login()

    if act.windows:
        desktop = act.desktop()
        python = Path(sys.executable).resolve()
        icon = python.parent.parent / "shafa.ico"
        act.shortcut(
            desktop / APP_LINK, python.with_name("pythonw.exe"), "-m shafa app",
            icon if icon.is_file() else None,
        )  # fmt: skip
        act.shortcut(desktop / ROOT_LINK, root)
        act.say(f"На робочому столі: «{APP_LINK[:-4]}» і «{ROOT_LINK[:-4]}»")
    act.say("Готово. Програма відкривається ярликом «Шафа-помічник».")
    return 0
