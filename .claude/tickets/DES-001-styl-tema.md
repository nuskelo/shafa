# DES-001 · Новий стиль і перемикач світла/темна тема

**Вага:** звичайна. **Автор:** Архітектор · 01.11.2026 · Модель: Sonnet · high
**Гілка:** `feature/DES-001-styl-tema` від `origin/main` (5c5f9dd, #85 UX-007) після `git fetch`.

## Навіщо
Влад затвердив новий теплий дизайн (світла + темна тема). Цей тікет — увесь вигляд програми, **крім панелі стану** (її розмітка — DES-002).

## Пакет дизайну
- `.claude/tickets/DES-dyzain/` — README.md + `novyi-dyzain/` (style.css, tema.html, panel-stanu.html). **Закомітити** разом з тікетами.
- Еталон (НЕ комітити, лише дивитись): `dist/dyzain-etalon/shafa-dyzain/maket/index.html` (відкрити в браузері) і `dist/dyzain-etalon/shafa-dyzain/skrynshoty/` (15 екранів × 1400/390 × light/dark). Живий макет: https://claude.ai/artifact/TWunZ7kWBQFWdTqjF5BMPg

## Що зробити (кожен пункт — окремий коміт)
0. Перший коміт: `.claude/handoff/UX-007.md` (+ `UX-007-screens/`) → `.claude/archive/handoff/`; тікети DES-001…003 і `DES-dyzain/`.
1. `src/shafa/static/style.css` ← `.claude/tickets/DES-dyzain/novyi-dyzain/style.css` **цілком, без правок**. Старі класи `.status/.tile` у ньому є — стара панель до DES-002 виглядатиме пристойно.
2. Тема. **Увага: CSP `script-src 'self'` (web.py:309) — вбудовані `<script>` з tema.html НЕ спрацюють.** Тому:
   - новий `src/shafa/static/theme.js` = фрагмент 1 з tema.html (ставить `data-theme="dark"` на `<html>` з `localStorage["shafa-theme"]`); додати у словник `STATIC` (web.py, дозволені файли) з типом JS;
   - `<script src="/static/theme.js"></script>` одразу ПІСЛЯ `<link rel="stylesheet" href="/static/style.css">` у `base.html`, `quit.html`, `error.html` — без `defer`, щоб не було білого «спалаху»;
   - обробник кліку (фрагмент 3) — в `app.js`;
   - кнопка (фрагмент 2): у `base.html` — перед `<form … class="quit">`, поза `{% if can_quit %}` (тобто завжди); у `card.html` (`header.top.top-card`) — останнім елементом `.top-in`, після `#robot.mini-wrap`.
   - За замовчуванням — світла; системну тему НЕ враховувати (рішення Влада).
3. Тести: `/static/theme.js` → 200 і тип JS; кнопка `data-theme-toggle` є на списку, Картці, Налаштуваннях, Сесіях; у відповідях сторінок немає `<script>` без `src` (CSP).

## Фаза 0 (лише гіпотези; зупинка лише якщо DRIFT міняє план)
- HYP-1: усі класи, що стилізовані в старому style.css і є в шаблонах після UX-007, є і в новому CSS. (Архітектор звірив — розбіжностей 0; підтверди.)
- HYP-2: `STATIC` у web.py — словник «ім'я → тип»; `pyproject.toml` (`static/*`) бере новий файл у пакет; `ops/build_windows.py` не має окремого списку static.
- HYP-3: `app.js` підключено в `<head>` без defer; обробник через `document.addEventListener("click")` працює з будь-якого місця.
Результат — коротко в `.claude/handoff/DES-001.md`.

## Доказ (Definition of Done)
- `make ci` зелений; тест на theme.js червоніє, якщо прибрати його зі `STATIC`.
- Скриншоти на копії з підставною Шафою (як підняти — QA-004 п. 1–2, порт 8799, не 8765): екрани 03 (Картка), 09 (Опубліковані), 13 (Налаштування), 15 (Вийти) — 1400 і 390, light і dark → `.claude/handoff/DES-001-screens/`. Звірити з `skrynshoty/` (розкладка, кольори, розміри; дані можуть відрізнятися; панель стану — ще стара, це ок).
- Вручну: темна → перезавантажити → темна без білого спалаху; кнопка є і на Картці.
- Жодних `transition`/`animation`, крім рядка `* { … none }` у CSS.
- Обрізай вивід команд (`| tail -n 40`).

## Для Власника (у звіті)
Після мержу — перезапуск програми на Mac (команду дасть Архітектор).

## Заборонено
`panel.html`, `panel_mini.html` (це DES-002); тексти; логіка; `db.py`, `rules.py`, `robot/guard.py`, `browser.py`, `RULES_VERSION`; `.github/`; `git pull` у main, пуш у main, мерж. Не комітити `dist/`.
