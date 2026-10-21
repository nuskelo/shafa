# CORE-016 — готово, чекає пуш і PR
**Гілка:** fix/CORE-016-sezon-termo · **PR:** після пушу · **Перевірки:** `make ci` — 2404 passed, 2 skipped
1. Фаза 0 (звичайна вага, лише HYP): `rules.season` ~190–240, `ai.check_season`, `review._apply_edited_text`, `prompts/blank.md` (рядок season), `fill.recheck`/`recalc` — VERIFIED, DRIFT немає.
2. Файли: `rules.py` (`_TERMO` більше не ознака зими, `_termo_season`, `season(text, section)`, `has_termo`), `ai.py` (`check_season` передає розділ; «термо» з двох сезонів лишає ті, що є в розділі), `review.py` (`SEASON_CHANGED` у `NOTE_CODES`; виправлено жорсткі два `?` у `act`), `fill.py` (`recheck_season`), `__main__.py` (`--recheck-season`), `prompts/blank.md`, шаблони `list.html`/`card.html`, `docs/rules.md`, `CLAUDE.md`, тести `test_core016.py` (35), `test_ai.py` (1 рядок: Columbia → Демісезон, Зима).
3. Відхилення: нова команда `--recheck-season` замість `--recheck` (той міняє лише Картки зі старішим RULES_VERSION). `RULES_VERSION` і `PROMPT_VERSION` не піднімав: сезон «термо» вирішує код поверх ШІ. Тип для «термо» — з тексту, інакше з розділу Картки.
4. Нові TD / OPEN-Q: немає. Тест без фіксу червоніє (20 з 35 падають, якщо повернути «термо» в ознаки зими).
5. Журнал: останній коміт гілки.
**Пілот (копія бази в tmp, dry-run):** зміниться 6 Карток — пости 3654, 3655, 6453 (Зима → Демісезон, Зима), 6566, 6581, 6755 (Зима → Демісезон); схвалених серед них 0. Не чіпаємо: 36 опублікованих, 2 в архіві; 19 `new`/проблем без бланка отримають правило при заповненні ШІ.
**Що потрібно:** мерж після зелених `quality` і `windows-tests`.
**Для Власника (після мержу й оновлення програми):**
`.venv/bin/python -m shafa fill --db ~/shafa-viktoria/shafa.sqlite --recheck-season --dry-run 2>&1 | tail -n 40` — покаже «Змінилось би: N»;
потім те саме без `--dry-run` (схвалені зі зміною підуть «На перевірку» з приміткою; у Telegram піде рядок «Сезон за новим правилом»).
