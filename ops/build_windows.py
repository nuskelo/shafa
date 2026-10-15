"""Збірка «Шафа-помічник» для Windows (WIN-001): вбудований Python 3.12.10 + бібліотеки + програма.

  python ops/build_windows.py lock   — закріпити залежності з контрольними сумами (TD-005) і суму
                                        архіву Python; результат — у ops/windows/, у git.
  python ops/build_windows.py build  — зібрати dist/shafa-windows/ і dist/shafa-windows-<версія>.zip.

Працює на Mac і на Windows: колеса для Windows (`win_amd64`, cp312) pip бере з PyPI за контрольними
сумами (`--require-hashes`), нічого не компілюється. Програма — лише файли з git (`git ls-files`).
Секретів у збірці немає: `.env` лежить у теці даних на ноутбуці, не в програмі.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "ops" / "windows"
REQUIREMENTS = LOCK_DIR / "requirements.txt"
PYTHON_LOCK = LOCK_DIR / "python.txt"
DIST = ROOT / "dist"
CACHE = ROOT / "build" / "cache"
PYTHON_VERSION = "3.12.10"  # остання збірка 3.12 для Windows на python.org (WIN-001 D2)
PYTHON_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
)
PLATFORM = ["--platform", "win_amd64", "--python-version", "3.12", "--implementation", "cp",
            "--only-binary=:all:"]  # fmt: skip
PTH = "python312.zip\n.\n..\\lib\nimport site\n"
INSTALL_CMD = (
    "@echo off\r\n"
    "chcp 65001 >nul\r\n"
    "rem Встановлення або оновлення «Шафа-помічник». Дані (налаштування, база) не чіпаються.\r\n"
    '"%~dp0app\\python\\python.exe" -m shafa.install "%~dp0app"\r\n'
    "echo.\r\n"
    "pause\r\n"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        with urllib.request.urlopen(url, timeout=120) as response:  # лише python.org
            target.write_bytes(response.read())
    return target


def requirement_lines(report: dict) -> list[str]:
    """Звіт `pip install --report` → рядки `назва==версія --hash=sha256:…` (без нашого пакета)."""
    lines = []
    for item in sorted(report["install"], key=lambda i: i["metadata"]["name"].lower()):
        name, version = item["metadata"]["name"], item["metadata"]["version"]
        if name.lower() == "shafa":
            continue
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        lines.append(f"{name}=={version} --hash=sha256:{digest}")
    return lines


def pinned(lines: list[str]) -> dict[str, str]:
    return {
        line.split("==")[0].lower(): line.split("==")[1].split()[0]
        for line in lines
        if "==" in line and not line.startswith("#")
    }


def lock() -> None:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "report.json"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--dry-run", "--ignore-installed",
             "--report", str(report_path), *PLATFORM, "--target", str(Path(tmp) / "t"), str(ROOT)],
            check=True,
        )  # fmt: skip
        lines = requirement_lines(json.loads(report_path.read_text(encoding="utf-8")))
    header = "# Залежності для Windows (cp312, win_amd64) з контрольними сумами — ops/build_windows.py lock\n"
    REQUIREMENTS.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    archive = download(PYTHON_URL, CACHE / Path(PYTHON_URL).name)
    PYTHON_LOCK.write_text(f"{PYTHON_URL} {sha256(archive)}\n", encoding="utf-8")
    print(f"Закріплено {len(lines)} пакетів і Python {PYTHON_VERSION}")


def version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
        check=True,
    ).stdout.strip()  # fmt: skip
    return f"{project['version']}+{commit}"


def tracked_package_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "src/shafa"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [ROOT / line for line in out.splitlines() if line]


def draw_icon(target: Path) -> None:
    """Проста власна іконка: вішак на синьому квадраті (без чужих логотипів — D7)."""
    from PIL import Image, ImageDraw

    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 248, 248), radius=48, fill=(37, 99, 235, 255))
    white, width = (255, 255, 255, 255), 14
    draw.arc((104, 40, 152, 88), start=180, end=90, fill=white, width=width)  # гачок
    draw.line((128, 88, 128, 110), fill=white, width=width)
    draw.line((128, 110, 40, 180), fill=white, width=width)  # плечики
    draw.line((128, 110, 216, 180), fill=white, width=width)
    draw.line((40, 180, 216, 180), fill=white, width=width)
    image.save(target, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def build() -> Path:
    url, digest = PYTHON_LOCK.read_text(encoding="utf-8").split()
    archive = download(url, CACHE / Path(url).name)
    if sha256(archive) != digest:
        raise SystemExit(f"Контрольна сума {archive.name} не збігається — не збираю")
    out = DIST / "shafa-windows"
    shutil.rmtree(out, ignore_errors=True)
    app = out / "app"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(app / "python")
    (app / "python" / "python312._pth").write_text(PTH, encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--require-hashes", "--no-deps",
         "--no-compile",
         *PLATFORM, "--target", str(app / "lib"), "-r", str(REQUIREMENTS)],
        check=True,
    )  # fmt: skip
    for source in tracked_package_files():
        target = app / "lib" / source.relative_to(ROOT / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    draw_icon(app / "shafa.ico")
    stamp = f"{version()} ({datetime.now(UTC):%Y-%m-%d})"
    (app / "VERSION").write_text(stamp + "\n", encoding="utf-8")
    (out / "Встановити.cmd").write_bytes(INSTALL_CMD.encode("utf-8"))
    package = DIST / f"shafa-windows-{version()}.zip"
    package.unlink(missing_ok=True)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(DIST))
    print(f"Зібрано: {package} ({package.stat().st_size // 1_000_000} МБ), версія {stamp}")
    return package


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "lock":
        lock()
    elif command == "build":
        build()
    else:
        raise SystemExit("python ops/build_windows.py lock | build")
