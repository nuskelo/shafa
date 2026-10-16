"""Команди.

`python -m shafa scan --export … --root … --db …` — зіставити папки й пости.
`python -m shafa import-old --export <папка експорту> --list <файл.txt> [--db …]` — старі пости
з Експорту (з фото) без папок → Картки з одним фото поста (CORE-005); база — програми.
`python -m shafa rules --export … --post N` — поля, які заповнює код, для одного поста.
`python -m shafa fill --db … --root … [--post N] [--limit K] [--dry-run]` — ШІ-бланк для Карток.
`python -m shafa fill --db … --root … --batch [--limit K] | --estimate` — пакет ШІ (Картки, чий пост
старший 7 днів) або оцінка без запитів; `fill --db … --collect` — забрати готові результати.
`python -m shafa ui --db … --root … [--port P] [--no-browser]` — екран перевірки в браузері.
`python -m shafa robot --login | --sync-catalog | --dry-fill --db … --post N [--post M …]` — робот
Шафи: ручний вхід Власника, знімок довідників, заповнення форми БЕЗ публікації (до 3 Карток).
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
from datetime import datetime
from pathlib import Path

from shafa import ai, batch, config, db, fill, photos, review, rules, web
from shafa.export import Post, read_export
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
    cmd = commands.add_parser("bot", help="Telegram-бот: пост → прев'ю → канал + папка з фото")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--check", action="store_true", help="лише перевірити токен і права в каналі")
    cmd = commands.add_parser("app", help="програма з ярлика: екран, бот, один екземпляр")
    cmd.add_argument("--no-browser", action="store_true", help="не відкривати браузер")
    cmd.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="порт на 127.0.0.1")
    commands.add_parser("quit", help="закрити програму: екран і бота (для оновлення)")
    commands.add_parser("setup", help="майстер налаштування на її ноутбуці (секрети приховано)")
    cmd = commands.add_parser("ui", help="екран перевірки в браузері (лише на цьому комп'ютері)")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="порт на 127.0.0.1")
    cmd.add_argument("--no-browser", action="store_true", help="не відкривати браузер")
    cmd.add_argument("--fake-shafa", action="store_true", help=argparse.SUPPRESS)  # «мавпа»
    cmd = commands.add_parser("robot", help="робот Шафи")
    mode = cmd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--login", action="store_true", help="відкрити профіль робота для входу")
    mode.add_argument("--sync-catalog", action="store_true", help="оновити знімок довідників")
    mode.add_argument("--dry-fill", action="store_true", help="заповнити форму, не публікувати")
    mode.add_argument("--publish", action="store_true", help="ОПУБЛІКУВАТИ схвалені на Шафі")
    mode.add_argument("--pause", action="store_true", help="пауза публікації після товару")
    mode.add_argument("--resume", action="store_true", help="продовжити після паузи")
    mode.add_argument("--stop", action="store_true", help="зупинити публікацію після товару")
    cmd.add_argument("--db", type=Path, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--post", type=int, action="append", help="номер поста (до 3 разів)")
    cmd.add_argument("--limit", type=int, help="скільки опублікувати (не більше ліміту доби)")
    # Підставна Шафа (UI-002, D5): лише тести й «мавпа»; у довідці не показується.
    cmd.add_argument("--fake-shafa", action="store_true", help=argparse.SUPPRESS)
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

    from shafa.robot import form, lock, publish

    root, limit, day_limit = None, 0, 0
    if args.publish or args.dry_fill:
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
    if args.dry_fill and (not args.post or len(args.post) > form.MAX_CARDS):
        parser.error(f"--post від 1 до {form.MAX_CARDS} разів")

    mode = next(m for m in ("login", "sync_catalog", "dry_fill", "publish") if getattr(args, m))
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

    from shafa.robot import browser, form, listings, publish, sync

    def log(line: str) -> None:
        print(line, flush=True)

    with sync_playwright() as p:
        if args.fake_shafa:
            return _fake_publish(p, args, held, root, limit, day_limit, log)
        if args.login:
            print("Увійдіть на Шафу у відкритому вікні й закрийте його.")
            browser.wait_for_manual_login(p)
            print("Вікно закрито.")
            return 0
        session = browser.Session(p, allow_upload=args.dry_fill, publish=args.publish)
        try:
            if args.sync_catalog:
                snapshot = sync.crawl(session, sync.CACHE, log=log)
                sync.save(snapshot)
                print(f"Знімок збережено: розділів {len(snapshot['sections'])}")
                return 0
            conn = db.connect(args.db)
            try:
                if args.dry_fill:
                    form.dry_fill(
                        conn, root, args.post, session, form.PageDriver, form.screens_dir(), log=log
                    )
                    return 0
                publish.Run(
                    conn, root, session, form.PageDriver,
                    lambda s: listings.load(s, log=log), form.screens_dir(), held, log=log,
                ).start(limit, day_limit)  # fmt: skip
            except KeyboardInterrupt:  # звіт сесії вже надруковано
                return 130
            finally:
                conn.close()
        finally:
            session.close()
    return 0


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
    finally:
        conn.close()
    app = web.App(
        args.db, root, cache_dir, channel_id=config.setting("TELEGRAM_CHANNEL_ID"),
        fake_shafa=args.fake_shafa,
    )  # fmt: skip
    server = web.make_server(app, args.port)
    url = f"http://{web.HOST}:{app.port}/"
    print(f"Екран перевірки: {url}\nЗупинити — Ctrl+C")
    if not args.fake_shafa and app.start_bot() == "bot_started":  # бот — разом з екраном
        print("Бот запускається окремим процесом (журнал — bot-run.log у теці даних)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
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
        f"Чекають 7 днів: {plan.young} · чекають відповіді ШІ: {plan.pending} · "
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


def run_fill(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
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
        results = [fill.fill(conn, client, p) for p in planned]
    finally:
        conn.close()
    print(format_filled(results, skipped))
    return 0


def run_bot(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from shafa.bot import run
    from shafa.bot.core import Bot
    from shafa.bot.telegram import Telegram
    from shafa.robot import lock

    root = _root(parser, args)
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
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
        settings = dataclasses.replace(settings, discussion_id=group)
        print(f"Бот @{name}: права в каналі є, коментарі — лише вона й бот")
        if args.check:
            return 0
        run.set_status(conn, started=True)
        bot = Bot(conn, tg, settings)
        bot.recover()
        print("Бот працює. Зупинити — Ctrl+C")
        run.poll(bot, tg, conn, stop=lambda: False)
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
    if args.command == "robot":
        return run_robot(parser, args)
    if args.command == "bot":
        return run_bot(parser, args)
    if args.command == "app":
        from shafa import app

        opener = (lambda url: None) if args.no_browser else webbrowser.open
        return app.start(opener, args.port)
    if args.command == "setup":
        from shafa import wizard

        return wizard.run()
    if args.command == "quit":
        from shafa import app

        print("Програму закрито" if app.quit_app() else "Програма не працювала")
        return 0
    root = _root(parser, args)
    if not args.export.is_file():
        parser.error(f"Файл Експорту не знайдено: {args.export}")

    posts = read_export(args.export)
    folders = list_folders(root, config.setting("TELEGRAM_CHANNEL_ID"))
    conn = db.connect(args.db)
    try:
        result = scan(conn, posts, folders)
    finally:
        conn.close()
    print(format_result(result))
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
    posts = read_export(export)
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
