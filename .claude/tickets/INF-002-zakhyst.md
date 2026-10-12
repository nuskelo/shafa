# INF-002 · Захист: git-хуки, дозволи Claude Code, CI

**Вага:** критична (доступи й захист гілок). **Автор:** Архітектор · 12.10.2026
**Гілка:** `chore/INF-002-zakhyst` від `origin/main` після `git fetch` (після мержу PR #1).
**Дозвіл:** у цьому тікеті дозволено створювати `.github/` (лише пункт 5).

## Навіщо
Зараз правила «не пушити в main», «без force-push», «нічого не видаляти» тримаються лише на домовленості. Після тікета їх тримають три шари: git-хуки на Mac, дозволи Claude Code і захист `main` на GitHub з обов'язковими перевірками. Заодно CI проганяє тести на Python 3.12 і на Windows — закриває TD-001.

## Фаза 0 — повністю, зупинка до коду
- HYP-1: Claude Code у VS Code читає `.claude/settings.json` проєкту; синтаксис правил `permissions.allow / ask / deny` для Bash і Read — звір із документацією Claude Code (посилання й цитата формату у звіт). Чи покриває `Bash(git push:*)` варіанти з `--force`, `-f`, `--delete`, `--no-verify`, `+refspec`, і в якому порядку діють ask/deny.
- HYP-2: `pip install --group dev` потребує pip ≥ 25.1; pip, вбудований у Python 3.12 (останній патч), старіший → `make setup` і CI мають спершу оновити pip. Перевір версії.
- HYP-3: pre-push хук отримує на stdin рядки `<local ref> <local sha> <remote ref> <remote sha>`; видалення = local sha з нулів; force (не fast-forward) визначається `git merge-base --is-ancestor <remote sha> <local sha>`. Звір із `git help githooks`.
- HYP-4: актуальні версії й SHA комітів для `actions/checkout` і `actions/setup-python` (закріплюємо за SHA з коментарем-версією). Звідки взяв — у звіт.
- HYP-5: на GitHub для приватного репозиторію на акаунті Власника доступний захист гілок / rulesets з обов'язковими перевірками (план акаунта може обмежувати). Якщо не перевірити — UNKNOWN, Власник перевірить сам.
Після Фази 0 зупинись: «INF-002 фаза 0 готова».

## Що зробити (кожен пункт — окремий коміт)
0. Перенести `.claude/handoff/INF-001.md` → `.claude/archive/handoff/INF-001.md`.
1. **`.githooks/pre-commit`:** забороняє коміт, якщо поточна гілка `main` (або HEAD від'єднаний від гілки тікета). Зрозуміле повідомлення українською.
2. **`.githooks/pre-push`:** пускає лише гілки `^(feature|fix|chore|docs)/[A-Z]+-[0-9]+(-[a-z0-9-]+)?$`; забороняє пуш у `main`, видалення гілок і тегів, не-fast-forward (force). POSIX `sh`, без залежностей.
3. **`ops/install-hooks.sh`:** `git config core.hooksPath .githooks` + `chmod +x`; ідемпотентний; друкує, що зробив.
4. **`.claude/settings.json`:** `git push` → ask; `--force`, `-f`, `--force-with-lease`, `--delete`, `--no-verify`, `git reset --hard`, `git push origin main`, `ssh`, `scp` → deny; читання `.env` і `*.env` → deny. Формат — за результатом HYP-1.
5. **`.github/workflows/quality.yml`:** на `pull_request` і `push` у `main`. Дві задачі з фіксованими іменами (вони стануть обов'язковими перевірками):
   - `quality` — ubuntu-latest, Python 3.12: оновити pip, `pip install --group dev`, `ruff check .`, `ruff format --check .`, `pytest -q`;
   - `windows-tests` — windows-latest, Python 3.12: оновити pip, встановити dev-групу, `pytest -q`.
   Actions закріплені за SHA. `permissions: contents: read`. Жодних секретів.
6. **Makefile:** `make setup` оновлює pip у `.venv` перед `--group dev` (за HYP-2).
7. **Тести хуків:** `tests/test_githooks.py` — запускає `pre-push` і `pre-commit` у тимчасовому git-репозиторії (`tmp_path`) з підставними рядками stdin: пуш гілки тікета → 0; пуш у `main` → не 0; видалення → не 0; force → не 0; гілка з неправильною назвою → не 0; коміт у `main` → не 0. На Windows — `skip` з причиною (хуки там не ставимо).
8. **Документи:** у `CLAUDE.md` — рядок про `ops/install-hooks.sh` у ритуалі старту (перевірка `git config core.hooksPath`); `TD-001` закрити (CI на 3.12 і Windows) з посиланням на перший зелений прогін.
9. **Журнал** — останнім комітом.

## Доказ (Definition of Done)
- `make ci` зелений локально; тести хуків червоніють, якщо в хуку прибрати відповідну перевірку (перевір хоча б для «пуш у main» і «force»; напиши у звіті).
- На PR зелені `quality` і `windows-tests`.
- Ручна перевірка після `ops/install-hooks.sh` на Mac: `git commit` у `main` відхилено (вивід у звіт; коміт не створюється).

## Для Власника (у звіті, по одній дії)
1. Після мержу на Mac: `sh ops/install-hooks.sh` (у папці `~/shafa`).
2. На GitHub: захист `main` — покроково, назви кнопок як у реальному інтерфейсі (Settings → Rules/Branches): лише через PR, обов'язкові перевірки `quality` і `windows-tests`, заборона force-push і видалення, без винятків (bypass list порожній).

## Заборонено
Пуш у `main`, мерж, `git pull` у `main`, `push --force`, `reset --hard`, зміни `.github/` поза пунктом 5, будь-які секрети в workflow. Не вмикати захист на GitHub самому — це робить Власник.
