import pytest

from conftest import CHANNEL_ID, make_folder
from shafa.__main__ import main


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", CHANNEL_ID)
    monkeypatch.delenv("SHAFA_ROOT_DIR", raising=False)


def test_scan_prints_summary_and_problems(tmp_path, root, export_path, capsys):
    make_folder(root, CHANNEL_ID + "10", ["1.jpg"])
    make_folder(root, "12", ["1.jpg"])
    db_path = tmp_path / "база.sqlite"

    code = main(["scan", "--export", str(export_path), "--root", str(root), "--db", str(db_path)])

    assert code == 0
    assert capsys.readouterr().out == (
        "Папок: 2 · нових: 1 · у Проблемах: 1\nПроблеми:\n  12: У поста 12 немає тексту\n"
    )


def test_root_from_env(tmp_path, root, export_path, monkeypatch, capsys):
    make_folder(root, "10", ["1.jpg"])
    monkeypatch.setenv("SHAFA_ROOT_DIR", str(root))

    main(["scan", "--export", str(export_path), "--db", str(tmp_path / "б.sqlite")])

    assert capsys.readouterr().out == "Папок: 1 · нових: 1 · у Проблемах: 0\n"


def test_root_required(tmp_path, export_path, capsys):
    with pytest.raises(SystemExit):
        main(["scan", "--export", str(export_path), "--db", str(tmp_path / "б.sqlite")])
    assert "SHAFA_ROOT_DIR" in capsys.readouterr().err
