"""KLB-003: команди робота Клубку — перевірка аргументів до браузера й замка."""

import pytest

from shafa.__main__ import main
from shafa.robot import lock


@pytest.fixture(autouse=True)
def no_lock(monkeypatch):
    def forbidden(*_a, **_kw):
        raise AssertionError("до браузера й замка не має дійти")

    monkeypatch.setattr(lock, "hold", forbidden)


def fails(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        main(argv)
    assert stop.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize("limit", [None, "0", "31"])
def test_publish_needs_limit_1_to_30(conn, root, tmp_path, capsys, limit):
    argv = ["robot", "--klubok-publish", "--db", str(tmp_path / "база.sqlite"), "--root", str(root)]
    if limit is not None:
        argv += ["--limit", limit]
    assert "--limit" in fails(argv, capsys)


def test_publish_one_post_only(conn, root, tmp_path, capsys):
    argv = ["robot", "--klubok-publish", "--db", str(tmp_path / "база.sqlite"), "--root",
            str(root), "--limit", "1", "--post", "1", "--post", "2"]  # fmt: skip
    assert "--post" in fails(argv, capsys)


def test_human_click_only_with_publish(capsys):
    assert "--human-click" in fails(["robot", "--klubok-snapshot", "--human-click"], capsys)


def test_snapshot_needs_db(tmp_path, capsys):
    argv = ["robot", "--klubok-snapshot", "--db", str(tmp_path / "немає.sqlite")]
    assert "Базу не знайдено" in fails(argv, capsys)


def test_modes_are_exclusive(capsys):
    fails(["robot", "--klubok-publish", "--klubok-snapshot"], capsys)
