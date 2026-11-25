"""KLB-018: чи не злетів вхід у Клубок — раннє попередження. Без браузера й мережі.

Вхід Клубку — лише сесійна cookie `PHPSESSID` (без «запам'ятати»): через 1–2 дні вона злітає, і
автостарт марно будить робота. `robot/klubok_login_check` раз відкриває `/item/add` (лише
читання) і пише тут результат; `klubok_auto.Auto` не запускає сесію, поки результат поганий;
панель показує «Робот не увійшов у Клубок»; Власнику йде одне повідомлення на добу.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from shafa import config

STATE = "klubok-login-check.json"  # {"at", "ok", "why"} — результат останньої перевірки
ALERT = "klubok-login-alert.json"  # {"day"} — коли востаннє писали Власнику
FRESH_MIN = 30  # хв: настільки свіжа перевірка годиться «перед автостартом»


def _path(name: str, folder: Path | None) -> Path:
    return (folder or config.data_dir()) / name


def _read(name: str, folder: Path | None) -> dict | None:
    try:
        data = json.loads(_path(name, folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(name: str, data: dict, folder: Path | None) -> None:
    path = _path(name, folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write(ok: bool, now: datetime, why: str = "", folder: Path | None = None) -> None:
    _write(STATE, {"at": now.isoformat(timespec="seconds"), "ok": ok, "why": why}, folder)


def read(folder: Path | None = None) -> tuple[datetime, bool, str] | None:
    data = _read(STATE, folder)
    if not data or not isinstance(data.get("ok"), bool):
        return None
    try:
        return datetime.fromisoformat(data["at"]), data["ok"], str(data.get("why") or "")
    except (KeyError, ValueError):
        return None


def verdict(
    now: datetime,
    login_at: datetime | None,
    folder: Path | None = None,
    minutes: int = FRESH_MIN,
) -> bool | None:
    """Чи годиться перевірка «перед автостартом»: True — вхід є, False — злетів, None — свіжої
    немає (її не було, вона застаріла чи Власниця входила після неї)."""
    found = read(folder)
    if found is None:
        return None
    at, ok, _why = found
    if now - at > timedelta(minutes=minutes) or (login_at is not None and login_at >= at):
        return None
    return ok


def failed(login_at: datetime | None, folder: Path | None = None) -> bool:
    """Остання перевірка — «вхід злетів», і вікно входу після неї не закривали."""
    found = read(folder)
    if found is None or found[1]:
        return False
    return login_at is None or login_at < found[0]


def alert_due(now: datetime, folder: Path | None = None) -> bool:
    """Власнику про втрату входу — не частіше разу на місцеву добу."""
    data = _read(ALERT, folder)
    return not data or data.get("day") != f"{now:%Y-%m-%d}"


def alert_sent(now: datetime, folder: Path | None = None) -> None:
    _write(ALERT, {"day": f"{now:%Y-%m-%d}"}, folder)
