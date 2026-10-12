# INF-001 · Риштування репозиторію

**Вага:** звичайна. **Автор:** Архітектор · 12.10.2026
**Гілка:** `chore/INF-001-ryshtuvannia` від `origin/main` після `git fetch` (`git switch -c chore/INF-001-ryshtuvannia --no-track origin/main`).

## Навіщо
Порожній репозиторій → робоча основа: правила для виконавця (`CLAUDE.md`), структура папок, Python-проєкт, `make ci` з лінтом і тестами, реєстри стану. Після цього кожен наступний тікет має куди класти код і як себе перевіряти.

## Контекст (прочитай перед роботою)
- `docs/process.md` — як ми працюємо (ролі, петля, Фаза 0, git, звіти). **Прочитай повністю.**
- `docs/concept.md` — що будуємо, глосарій, чого НЕ робимо.
- `docs/rules.md` — правила заповнення картки (для майбутніх CORE-тікетів, тут лише знати).
- Ці файли й `.claude/` уже лежать у робочій копії як незакомічені — їх писав Архітектор. Закоміть їх у цій гілці **без змін змісту** (окремим комітом `docs: concept, rules, process, research від Архітектора`).

## Фаза 0 (лише гіпотези)
- HYP-1: на GitHub у `main` є хоча б один коміт (README від Власника), `git fetch` бачить `origin/main`.
- HYP-2: на Mac є Python ≥ 3.12 (`python3 --version`); якщо ні — UNKNOWN, зупинись і напиши Власнику команду встановлення (Homebrew), одну.
- HYP-3: у робочій копії є незакомічені `docs/concept.md`, `docs/rules.md`, `docs/process.md`, `docs/research/*.md`, `.claude/tickets/INF-001-ryshtuvannia.md`, `.claude/ARCHITECT.md`, `.claude/state/STATUS.md`, `OPEN_QUESTIONS.md`.
Якщо HYP-1 або HYP-3 — DRIFT, зупинись і напиши звіт. Інакше працюй далі без зупинки.

## Що зробити (кожен пункт — окремий коміт)
1. **Документи Архітектора** — закомітити як є (див. «Контекст»).
2. **Python-проєкт:**
   - `pyproject.toml`: пакет `shafa` (src-layout: `src/shafa/__init__.py` з `__version__ = "0.1.0"`), `requires-python = ">=3.12"`, без runtime-залежностей поки що; dev-залежності `pytest`, `ruff` — **закріплені версії** (`==`).
   - Налаштування ruff: line-length 100, правила за замовчуванням + `I` (сортування імпортів). Кодування файлів UTF-8.
   - `tests/test_smoke.py`: імпорт `shafa`, `__version__` — рядок формату `X.Y.Z`.
3. **Makefile:** `make setup` (створює `.venv`, ставить dev-залежності), `make lint` (`ruff check` + `ruff format --check`), `make test` (`pytest -q`), `make ci` = lint + test. Працює на Mac; Windows-збірку зробимо окремим тікетом пізніше.
4. **Службові файли:**
   - `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.env`, `*.sqlite`, `*.db`, `data/`, `result.json`, `*.jpg`, `*.jpeg`, `*.JPG` (крім `tests/fixtures/**`), `.DS_Store`.
   - `.env.example` — **лише назви**: `ANTHROPIC_API_KEY=`, `TELEGRAM_BOT_TOKEN=`, `TELEGRAM_OWNER_CHAT_ID=`, `TELEGRAM_SELLER_CHAT_ID=`, `TELEGRAM_CHANNEL_ID=`, `SHAFA_ROOT_DIR=`.
   - `docs/TECH_DEBT.md` (заголовок + формат рядка `TD-NNN · що · де · умова закриття`), `LEARNED_CONVENTIONS.md` (заголовок + формат `LC-NNN`).
   - `.claude/templates/ticket.md` і `.claude/templates/handoff.md` — шаблони з розділів 5 і 6 `docs/process.md`.
   - `README.md` — 10–15 рядків: що це, посилання на `docs/concept.md`, `make setup && make ci`.
5. **`CLAUDE.md`** за розділами `docs/process.md`, коротко (≤ 150 рядків):
   - ролі й петля (твої зупинки закінчуються блоком «Для Власника: … · Скажи Архітектору: …»);
   - глосарій — з `docs/concept.md`;
   - **жорсткі правила проєкту:** нічого не публікувати й не відправляти на живу Шафу чи в живий Telegram-канал без окремого тікета й дозволу Власника; секрети лише в `.env` поза git; реальні дані Продавчині (експорт, фото, її оголошення) — не в репозиторій, тести лише на фікстурах; код кросплатформний (Mac для розробки, Windows для продакшну: `pathlib`, UTF-8, кирилиця в шляхах);
   - git-правила (розділ 7), Фаза 0 (розділ 4), ваги (розділ 3), звіт (розділ 6), ритуал старту з рядком `[Pre-flight: …]` (розділ 11).
6. **Журнал:** `.claude/state/log/<дата-час з команди date>-INF-001.md`, 3–5 рядків. Останній коміт гілки.

## Доказ (Definition of Done)
- `make ci` зелений локально (вивід у звіт: кількість тестів, «All checks passed»).
- Перевір, що smoke-тест **червоніє**, якщо прибрати `__version__` (поверни назад; напиши у звіті, що перевірив).
- Гілка запушена, PR відкритий у `main`. CI на GitHub ще немає — це INF-002.

## Для Власника (у звіті)
- Підтвердити пуш гілки кнопкою.
- Мержити після слова Архітектора «мерж».

## Заборонено
Пуш у `main`, мерж, `git pull` у `main`, `.github/`, `push --force`, `reset --hard`, сервер. Секрети — ні в код, ні в тести, ні у звіт. Не змінювати зміст документів Архітектора (опечатки — записати у звіт як пропозицію).
