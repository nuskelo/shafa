"""Команди.

`python -m shafa scan --export … --root … --db …` — зіставити папки й пости.
`python -m shafa import-old --export <папка експорту> --list <файл.txt> [--db …]` — старі пости
з Експорту (з фото) без папок → Картки з одним фото поста (CORE-005); база — програми.
`python -m shafa rules --export … --post N` — поля, які заповнює код, для одного поста.
`python -m shafa fill --db … --root … [--post N] [--limit K] [--dry-run]` — ШІ-бланк для Карток.
`python -m shafa fill --db … --root … --batch [--limit K] | --estimate` — пакет ШІ (Картки, чий пост
старший 7 днів) або оцінка без запитів; `fill --db … --collect` — забрати готові результати.
`python -m shafa fill --db … --refill N [M …]` — «Перезаповнити» лише ці пости, без ШІ (CORE-010).
`python -m shafa ui --db … --root … [--port P] [--no-browser]` — екран перевірки в браузері.
`python -m shafa health [--json]` — самоперевірка (UI-003): база, папка, диск, ключі, бот, сайт,
вхід робота; без браузера, нічого не міняє; код виходу 1, якщо є «помилка».
`python -m shafa transfer-pack --out <zip>` / `transfer-unpack --zip <zip>` — перенесення пілоту
на інший комп'ютер (OPS-001): без секретів, нічого не видаляє й не перезаписує.
`python -m shafa report [--day YYYY-MM-DD]` — текст щоденного звіту Власнику (BOT-009); лише друкує,
нічого не надсилає.
`python -m shafa robot --login | --sync-catalog | --dry-fill --db … --post N [--post M …]` — робот
Шафи: ручний вхід Власника, знімок довідників, заповнення форми БЕЗ публікації (до 3 Карток).
`python -m shafa robot --klubok-dry-fill --fake-klubok --db … --post N [--post M …]` — KLB-001: форма
Клубку заповнюється й звіряється на ПІДСТАВНОМУ Клубку (до 3 Карток), нічого не публікується, базу
не пише; `python -m shafa klubok-catalog --from <тека з categors.json і filters.json>` — знімок.
`python -m shafa klubok-queue --db <копія> --root <Головна> [--today РРРР-ММ-ДД]` — KLB-002: пробний
прогін черги Клубку (копія бази в пам'яті, файл не змінюється; ≤ 60 рядків).
`python -m shafa klubok-import --export <тека Експорту> --wave 1|4 [--dry-run] [--db …] [--root …]` —
KLB-008: старі пости хвилі в чергу Клубку (еталон Шафи чи нова Картка «лише Клубок»); `--dry-run` —
копія бази в пам'яті, нічого не пише (≤ 60 рядків).
`python -m shafa robot --klubok-login` — KLB-003: вхід у Клубок руками (профіль `<дані>/klubok-profile`);
`--klubok-snapshot --db …` — знімок її оголошень (лише читання сайту); `--klubok-publish --db … --root …
--limit N [--post P] [--human-click]` — ПУБЛІКАЦІЯ на Клубку з черги (живий сайт, лише з дозволу
Власника); з `--fake-klubok` — на підставному Клубку й на копії бази (файл не змінюється).
`python -m shafa robot --publish --db … [--limit N]` — ПУБЛІКАЦІЯ схвалених (живий сайт!);
`--pause | --resume | --stop --db …` — керування запущеною публікацією (поточний товар дописується).
`python -m shafa bot --db … --root … [--check]` — Telegram-бот: пост від Продавчині → прев'ю →
«Опублікувати» → канал + папка з фото; `--check` — лише перевірити токен і права в каналі.

Налаштування (`SHAFA_ROOT_DIR`, `TELEGRAM_CHANNEL_ID`, `ANTHROPIC_API_KEY`) — зі змінних
середовища або з `.env` у корені проєкту.
"""

import argparse
import dataclasses
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

from shafa import ai, batch, config, db, fill, photos, review, rules, web
from shafa.export import ExportError, Post, read_channel_export, read_export
from shafa.folders import list_folders
from shafa.scan import ScanResult, scan


def format_result(result: ScanResult) -> str:
    lines = [f"Папок: {result.folders} · нових: {result.new} · у Проблемах: {len(result.problems)}"]
    if result.problems:
        lines.append("Проблеми:")
        lines += [f"  {p.folder_name}: {p.text}" for p in result.problems]
    return "\n".join(lines)


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def format_rules(post: Post) -> str:
    """Поля, які заповнює код, для одного поста — для ручної перевірки."""
    price = rules.price_from_text(post.text)
    state = rules.condition(post.text)
    insole = rules.insole_lengths(post.text)
    lines = [
        f"Пост {post.post_id}",
        f"Ціна: {price} → {rules.shafa_price(price)}" if price else "Ціна: немає",
        f"Стан: {state}",
        "Устілка: " + (" · ".join(_number(x) for x in insole) if insole else "—"),
        f"Мембрана: {rules.membrane(post.text) or '—'}",
        "Опис:",
        rules.clean_description(post.text, state),
    ]
    return "\n".join(lines)


def _cents(micro_usd: int) -> str:
    return f"{micro_usd / 10_000:.2f}".replace(".", ",") + " ¢"


def format_plan(planned: list[fill.Planned], skipped: list[fill.Skipped]) -> str:
    """Що було б надіслано: фото, токени й орієнтовна вартість. Нічого не надсилалось."""
    lines = []
    for p in planned:
        tokens = p.estimate.input_tokens
        lines.append(
            f"{p.folder_name} (пост {p.post_id}): фото {len(p.photos)}, "
            f"≈ {tokens} токенів на вхід, ≈ {_cents(p.estimate.cost_micro_usd)}"
        )
    lines += [f"{s.folder_name}: пропущено — {s.text}" for s in skipped]
    total = sum(p.estimate.cost_micro_usd for p in planned)
    lines.append(
        f"Карток: {len(planned)} · орієнтовно {_cents(total)}, пакетом ≈ {_cents(total // 2)} · "
        "нічого не надіслано"
    )
    return "\n".join(lines)


def format_filled(results: list[fill.Filled], skipped: list[fill.Skipped]) -> str:
    lines = []
    for r in results:
        cost = _cents(r.usage.cost_micro_usd) if r.usage else "0 ¢"
        if r.error:
            lines.append(f"{r.folder_name}: помилка — {r.error} ({cost})")
        else:
            lines.append(f"{r.folder_name}: бланк готовий, жовтих полів {r.yellow} ({cost})")
    lines += [f"{s.folder_name}: пропущено — {s.text}" for s in skipped]
    total = sum(r.usage.cost_micro_usd for r in results if r.usage)
    done = sum(1 for r in results if not r.error)
    lines.append(f"Бланків: {done} з {len(results)} · вартість {_cents(total)}")
    return "\n".join(lines)


def _add_root(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--root",
        type=Path,
        default=config.setting("SHAFA_ROOT_DIR"),
        help="Головна папка (за замовчуванням SHAFA_ROOT_DIR)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m shafa")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("scan", help="зіставити папки товарів із постами Експорту")
    cmd.add_argument("--export", type=Path, required=True, help="шлях до result.json")
    _add_root(cmd)
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    cmd = commands.add_parser("import-old", help="старі пости з Експорту → Картки з одним фото")
    cmd.add_argument("--export", type=Path, required=True, help="папка Експорту (або result.json)")
    cmd.add_argument("--list", type=Path, required=True, help="файл з номерами постів (.txt)")
    cmd.add_argument("--db", type=Path, help="файл бази (за замовчуванням — база програми)")
    cmd = commands.add_parser("rules", help="поля, які заповнює код, для одного поста")
    cmd.add_argument("--export", type=Path, required=True, help="шлях до result.json")
    cmd.add_argument("--post", type=int, required=True, help="номер поста")
    cmd = commands.add_parser("fill", help="ШІ-бланк для Карток new")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--post", type=int, help="лише Картка цього поста")
    cmd.add_argument(
        "--limit",
        type=int,
        help=f"скільки Карток (по одній — не більше {fill.MAX_PER_RUN}; пакетом — ліміт пакета)",
    )
    mode = cmd.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="зібрати запити й порахувати, нічого не надсилати"
    )
    mode.add_argument("--batch", action="store_true", help="відправити пакет (−50%%)")
    mode.add_argument("--collect", action="store_true", help="забрати результати пакетів")
    mode.add_argument("--estimate", action="store_true", help="що піде в пакет — без запитів")
    mode.add_argument(
        "--refill",
        type=int,
        nargs="+",
        metavar="ПОСТ",
        help="«Перезаповнити» лише ці пости (потім — «Оновити» чи --batch); ШІ тут не викликається",
    )
    cmd.add_argument(
        "--recheck-sizes",
        action="store_true",
        help="повернути розмір Карткам, де він зник (CORE-013); з --dry-run — лише показати; без ШІ",
    )
    cmd.add_argument(
        "--recheck-season",
        action="store_true",
        help="сезон за правилом «термо» (CORE-016); з --dry-run — лише показати; без ШІ",
    )
    cmd.add_argument(
        "--precheck",
        action="store_true",
        help="Схвалені, яких робот не опублікує, — назад «На перевірку» (QA-002); з --dry-run — "
        "лише показати; без ШІ й мережі",
    )
    cmd = commands.add_parser("bot", help="Telegram-бот: пост → прев'ю → канал + папка з фото")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--check", action="store_true", help="лише перевірити токен і права в каналі")
    cmd.add_argument(
        "--server", action="store_true",
        help="бот на сервері (OPS-003): без сайту, звіту й тривог; усе для компа — у вихідну чергу",
    )  # fmt: skip
    cmd = commands.add_parser("outbox", help="вихідна черга бота на сервері для компа (OPS-003)")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite сервера")
    _add_root(cmd)
    act = cmd.add_mutually_exclusive_group(required=True)
    act.add_argument("--give", type=int, metavar="N", help="zip з усім після N — у stdout")
    act.add_argument("--ack", type=int, metavar="N", help="комп застосував усе до N")
    act.add_argument(
        "--ssh", action="store_true",
        help="дія з SSH_ORIGINAL_COMMAND («give N» / «ack N») — для примусової команди SSH",
    )  # fmt: skip
    cmd = commands.add_parser("bot-pack", help="перехід бота на сервер: дані бота в один zip")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--out", type=Path, required=True, help="куди записати zip (має не існувати)")
    cmd = commands.add_parser("bot-unpack", help="перехід бота на сервер: дані бота з zip сюди")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite сервера")
    _add_root(cmd)
    cmd.add_argument("--zip", type=Path, required=True, help="zip з `bot-pack`")
    cmd = commands.add_parser("app", help="програма з ярлика: екран, бот, один екземпляр")
    cmd.add_argument("--no-browser", action="store_true", help="не відкривати браузер")
    cmd.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="порт на 127.0.0.1")
    cmd = commands.add_parser("health", help="самоперевірка: що працює, а що ні (UI-003)")
    cmd.add_argument("--json", action="store_true", help="вивести як JSON")
    cmd = commands.add_parser("report", help="щоденний звіт Власнику: лише друкує (BOT-009)")
    cmd.add_argument("--day", help="доба YYYY-MM-DD (типово сьогодні, місцевий час)")
    commands.add_parser("quit", help="закрити програму: екран і бота (для оновлення)")
    commands.add_parser("setup", help="майстер налаштування на її ноутбуці (секрети приховано)")
    cmd = commands.add_parser("transfer-pack", help="перенесення пілоту: усе потрібне в один zip")
    cmd.add_argument("--out", type=Path, required=True, help="куди записати zip (має не існувати)")
    cmd = commands.add_parser("transfer-unpack", help="перенесення пілоту: розпакувати zip сюди")
    cmd.add_argument("--zip", type=Path, required=True, help="zip з `transfer-pack`")
    cmd = commands.add_parser("ui", help="екран перевірки в браузері (лише на цьому комп'ютері)")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="порт на 127.0.0.1")
    cmd.add_argument("--no-browser", action="store_true", help="не відкривати браузер")
    cmd.add_argument("--fake-shafa", action="store_true", help=argparse.SUPPRESS)  # «мавпа»
    cmd = commands.add_parser(
        "klubok-catalog", help="знімок довідника Клубку з categors.json і filters.json (KLB-001)"
    )
    cmd.add_argument("--from", dest="source", type=Path, required=True, help="тека з двома JSON")
    cmd = commands.add_parser(
        "klubok-queue", help="пробний прогін черги Клубку на копії бази, нічого не пише (KLB-002)"
    )
    cmd.add_argument("--db", type=Path, required=True, help="файл бази (читається, не змінюється)")
    _add_root(cmd)
    cmd.add_argument("--today", help="день РРРР-ММ-ДД (типово сьогодні)")
    cmd = commands.add_parser(
        "klubok-import", help="старі пости з Експорту в чергу Клубку хвилею 1 чи 4 (KLB-008)"
    )
    cmd.add_argument("--export", type=Path, required=True, help="папка Експорту (або result.json)")
    cmd.add_argument("--wave", type=int, required=True, choices=(1, 4), help="хвиля: 1 чи 4")
    cmd.add_argument("--dry-run", action="store_true", help="лише звіт, нічого не пише")
    cmd.add_argument("--db", type=Path, help="файл бази (за замовчуванням — база програми)")
    _add_root(cmd)
    cmd = commands.add_parser("robot", help="робот Шафи")
    mode = cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--login", action="store_true", help="відкрити профіль робота для входу")
    mode.add_argument("--sync-catalog", action="store_true", help="оновити знімок довідників")
    mode.add_argument("--dry-fill", action="store_true", help="заповнити форму, не публікувати")
    mode.add_argument("--publish", action="store_true", help="ОПУБЛІКУВАТИ схвалені на Шафі")
    mode.add_argument(
        "--klubok-dry-fill", action="store_true",
        help="KLB-001: заповнити форму Клубку (поки лише підставного), не публікувати",
    )  # fmt: skip
    mode.add_argument(
        "--klubok-login",
        action="store_true",
        help="KLB-003: увійти в Клубок руками (окремий профіль)",
    )
    mode.add_argument(
        "--klubok-snapshot", action="store_true",
        help="KLB-003: перечитати її оголошення на Клубку (лише читання)",
    )  # fmt: skip
    mode.add_argument(
        "--klubok-publish", action="store_true",
        help="KLB-003: ОПУБЛІКУВАТИ на Клубку з черги (потрібні --limit, живий сайт!)",
    )  # fmt: skip
    mode.add_argument(
        "--klubok-confirm", action="store_true",
        help="KLB-007: підтвердити, що невпевнена публікація відбулась (--post P --klubok-id N)",
    )  # fmt: skip
    mode.add_argument(
        "--rehearse", action="store_true",
        help="пробний прогін (QA-003): форма заповнюється, нічого не публікується; помилки — в «Проблеми»",
    )  # fmt: skip
    mode.add_argument("--pause", action="store_true", help="пауза публікації після товару")
    mode.add_argument("--resume", action="store_true", help="продовжити після паузи")
    mode.add_argument("--stop", action="store_true", help="зупинити публікацію після товару")
    cmd.add_argument("--db", type=Path, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument(
        "--post", type=int, action="append", help="номер поста (dry-fill — до 3, rehearse — до 30)"
    )
    cmd.add_argument(
        "--sections", action="store_true", help="з --rehearse: по одній Картці з кожного розділу"
    )
    cmd.add_argument("--count", type=int, help="з --rehearse: скільки Карток (типово 5, до 30)")
    cmd.add_argument("--klubok-id", type=int, help="з --klubok-confirm: id оголошення на Клубку")
    cmd.add_argument("--limit", type=int, help="скільки опублікувати (не більше ліміту доби)")
    cmd.add_argument(
        "--human-click", action="store_true",
        help="з --klubok-publish: робот заповнює форму, «Опубликовать» тисне людина",
    )  # fmt: skip
    # Підставна Шафа (UI-002, D5): лише тести й «мавпа»; у довідці не показується.
    cmd.add_argument("--fake-shafa", action="store_true", help=argparse.SUPPRESS)
    # Підставний Клубок (KLB-001): лише тести й ручний прогін; у довідці не показується.
    cmd.add_argument("--fake-klubok", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_control(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Пауза / Продовжити / Стоп для публікації, що йде в іншому вікні (UI-002 — кнопками)."""
    from shafa.robot import publish

    if args.db is None or not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    what = "pause" if args.pause else "stop" if args.stop else None
    conn = db.connect(args.db)
    try:
        found = publish.request(conn, what)
    finally:
        conn.close()
    if not found:
        print("Робот зараз не публікує")
        return 1
    print({"pause": "Пауза після поточного товару", "stop": "Стоп після поточного товару"}.get(
        what, "Продовжуємо"))  # fmt: skip
    return 0


def run_robot(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.pause or args.resume or args.stop:
        return run_control(parser, args)
    if args.human_click and not args.klubok_publish:
        parser.error("--human-click лише з --klubok-publish")
    if args.klubok_confirm:
        return run_klubok_confirm(parser, args)
    if args.klubok_id is not None:
        parser.error("--klubok-id лише з --klubok-confirm")
    if args.klubok_login or args.klubok_snapshot or args.klubok_publish:
        return run_klubok(parser, args)
    if args.klubok_dry_fill or args.fake_klubok:
        return run_klubok_dry_fill(parser, args)

    from shafa.robot import form, lock, publish

    root, limit, day_limit = None, 0, 0
    if args.publish or args.dry_fill or args.rehearse:
        root = _root(parser, args)
        if args.db is None or not args.db.is_file():
            parser.error(f"Базу не знайдено: {args.db}")
    if args.publish:
        try:
            day_limit = publish.daily_limit()
        except publish.SetupError as error:
            parser.error(str(error))
        limit = day_limit if args.limit is None else args.limit
        if not 1 <= limit <= day_limit:
            parser.error(f"--limit від 1 до {day_limit} (ліміт доби)")
    if args.fake_shafa and not args.publish:
        parser.error("--fake-shafa лише разом із --publish")
    if (args.sections or args.count is not None) and not args.rehearse:
        parser.error("--sections і --count — лише з --rehearse")
    if args.rehearse:
        _check_rehearse(parser, args)
    if args.dry_fill and (not args.post or len(args.post) > form.MAX_CARDS):
        parser.error(f"--post від 1 до {form.MAX_CARDS} разів")

    mode = next(
        m for m in ("login", "sync_catalog", "dry_fill", "publish", "rehearse") if getattr(args, m)
    )
    try:  # замок — до профілю Chrome і до будь-яких змін у базі (D1)
        held = lock.acquire(mode.replace("_", "-"))
    except lock.Busy as error:
        print(error)
        return 2
    try:
        return _with_browser(args, held, root, limit, day_limit)
    finally:
        held.release()


def _with_browser(args, held, root, limit: int, day_limit: int) -> int:
    from playwright.sync_api import sync_playwright

    from shafa.robot import browser, control, form, listings, publish, rehearse, sync

    def log(line: str) -> None:
        print(line, flush=True)

    with sync_playwright() as p:
        if args.fake_shafa:
            return _fake_publish(p, args, held, root, limit, day_limit, log)
        if args.login:
            print("Увійдіть на Шафу у відкритому вікні й закрийте його.", flush=True)
            why = browser.wait_for_manual_login(p)
            control.mark_login()  # UX-002: вікно закрито — «Старт» знову доступний
            print({"closed": "Вікно закрито.", "done": "Вікно закрито кнопкою «Я увійшов».",
                   "timeout": "15 хв минуло — вікно закрито."}[why], flush=True)  # fmt: skip
            return 0
        cards = None
        if args.rehearse:  # Картки — до Chrome: порожній прогін вікна не відкриває
            cards = _rehearse_cards(args, root, log)
            if not cards:
                return 0
        session = browser.Session(
            p, allow_upload=args.dry_fill or args.rehearse, publish=args.publish
        )
        try:
            if args.sync_catalog:
                snapshot = sync.sync_and_save(session, sync.CACHE, log=log)
                print(f"Знімок збережено: розділів {len(snapshot['sections'])}")
                print("Далі: fill --db <база> --precheck — схвалені, яких робот тепер не опублікує")
                return 0
            conn = db.connect(args.db)
            try:
                if args.rehearse:
                    rehearse.rehearse(
                        conn, root, cards, session, form.PageDriver, form.screens_dir(), log=log
                    )
                    return 0
                if args.dry_fill:
                    form.dry_fill(
                        conn, root, args.post, session, form.PageDriver, form.screens_dir(), log=log
                    )
                    return 0
                publish.Run(
                    conn, root, session, form.PageDriver,
                    lambda s: listings.load(s, log=log), form.screens_dir(), held, log=log,
                    load_declined=lambda s: listings.load_declined(s, log=log),
                    fetch_photo=lambda url: listings.fetch_photo(session, url),
                    page_photo=lambda url: listings.page_photo(session, url),
                    photos_dir=duplicates_dir(args.db), presence=channel_presence(root, log),
                    judge=duplicate_judge(conn),
                ).start(limit, day_limit)  # fmt: skip
            except KeyboardInterrupt:  # звіт сесії вже надруковано
                return 130
            finally:
                conn.close()
        finally:
            session.close()
    return 0


def _check_rehearse(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    from shafa.robot import rehearse

    if args.sections and args.post:
        parser.error("--sections і --post разом не можна")
    if args.post and len(args.post) > rehearse.REHEARSE_MAX:
        parser.error(f"--post — не більше {rehearse.REHEARSE_MAX} разів")
    if args.count is None:
        try:
            args.count = rehearse.count_setting()
        except rehearse.SetupError as error:
            parser.error(str(error))
    if not 1 <= args.count <= rehearse.REHEARSE_MAX:
        parser.error(f"--count від 1 до {rehearse.REHEARSE_MAX}")


def _rehearse_cards(args, root, log) -> list[int]:
    """Які Картки проходить пробний прогін; порожньо — нічого робити (вікно Chrome не потрібне)."""
    from shafa.robot import publish, rehearse

    conn = db.connect(args.db)
    try:
        if args.post:
            cards, missing = rehearse.pick_posts(conn, args.post)
            for post_id in missing:
                log(f"Пост {post_id}: Картки немає")
        elif args.sections:
            cards = rehearse.pick_sections(conn, root, args.count)
        else:
            cards = rehearse.pick_tomorrow(conn, datetime.now().astimezone(), args.count)
    except publish.SetupError as error:
        log(f"Пробний прогін не запущено: {error}")
        return []
    finally:
        conn.close()
    if not cards:
        log("Пробний прогін: немає Карток для перевірки")
    return cards


def _fake_publish(p, args, held, root, limit: int, day_limit: int, log) -> int:
    """Публікація на підставну Шафу: справжній двигун, браузер лише на 127.0.0.1 (D5)."""
    import tempfile

    from shafa.robot import fake, publish

    server = fake.Server()
    session = fake.FakeSession(p, server.base)
    conn = db.connect(args.db)
    try:
        with tempfile.TemporaryDirectory(prefix="fake-screens-") as screens:
            publish.Run(
                conn, root, session, fake.FakeDriver, lambda s: [], Path(screens), held,
                log=log, rand=fake.pause_seconds,
            ).start(limit, day_limit)  # fmt: skip
    except KeyboardInterrupt:
        return 130
    finally:
        conn.close()
        log(f"Підставна Шафа: заблоковано запитів назовні — {len(session.outside())}")
        session.close()
        server.close()
    return 0


def duplicate_judge(conn):
    """ШІ, що порівнює схоже оголошення з Карткою (DUP-001). Ключ береться лише в момент запиту;
    не налаштовано чи бюджет не прочитано — ШІ не питаємо, Картка йде в «Проблеми» як раніше."""
    from shafa import dup

    def budget_left() -> int:
        return batch.budget_left(conn, datetime.now().astimezone(), batch.limits())

    return dup.Judge(conn, lambda: ai.make_client(config.api_key()), budget_left)


def channel_presence(root: Path, log):
    """Перевірка «пост ще є в каналі» для робота (SITE-002) або None — бот не налаштований."""
    from shafa.bot import presence, run
    from shafa.bot.telegram import Telegram

    try:
        settings = run.settings(root)
        token = run.token()
    except run.SetupError as error:
        log(f"Канал не перевіряю перед публікацією: {error}")
        return None
    chat = presence.service_chat(settings.owner_chat, settings.seller_chat)
    if chat is None:
        return None
    return presence.Presence(
        Telegram(token), settings.channel_id, chat, log=log, data_dir=config.data_dir()
    ).check


def duplicates_dir(db_path: Path) -> Path:
    """Фото схожих оголошень (CORE-007) — у теці даних поруч із базою, для екрана Проблем."""
    from shafa.robot.listings import PHOTOS_DIR

    return db_path.resolve().parent / PHOTOS_DIR


def thumbs_dir(db_path: Path) -> Path:
    """Кеш мініатюр — у теці даних поруч із базою, не в папках з фото."""
    return db_path.resolve().parent / "thumbs"


def run_ui(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    root = _root(parser, args)
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    cache_dir = thumbs_dir(args.db)
    conn = db.connect(args.db)
    try:
        for item_id in review.purge_archive(conn):
            photos.drop_thumbnails(cache_dir, item_id)
        if count := fill.recheck(conn):  # нова версія правил — перерахунок без ШІ (CORE-009 B)
            print(f"Перераховано за новими правилами, без ШІ: {count}")
    finally:
        conn.close()
    app = web.App(
        args.db, root, cache_dir, channel_id=config.setting("TELEGRAM_CHANNEL_ID"),
        fake_shafa=args.fake_shafa,
    )  # fmt: skip
    server = web.make_server(app, args.port)
    app.klubok_sync.start()  # KLB-010: хвилі й черга Клубку — у фоні
    url = f"http://{web.HOST}:{app.port}/"
    print(f"Екран перевірки: {url}\nЗупинити — Ctrl+C")
    if not args.fake_shafa and app.start_bot() == "bot_started":  # бот — разом з екраном
        print("Бот запускається окремим процесом (журнал — bot-run.log у теці даних)")
    sync_stop = None
    if not args.fake_shafa:  # OPS-004: бот на сервері — обмін, звіт, тривоги, сайт
        from shafa.app import start_sync

        sync_stop = start_sync(args.db, root)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if sync_stop is not None:
            sync_stop.set()
        server.server_close()
    return 0


def run_rules(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.export.is_file():
        parser.error(f"Файл Експорту не знайдено: {args.export}")
    post = read_export(args.export).get(args.post)
    if post is None:
        parser.error(f"Поста з номером {args.post} немає в експорті")
    print(format_rules(post))
    return 0


def run_klubok_confirm(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """KLB-007: лише база. Не під час живої сесії робота (замок), Клубок не чіпаємо."""
    from datetime import UTC, datetime

    from shafa.klubok import queue as klubok_queue
    from shafa.robot import lock

    if not args.post or len(args.post) != 1 or args.klubok_id is None or args.klubok_id <= 0:
        parser.error("--klubok-confirm: потрібні один --post і додатний --klubok-id")
    if args.db is None or not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    if lock.busy() is not None:
        print("Робот зараз працює — дочекайтесь кінця сесії")
        return 2
    conn = db.connect(args.db)
    try:
        print(klubok_queue.confirm(conn, args.post[0], args.klubok_id, datetime.now(UTC)))
    except klubok_queue.ConfirmError as error:
        print(error)
        return 1
    finally:
        conn.close()
    return 0


def run_klubok(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """KLB-003: вхід, знімок, публікація на Клубку. Той самий замок, що й у робота Шафи."""
    from shafa.klubok import queue as klubok_queue
    from shafa.robot import klubok_publish, lock

    root = None
    if args.klubok_publish:
        low, high = klubok_publish.LIMIT_RANGE
        if args.limit is None or not low <= args.limit <= high:
            parser.error(f"--klubok-publish: --limit обов'язковий, від {low} до {high}")
        if args.post and len(args.post) > 1:
            parser.error("--klubok-publish: --post — один номер")
        root = _root(parser, args)
        try:
            klubok_queue.daily_limit()
            klubok_queue.from_post()
        except klubok_queue.SetupError as error:
            parser.error(str(error))
    if (args.klubok_snapshot or args.klubok_publish) and (args.db is None or not args.db.is_file()):
        parser.error(f"Базу не знайдено: {args.db}")
    if args.fake_klubok and args.klubok_login:
        parser.error("--fake-klubok не для входу")
    mode = "klubok-login" if args.klubok_login else (
        "klubok-snapshot" if args.klubok_snapshot else "klubok-publish"
    )  # fmt: skip
    try:
        with lock.hold(mode):
            return _klubok_with_browser(args, root)
    except lock.Busy as error:
        print(error)
        return 2


def _klubok_with_browser(args: argparse.Namespace, root: Path | None) -> int:
    import signal
    import sqlite3
    import tempfile
    from contextlib import closing

    from playwright.sync_api import sync_playwright

    from shafa.robot import browser, klubok_fake, klubok_form, klubok_publish
    from shafa.robot import form as shafa_form

    def log(line: str) -> None:
        print(line, flush=True)

    if args.klubok_login:
        with sync_playwright() as p:
            print("Увійдіть у Клубок у відкритому вікні й закрийте його.", flush=True)
            why = browser.wait_for_manual_login(
                p, profile=klubok_form.profile_dir(), url=klubok_form.FORM_URL
            )
        print({"closed": "Вікно закрито.", "done": "Вікно закрито кнопкою «Я увійшов».",
               "timeout": "15 хв минуло — вікно закрито."}[why], flush=True)  # fmt: skip
        return 0

    stop: list[bool] = []

    def on_sigint(_signum, _frame) -> None:
        if not stop:
            print("Ctrl+C: доробляю поточну Картку й зупиняюсь", flush=True)
        stop.append(True)

    server = temp = None
    db_path = args.db
    if args.fake_klubok:  # підставний Клубок — лише на копії бази: файл не змінюється
        server = klubok_fake.Server(listings=120)
        temp = tempfile.TemporaryDirectory(prefix="klubok-fake-")
        db_path = Path(temp.name) / "копія.sqlite"
        with closing(sqlite3.connect(args.db)) as source, closing(sqlite3.connect(db_path)) as copy:
            source.backup(copy)
        log(f"Підставний Клубок, копія бази: {db_path}")
    conn = db.connect(db_path)
    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        with sync_playwright() as p:
            if server is not None:
                session: klubok_form.Session = klubok_fake.FakeSession(
                    p, server.base, headless=False, publishing=args.klubok_publish,
                    human=args.human_click,
                )  # fmt: skip
            else:
                session = klubok_form.LiveSession(
                    p, publishing=args.klubok_publish, human=args.human_click
                )
            try:
                if args.klubok_snapshot:
                    return _klubok_snapshot(conn, root, session, log)
                opts = klubok_publish.Options(
                    limit=args.limit, post_id=args.post[0] if args.post else None,
                    human=args.human_click,
                )  # fmt: skip
                result = klubok_publish.run(
                    conn, root, session, opts, stopping=lambda: bool(stop), log=log,
                    screens=shafa_form.screens_dir(), now_file=klubok_publish.now_path(),
                )  # fmt: skip
                print("\n".join(result.lines()), flush=True)
                return 0 if result.code in (klubok_publish.DONE, klubok_publish.STOPPED) else 1
            finally:
                session.close()
    finally:
        signal.signal(signal.SIGINT, previous)
        conn.close()
        if server is not None:
            server.close()
        if temp is not None:
            temp.cleanup()


def _klubok_snapshot(conn, root, session, log) -> int:
    import random

    from shafa.klubok import refresh, snapshot
    from shafa.robot import klubok_form, klubok_publish

    opened = session.open_form()
    if not opened.ok or session.user_id() != klubok_form.KLUBOK_USER:
        log(klubok_publish.LOGIN_TEXT)
        return 1
    try:
        snapshot.refresh(
            conn, session.fetch_text,
            pause=lambda: klubok_publish.nap(
                random.uniform(*klubok_publish.PAGE_PAUSE), lambda: False
            ),
            wait=lambda s: klubok_publish.nap(s, lambda: False), log=log,
        )  # fmt: skip
    except snapshot.SnapshotError as error:
        log(f"Знімок не записано: {error.text}")
        return 1
    refresh.refresh(conn, root)  # KLB-010: дублі за свіжим знімком — у черзі одразу
    return 0


def run_klubok_dry_fill(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """KLB-001: форма Клубку заповнюється й звіряється, НЕ публікується; лише підставний Клубок."""
    from shafa.robot import form as shafa_form
    from shafa.robot import klubok_fake, klubok_form, lock

    if not args.klubok_dry_fill:
        parser.error("--fake-klubok лише разом із --klubok-dry-fill")
    if not args.fake_klubok:
        parser.error("Живий Клубок — з KLB-003; поки лише --fake-klubok")
    if not args.post or len(args.post) > shafa_form.MAX_CARDS:
        parser.error(f"--post від 1 до {shafa_form.MAX_CARDS} разів")
    root = _root(parser, args)
    if args.db is None or not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    try:
        conn = klubok_form.open_readonly(args.db)  # лише читання: у базу нічого не пишемо
    except klubok_form.KlubokError as error:
        print(error.text)
        return 2
    from playwright.sync_api import sync_playwright

    server = klubok_fake.Server()
    try:
        with lock.hold("klubok-dry-fill"), sync_playwright() as p:  # той самий замок, що й у Шафи
            session = klubok_fake.FakeSession(p, server.base, headless=False)
            try:
                klubok_form.dry_fill(
                    conn, root, args.post, session, klubok_form.KlubokDriver,
                    shafa_form.screens_dir(), log=lambda line: print(line, flush=True),
                )  # fmt: skip
            finally:
                session.close()
    except lock.Busy as error:
        print(error)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        server.close()
        conn.close()
    return 0


def _root(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    if args.root is None:
        parser.error("потрібна Головна папка: --root або змінна SHAFA_ROOT_DIR")
    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"Головну папку не знайдено: {root}")
    return root


def format_estimate(plan: batch.Plan, spent: int, lim: batch.Limits) -> str:
    lines = [
        f"{p.folder_name} (пост {p.post_id}): фото {len(p.photos)}, "
        f"≈ {_cents(p.estimate.cost_micro_usd)}"
        for p in plan.planned
    ]
    lines += [f"{s.folder_name}: у «Проблеми» — {s.text}" for s in plan.skipped]
    lines.append(
        f"У пакет: {len(plan.planned)} · ≈ {_cents(plan.estimate)} (найгірше: кеш не спрацював) · "
        f"лишиться в черзі {plan.left}{' (ліміт грошей)' if plan.over_budget else ''}"
    )
    lines.append(
        f"Нові чекають 7 днів: {plan.young} · чекають відповіді ШІ: {plan.pending} · "
        f"{f'літні чекають весни: {plan.summer} · ' if plan.summer else ''}"
        f"сьогодні {batch.dollars(spent)} з {batch.dollars(lim.daily_micro_usd)} · нічого не надіслано"
    )
    return "\n".join(lines)


def _client(parser: argparse.ArgumentParser):
    try:
        return ai.make_client(config.api_key())
    except config.ConfigError as error:
        parser.error(str(error))


def run_batch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`fill --batch | --collect | --estimate`."""
    try:
        lim = batch.limits()
    except batch.SetupError as error:
        parser.error(str(error))
    if args.limit is not None and not 1 <= args.limit <= lim.batch_limit:
        parser.error(f"--limit від 1 до {lim.batch_limit}")
    root = None if args.collect else _root(parser, args)
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    client = None if args.estimate else _client(parser)
    today = datetime.now().astimezone()
    conn = db.connect(args.db)
    try:
        if args.estimate:
            plan = batch.prepare(conn, root, today, lim, args.limit)
            print(format_estimate(plan, batch.spent_today(conn, today), lim))
        elif args.collect:
            report = batch.collect(conn, client, today)
            waiting = f"пакетів ще в роботі: {report.waiting}" if report.waiting else None
            print("; ".join(t for t in (batch.collected_text(report), waiting) if t) or "Нічого")
        else:
            print(batch.sent_text(batch.send(conn, client, root, today, lim, args.limit), lim))
    finally:
        conn.close()
    return 0


def run_refill(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    conn = db.connect(args.db)
    try:
        result = fill.refill_posts(conn, args.refill)
    finally:
        conn.close()
    print("У чергу ШІ: " + (", ".join(map(str, result.marked)) or "нічого"))
    if result.skipped:
        print("Не позначено (немає Картки чи вона не в «Перевірити»/«Проблеми»): ")
        print("  " + ", ".join(map(str, result.skipped)))
    return 0


def run_recheck_sizes(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    conn = db.connect(args.db)
    try:
        result = fill.recheck_sizes(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "Буде повернуто розмір" if args.dry_run else "Розмір повернуто, Картки в «Перевірити»"
    print(f"{verb}: " + (", ".join(map(str, result.fixed)) or "нікому"))
    if result.still_empty:
        print("Розміру немає й у відповіді ШІ (потрібне «Перезаповнити»), не чіпав:")
        print("  " + ", ".join(map(str, result.still_empty)))
    return 0


def run_recheck_season(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    conn = db.connect(args.db)
    try:
        result = fill.recheck_season(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    for post, was, became in result.changed:
        print(f"Пост {post}: {was} → {became}")
    verb = "Змінилось би" if args.dry_run else "Змінилось"
    print(f"{verb}: {len(result.changed)}; схвалених іде «На перевірку»: {len(result.returned)}")
    return 0


def run_precheck(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    root = _root(parser, args)
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    conn = db.connect(args.db)
    try:
        result = fill.precheck_approved(conn, root, dry_run=args.dry_run)
    finally:
        conn.close()
    for post, why in result.returned:
        print(f"Пост {post}: {why}")
    verb = "Буде повернуто на перевірку" if args.dry_run else "Повернуто на перевірку"
    print(f"{verb}: {len(result.returned)}; робот опублікує: {result.kept}")
    return 0


def run_fill(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.precheck:
        return run_precheck(parser, args)
    if args.recheck_sizes:
        return run_recheck_sizes(parser, args)
    if args.recheck_season:
        return run_recheck_season(parser, args)
    if args.refill:
        return run_refill(parser, args)
    if args.batch or args.collect or args.estimate:
        if args.post is not None:
            parser.error("--post — лише для запитів по одній Картці")
        return run_batch(parser, args)
    root = _root(parser, args)
    if args.limit is None:
        args.limit = fill.MAX_PER_RUN
    if not 1 <= args.limit <= fill.MAX_PER_RUN:
        parser.error(f"--limit від 1 до {fill.MAX_PER_RUN}")
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    client = None if args.dry_run else _client(parser)
    conn = db.connect(args.db)
    try:
        rows = fill.pick_items(conn, args.post, args.limit)
        if not rows:
            if args.post is None:
                print("Немає Карток у статусі new")
            else:
                print(f"Немає Картки поста {args.post} у статусі new")
            return 0
        planned, skipped = [], []
        for row in rows:
            result = fill.plan(conn, root, row)
            (planned if isinstance(result, fill.Planned) else skipped).append(result)
        if args.dry_run:
            print(format_plan(planned, skipped))
            return 0
        results = fill.fill_all(conn, client, planned)
    finally:
        conn.close()
    print(format_filled(results, skipped))
    return 0


def run_bot(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from shafa.bot import client, run
    from shafa.bot.core import Bot
    from shafa.bot.telegram import Telegram
    from shafa.robot import lock

    root = _root(parser, args)
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    if client.enabled() and not args.server and not args.check:
        print(run.SERVER_TEXT)  # OPS-004: два опитувачі — 409 і падіння бота на сервері
        return 2
    try:
        settings, token = run.settings(root), run.token()
    except run.SetupError as error:
        parser.error(str(error))
    try:
        held = lock.acquire("bot", config.data_dir() / run.LOCK_NAME)
    except lock.Busy:
        print("Бот уже працює — другий запуск нічого не чіпає")
        return 2
    tg = Telegram(token)
    conn = db.connect(args.db)
    try:
        name, group = run.check_rights(tg, settings.channel_id)
        settings = dataclasses.replace(settings, discussion_id=group, server=args.server)
        print(f"Бот @{name}: права в каналі є, коментарі — лише вона й бот")
        if args.check:
            return 0
        run.set_status(conn, started=True)
        bot = Bot(conn, tg, settings)
        bot.recover()
        # OPS-003: на сервері сайту, нічної перевірки, звіту й тривог немає — це робить комп
        stop_site = None if args.server else run.start_site(args.db, root, token, settings)
        print("Бот працює на сервері (усе для компа — у вихідній черзі). Зупинити — Ctrl+C"
              if args.server else "Бот працює. Зупинити — Ctrl+C")  # fmt: skip
        try:
            run.poll(bot, tg, conn, stop=lambda: False)
        finally:
            if stop_site is not None:
                stop_site.set()
    except run.SetupError as error:
        print(f"Бот зупинився: {error}")
        run.set_status(conn, error=str(error))
        return 1
    except KeyboardInterrupt:
        print("Бота зупинено")
    finally:
        conn.close()
        tg.close()
        held.release()
    return 0


def run_outbox(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Вихідна черга сервера (OPS-003). У stdout — лише zip (`give`) чи «ok N» (`ack`): його читає
    комп через SSH; усе інше — у stderr."""
    import os

    from shafa.bot import outbox

    root = _root(parser, args)
    if not args.db.is_file():
        print(f"Базу не знайдено: {args.db}", file=sys.stderr)
        return 2
    try:
        if args.ssh:
            action, number = outbox.parse_ssh(os.environ.get("SSH_ORIGINAL_COMMAND"))
        elif args.give is not None:
            action, number = "give", args.give
        else:
            action, number = "ack", args.ack
        conn = db.connect(args.db)
        try:
            if action == "give":
                data, _ = outbox.give_bytes(conn, root, number)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            else:
                print(f"ok {outbox.ack(conn, number, root).upto}")
        finally:
            conn.close()
    except outbox.OutboxError as error:
        print(f"Не вдалося: {error}", file=sys.stderr)
        return 2
    return 0


def run_bot_move(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from shafa.bot import move

    root = _root(parser, args)
    try:
        if args.command == "bot-pack":
            print(move.pack(args.db, root, args.out).text())
        else:
            print(move.unpack(args.zip, args.db, root).text())
    except move.MoveError as error:
        print(f"Не вдалося: {error}", file=sys.stderr)
        return 2
    return 0


def run_health(args: argparse.Namespace) -> int:
    import json

    from shafa import app, health

    checks = health.run_all(config.data_dir(), app.root_dir(), deps=health.real_deps())
    if args.json:
        print(json.dumps([dataclasses.asdict(c) for c in checks], ensure_ascii=False, indent=2))
    else:
        print(health.render(checks))
    return 1 if health.has_failures(checks) else 0


def run_transfer(args: argparse.Namespace) -> int:
    from shafa import transfer

    try:
        if args.command == "transfer-pack":
            print(transfer.pack(args.out).text())
        else:
            print(transfer.unpack(args.zip, log=print).text())
    except transfer.TransferError as error:
        print(f"Не вдалося: {error}", file=sys.stderr)
        return 2
    return 0


def run_report(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from shafa import app
    from shafa.bot import report

    today = datetime.now().astimezone().date()
    try:
        day = date.fromisoformat(args.day) if args.day else today
    except ValueError:
        parser.error("--day: потрібна дата YYYY-MM-DD, наприклад 2026-10-21")
    path = app.db_path()
    if not path.is_file():
        print("Бази даних ще немає — запустіть програму з ярлика")
        return 1
    conn = db.connect(path)
    try:
        print(report.build(conn, config.data_dir(), day, late=day != today))
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "outbox":  # до будь-якого друку: stdout — це zip для компа
        return run_outbox(parser, args)
    note = config.migrate_env()
    if note:
        print(note)
    if args.command == "rules":
        return run_rules(parser, args)
    if args.command == "import-old":
        return run_import_old(parser, args)
    if args.command == "fill":
        return run_fill(parser, args)
    if args.command == "ui":
        return run_ui(parser, args)
    if args.command == "klubok-catalog":
        return run_klubok_catalog(args)
    if args.command == "klubok-queue":
        return run_klubok_queue(parser, args)
    if args.command == "klubok-import":
        return run_klubok_import(parser, args)
    if args.command == "robot":
        return run_robot(parser, args)
    if args.command == "bot":
        return run_bot(parser, args)
    if args.command == "app":
        from shafa import app

        opener = (lambda url: None) if args.no_browser else webbrowser.open
        return app.start(opener, args.port)
    if args.command == "health":
        return run_health(args)
    if args.command == "report":
        return run_report(parser, args)
    if args.command == "setup":
        from shafa import wizard

        return wizard.run()
    if args.command in ("transfer-pack", "transfer-unpack"):
        return run_transfer(args)
    if args.command in ("bot-pack", "bot-unpack"):
        return run_bot_move(parser, args)
    if args.command == "quit":
        from shafa import app

        print("Програму закрито" if app.quit_app() else "Програма не працювала")
        return 0
    root = _root(parser, args)
    if not args.export.is_file():
        parser.error(f"Файл Експорту не знайдено: {args.export}")

    try:
        posts = read_channel_export(args.export)
    except ExportError as error:  # KLB-010 (N1): Експорт іншого чату — нічого не пишемо
        print(error)
        return 2
    folders = list_folders(root, config.setting("TELEGRAM_CHANNEL_ID"))
    conn = db.connect(args.db)
    try:
        result = scan(conn, posts, folders)
    finally:
        conn.close()
    print(format_result(result))
    return 0


def run_klubok_catalog(args: argparse.Namespace) -> int:
    from shafa.klubok import catalog

    try:
        snapshot = catalog.build(args.source)
    except catalog.CatalogError as error:
        print(f"Знімок не збережено: {error}")
        return 2
    catalog.save(snapshot)
    print(f"Знімок збережено: {catalog.SNAPSHOT.name}, листків {len(snapshot['leaves'])}")
    return 0


def run_klubok_queue(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from datetime import date as date_
    from datetime import datetime, time

    from shafa.klubok import preview, queue

    root = _root(parser, args)
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    today = datetime.now().astimezone()
    if args.today:
        try:
            day = date_.fromisoformat(args.today)
        except ValueError:
            parser.error("--today — дата РРРР-ММ-ДД")
        today = datetime.combine(day, time(12, 0)).astimezone()
    try:
        conn = preview.open_copy(args.db)
    except preview.CopyError as error:
        print(error)
        return 2
    try:
        print("\n".join(preview.text(conn, root, today)))
    except queue.SetupError as error:
        print(f"Налаштування Клубку з помилкою: {error}")
        return 2
    finally:
        conn.close()
    return 0


def run_klubok_import(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from datetime import datetime

    from shafa import app
    from shafa.klubok import importer, preview, refresh

    export = args.export / "result.json" if args.export.is_dir() else args.export
    if not export.is_file():
        parser.error(f"Експорт не знайдено: {export}")
    root = _root(parser, args)
    path = args.db or app.db_path()
    if not path.is_file():
        parser.error(f"Базу не знайдено: {path}")
    try:
        posts = read_channel_export(export)
    except ExportError as error:  # KLB-010 (N1)
        print(error)
        return 2
    try:
        conn = preview.open_copy(path) if args.dry_run else db.connect(path)
    except (preview.CopyError, RuntimeError) as error:
        print(error)
        return 2
    try:
        today = datetime.now().astimezone()
        report = importer.run(
            conn, posts, export.parent, root, args.wave, apply=not args.dry_run, today=today
        )
        lines = report.lines()
        if not args.dry_run:  # KLB-010: черга одразу актуальна — без «Перерахувати чергу»
            count = refresh.refresh(conn, root, today)
            if count is not None:
                lines.append(f"Черга Клубку перерахована: {count} пар")
    except importer.ImportFailed as error:
        print(error)
        return 2
    finally:
        conn.close()
    print("\n".join(lines))
    return 0


def run_import_old(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from shafa import app, borh

    export = args.export / "result.json" if args.export.is_dir() else args.export
    if not export.is_file():
        parser.error(f"Експорт не знайдено: {export}")
    if not args.list.is_file():
        parser.error(f"Файл-список не знайдено: {args.list}")
    try:
        numbers = borh.read_list(args.list)
    except (borh.ListError, UnicodeDecodeError) as error:
        parser.error(f"Файл-список: {error}")
    try:
        posts = read_channel_export(export)
    except ExportError as error:  # KLB-010 (N1)
        print(error)
        return 2
    path = args.db or app.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    try:
        result = borh.import_old(conn, posts, export.parent, numbers)
    finally:
        conn.close()
    print(result.text())
    return 0


LOG_NAME = "app.log"


def prepare_output() -> None:
    """Вивід програми.

    Ярлик запускає `pythonw.exe` — без вікна, і тоді `sys.stdout`/`sys.stderr` — None: друк і
    помилки йдуть у журнал `app.log` у теці даних. У консолі Windows може не бути кирилиці чи «·» —
    там краще знак питання, ніж падіння.
    """
    if sys.stdout is None or sys.stderr is None:
        path = config.data_dir() / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


if __name__ == "__main__":
    prepare_output()
    sys.exit(main())
