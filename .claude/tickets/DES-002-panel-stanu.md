# DES-002 · Панель стану v2 (цифри в прямокутниках, «Старт» праворуч)

**Вага:** звичайна, але **Фаза 0 зі зупинкою до коду** (багато станів). **Автор:** Архітектор · 01.11.2026 · Модель: Sonnet · high
**Гілка:** `feature/DES-002-panel-stanu` від `origin/main` **після мержу DES-001**, після `git fetch`.

## Навіщо
Влад про стару панель: «Старт збоку незрозуміло», «цифри дрібно», «менше місця вниз». Нова розмітка — `.claude/tickets/DES-dyzain/novyi-dyzain/panel-stanu.html` (у ній числа/тексти — приклад зі стану 31.10). Стилі вже в CSS з DES-001, зокрема для станів, яких нема в макеті: `.sp-robot.is-running|is-warn|is-error`, `.sp-error`, `.sp-stop`, `.sp-li.is-warn|is-bad`, `.health-rows` у `.sp-li`, `.sp .fake`.
**Головне правило:** кожен текст і кожна кнопка (name/value, умова появи, disabled), що зараз є в `panel.html` у будь-якому стані, — лишаються.

## Що зробити
1. `panel.html`: `<section class="status">` → `<section class="sp">` за panel-stanu.html. Дані — ті самі поля контексту `_panel_context` (web.py:616).
   - **Робот** `.sp-robot` + клас: `is-running` (tones.robot = ok), `is-warn` (warn), `is-error` (bad), без класу — idle. `.sp-title` = `robot.text`, крапка = tones.robot. `.sp-note` — один головний рядок; решта рядків робота (progress, doing, previous, «Публікує: у цій сесії…», why_not, eta, idle_note, last + посилання, rehearsal, «Не закривайте вікно…», «Пауза — робот зачекає…», robot-help для login_needed/never_logged) — `.sp-line` у `.sp-foot`/`.sp-main`. Помилки crashed/failed — `.sp-error`. `details.more-info.sp-more` з `data-key="robot"`.
   - **4 прямокутники** `.stat`: `stat-key` «Сьогодні опублікує» (`до N Карток · ≈ T` — з `robot.queue_today` і оцінки часу, див. HYP-3); «З минулих постів» `started.old` з `limits.old`; «З нових (бот)» `started.new` з `limits.new`; «Схвалених» `approved`. Що в `stat-key`, коли робот працює/на паузі — запропонуй у Фазі 0.
   - **`.sp-actions`** — форма `post /robot` з `token` і `back`; кнопки й умови як у `.status-actions` зараз: start → `.sp-start` (disabled як зараз), rehearse → `.sp-rehearse`, stop → `.sp-stop`, pause / resume / login / login_done — у тій самій формі.
   - **ШІ** `.sp-box.sp-ai`: заголовок «ШІ{: busy}»; форма `post /ai` (token, back) з `.sp-find` (`action=update`, disabled коли busy) у заголовку; recheck і посилання refill — видимі без «Детальніше». Прямокутники замість `ai.counters`: «Готові до перевірки» (`.stat-link` → /review), «Готові йти на ШІ», «ШІ зараз заповнює» (коли > 0), «Чекають своєї черги» (held), «Нових постів з бота» (`.stat-warn` коли > 0). filling / checking / «Тримайте вікно…» / finished / note — `.sp-line`; пояснення (Натискайте після…, held-текст, ai.detail, вартість, «Програму оновили…» якщо не дія) — у `details.sp-more data-key="ai"`.
   - **Список** `.sp-box.sp-list`: Бот (+ форма `/bot` «Запустити бота», detail у details `data-key="bot"`), Самоперевірка (усі гілки заголовка; `ul.health-rows` всередині `.sp-li`, `details data-key="health" open`; кнопка `.sp-check` у формі `/health`), Сайт (цифри `<b>`). `.sp-li.is-warn|is-bad` за tones.
   - `fake` → `<p class="fake">` першим у `.sp`. Рядок `data-counts` у кінці — лишити.
2. Числа без розбору текстів: додати в `AiState` (web.py:360) числові поля (напр. `to_check`, `held_count`) — **без зміни підрахунку**. `counters`, `groups_text` лишити (їх бере `panel_mini.html`).
3. `app.js` `markStale` шукає `.status` (≈ рядок 227) — має працювати з `.sp` (на Картці лишається `.status.mini`).
4. `panel_mini.html` — не чіпати.
5. Тести: оновити наявні тести панелі (не видаляти перевірки текстів/кнопок). Новий тест-«перепис»: для кожного стану (робот: усі коди з `ROBOT_TONES` + never_logged; ШІ: busy, error, filling, finished, held, new_posts, recheck/refill; бот: running, no_token, stopped, failed; самоперевірка: none, running, ok, problems warn/bad, old; fake) — множина видимих текстів і кнопок (name/value/disabled) нового шаблону ⊇ старого (старий взяти з `git show origin/main:src/shafa/templates/panel.html` у фікстуру).

## Фаза 0 — ЗУПИНКА до коду
У `.claude/handoff/DES-002.md`:
- HYP-1: **таблиця всіх гілок panel.html**: умова → текст/кнопка → куди в новій розмітці. Це головне.
- HYP-2: `/robot/panel` (web.py:1139) і `base.html` include рендерять panel.html тим самим контекстом → новий шаблон працює в обох без змін web.py.
- HYP-3: звідки `robot.eta_text` і чи є хвилини окремим числом; якщо лише рядок — пропозиція (поле в State, розрахунок той самий).
- HYP-4: `held` (batch.held_back) і «Готові до перевірки» доступні в `ai_state` числами.
- HYP-5: `app.js` (swap, updateCounts, «кнопку тиснуть», details[data-key]) не залежить від інших класів старої панелі.
- HYP-6: список тестів, що перевіряють розмітку `.status/.tile/.tile-sub`.
- Пропозиції: що в `stat-key` під час роботи; нульові прямокутники ШІ — ховати чи «0».
Потім — «DES-002 фаза 0 готова».

## Доказ (Definition of Done)
- `make ci` зелений; тест-перепис червоніє, якщо прибрати будь-яку кнопку чи рядок.
- Скриншоти → `.claude/handoff/DES-002-screens/`: Головна (01) у стані «чекає» 1400/390 × light/dark — як `skrynshoty/01-holovna-*`; висота панелі на 1400 ≈ 340 px (±30). Плюс 1400 light: робот працює, пауза, помилка, login_needed, ШІ працює, самоперевірка з проблемами, fake.
- Автооновлення кожні 5 с не скидає розкриті «Детальніше»; при зупиненому сервері — жовта примітка «Немає зв'язку…».

## Заборонено
Тексти (виняток — підписи прямокутників: «Сьогодні опублікує», «З минулих постів», «З нових (бот)», «Схвалених», «Готові до перевірки», «Готові йти на ШІ», «ШІ зараз заповнює», «Чекають своєї черги», «Нових постів з бота»); логіка й підрахунки; `db.py`, `rules.py`, `robot/guard.py`, `browser.py`, `RULES_VERSION`; `.github/`; пуш у main, мерж; `dist/`.
