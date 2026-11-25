"""KLB-018: чи не злетів вхід у Клубок — раннє попередження. Без браузера й мережі.

Вхід Клубку — лише сесійна cookie `PHPSESSID` (без «запам'ятати»): через 1–2 дні вона злітає, і
автостарт марно будить робота. `robot/klubok_login_check` раз відкриває `/item/add` (лише
читання) і пише тут результат; `klubok_auto.Auto` не запускає сесію, поки результат поганий;
панель показує «Робот не увійшов у Клубок»; Власнику йде одне повідомлення на добу.
"""

from shafa.loginstate import FRESH_MIN, Store

STATE = "klubok-login-check.json"  # {"at", "ok", "why"} — результат останньої перевірки
ALERT = "klubok-login-alert.json"  # {"day"} — коли востаннє писали Власнику

_store = Store(STATE, ALERT)
write, read, verdict = _store.write, _store.read, _store.verdict
failed, alert_due, alert_sent = _store.failed, _store.alert_due, _store.alert_sent

__all__ = [
    "ALERT",
    "FRESH_MIN",
    "STATE",
    "alert_due",
    "alert_sent",
    "failed",
    "read",
    "verdict",
    "write",
]
