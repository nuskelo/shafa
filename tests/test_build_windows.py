"""Збірка для Windows (ops/build_windows.py) — без мережі: закріплення, ._pth, Встановити.cmd."""

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_windows", ROOT / "ops" / "build_windows.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_lock_has_every_dependency_pinned_with_hash():
    lines = [
        line for line in build.REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]  # fmt: skip
    assert lines and all(" --hash=sha256:" in line and "==" in line for line in lines)
    assert all(len(line.split("sha256:")[1]) == 64 for line in lines)


def test_lock_matches_pyproject_pins():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    locked = build.pinned(build.REQUIREMENTS.read_text(encoding="utf-8").splitlines())
    for requirement in project["dependencies"]:
        name, version = requirement.split("==")
        assert locked[name.lower()] == version, f"{name}: перезапусти ops/build_windows.py lock"


def test_python_is_312_10_with_checksum():
    url, digest = build.PYTHON_LOCK.read_text(encoding="utf-8").split()
    assert url.endswith("/3.12.10/python-3.12.10-embed-amd64.zip")
    assert len(digest) == 64


def test_pth_points_to_lib_and_site():
    assert build.PTH.splitlines() == ["python312.zip", ".", "..\\lib", "import site"]


def test_install_cmd_windows_friendly():
    data = build.INSTALL_CMD.encode("utf-8")
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert "chcp 65001" in build.INSTALL_CMD
    assert '"%~dp0app\\python\\python.exe" -m shafa.install "%~dp0app"' in build.INSTALL_CMD


def test_requirement_lines_from_report():
    report = {"install": [
        {"metadata": {"name": "shafa", "version": "0.1.0"}, "download_info": {}},
        {"metadata": {"name": "Pillow", "version": "12.3.0"},
         "download_info": {"archive_info": {"hashes": {"sha256": "ab" * 32}}}},
    ]}  # fmt: skip
    assert build.requirement_lines(report) == [f"Pillow==12.3.0 --hash=sha256:{'ab' * 32}"]


def test_package_has_no_secrets_or_her_data():
    files = [p.relative_to(ROOT).as_posix() for p in build.tracked_package_files()]
    assert files and all(f.startswith("src/shafa/") for f in files)
    assert not [f for f in files if f.endswith((".env", ".sqlite", ".jpg", ".jpeg", "result.json"))]


def test_icon_is_drawn(tmp_path):
    from PIL import Image

    build.draw_icon(tmp_path / "shafa.ico")
    with Image.open(tmp_path / "shafa.ico") as icon:
        assert icon.format == "ICO" and (256, 256) in icon.info["sizes"]
