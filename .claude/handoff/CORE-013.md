# CORE-013 — готово, PR відкрито
**Гілка:** `feature/CORE-013-rozmir-znimok` · **PR:** посилання в чаті · **Перевірки:** `make ci` зелений локально (1815 passed)
1. Причина підтверджена: у `shafa-catalog.json` 34 ключі «Европейский/Украинский/Американский» у 15 розділах; `catalog.size_system` шукав лише українські → `sizes()` = [] → бланк без розміру, `approve_block` пускав.
2. Файли: `robot/lang.py` (`size_groups`), `robot/sync.py` (`section_entry`), `catalog.py` (`_unify` теж нормалізує `sizes`), `data/shafa-catalog.json` (скрипт, лише ключі; тепер лише Європейський/Український/Американський), `fill.py` (`sizes_candidates`, `recheck_sizes`), `__main__.py` (`fill --recheck-sizes`), `tests/test_core013.py` (10 тестів), `CLAUDE.md` (рядки catalog і fill).
3. Відхилень немає. `RULES_VERSION` не піднято: нова команда не залежить від версій, а підняття відправило б усі схвалені в `stale_rules` і перерахувало б усе.
4. Нові TD / OPEN-Q: немає. `approve_block` не міняв: `no_sizes` працює, коли знімок правильний (тест).
5. Без фіксу тести червоніють: прибрав нормалізацію в `catalog._unify` і `sync` — впали 4 з 10 (sync, старий російський знімок, розмір ШІ, схвалення без розміру).
Перебудова з збереженої відповіді ШІ (без запитів): беруться `review`, схвалені й `problem` (не `publish_uncertain`/`possible_duplicate`/`already_published`) з `sizes=[]` у розділі, де розмір є; з її правкою розміру — ні. Повертаються в `review` (схвалені теж), правки `edits` лишаються. Відповідь ШІ без розміру → лишаємо, друкуємо номер (треба «Перезаповнити»).
**Що потрібно:** Влад запускає команди нижче на пілотній базі (я її не чіпав); до цього робота на пілоті не запускати.
**Для Власника:**
- закрити програму/екран; копія: `cp ~/shafa-viktoria/shafa.sqlite ~/shafa-viktoria/shafa.sqlite.bak-core013`
- що буде зачеплено (нічого не міняє): `.venv/bin/python -m shafa fill --db ~/shafa-viktoria/shafa.sqlite --recheck-sizes --dry-run 2>&1 | tail -n 40`
- ручний пошук уражених: `sqlite3 ~/shafa-viktoria/shafa.sqlite "SELECT i.post_id, i.status, i.problem_code FROM items i JOIN blanks s ON s.item_id=i.id AND s.field='sizes' AND s.value IN ('[]','null') WHERE i.purged_at IS NULL AND i.status IN ('review','approved','problem') ORDER BY i.post_id;"` (включає і розділи без розміру — сумки, їх команда не чіпає)
- виправити: `.venv/bin/python -m shafa fill --db ~/shafa-viktoria/shafa.sqlite --recheck-sizes 2>&1 | tail -n 40`
- очікувано: 1427, 1766, 3400, 3645, 3763, 3896, 4433, 4461 (якщо ШІ давав розмір); схвалені стануть «Перевірити» — схвалити знову вручну.
