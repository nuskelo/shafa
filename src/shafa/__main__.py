"""Команди.

`python -m shafa scan --export … --root … --db …` — зіставити папки й пости.
`python -m shafa rules --export … --post N` — поля, які заповнює код, для одного поста.
`python -m shafa fill --db … --root … [--post N] [--limit K] [--dry-run]` — ШІ-бланк для Карток.
`python -m shafa ui --db … --root … [--port P] [--no-browser]` — екран перевірки в браузері.

Налаштування (`SHAFA_ROOT_DIR`, `TELEGRAM_CHANNEL_ID`, `ANTHROPIC_API_KEY`) — зі змінних
середовища або з `.env` у корені проєкту.
"""

import argparse
import sys
import webbrowser
from pathlib import Path

from shafa import ai, config, db, fill, photos, review, rules, web
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
        default=fill.MAX_PER_RUN,
        help=f"скільки Карток (не більше {fill.MAX_PER_RUN})",
    )
    cmd.add_argument(
        "--dry-run", action="store_true", help="зібрати запити й порахувати, нічого не надсилати"
    )
    cmd = commands.add_parser("ui", help="екран перевірки в браузері (лише на цьому комп'ютері)")
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    _add_root(cmd)
    cmd.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="порт на 127.0.0.1")
    cmd.add_argument("--no-browser", action="store_true", help="не відкривати браузер")
    return parser


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
    app = web.App(args.db, root, cache_dir, channel_id=config.setting("TELEGRAM_CHANNEL_ID"))
    server = web.make_server(app, args.port)
    url = f"http://{web.HOST}:{app.port}/"
    print(f"Екран перевірки: {url}\nЗупинити — Ctrl+C")
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


def run_fill(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    root = _root(parser, args)
    if not 1 <= args.limit <= fill.MAX_PER_RUN:
        parser.error(f"--limit від 1 до {fill.MAX_PER_RUN}")
    if not args.db.is_file():
        parser.error(f"Базу не знайдено: {args.db}")
    client = None
    if not args.dry_run:
        try:
            client = ai.make_client(config.api_key())
        except config.ConfigError as error:
            parser.error(str(error))
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "rules":
        return run_rules(parser, args)
    if args.command == "fill":
        return run_fill(parser, args)
    if args.command == "ui":
        return run_ui(parser, args)
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


if __name__ == "__main__":
    # Консоль Windows може не мати кирилиці чи «·» — краще знак питання, ніж падіння.
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
