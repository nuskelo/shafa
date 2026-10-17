# CORE-006 — готово, PR відкрито
**Гілка:** feature/CORE-006-cherha-starykh · **PR:** див. нижче · **Перевірки:** `make ci` локально зелений (1128 passed, 2 skipped)
1. Фаза 0: не потрібна (малий тікет).
2. Файли: `db.py` (схема 8, `queue_pos`), `borh.py`, `robot/publish.py`, `batch.py`, `review.py`, `docs/install-windows.md`; тести `test_db/borh/limits/batch`.
3. Відхилення: `Limits` отримав `old_per_day` (з `SHAFA_DAILY_OLD`); «наперед» рахує Картки `review`/`approved` + старі в пакетах, що чекають. Екран (п. 4) — одна правка сортування, зроблено.
4. Нові TD / OPEN-Q: немає. Старі Картки без `queue_pos` (імпортовані до цього тікета) йдуть після тих, що зі списку, за датою — повторний `import-old` з тим самим списком дасть їм позиції.
5. Журнал: останній коміт гілки.
Мутації: без `queue_pos` у `ORDER BY` робота й без ліміту наперед — 5 тестів червоніють.
**Що потрібно:** мерж лише після зелених `quality`, `windows-tests`, `windows-package`; потім новий пакет Windows.
