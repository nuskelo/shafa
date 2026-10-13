"""Команди.

`python -m shafa scan --export … --root … --db …` — зіставити папки й пости.
`python -m shafa rules --export … --post N` — поля, які заповнює код, для одного поста.
"""

import argparse
import os
import sys
from pathlib import Path

from shafa import db, rules
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m shafa")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("scan", help="зіставити папки товарів із постами Експорту")
    cmd.add_argument("--export", type=Path, required=True, help="шлях до result.json")
    cmd.add_argument(
        "--root",
        type=Path,
        default=os.environ.get("SHAFA_ROOT_DIR") or None,
        help="Головна папка (за замовчуванням SHAFA_ROOT_DIR)",
    )
    cmd.add_argument("--db", type=Path, required=True, help="файл бази SQLite")
    cmd = commands.add_parser("rules", help="поля, які заповнює код, для одного поста")
    cmd.add_argument("--export", type=Path, required=True, help="шлях до result.json")
    cmd.add_argument("--post", type=int, required=True, help="номер поста")
    return parser


def run_rules(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if not args.export.is_file():
        parser.error(f"Файл Експорту не знайдено: {args.export}")
    post = read_export(args.export).get(args.post)
    if post is None:
        parser.error(f"Поста з номером {args.post} немає в експорті")
    print(format_rules(post))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "rules":
        return run_rules(parser, args)
    if args.root is None:
        parser.error("потрібна Головна папка: --root або змінна SHAFA_ROOT_DIR")
    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"Головну папку не знайдено: {root}")
    if not args.export.is_file():
        parser.error(f"Файл Експорту не знайдено: {args.export}")

    posts = read_export(args.export)
    folders = list_folders(root, os.environ.get("TELEGRAM_CHANNEL_ID") or None)
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
