# ROBOT-018 — готово, чекає пушу гілки
**Гілка:** `fix/ROBOT-018-maliuky-brend` · **PR:** ще нема · **Перевірки:** `make ci` зелений (3189 passed, 2 skipped)
1. Фаза 0: HYP-1 VERIFIED — `PageWidget.press` (form.py) клікає кнопку за точним текстом, відрізнити «вкладок нема» не вмів; тепер `PageDriver.has_system_tabs` (кнопка блоку, що не розмір/«Інший»). HYP-2 VERIFIED у знімку: усі 5 розділів `Для малюків/Взуття/*` мають лише «Український» (22 є); які саме без вкладок — UNKNOWN (знімок цього не віддає). HYP-3 VERIFIED: пошук — `PageDriver.brand`, «є у довіднику» — `catalog.canonical` у plan.py; у знімку 7904 брендів, 20 груп із різним написанням за пробілами («Marks & Spencer»/«Marks&Spencer», «J. Crew»/«J.Crew»…).
2. Файли: `src/shafa/robot/form.py` (`size_system`, `has_system_tabs`, `is_size_text`, `brand_variants`, `_find_brand`), тести `test_robot017_page.py`, `test_robot006_page.py`, `docs/TECH_DEBT.md` (TD-066).
3. Відхилення від тікета: окремий коміт `tests/test_ux004.py` — тест падав і на чистому `main` (жорстка дата `archived_at` 30.10 вийшла за 7 днів Архіву); тепер `db.now()`. Крок 1 — не «в plan.py», а в `execute` (там вибір), plan без змін.
4. Нові TD: TD-066 (наживо не перевірено). OPEN-Q: немає.
5. Доказ: без фіксу 7 нових тестів червоні (кросівки без вкладок і бренд за іншим написанням — по суті, решта — немає методів); вкладки є, але потрібної нема — досі `FieldMissing`; «схожий» бренд (`R.Marks`) не вибирається. Журнал — останнім комітом.
**Що потрібно:** дозвіл на пуш гілки.
