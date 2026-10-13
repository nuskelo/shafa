"""Команди: `python -m shafa scan --export … --root … --db …`."""

import argparse
import os
import sys
from pathlib import Path

from shafa import db
from shafa.export import read_export
from shafa.folders import list_folders
from shafa.scan import ScanResult, scan


def format_result(result: ScanResult) -> str:
    lines = [f"Папок: {result.folders} · нових: {result.new} · у Проблемах: {len(result.problems)}"]
    if result.problems:
        lines.append("Проблеми:")
        lines += [f"  {p.folder_name}: {p.text}" for p in result.problems]
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
