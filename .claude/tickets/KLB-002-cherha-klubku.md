# KLB-002 · Клубок: база 18, черга, ліміт доби, дублі, рядок «Клубок» у Картці, налаштування

**Вага:** критична (міграція бази, дублі = блок акаунта). **Автор:** Архітектор · 10.11.2026
**Гілка:** `feature/KLB-002-cherha-klubku` від `origin/main` після `git fetch` (має бути мерж #99 / KLB-001). Перший коміт — `.claude/handoff/KLB-001.md` → `.claude/archive/handoff/`.
**Проєкт змін:** `docs/klubok.md` — прочитай повністю (розділи «База», «Черга й ліміт»). Після цього тікета він має відповідати коду.

## Навіщо
Підготувати все, крім живого сайту: де програма пам'ятає оголошення Клубку, які Картки й у якому порядку підуть, скільки на день, що не піде і чому, де Вікторія бачить «Клубок» на Картці й вирішує «дубль чи ні». **Живого Клубку, публікації й робота-сесії тут немає** (KLB-003). Шафа поводиться рівно як зараз.

## Що звірив Архітектор у коді (Фаза 0 перевіряє, не на слово)
- `db.py:8` `SCHEMA_VERSION = 17`; `MIGRATIONS` (`db.py:~505`) — ключі 1…16, крок — рядок SQL або функція; `connect` відмовляє базі новішої версії (`db.py:524`).
- `sessions` (`db.py:122` + `CONTROL` з `stop_code`, `item_started_at`) читають без фільтра: `web.py:647,1227,1816,1832`, `health.py:338`, `robot/control.py:384,387,400`, `robot/publish.py:354` (`recover` закриває **всі** відкриті), `:394,401` (Пауза/Стоп — `WHERE ended_at IS NULL`), `:532,554,579`, `bot/alerts.py:128,187`, `bot/report.py:109`, `bot/actions.py:362`.
- **Фото опублікованих на Шафі:** `publish.move_published` переносить папку в `<Головна>/Опубліковано/` (варіанти — коли всі сестри опубліковані, `variants.all_published`), а `klubok/fields.py:148` шукає фото через `folders.item_folder` — це `<Головна>/<папка>`. Тобто для `published` зараз буде `klubok_no_photos`. Плюс `purge_published` / `purge_old_photos` (`publish.py:437,468`) стирають фото через 3 дні, якщо ввімкнено `SHAFA_DELETE_PUBLISHED`.
- Дублі Шафи: `robot/listings.find_duplicate` (група розділів + бренд рівно + `sizes_close` ±0,5 + `similar` назва з `shoe_type` + `prices_close` 40 %). Клубок `get-items` (KLB-003) віддає `id, link, name, price, sold, moderated, renew_date…` — **бренду, розмірів і, можливо, категорії там немає**; розмір є в назві («… 39 розміру …», `docs/klubok.md`, аналіз CSV її 1786).
- Ліміт Шафи — `publish._started_rows` (місцева доба, `publish_started_at`, будь-яка спроба до кліку).
- Налаштування — `options.OPTIONS` / `_LIMITS`, блоки — `web.settings_blocks`; читання — `config.setting`.
- Стать дитячого: `klubok/fields.sex_for` лише з тексту (питання з KLB-001 — рішення нижче, D8).

## Рішення Архітектора (робити так; DRIFT у Фазі 0 — зупинка)
**D1 · Схема 18** — крок `MIGRATIONS[17] = KLUBOK` (рядок SQL, наявні таблиці не міняються, крім одного `ALTER`):
```sql
CREATE TABLE klubok_ads (
    item_id            INTEGER PRIMARY KEY REFERENCES items (id),
    state              TEXT NOT NULL CHECK (state IN ('queued','publishing','published','problem',
                                                      'sold_asked','sold','removed','skipped')),
    klubok_id          INTEGER UNIQUE,
    url                TEXT,
    problem_code       TEXT,
    problem_text       TEXT,
    dup_klubok_id      INTEGER,   -- схоже оголошення на Клубку (її або програми)
    dup_name           TEXT,
    dup_ok_at          TEXT,      -- «Інша пара — виставити»: дублі для Картки більше не перевіряються
    queued_at          TEXT,
    publish_started_at TEXT,
    published_at       TEXT,
    renewed_at         TEXT,
    gone_at            TEXT,
    sold_ok_at         TEXT,
    removed_at         TEXT,
    session_id         INTEGER REFERENCES sessions (id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX klubok_ads_started ON klubok_ads (publish_started_at);
CREATE TABLE klubok_listings (
    id          INTEGER PRIMARY KEY,   -- id оголошення на Клубку
    name        TEXT NOT NULL,
    price       INTEGER,
    sizes       TEXT NOT NULL,         -- JSON; з назви («38,5 розміру» → ["38,5"]), код
    brand       TEXT,                  -- NULL: get-items не дає
    category_id INTEGER,               -- NULL: якщо get-items не дає
    url         TEXT,
    photo       TEXT,
    sold        INTEGER NOT NULL DEFAULT 0,
    moderated   INTEGER,
    renew_date  TEXT,
    post_id     INTEGER,               -- лише надійне зіставлення (або «Так, дубль»)
    asked_at    TEXT,                  -- KLB-005: уже показано в «Схоже, продано»
    added_at    TEXT NOT NULL
);
CREATE TABLE klubok_listings_read (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    read_at TEXT NOT NULL,
    count   INTEGER NOT NULL
);
ALTER TABLE sessions ADD COLUMN site TEXT NOT NULL DEFAULT 'shafa' CHECK (site IN ('shafa', 'klubok'));
```
Коментар над кроком — як у версії 17 (що й навіщо). Біля `_rebuild_items` — рядок: «майбутня перебудова `items` мусить зберегти `klubok_ads` (FK)».

**D2 · Сесії.** Усі наявні читання/зміни `sessions` (список вище + що знайдеш ще, і в шаблонах) — з `site = 'shafa'`; `INSERT` Шафи явно пише `site`. `recover`, Пауза/Стоп, «остання сесія» в панелі, здоров'ї й боті — лише Шафа. Сесій Клубку цей тікет не створює (KLB-003), але тест вставляє відкриту сесію `klubok` і доводить, що Шафа її не бачить і не закриває.

**D3 · Хто йде в чергу** — новий модуль `src/shafa/klubok/queue.py`, чиста функція `plan(conn, root, today) -> list[Verdict]` (без запису) і `sync(conn, root, today)` (пише). Кандидат:
`items.status IN ('approved','published')` · `purged_at IS NULL` · `post_id > SHAFA_KLUBOK_FROM_POST` · рядка `klubok_ads` немає або він `queued`/`skipped`. `publishing` і `problem` Шафи — ні (в дорозі / нерозв'язане).
Вердикт: `queued` або `skipped` + код: Skip з `fields.ad` (`klubok_no_category`, `klubok_size`, …), `klubok_post_gone` (`publish.known_gone` / Картка в архіві з `post_deleted`), `klubok_post_done` (D6.1). `sync` перераховує лише рядки `queued`/`skipped`; решту станів не чіпає ніколи. Картку знято зі схвалених (`unapprove`) → її `queued` рядок видаляється.
**Порядок:** зимове (`fields._winter` — зроби публічним) → інше → літнє (сезон лише «Літо») останнім; усередині — старіші пости першими (`posts.date`, далі `items.id`).

**D4 · Ліміт доби.** `SHAFA_KLUBOK_DAILY_LIMIT`, типово 10, межі 1–30. Рахувати `klubok_ads.publish_started_at` за місцеву добу — тим самим способом, що `publish._started_rows` (винеси спільне, не копіюй). `take(conn, root, today) -> list[Verdict]`: скільки ще можна сьогодні, по порядку `plan`. Шафин ліміт і Клубку — незалежні.
`take` повертає порожньо з причиною, коли: Клубок вимкнено (`SHAFA_KLUBOK`), знімка `klubok_listings_read` немає або він старший за 24 год (`klubok_snapshot_old` — KLB-003 перечитує перед сесією), ліміт вичерпано.

**D5 · Стани для KLB-003** — функції в `queue.py`, кожна в одній транзакції й з умовою на поточний стан (як `publish._problem`): `start(item_id, session_id)` `queued→publishing` + `publish_started_at`; `published(item_id, klubok_id, url)`; `problem(item_id, code, text)`; `recover()` — `publishing → problem klubok_uncertain` («Перевірте на Клубку, чи опубліковано»), повтору немає. Тести на фейкових даних; ніхто їх ще не викликає, крім тестів.

**D6 · Дублі** — `src/shafa/klubok/dup.py`, викликається в `take` **для кожної Картки перед видачею** (KLB-003 бере по одній і перевіряє знову після кожної публікації):
1. Пост уже має оголошення на Клубку: інша Картка того самого `post_id`, не сестра-розмір (`variants.sisters`, як `publish.already_published`), з `klubok_ads` у `publishing`/`published`/`sold`/`removed` або `problem klubok_uncertain` → `skipped klubok_post_done`.
2. Схожа на її **активне** оголошення (`klubok_listings`, `sold = 0`) або на **виставлене програмою** (`klubok_ads` `publishing`/`published` — поля з їхніх Карток) → `problem klubok_duplicate`, `dup_klubok_id`, `dup_name`. Правило — на основі функцій `robot/listings.py` (`sizes_close`, `similar`, `prices_close`), але під дані Клубку: розміри оголошення — з назви (новий розбір «N розміру», «N,5 розміру», «N-M розміру»); бренд — `brand` рядка, а якщо NULL — бренд Картки (casefold, є і кирилична назва з `catalog`, якщо вона там є) **міститься в назві**; Картка без бренду — бренд не перешкода; категорія — лише якщо `category_id` відомий (те саме «Женское/Мужское/Детское»), інакше не порівнюється. Сумнів — краще «Проблема». `dup_ok_at` заповнено → пункт 2 пропускається.
3. Рішення Вікторії на Картці: «Інша пара — виставити» → `dup_ok_at`, стан `queued`; «Так, дубль — не виставляти» → `skipped klubok_dup_yes`, і якщо це її оголошення без `post_id` — записати `post_id` Картки (знадобиться KLB-005).

**D7 · Фото.** `folders.photo_folder(root, folder_name)`: `item_folder`, а якщо такої теки немає й це не старий пост — `<Головна>/Опубліковано/<base_name>`. `fields.ad` бере фото через неї. `purge_published` і `purge_old_photos` **не стирають** фото Картки, поки Клубку вони ще потрібні: `queue.needs_photos(conn, item_id)` — стан `queued`/`publishing`/`problem`, або Клубок увімкнено й Картка — кандидат без рядка. Тест червоніє, якщо прибрати перевірку.

**D8 · Стать дитячого (питання з KLB-001):** спершу розділ Картки (`catalog.GIRLS` → 0, `catalog.BOYS` → 1 — вона його перевірила), інакше `rules.said_gender` тексту, інакше 2.

**D9 · Налаштування** (`options.OPTIONS`, `_LIMITS`, окремий блок «Клубок» у `settings_blocks`):
- `SHAFA_KLUBOK` — flag «Виставляти пари на Клубок», типово вимкнено (поки KLB-003 не готовий — нічого не відбувається, лише рядок у Картці);
- `SHAFA_KLUBOK_DAILY_LIMIT` — «Публікацій на Клубку за добу», 1–30, типово 10;
- `SHAFA_KLUBOK_FROM_POST` — «На Клубок — пости з номером більше за», 1–1 000 000, типово 7200.
Підказки простою мовою; читання — однією функцією в `queue.py` з тими самими межами (як `publish.daily_limit`).

**D10 · Екран.**
- **Картка (`card.html`)** — рядок «Клубок: …» біля посилання «Відкрити на Шафі», лише коли `SHAFA_KLUBOK` увімкнено або рядок `klubok_ads` уже є. GET нічого не пише (для Картки без рядка — `plan` для однієї Картки). Тексти: «у черзі» (+ місце в черзі) · «не піде: <причина простою мовою>» · «публікується» · «виставлено <дата> · Відкрити на Клубку» · «Проблема: …» · дубль — назва схожого, посилання на нього (`klubok_listings.url` / `klubok_ads.url`) і дві кнопки (D6.3), POST через `card_post`, дії `klubok_dup_other` / `klubok_dup_yes`, з `stale` як у наявних дій.
- **Проблеми** — окремий блок «Клубок: треба рішення» над наявним списком: Картки з `klubok_ads.state = 'problem'` (назва, причина, посилання на Картку). Лічильник вкладки Шафи не змінюється.
- Коди причин — у `web.MESSAGES` / окремому словнику, без англійських кодів на екрані.

**D11 · Пробний прогін.** `python -m shafa klubok-queue --db <копія> --root <Головна> [--today РРРР-ММ-ДД]` — база `mode=ro` (як `klubok_form.open_readonly`), нічого не пише: стан знімка, ліміт сьогодні (скільки почато), перші 30 Карток у порядку (пост, назва, категорія Клубку, фото N), підсумок «не піде» за кодами, дублі. Вихід ≤ 60 рядків.

## Фаза 0 (до коду, зупинка: «KLB-002 фаза 0 готова»)
Звіт у `.claude/handoff/KLB-002.md`, кожне — VERIFIED / DRIFT (файл:рядок) / UNKNOWN.
- HYP-1: `SCHEMA_VERSION` 17, ключі `MIGRATIONS` 1…16, крок 17 — новий; `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT … CHECK` працює на SQLite з `.venv` і на Windows CI (версія `sqlite3.sqlite_version` у звіт; на Windows — з логу `windows-tests`, якщо видно).
- HYP-2: повний список місць, що читають/міняють `sessions` (py + шаблони + тести) — таблицею у звіт; нових порівняно зі списком вище — позначити.
- HYP-3: фото `published` — в «Опубліковано», `fields.ad` для них зараз дає `klubok_no_photos` (DRIFT очікуваний, підтверди тестом або на копії).
- HYP-4 (на копії бази, `/tmp/klb002-copy.sqlite` — зробить Власник; Головна — **лише читання**, `is_dir/is_file`): скільки кандидатів (`approved`/`published`, пост > 7200), з них зимових; скільки старих `borh-` серед них і скільки фото в них; розподіл Skip-кодів; скільки з фото знайде `item_folder` і скільки `photo_folder`. Очікування з аналізу: ~136 пар, ~73 зимові.
- HYP-5: розбір розміру з назв її оголошень. Даних `get-items` у репо немає; якщо Власник дасть шлях до CSV її 1786 оголошень (`klubok-2.zip/G_ogoloshennya`) — порахуй, скільки назв розбирається і скільки з кандидатів HYP-4 правило D6.2 позначить дублем. Немає CSV — UNKNOWN, правило перевіряємо в KLB-003 на живому знімку.
- HYP-6: `SHAFA_DELETE_PUBLISHED` на живій програмі — UNKNOWN (питання Власнику, не читати `~/shafa-viktoria` самому).
- Що з D1–D11 суперечить коду — DRIFT, зупинка.

## Що зробити (кожен пункт — коміт)
1. `db.py`: схема 18 (D1) + тести міграції.
2. `sessions.site` у всіх наявних місцях (D2) + тест «відкрита сесія Клубку не видна Шафі».
3. `folders.photo_folder`, `fields.ad` через неї, захист від стирання фото (D7), стать з розділу (D8).
4. `klubok/queue.py`: `plan`, `sync`, порядок, ліміт, `take`, стани (D3–D5) + налаштування (D9).
5. `klubok/dup.py` + розбір розміру з назви (D6).
6. Екран: рядок у Картці, дії дублю, блок у Проблемах (D10).
7. `klubok-queue` (D11).
8. `docs/klubok.md` (розділи «База», «Черга й ліміт» — як зроблено), `CLAUDE.md` (рядки модулів, команда), журнал.

## Доказ (DoD)
- Міграція: база 17 з даними (Картки, сесії, `my_listings`, `outbox`) → 18: дані ті самі, старі сесії `site = 'shafa'`, `foreign_key_check` порожній, повторне відкриття нічого не міняє, база 19 → відмова. Тест на копії старої фікстури бази, якщо така є.
- Червоніють без фіксу (перевір мутацією й напиши у звіті): `recover` Шафи не чіпає сесію Клубку; ліміт 10 рахує спроби за місцеву добу; зимове перед іншим; той самий пост удруге → `klubok_post_done`; сестра-розмір — не дубль; схоже на її оголошення → `klubok_duplicate`; після «Інша пара» — `queued` і більше не дубль; фото «Опубліковано» знайдено; `purge_published` не стирає потрібне Клубку; знімок старший за 24 год → `take` порожній.
- `sync` не змінює `publishing`/`published`/`problem`/`sold*`/`removed`.
- Шафа без змін: наявні тести зелені без правок їхньої суті (якщо тест довелося змінити — чому, у звіт).
- `make ci` зелений; на PR зелені `quality`, `windows-package`, `windows-tests`.

## Для Власника (у звіті, по одній команді)
- Перед Фазою 0 — копія бази (на Mac): `sqlite3 ~/shafa-viktoria/shafa.sqlite ".backup /tmp/klb002-copy.sqlite"`.
- Після PR — пробний прогін `klubok-queue` на копії з `--root` Головної, `| tail -n 60`.
- Після мержу оновлення живої програми — **спершу копія бази** (схема 18 назад на 17 не відкривається: стара програма скаже «Невідома версія бази 18»). Порядок оновлення Mac/сервер — чат «Шафа + сервер».

## Заборонено
`git pull` у main, пуш у main, мерж, `.github/`, сервер (`ssh`/`scp`), живий klubok.com, мережа в тестах. `~/shafa`, `~/shafa-viktoria`, Головна — нічого не писати й не запускати там (Головна — лише читання в HYP-4 і прогоні). Міграція лише на копіях. Сесії Клубку, вхід, публікація, «Обновить все», «Продано» — KLB-003…005. Секрети — ні в код, ні в тести, ні у звіт.

## Ітерація 1 — рішення по Фазі 0 (Архітектор · 10.11, вечір)
1. **HYP-1 — так:** крок 17 — функція `_klubok(conn)` з одним `executescript("BEGIN;" + KLUBOK + "PRAGMA user_version = 18; COMMIT;")`, як `_bot_queue`. Тест: обрив посеред кроку (виняток у `executescript`) → база лишається 17 без нових таблиць, наступний `connect` мігрує нормально.
2. **DRIFT-1 → (а):** `needs_photos` **не залежить від `SHAFA_KLUBOK`**: фото захищені для кожного кандидата D3 (пост > `SHAFA_KLUBOK_FROM_POST`, `approved`/`published`), поки в нього немає рядка `klubok_ads` у стані `published`/`skipped`/`sold`/`removed`. Тест червоніє, якщо повернути умову на прапорець. Ті, що вже без фото (24 на копії), — `skipped klubok_no_photos`, `klubok-queue` показує їх окремим списком (пост, назва). Новий TD: «фото для Клубку з оголошення на Шафі (`shafa_url`) для пар, чиї фото стерто» — не робити, лише записати. Вимкнути стирання на живій програмі до оновлення — дія Власника, не твоя.
3. **DRIFT-2:** `klubok-queue` відкриває файл `mode=ro`, копіює базу в пам'ять (`Connection.backup` у `:memory:`) і мігрує **в пам'яті** тим самим кодом, що й `connect` (винеси міграції в `migrate(conn)`; `connect` викликає його). Файл не змінюється: тест — sha256 і `user_version` файлу 17 до й після прогону. База новіша за 18 — відмова, як у `connect`.
4. **DRIFT-3 — так,** як пропонуєш: `klubok_post_gone` лише за `known_gone` (опубліковані на Шафі, чий пост зник).
5. **Головна** — `/Users/nuskelo/Шафа` (`~/Шафа`), підтверджено (передача Архітектора). Лише читання.
6. **HYP-5 — не зупиняє.** Розбір розміру з назви — фікстура з `item_name` сторінок `~/klubok-4/*.html` (лише назви; `token`, телефон і решту `data-user-item` не копіювати) + граничні випадки руками. CSV — якщо Власник дасть шлях, порахуй хибні дублі в звіті PR; ні — правило перевіряємо в KLB-003 на живому знімку.
7. **windows-tests (#99 скасовано на 90 % через 60 хв; OPS-005 — 28 хв):** перед пушем — локально `pytest --durations=25 -q | tail -n 40`, найповільніші — в опис PR. Якщо `windows-tests` на PR KLB-002 знову впреться в ліміт — зупинись і напиши, які тести найдовші (підозра — Chromium-тести KLB-001). `.github/` не чіпати.
Далі — код за пунктами «Що зробити», без нової зупинки, якщо не буде нового DRIFT.
