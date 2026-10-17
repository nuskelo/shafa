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
    ("SITE_INTEGRATION_KEY", "Ключ сайту Viktoria Shoes (необов'язково; Enter — пропустити)"),
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


# Ярлик через системний IShellLinkW (Unicode). WScript.Shell з PowerShell зберігає назву ярлика
# в ANSI — «Шафа — фото товарів.lnk» стає «????» (знайдено в windows-tests, WIN-001).
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_SHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_PERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
# Номери методів у таблиці COM (порядок оголошення в shobjidl.h / objidl.h; 0–2 — IUnknown).
_RELEASE, _GET_PATH, _SET_WORKING_DIR, _SET_ARGUMENTS, _SET_ICON, _SET_PATH = 2, 3, 9, 11, 17, 20
_QUERY_INTERFACE, _LOAD, _SAVE = 0, 5, 6


def _com():  # pragma: no cover — лише Windows (windows-tests)
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    def guid(text: str) -> GUID:
        value = GUID()
        ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(value))
        return value

    def method(obj: ctypes.c_void_p, index: int, *types):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *types)(table[index])

    return ctypes, wintypes, guid, method


def _open_link():  # pragma: no cover — лише Windows
    ctypes, _, guid, method = _com()
    ctypes.oledll.ole32.CoInitialize(None)
    link = ctypes.c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(
        ctypes.byref(guid(_CLSID_SHELL_LINK)), None, 1,  # CLSCTX_INPROC_SERVER
        ctypes.byref(guid(_IID_SHELL_LINK_W)), ctypes.byref(link),
    )  # fmt: skip
    persist = ctypes.c_void_p()
    method(link, _QUERY_INTERFACE, ctypes.c_void_p, ctypes.c_void_p)(
        link, ctypes.byref(guid(_IID_PERSIST_FILE)), ctypes.byref(persist)
    )
    return link, persist


def _release(*objects) -> None:  # pragma: no cover — лише Windows
    _, _, _, method = _com()
    for obj in objects:
        method(obj, _RELEASE)(obj)


def shortcut(link: Path, target: Path, arguments: str = "", icon: Path | None = None) -> None:
    """Ярлик Windows (.lnk) з кирилицею в назві й шляхах."""
    ctypes, wintypes, _, method = _com()
    com, persist = _open_link()
    try:
        method(com, _SET_PATH, wintypes.LPCWSTR)(com, str(target))
        method(com, _SET_ARGUMENTS, wintypes.LPCWSTR)(com, arguments)
        method(com, _SET_WORKING_DIR, wintypes.LPCWSTR)(com, str(target.parent))
        if icon is not None:
            method(com, _SET_ICON, wintypes.LPCWSTR, ctypes.c_int)(com, str(icon), 0)
        method(persist, _SAVE, wintypes.LPCWSTR, wintypes.BOOL)(persist, str(link), True)
    except OSError as error:
        raise OSError(f"Ярлик «{link.name}» не створено: {error}") from error
    finally:
        _release(persist, com)


def shortcut_target(link: Path) -> Path:  # pragma: no cover — лише Windows (перевірка ярлика)
    ctypes, wintypes, _, method = _com()
    com, persist = _open_link()
    try:
        method(persist, _LOAD, wintypes.LPCWSTR, wintypes.DWORD)(persist, str(link), 0)
        buffer = ctypes.create_unicode_buffer(32768)
        method(com, _GET_PATH, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(
            com, buffer, len(buffer), None, 0
        )
        return Path(buffer.value)
    finally:
        _release(persist, com)


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
