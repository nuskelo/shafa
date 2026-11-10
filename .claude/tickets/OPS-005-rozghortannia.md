# OPS-005 · Розгортання на сервері і перехід Mac → сервер

**Вага:** критична (сервер). **Автор:** Архітектор · 10.11.2026 · Модель: Sonnet · high
**Гілка:** `feature/OPS-005-server` від `origin/main` після мержу OPS-004.
**Основа:** `.claude/handoff/OPS-002-proekt.md` п. 6, 7.

## Що зробити (у репо; сервер робить Власник за командами Архітектора)
1. `ops/server/`: `shafa-bot.service` (systemd, `User=ubuntu`, `SHAFA_DATA_DIR`, `Restart=always`, `RestartSec=10`, `MemoryMax=400M`), таймер нічної копії бази (`sqlite3 .backup`, 7 копій), `install.sh` (venv, `pip install -e .` без браузера Playwright, служба), рядок `authorized_keys` для ключа обміну з примусовою командою (шаблон, без ключа).
2. `docs/install-server.md`: по кроках для Власника — пояс Europe/Kyiv, swap 1–2 ГБ, пакети, клон, `.env` (лише назви; вводить сам, `chmod 600`), ключ обміну, запуск, журнал (`journalctl -u shafa-bot -n 50`), оновлення (`git pull --ff-only` + `systemctl restart`).
3. Перехід (п. 7 проєкту): чекліст — Mac `quit` → `bot-pack` → `scp` → `bot-unpack` → старт служби → Mac з `SHAFA_BOT_SERVER` → перевірка **тестовим постом у тестовому каналі**.
4. Перевірка скриптів без сервера: `shellcheck`, `systemd-analyze verify` (якщо є в CI-образі), тест рендеру шаблонів.

## Заборонено
`ssh`/`scp` на сервер (лише Власник); секрети; `.github/`; пуш у main, мерж. Вивід обрізай.
