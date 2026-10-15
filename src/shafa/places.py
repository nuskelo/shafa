"""Місця на диску її ноутбука: робочий стіл, Головна папка, OneDrive (WIN-001).

На Windows «Робочий стіл» часто перенесено в OneDrive (`…\\OneDrive\\Desktop`): його шлях беремо в
самої Windows (Known Folder), а не `~\\Desktop`. Головна папка з тисячами фото в OneDrive
синхронізувалася б у хмару (квота), а файли «лише в хмарі» докачувались би при читанні — тому
типово вона поза OneDrive (`C:\\Users\\<вона>\\Шафа`), а на робочому столі — ярлик до неї.
"""

import os
from pathlib import Path

ROOT_NAME = "Шафа"
_ONEDRIVE_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"  # FOLDERID_Desktop


def _known_folder(guid: str) -> Path | None:  # pragma: no cover — перевіряється в windows-tests
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    value = GUID()
    ctypes.oledll.ole32.CLSIDFromString(guid, ctypes.byref(value))
    path = ctypes.c_wchar_p()
    try:
        ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(path))
        return Path(path.value) if path.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def desktop() -> Path:
    """Робочий стіл її користувача (на Windows — звідки б його не перенесли)."""
    if os.name == "nt":
        try:
            found = _known_folder(_DESKTOP)
        except OSError:
            found = None
        if found:
            return found
    return Path.home() / "Desktop"


def default_root() -> Path:
    """Типова Головна папка — у її теці користувача, поза OneDrive."""
    return Path.home() / ROOT_NAME


def in_onedrive(path: Path) -> bool:
    """Чи лежить папка в OneDrive (синхронізується в хмару)."""
    resolved = Path(os.path.abspath(path))
    roots = [Path(os.path.abspath(os.environ[v])) for v in _ONEDRIVE_VARS if os.environ.get(v)]
    if any(resolved == r or r in resolved.parents for r in roots):
        return True
    return any(part.lower().startswith("onedrive") for part in resolved.parts)
