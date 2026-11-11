# KLB-003 · Клубок: вхід, знімок її оголошень, публікація (+ швидке «місце в черзі»)

**Вага:** критична (чужий сайт, реальний акаунт, дубль = блок). **Автор:** Архітектор · 11.11.2026
**Гілка:** `feature/KLB-003-publikatsiia-klubku` від `origin/main` після `git fetch` (має бути мерж #100 / KLB-002).
Перший коміт: `.claude/handoff/KLB-002.md` → `.claude/archive/handoff/`; якщо `.claude/tickets/KLB-002-cherha-klubku.md` немає в `origin/main` — додай його в той самий коміт.
**Проєкт змін:** `docs/klubok.md` (розділ «Публікація»). Після тікета він має відповідати коду.

## Навіщо
Робот Клубку вперше працює з живим сайтом: Влад входить руками, робот читає її ~1787 оголошень (для дублів), бере Картки з черги KLB-002 і публікує — по одній, з ліміту доби. Перші 1–3 — при Владі, після мержу. Плюс: рядок «Клубок» на Картці зараз рахує «місце N» через повний `plan` з пошуком дублів — з 1787 оголошеннями це повільно.

## Що звірив Архітектор (Фаза 0 перевіряє, не на слово)
- `~/klubok-4/add.html`: форма `@submit.prevent='reCaptcha("6Lejz…", "Add_new_item", submitForm)'`; кнопка `#btn-submit` (`type=submit`, «Опубликовать», `:disabled="submitted || numberOfUploading() > 0 || isDisabled"`); `reCaptcha` → `grecaptcha.ready/execute` → `submitForm(token)` → `userItem.captcha = token` → `axios.post('https://klubok.com/api/add-edit-used', …)`. Відповідь: `status == 'success' && used_id > 0` → `location.replace('/payment/{used_id}?action=used-submit-ok-logged')`; `status == 'Error'` → `location.replace('/error429?action=Add_new_item&score=…&message=…')`. Скрипт: `https://www.google.com/recaptcha/api.js?render=…`.
- `robot/klubok_guard.py`: POST лише `/api/upload-used-photo`; `payment` — у `DANGER_SUBSTRINGS`; чужий хост — лише GET; `/api/get-items` уже в `PAGES`.
- `robot/klubok_form.py`: `Session._route` (блок → `route.abort()` + `blocked`), `Session.click` (перевірка `click_allowed`), `KlubokDriver` + `verify` (KLB-001).
- `klubok/queue.py`: `take` (кличе `sync`, перевіряє дубль кожної), `start` (`queued → publishing`), `published`, `problem` (`queued/publishing → problem`), `recover`. Переходу `publishing → queued` немає.
- `web.py:1966` `_queue_place` → `klubok_queue.plan(conn, root)` → `judge` → `dup.find` **для кожного кандидата** проти всіх `klubok_listings` (зараз 0 рядків, після знімка ~1787) — на кожному відкритті Картки.
- `robot/browser.py:328` `wait_for_manual_login` — профіль і адреса Шафи вшиті (`launch`, `NEW_URL`).
- `sessions`: `started_at`, `ended_at`, `stop_code`, `item_started_at`, `site` (KLB-002: усе наявне читає лише `shafa`).

## Рішення Архітектора (робити так; DRIFT у Фазі 0 — зупинка)
**D1 · Вхід — `robot --klubok-login`.** Профіль `<дані>/klubok-profile/`, сторінка `https://klubok.com/item/add` (якщо не ввійшли — сайт сам веде на вхід, HYP-4). Без guard (вводить людина), той самий `robot.lock`. `wait_for_manual_login` параметризуй (профіль, адреса) — поведінка Шафи без змін. Пароля в програмі немає ніде.

**D2 · Перевірка входу на початку кожної сесії:** GET `/item/add` через guard → є `#addItemForm` і `#user_id` = `198135` (константа `KLUBOK_USER` у коді, як у `docs/klubok.md`). Інакше — стоп `klubok_login`: «Увійдіть у Клубок: python -m shafa robot --klubok-login».

**D3 · Знімок — `klubok/snapshot.py` + `robot --klubok-snapshot`** (лише читання; та сама функція на початку публікації).
- `fetch` усередині сторінки (cookies сесії): `GET /api/get-items?user_id=198135&type=0&sort=date&limit=60&offset=N`, N += 60, поки сторінка < 60; пауза 1–2 с між сторінками; жорстка межа 60 сторінок.
- Запис **в одній транзакції**, лише якщо прочитано повністю: upsert за `id` (`name`, `price`, `sizes` = JSON `dup.title_sizes(name)`, `url` = `link` лише якщо `https://klubok.com/…`, `photo`, `sold`, `moderated`, `renew_date`; `added_at` — лише при вставці). `post_id`, `asked_at`, `brand`, `category_id` не чіпати. Чого немає в повному знімку → `sold = 1` (рядок лишається). `klubok_listings_read` (id 1): `read_at` UTC, `count`.
- Захист: помилка / тайм-аут / не JSON / HTTP ≠ 200 → нічого не пишемо, стоп `klubok_snapshot_failed`. Активних менше ніж 90 % від попереднього `count` → нічого не пишемо, стоп `klubok_snapshot_drop` (рішення людини).

**D4 · Сесія — `robot --klubok-publish --limit N [--post P] [--human-click]`.** `--limit` обов'язковий, 1–30. `--post` — лише Картки цього поста (для живої перевірки сумки, бренду поза довідником).
1. `lock.hold("klubok-publish")` → `queue.recover` (у лог) → `INSERT INTO sessions (started_at, site) VALUES (?, 'klubok')`.
2. D2 → D3 → цикл: щоразу `queue.take(conn, root, today)` (порожньо — стоп із його кодом: вимкнено / знімок старий / ліміт); беремо першу (з `--post` — першу цього поста).
3. `queue.start` → свіже завантаження `/item/add` → `KlubokDriver` + `verify` (KLB-001). Помилка до кліку → `queue.problem` з кодом KLB-001, далі наступна.
4. Квиток (D6) → клік `#btn-submit` (або чекання людини, D5) → чекати відповідь `add-edit-used` ≤ 60 с:
   - `status == "success"` і `used_id > 0` → `queue.published(item, used_id, url)`; `url` — з `link` (одна сторінка `get-items` offset 0 після публікації), інакше — шаблон з HYP-1.
   - `status == "Error"` → `queue.problem(…, "klubok_captcha", "Клубок не прийняв: захист від роботів (оцінка …). Перевірте на Клубку, що оголошення немає.")` → **стоп сесії** `klubok_captcha`.
   - запит пішов, певної відповіді немає (тайм-аут, HTTP ≠ 200, не JSON, інший `status`) → `problem klubok_uncertain` (текст як у `recover`) → **стоп** `klubok_uncertain`. Повтору немає.
   - запит **не пішов** за 60 с (guard не бачив POST `add-edit-used`) → нове `queue.release` (`publishing → queued`, `publish_started_at` лишається — спроба в ліміті) → стоп `klubok_not_sent`, у лог — список заблокованих запитів.
   - HTTP 429 від `klubok.com` на будь-якому запиті → стоп `klubok_429`.
5. Між Картками пауза 60–180 с (випадково). Ctrl+C — доробити поточну (дочекатись відповіді), потім стоп.
6. Кінець: `ended_at`, `stop_code`; звіт у термінал ≤ 15 рядків (виставлено: id · назва; проблеми; чому зупинились). Стопи `klubok_captcha`, `klubok_uncertain`, `klubok_login`, `klubok_429`, `klubok_snapshot_*` → `events.emit(…, loud=True)`.

**D5 · `--human-click`** (запасний план для reCAPTCHA): робот заповнює й звіряє, квиток видає, але сам не тисне; пише «Перевірте форму й натисніть „Опубликовать“ — чекаю 10 хв». Далі — та сама обробка відповіді (D4.4), тайм-аут 10 хв.

**D6 · Guard — лише в режимі публікації** (`--klubok-dry-fill`, `--fake-klubok`, знімок — як були):
- `Ticket` на одну Картку: видається після успішного `verify`, пропускає **один** POST `https://klubok.com/api/add-edit-used` (точний хост і шлях), гасне на першому; другий — блок.
- Клік «Опубликовать» — лише елемент `#btn-submit` і лише з живим квитком, не в `--human-click`.
- reCAPTCHA: GET і POST лише `https://www.google.com/recaptcha/*`; GET `https://www.gstatic.com/recaptcha/*`. Інших чужих POST — ні (HYP-3).
- `/payment/…` і `/error429` лишаються заблокованими: після `success` / `Error` переадресацію обірвано — це очікувано, не стоп (у `blocked` з позначкою «очікувано»). Будь-який інший блок — як зараз.

**D7 · Швидке «місце в черзі» (`web.py`)** — нове `queue.place(conn, root, item_id) -> int | None`: лише рядки `klubok_ads.state = 'queued'`, порядок як у `plan` (зимове → інше → «Літо», далі `posts.date`, `items.id`), сезон — `fields.card_path` / `winter` / `summer_only`. **Без `dup.find`, без `fields.ad`, без запису.** Картка без рядка — `None` («у черзі» без номера). `_queue_place` → `queue.place`.

**D8 · TD у `docs/TECH_DEBT.md`:**
- `TD-072 · windows-tests не встигає за 60 хв (обрізано в #99, #100) · CI / повільні тести · закрито, коли windows-tests проходить повністю < 60 хв` (`.github/` не чіпати — лише запис і перелік найповільніших тестів).
- `TD-073 · локально Python 3.14, CI 3.12 — помилку анотації (d4a1d0c) впіймав лише CI · make ci · закрито, коли make ci ганяється на 3.12 локально`.

## Фаза 0 (до коду) → `.claude/handoff/KLB-003.md`, стоп «KLB-003 фаза 0 готова»
- HYP-1: `get-items` відповідає JSON **без входу**. Дозволено рівно 2 GET `curl` без cookies: `offset=0` і `offset=1740`. Записати поля (id, link, name, price, sold, moderated, renew_date, photo?), чи є загальна кількість, шаблон `link`. Фікстура — 3 записи. Більше живих запитів — ні.
- HYP-2: розмітка й логіка `add.html` — як вище (файл:рядок).
- HYP-3: які хости/шляхи кличе reCAPTCHA v3 (`api.js`, `api2/reload`, `api2/clr`…) — UNKNOWN до живого прогону; guard пише всі заблоковані чужі запити в лог сесії.
- HYP-4: куди веде `/item/add` без входу (1 GET `curl -sI` — адреса переадресації).
- HYP-5: `KlubokDriver` + `verify` KLB-001 працюють на `/item/add` без змін (підставний Клубок).
- HYP-6: `wait_for_manual_login` параметризується без зміни поведінки Шафи.
- HYP-7: `sessions.ended_at` / `stop_code` вистачає для сесій Клубку; екран і бот їх не бачать (`site = 'shafa'`).
- HYP-8: час `_queue_place` зараз і `queue.place` — на синтетиці 1787 оголошень × 48 у черзі (цифри — в PR).

## Підставний Клубок (`robot/klubok_fake.py`)
`get-items` посторінково (1787 штучних, 0 і 1 неповна сторінка, помилка на сторінці N, різке падіння кількості); `/item/add` без входу (переадресація); `add-edit-used` → `success` / `Error` (score) / тайм-аут / HTTP 500 / не JSON / «кнопка не надсилає»; підставна reCAPTCHA на чужому хості (перевірка дозволу guard); 429.

## Доказ (DoD) — тести червоніють без фіксу
- Квиток: без квитка POST `add-edit-used` і клік «Опубликовать» — блок; другий POST за одним квитком — блок; у `--klubok-dry-fill` публікація неможлива (як у KLB-001).
- Відповіді: `success` → `published` з id; `Error` → `problem klubok_captcha` + стоп; тайм-аут після відправки → `klubok_uncertain` + стоп, повтору немає; не відправлено → `queued` + стоп; 429 → стоп.
- Переадресація на `/payment/…` не завантажується, у ній нічого не клікається.
- Знімок: неповний — база не змінилась; падіння > 10 % — не змінилась; зниклі → `sold = 1`; `post_id` / `asked_at` вціліли.
- Ліміт: `--limit` і ліміт доби (спроба `release` теж рахується).
- `queue.place`: `dup.find` і `fields.ad` підмінені на «впасти» — Картка відкривається з «місце N»; порядок = порядок `plan`.
- Сесія: `site = 'klubok'`, екран Шафи її не показує (Пауза/Стоп/здоров'я — як були).
- `make ci` зелений; на PR `quality`, `windows-package` зелені; `windows-tests` — або зелений, або обрізаний лімітом (TD-072).

## Для Власника (у звіті, після мержу — по одній)
Порядок живого прогону: оновлення живої програми (чат «Шафа + сервер», спершу копія бази) → `robot --klubok-login` → `robot --klubok-snapshot` (≈ 1787) → `klubok-queue` на копії (дублі) → у Налаштуваннях Клубок увімкнути, ліміт 10 → `robot --klubok-publish --limit 1 --human-click` → `--limit 1` двічі (одна — сумка з справжнім фото через `--post`). Після кожної: оголошення на Клубку, порядок фото, бренд.

## Заборонено
Живий klubok.com (крім 3 GET Фази 0), публікація з робочої копії, жива база й `~/shafa`, `~/shafa-viktoria`, `~/Шафа`; кліки на `/payment`, рекламу, «горячим», оновлення, продаж, видалення; `.github/`; пароль чи cookies — у код, тести, лог, звіт. `git pull` у main, пуш у main, мерж.
