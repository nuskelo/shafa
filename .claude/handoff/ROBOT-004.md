# ROBOT-004 — готово, PR відкрито
**Гілка:** `fix/ROBOT-004-rozmitka-spyskiv` · **PR:** див. GitHub · **Перевірки:** `make ci` — 1549 тестів (+2 пропущено); CI — чекаємо
1. Фаза 0 не потрібна (звичайна вага). Гіпотеза тікета (TD-041: `CHIPS` вгадано за react-select) — у коді так: кнопка чипа лише `[class*=emove]`/aria «Remove…» або «×» текстом (`form.py`, `REMOVE`, старий `CHIPS`).
2. Файли: `src/shafa/robot/form.py` (`CHIPS`, `FIND_BLOCK` позначає підпис, `MARKUP`, `markup`, `save_markup`, `FieldMissing.field/markup`, дамп у `pick`/`verify`/`dry_fill`), `src/shafa/robot/publish.py` (дамп при `FieldMissing`); тести `tests/test_robot004.py`, `tests/test_robot004_page.py`.
3. Чип тепер: текст + одна кнопка без тексту («×», SVG, кнопка, role=button, aria-label, клас remove|delete|clear|close|cross). Не чип: кнопка очищення поля (клас `indicator`; спільна для кількох чипів; поруч із полем введення), значок біля підпису поля.
4. Дамп: `robot-screens/<час>-<пост>-<поле>.html` поруч зі скриншотом — блок поля і відкриті меню поза ним (меню сайту в header/nav/footer — ні), без `<script>`/`<style>` і `value` прихованих input-ів, ≤ 50 КБ (без половини літери). Пишеться при зупинці в полі-списку: у `pick` і у фінальній звірці `verify`, і в `--dry-fill`, і в `--publish`. Шлях — у журнал.
5. Мутації 13/13 червоніють (кандидати SVG/role/aria, одна кнопка на чип, значок підпису, клас indicator, `value` прихованих, меню поза блоком, nav, дамп у pick/verify, 50 КБ, назва файлу, файл у dry_fill/publish).
6. Відхилення від тікета: немає. Тести сторінки — у Chromium Playwright; у CI без браузера пропускаються (як ROBOT-003), локально пройшли.
7. Нові TD: TD-043 — поле з одним значенням без чипа (react-select `single-value`) `CHIPS` не читає. TD-041 оновлено, не закрито (потрібен живий dry-fill). OPEN-Q — немає.
8. Жива Шафа, канали, сайт, `.env`, пілот — не чіпались (копія бази для цього тікета не знадобилась).
9. Журнал: `.claude/state/log/2026-10-18-1733-ROBOT-004.md` (останній коміт).
**Що потрібно:** мерж після зелених `quality`, `windows-tests`, `windows-package`; далі — повторний `--dry-fill 878` при Власнику. Якщо знову зупинка в списку — Архітектору файл `robot-screens/…-<поле>.html` (шлях у журналі робота).
**Для Власника:** нічого до мержу.
