# FIX-CORE-014-off — готово, PR відкрито
**Гілка:** fix/CORE-014-off · **PR:** див. нижче · **Перевірки:** make ci
1. Перемикач `variants.SPLIT_SIZES = False`; перевірено в точках входу: `scan.find_problem` і `_save_folder` (через `variants.size_variants`), `variants.split_card` (його кличе й `borh`), `variants.split_waiting` (`fill.recheck`, старт `app`).
2. Файли: `variants.py`, `scan.py`, `tests/test_fix_core014_off.py` (нові), `tests/test_core014.py` (autouse monkeypatch True), `docs/rules.md`, `CLAUDE.md`, `docs/TECH_DEBT.md`.
3. Тести: 3654/3655/3659 → `many_items` з колишніми текстами, жодного «~», recheck/старт нічого не розбивають, ШІ не викликається. Якщо повернути True — нові тести червоніють.
4. Відхилення: немає. Нове TD: TD-059.
5. Журнал: останній коміт гілки.
**Що потрібно:** мерж.
