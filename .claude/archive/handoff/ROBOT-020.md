# ROBOT-020 — готово, PR відкрито
**Гілка:** `fix/ROBOT-020-kanal-ne-vidpoviv` · **PR:** #118 · **Перевірки:** `make ci` зелений (4083 тести); `quality` — на PR
1. Фаза 0: HYP-1 VERIFIED, але з поправкою: `__main__.py:485` передавав лише `Presence.check` (рядок); `retry_after` — атрибут `Presence` (`bot/presence.py`, `look`), `site.py` теж читає його. Тепер `channel_presence` віддає сам `Presence`, у `Run` іде `presence=.check` + `retry_after=` (інші виклики `Run` не змінились). HYP-2 VERIFIED: `klubok_publish`/`klubok/*` живої перевірки каналу не роблять, лише `publish.known_gone` (база) — змін для Клубку немає.
2. Файли: `robot/publish.py` (`_channel`, `_countdown`, `_post_deleted`, `Report.deferred`), `robot/control.py` (рядок панелі, `LAST["telegram_busy"]`), `__main__.py`, `tests/test_robot_publish.py`, `tests/test_ux002.py`, `CLAUDE.md`.
3. Відхилення від тікета: «429 тричі» і «2 повтори» зведено до одного правила — до 3 перевірок на Картку (для 429 між ними чекаємо `retry_after`+5 с). Після 1 відповіді «retry after 674» вміщується одне очікування, друге вже > 20 хв → `telegram_busy`.
4. Нові TD / OPEN-Q: немає. Тести доводять фікс: з вимкненим відкладенням і повторами червоніють 8 нових/змінених.
5. Журнал: останній коміт гілки.
**Що потрібно:** «мерж» від Архітектора після зеленого `quality`.
**Для Власника:** нічого; після оновлення програми «Старт» як завжди.
