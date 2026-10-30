# CORE-019 — зник з каналу = продано і для старих постів · готово, PR відкрито
**Гілка:** fix/CORE-019-znyk-prodano-stari · **PR:** див. нижче · **Перевірки:** `make ci` локально зелений
1. Фаза 0: не потрібна (мала задача). Перевірено кодом: `from_bot` використовувався лише в `_post_deleted` (`robot/publish.py`); `site.py:657/839` викликають `archive_gone` без власних умов про `source`.
2. Файли: `src/shafa/review.py` (прибрано `source = 'bot'` в `archive_gone` і `archive_all_gone`), `src/shafa/robot/publish.py` (`from_bot` → `known_gone`; `_post_deleted` для всіх Карток з `post_id`, `post_checks.gone_at` → Архів без presence), `CLAUDE.md` (рядки `review.py`, `robot/publish.py`), `tests/test_core018.py`, `tests/test_robot_publish.py`.
3. Відхилення від тікета: немає. `gone_at` перевіряється і коли presence-перевірки немає зовсім (`presence is None`). Для `docs/rules.md` змін немає (про це правило там нічого не було). Тексти подій `post.gone`/`site.night` загальні — правки не потрібні (пункт 3 тікета — перевірено).
   Тести, переписані під нове правило: `test_old_post_is_never_archived` → `test_old_post_is_archived_too`; `test_night_gone_old_post_card_untouched` → `…_archived`; `test_cleanup_archives_cancelled_and_gone_idempotent` (старий пост тепер у `{…, 4: 1}`); `test_only_bot_posts_checked` (robot) замінено на `test_old_export_post_*`. Нові: старі `published`/`shafa_id` не чіпаються (`archive_gone` і `archive_all_gone`), `gone_at` без виклику presence, `gone_at` без presence-перевірки, «невідомо» → публікує.
   Без фіксу 8 тестів червоніють (перевірено `git stash` для `src`).
4. Нові TD / OPEN-Q: немає.
5. Журнал: `.claude/state/log/2026-10-30-1554-CORE-019.md` (останній коміт гілки).
**Що потрібно:** після мержу й перезапуску програми `archive_all_gone` прибере 6967, 240, 4617, 6976, 6095, 6948 в Архів (118 уже `published` — не чіпається). Мерж — Власник, коли зелені `quality` і `windows-tests`.
