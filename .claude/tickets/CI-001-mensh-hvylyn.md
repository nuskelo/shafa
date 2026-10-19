# CI-001 · Менше хвилин GitHub Actions

> Архітектор, 19.10.2026. Після мержу PR #33. Гілка `feature/CI-001-mensh-hvylyn` від свіжого `origin/main`. Першим комітом — тікет, STATUS.md, перенесення `.claude/handoff/BOT-003.md` в архів.

## Проблема
За добу витрачено ~700 хв із 3000/міс. Причини: Windows-хвилини рахуються ×2; кожен пуш у гілку PR перезапускає всі 3 перевірки; після мержу все ще раз біжить на `push: main`; старі запуски не скасовуються.

## Що зробити (`.github/workflows/quality.yml`, `windows-package.yml`)
1. Прибрати `push: branches: [main]` з обох (код уже перевірено в PR).
2. `concurrency: { group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true }` в обох.
3. `windows-package.yml`: лише `workflow_dispatch` (вручну) + `pull_request` з `paths:` на файли пакування/встановлення (`src/shafa/install.py`, `src/shafa/wizard.py`, скрипти/spec збирання, `pyproject.toml`, сам workflow — перевір, що ще впливає на пакет). Пакет потрібен лише перед поверненням програми Вікторії.
4. `windows-tests` (у `quality.yml`): лишається на PR, але `needs: quality` — якщо Linux-тести червоні, Windows не запускається. `timeout-minutes` розумний для обох джобів.
5. Кеш pip (`actions/setup-python` `cache: pip`), якщо ще нема.
6. `docs/` / `CLAUDE.md`: правило мержу — зелені `quality` і `windows-tests`; `windows-package` — коли змінено пакування або перед випуском для Вікторії (запуск вручну: Actions → windows-package → Run workflow).
7. Виконавцю: пушити гілку один раз у кінці (не після кожного коміту).

## Межі
Код програми не чіпати. `.env`, пілот, канали, сайт — ні.

## Кінець
`make ci` → журнал, handoff, пуш, PR → зупинись. У handoff — скільки хвилин тривали джоби останнього запуску (з логів GitHub, якщо видно).
