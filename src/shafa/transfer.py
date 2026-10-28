"""Перенесення пілоту з одного комп'ютера на інший (OPS-001): `transfer-pack` і `transfer-unpack`.

`pack` (Mac Власника) складає один zip: знімок бази, фото старих постів (`borh/`), фото бота з
Головної папки, json-стани й `manifest.json`. **Секретів у zip немає ніколи**: ні `.env`, ні профілю
робота (вхід на Шафу — її), ні замків, ні кешів, які відтворюються. З `.env` у manifest потрапляють
лише несекретні `SHAFA_*` і `TELEGRAM_*_ID`.

`unpack` (її Windows) нічого не видаляє: стара база й json-стани перейменовуються з позначкою
`.до-перенесення-<час>`, наявна тека з тим самим номером не перезаписується (нова —
`<номер>-перенос`). Спочатку перевіряється база з архіву (хеш, міграції, цілісність, лічильники),
потім розпаковуються фото в нові імена, і лише тоді база й стани стають на місце; помилка на
півдорозі прибирає лише те, що створили ми. Секрети в `.env` не чіпаються.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from shafa import app, config, db, events, folders
from shafa.bot import run as bot_run
from shafa.robot import lock

FORMAT = 1
MANIFEST = "manifest.json"
DATA, ROOT = "data", "root"
DB_NAME = app.DB_NAME
ASIDE = ".до-перенесення-"  # `shafa.sqlite.до-перенесення-<час>`
COPY = "-перенос"  # `<номер>-перенос`
TMP = ".перенос-tmp"

# json-стани теки даних (інше — журнали, кеші й секрети — не переноситься)
STATE_FILES = (
    "alerts-last.json",
    "report-last.json",
    "post-changes.json",
    "photos-purged.json",
    "presence-leftovers.json",
    "rehearse-last.json",
    "health-last.json",
)
DATA_DIRS = (folders.OLD_DIR, "duplicates")  # фото старих постів і знайдені дублі
SIDE_SUFFIXES = ("-journal", "-wal", "-shm")
STORED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}  # не стискаються

LOCKS = (("програма", app.LOCK_NAME), ("бот", bot_run.LOCK_NAME), ("робот", "robot.lock"))
SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY", "COOKIE", "SESSION")
SETTING_NAME = re.compile(r"^(SHAFA_[A-Z0-9_]+|TELEGRAM_[A-Z0-9_]*_ID)$")
NOT_TRANSFERRED = ("SHAFA_ROOT_DIR", "SHAFA_DATA_DIR", "SHAFA_TESTING", "SHAFA_LEGACY_ENV")
STATUS_LABELS = {
    "published": "опубліковано",
    "approved": "схвалено",
    "review": "на перевірці",
    "new": "нових",
    "problem": "проблем",
    "publishing": "публікується",
    "archived": "в архіві",
}


class TransferError(Exception):
    """Перенесення не відбулось. Текст — для людини, без секретів."""


def transferable(name: str, value: str) -> bool:
    """Чи можна цей рядок `.env` везти в zip і назад: назва `SHAFA_*`/`TELEGRAM_*_ID`, не секрет,
    не шлях її комп'ютера, значення в один рядок."""
    if not SETTING_NAME.match(name) or name in NOT_TRANSFERRED:
        return False
    if any(word in name for word in SECRET_WORDS):
        return False
    return not any(char in value for char in "\r\n\0")


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _running(data_dir: Path) -> list[str]:
    return [label for label, name in LOCKS if lock.busy(data_dir / name) is not None]


def _refuse_if_running(data_dir: Path) -> None:
    running = _running(data_dir)
    if running:
        raise TransferError(
            f"Спершу «Вийти»: зараз працює {', '.join(running)}. Закрий програму (кнопка «Вийти» "
            "на екрані або `python -m shafa quit`), дочекайся кінця й повтори."
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall()
    return {row[0]: row[1] for row in sorted((r[0], r[1]) for r in rows)}


def _arcname(*parts: str) -> str:
    return unicodedata.normalize("NFC", "/".join(parts))  # Mac віддає ім'я розкладеним (NFD)


def _walk(base: Path, prefix: str, skip: frozenset[Path]) -> Iterator[tuple[str, Path]]:
    """Усе під `base`: (ім'я в архіві, шлях). Символьні посилання й службові `.DS_Store` — ні."""
    for current, dirs, names in os.walk(base, followlinks=False):
        dirs.sort()
        here = Path(current)
        for name in dirs[:]:
            if (here / name).is_symlink():
                dirs.remove(name)
        for name in dirs + sorted(names):
            path = here / name
            if path.is_symlink() or name.startswith("._") or name == ".DS_Store" or path in skip:
                continue
            yield _arcname(prefix, *path.relative_to(base).parts), path


# --- pack ---


@dataclass
class Packed:
    out: Path
    size: int
    manifest: dict

    def text(self) -> str:
        files = self.manifest["files"]
        parts = [
            f"{STATUS_LABELS.get(status, status)} {count}"
            for status, count in self.manifest["statuses"].items()
        ]
        return (
            f"Готово: {self.out} ({self.size / 1_048_576:.1f} МБ)\n"
            f"Картки: {', '.join(parts) or 'немає'}.\n"
            f"Файлів: {files['count']} ({files['bytes'] / 1_048_576:.1f} МБ), тек фото: "
            f"{self.manifest['photo_dirs']}.\n"
            "Секретів у архіві немає: токени, ключі й вхід на Шафу їй не їдуть."
        )


def _snapshot(source: Path, target: Path) -> None:
    """Знімок бази через backup API (не копія файлу) і `integrity_check`."""
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if result != "ok":
        raise TransferError(f"База пошкоджена (integrity_check: {result}) — переносити нічого.")


def _write(zf: zipfile.ZipFile, arcname: str, path: Path) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname, strict_timestamps=False)
    stored = path.suffix.lower() in STORED
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with open(path, "rb") as source, zf.open(info, "w", force_zip64=True) as target:
        shutil.copyfileobj(source, target, 1 << 20)


def pack(out: Path) -> Packed:
    data_dir, root = config.data_dir(), app.root_dir()
    _refuse_if_running(data_dir)
    db_file = data_dir / DB_NAME
    if not db_file.is_file():
        raise TransferError(f"Немає бази {db_file} — нічого переносити.")
    if out.exists():
        raise TransferError(f"{out} уже є — видали його або вкажи інше ім'я (нічого не затираю).")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    snapshot = out.with_name(out.name + ".db.part")
    try:
        _snapshot(db_file, snapshot)
        conn = sqlite3.connect(snapshot)
        try:
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
            statuses = _counts(conn)
        finally:
            conn.close()
        entries: list[tuple[str, Path]] = [(_arcname(DATA, DB_NAME), snapshot)]
        for name in STATE_FILES:
            if (data_dir / name).is_file():
                entries.append((_arcname(DATA, name), data_dir / name))
        skip = frozenset({part, snapshot, out})
        entries += _tree(data_dir, DATA, DATA_DIRS, skip)
        if root.is_dir():
            entries += _tree(root, ROOT, None, skip)
        dirs = sorted({a for a, p in entries if p.is_dir()})
        files = [(a, p) for a, p in entries if p.is_file() and a != _arcname(DATA, DB_NAME)]
        manifest = {
            "format": FORMAT,
            "app_version": events.version(),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "schema": schema,
            "statuses": statuses,
            "files": {"count": len(files), "bytes": sum(p.stat().st_size for _, p in files)},
            "photo_dirs": sum(1 for a in dirs if _is_unit(a)),
            "db_sha256": _sha256(snapshot),
            "settings": {
                key: value
                for key, value in sorted(config.read_env_file(config.env_file()).items())
                if transferable(key, value)
            },
        }
        with zipfile.ZipFile(part, "w", allowZip64=True) as zf:
            for arc in dirs:
                zf.mkdir(arc)
            _write(zf, _arcname(DATA, DB_NAME), snapshot)
            for arc, path in files:
                _write(zf, arc, path)
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
        snapshot.unlink(missing_ok=True)
    return Packed(out, out.stat().st_size, manifest)


def _tree(base: Path, prefix: str, only: tuple[str, ...] | None, skip: frozenset[Path]) -> list:
    """Вміст `base` (усе, або лише названі підтеки) для архіву."""
    entries: list[tuple[str, Path]] = []
    if only is None:
        return list(_walk(base, prefix, skip))
    for name in only:
        folder = base / name
        if folder.is_dir() and not folder.is_symlink():
            entries.append((_arcname(prefix, name), folder))
            entries += _walk(folder, _arcname(prefix, name), skip)
    return entries


def _is_unit(arcname: str) -> bool:
    """Тека-одиниця фото: `root/<н>`, `root/Опубліковано/<н>`, `data/borh/<н>`."""
    parts = arcname.split("/")
    if parts[0] == ROOT:
        return len(parts) == (3 if parts[1:2] == [folders.PUBLISHED_DIR] else 2)
    return parts[0] == DATA and len(parts) == 3 and parts[1] == folders.OLD_DIR


# --- unpack ---


@dataclass
class Unpacked:
    statuses: dict[str, int]
    photo_dirs: int
    files: int
    root: Path
    side_by_side: list[str] = field(default_factory=list)  # `<номер>-перенос` і подібне
    aside: list[str] = field(default_factory=list)  # перейменоване старе
    changed: list[tuple[str, str | None, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # Картки без теки з фото

    def text(self) -> str:
        parts = [f"{STATUS_LABELS.get(s, s)} {n}" for s, n in self.statuses.items()]
        lines = [
            (
                f"Перенесено: {', '.join(parts) or 'карток немає'}. Фото: {self.photo_dirs} тек "
                f"({self.files} файлів). Головна папка: {self.root}."
            )
        ]
        if self.aside:
            lines.append("Стара база й стани лишились поруч (нічого не видалено):")
            lines += [f"  {name}" for name in self.aside]
        if self.side_by_side:
            lines.append("Теки, що вже були, не перезаписано — перенесене лежить поруч:")
            lines += [f"  {name}" for name in self.side_by_side]
        if self.changed:
            lines.append("Змінено налаштувань (секрети не чіпались):")
            lines += [f"  {key}: {old if old is not None else '(не було)'} → {new}"
                      for key, old, new in self.changed]  # fmt: skip
        else:
            lines.append("Змінено налаштувань: жодного.")
        if self.missing:
            shown = ", ".join(self.missing[:20]) + (" …" if len(self.missing) > 20 else "")
            lines.append(f"УВАГА: {len(self.missing)} Карток без теки з фото: {shown}")
        lines.append(
            "Перевір: бот у `.env` той самий, що на Mac (інакше два боти), а на Mac програма "
            "закрита й більше не запускається з цим `.env` (той самий токен = конфлікт)."
        )
        return "\n".join(lines)


def _safe(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    bad = path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
    if bad or path.parts[0] not in (DATA, ROOT, MANIFEST):
        raise TransferError(f"У архіві підозрілий шлях «{name}» — це не наш архів.")
    return path


def _unit(path: PurePosixPath) -> tuple[str, ...] | None:
    """Одиниця перенесення: те, що або кладеться на місце як є, або (якщо вже є) поруч."""
    parts = path.parts
    if parts[0] == ROOT:
        depth = 3 if parts[1:2] == (folders.PUBLISHED_DIR,) else 2
    elif parts[0] == DATA and len(parts) > 1 and parts[1] in DATA_DIRS:
        depth = 3
    else:
        depth = 2
    return parts[:depth] if len(parts) >= depth else None


def _free(path: Path) -> Path:
    candidate = path.with_name(path.name + COPY)
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}{COPY}{number}")
        number += 1
    return candidate


def _read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
        broken = [
            not isinstance(manifest["schema"], int),
            not isinstance(manifest["files"]["count"], int),
            not isinstance(manifest["db_sha256"], str),
            not isinstance(manifest["statuses"], dict),
        ]
    except KeyError as error:
        raise TransferError("У архіві немає або зіпсований manifest.json.") from error
    except (ValueError, TypeError) as error:
        raise TransferError("manifest.json в архіві не читається.") from error
    if manifest["format"] != FORMAT:
        raise TransferError("Архів зроблено іншою версією перенесення — онови програму.")
    if any(broken):
        raise TransferError("manifest.json в архіві зіпсований.")
    return manifest


def _verify_db(path: Path, manifest: dict) -> None:
    if _sha256(path) != manifest["db_sha256"]:
        raise TransferError("База в архіві не збігається з manifest (архів пошкоджено).")
    conn = db.connect(path)  # міграції до схеми цієї програми
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        statuses = _counts(conn)
    finally:
        conn.close()
    if result != "ok":
        raise TransferError(f"База з архіву пошкоджена (integrity_check: {result}).")
    if statuses != manifest["statuses"]:
        raise TransferError(f"Лічильники Карток не збіглись: {statuses} ≠ {manifest['statuses']}.")


def _put_aside(path: Path, stamp: str) -> str | None:
    if not path.exists():
        return None
    target = path.with_name(path.name + ASIDE + stamp)
    number = 2
    while target.exists():
        target = path.with_name(f"{path.name}{ASIDE}{stamp}-{number}")
        number += 1
    path.rename(target)
    return target.name


def _extract(zf: zipfile.ZipFile, member: zipfile.ZipInfo, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as source, open(target, "wb") as dest:
        shutil.copyfileobj(source, dest, 1 << 20)


def unpack(zip_path: Path, log: Callable[[str], None] = lambda text: None) -> Unpacked:
    data_dir, root = config.data_dir(), app.root_dir()
    _refuse_if_running(data_dir)
    if not zip_path.is_file():
        raise TransferError(f"Архів не знайдено: {zip_path}")
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as error:
        raise TransferError(f"{zip_path} — не zip-архів.") from error
    with archive as zf:
        manifest = _read_manifest(zf)
        if manifest["schema"] > db.SCHEMA_VERSION:
            raise TransferError(
                f"Архів зроблено новішою програмою (схема бази {manifest['schema']}, ця — "
                f"{db.SCHEMA_VERSION}): спершу онови програму."
            )
        bad = zf.testzip()
        if bad:
            raise TransferError(f"Архів пошкоджено (не збігається вміст «{bad}»).")
        members = [(_safe(info.filename), info) for info in zf.infolist()]
        data_dir.mkdir(parents=True, exist_ok=True)
        staged_db = data_dir / (DB_NAME + TMP)
        staged_db.unlink(missing_ok=True)
        created: list[Path] = []  # лише те, що створили ми — для відкату
        try:
            db_member = next(
                (info for path, info in members if str(path) == _arcname(DATA, DB_NAME)), None
            )
            if db_member is None:
                raise TransferError("В архіві немає data/shafa.sqlite.")
            _extract(zf, db_member, staged_db)
            _verify_db(staged_db, manifest)
            log("База з архіву перевірена.")
            placed = _place_files(zf, members, data_dir, root, created)
            if placed.files != manifest["files"]["count"]:
                raise TransferError(
                    f"Файлів розпаковано {placed.files}, а в manifest {manifest['files']['count']}."
                )
        except BaseException:
            staged_db.unlink(missing_ok=True)
            for path in reversed(created):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            raise
    return _finish(manifest, placed, staged_db, data_dir, root)


@dataclass
class _Placed:
    files: int = 0
    units: int = 0
    side_by_side: list[str] = field(default_factory=list)
    pending: list[tuple[Path, Path]] = field(default_factory=list)  # (тимчасовий, остаточний)


def _place_files(zf, members, data_dir: Path, root: Path, created: list[Path]) -> _Placed:
    """Розпакувати все, крім бази. Теки — у нові імена; окремі файли — під тимчасовим іменем."""
    placed = _Placed()
    destinations: dict[tuple[str, ...], Path] = {}
    for path, info in members:
        if path.parts[0] == MANIFEST or str(path) == _arcname(DATA, DB_NAME):
            continue
        base = data_dir if path.parts[0] == DATA else root
        rest = PurePosixPath(*path.parts[1:])
        unit = _unit(path)
        if unit is None:  # контейнер: `root/Опубліковано/`, `data/borh/`
            if not (base / rest).exists():
                (base / rest).mkdir(parents=True)
                created.append(base / rest)
            continue
        relative = PurePosixPath(*unit[1:])
        whole = base / relative
        is_file_unit = len(unit) == len(path.parts) and not info.is_dir()
        if is_file_unit:  # json-стан чи файл дубля: старий піде вбік у кінці
            temp = whole.with_name(whole.name + TMP)
            _extract(zf, info, temp)
            created.append(temp)
            placed.pending.append((temp, whole))
            placed.files += 1
            continue
        dest = destinations.get(unit)
        if dest is None:
            dest = whole if not whole.exists() else _free(whole)
            if dest != whole:
                placed.side_by_side.append(str(dest))
            dest.mkdir(parents=True)
            created.append(dest)
            destinations[unit] = dest
            placed.units += _is_unit("/".join(unit))
        inner = PurePosixPath(*path.parts[len(unit) :])
        if info.is_dir():
            (dest / inner).mkdir(parents=True, exist_ok=True)
        else:
            _extract(zf, info, dest / inner)
            placed.files += 1
    return placed


def _finish(
    manifest: dict, placed: _Placed, staged_db: Path, data_dir: Path, root: Path
) -> Unpacked:
    """Усе розпаковано й перевірено: тепер база й стани стають на місце (старе — вбік)."""
    stamp = _stamp()
    aside: list[str] = []
    final_db = data_dir / DB_NAME
    for suffix in ("", *SIDE_SUFFIXES):  # `-wal`/`-journal` старої бази не мають лишитись при новій
        name = _put_aside(final_db.with_name(DB_NAME + suffix), stamp)
        if name:
            aside.append(name)
    staged_db.rename(final_db)
    for temp, whole in placed.pending:
        name = _put_aside(whole, stamp)
        if name:
            aside.append(name)
        temp.rename(whole)
    changed = _update_env(manifest.get("settings", {}))
    conn = db.connect(final_db)
    try:
        statuses = _counts(conn)
        missing = _missing_photos(conn, root)
    finally:
        conn.close()
    if statuses != manifest["statuses"]:
        raise TransferError(f"Після перенесення лічильники інші: {statuses}.")
    return Unpacked(
        statuses, placed.units, placed.files, root, placed.side_by_side, aside, changed, missing
    )


def _update_env(settings: dict) -> list[tuple[str, str | None, str]]:
    """Дописати/оновити в `.env` лише несекретне. Секрети й `SHAFA_ROOT_DIR` — її."""
    current = config.read_env_file(config.env_file())
    new = {
        key: value
        for key, value in settings.items()
        if isinstance(value, str) and transferable(key, value) and current.get(key) != value
    }
    if new:
        config.save_settings(new)
    return [(key, current.get(key), value) for key, value in new.items()]


def _missing_photos(conn: sqlite3.Connection, root: Path) -> list[str]:
    """Картки `new`/`review`/`approved` постів бота й старих постів без теки з фото."""
    rows = conn.execute(
        "SELECT i.folder_name, p.source FROM items i LEFT JOIN posts p ON p.post_id = i.post_id "
        "WHERE i.status IN ('new', 'review', 'approved') ORDER BY i.id"
    ).fetchall()
    return [
        name
        for name, source in rows
        if (source == "bot" or folders.is_old(name))
        and not folders.item_folder(root, name).is_dir()
    ]
