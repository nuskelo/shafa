"""Стан ранньої перевірки входу на сайт (KLB-018 для Клубку, ROBOT-019 для Шафи). Без браузера.

Робот раз відкриває форму (лише читання) і пише тут результат: `{at, ok, why}` у файл теки даних.
Панель показує «треба увійти», доки вхід злетілий і вікно входу після перевірки не закривали;
Власнику про втрату входу — одне повідомлення на місцеву добу (`alert_due`/`alert_sent`).
Кожен сайт має свої два файли — це й відрізняє `Store` Шафи від `Store` Клубку.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from shafa import config

FRESH_MIN = 30  # хв: настільки свіжа перевірка годиться «перед стартом»


class Store:
    def __init__(self, state: str, alert: str) -> None:
        self.state, self.alert = state, alert  # імена файлів у теці даних

    @staticmethod
    def _path(name: str, folder: Path | None) -> Path:
        return (folder or config.data_dir()) / name

    def _read(self, name: str, folder: Path | None) -> dict | None:
        try:
            data = json.loads(self._path(name, folder).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, name: str, data: dict, folder: Path | None) -> None:
        path = self._path(name, folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def write(self, ok: bool, now: datetime, why: str = "", folder: Path | None = None) -> None:
        self._write(
            self.state, {"at": now.isoformat(timespec="seconds"), "ok": ok, "why": why}, folder
        )

    def read(self, folder: Path | None = None) -> tuple[datetime, bool, str] | None:
        data = self._read(self.state, folder)
        if not data or not isinstance(data.get("ok"), bool):
            return None
        try:
            return datetime.fromisoformat(data["at"]), data["ok"], str(data.get("why") or "")
        except (KeyError, ValueError):
            return None

    def verdict(
        self,
        now: datetime,
        login_at: datetime | None,
        folder: Path | None = None,
        minutes: int = FRESH_MIN,
    ) -> bool | None:
        """Чи годиться перевірка «перед стартом»: True — вхід є, False — злетів, None — свіжої
        немає (її не було, вона застаріла чи вікно входу закривали після неї)."""
        found = self.read(folder)
        if found is None:
            return None
        at, ok, _why = found
        if now - at > timedelta(minutes=minutes) or (login_at is not None and login_at >= at):
            return None
        return ok

    def failed(self, login_at: datetime | None, folder: Path | None = None) -> bool:
        """Остання перевірка — «вхід злетів», і вікно входу після неї не закривали."""
        found = self.read(folder)
        if found is None or found[1]:
            return False
        return login_at is None or login_at < found[0]

    def alert_due(self, now: datetime, folder: Path | None = None) -> bool:
        """Власнику про втрату входу — не частіше разу на місцеву добу."""
        data = self._read(self.alert, folder)
        return not data or data.get("day") != f"{now:%Y-%m-%d}"

    def alert_sent(self, now: datetime, folder: Path | None = None) -> None:
        self._write(self.alert, {"day": f"{now:%Y-%m-%d}"}, folder)
