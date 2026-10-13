import pytest

from conftest import CHANNEL_ID, make_folder
from shafa import ai, config
from shafa.__main__ import format_rules, main
from shafa.export import Post
from test_ai import GOOD, FakeClient, message
from test_fill import add_item


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


def test_rules_prints_fields_for_one_post(export_path, capsys):
    code = main(["rules", "--export", str(export_path), "--post", "23"])

    assert code == 0
    assert capsys.readouterr().out == (
        "Пост 23\n"
        "Ціна: 900 → 1040\n"
        "Стан: Ідеальний\n"
        "Устілка: —\n"
        "Мембрана: —\n"
        "Опис:\n"
        "Куплені за 2000грн. Взуті раз. Ідеальний стан, без дефектів.\n"
    )


def test_rules_without_price(export_path, capsys):
    main(["rules", "--export", str(export_path), "--post", "21"])
    assert "Ціна: немає\n" in capsys.readouterr().out


def test_rules_unknown_post(export_path, capsys):
    with pytest.raises(SystemExit):
        main(["rules", "--export", str(export_path), "--post", "999"])
    assert "Поста з номером 999 немає в експорті" in capsys.readouterr().err


def test_format_rules_insole_and_membrane():
    post = Post(
        5,
        "2025-01-11T10:00:00",
        "Термо черевики Lowa з мембраною gore-tex у відмінному стані. 38 розмір. "
        "Устілка до згину на п'ятку 24,5см, від краю до краю 25,5см. Ширина устілки 9см. "
        "1400грн\n#VS_38_розмір\n\nПишіть сюди 👉 @prodavchynia_test",
    )
    assert format_rules(post).splitlines()[1:5] == [
        "Ціна: 1400 → 1610",
        "Стан: Ідеальний",
        "Устілка: 24,5 · 25 · 25,5",
        "Мембрана: Gore-Tex",
    ]


# --- fill ---


@pytest.fixture
def db_path(conn, tmp_path):
    return tmp_path / "база.sqlite"


@pytest.fixture
def no_client(monkeypatch):
    def forbidden(key):
        raise AssertionError("клієнт API не має створюватись")

    monkeypatch.setattr(ai, "make_client", forbidden)


def fill_args(db_path, root, *extra):
    return ["fill", "--db", str(db_path), "--root", str(root), *extra]


def test_fill_dry_run_sends_nothing(conn, db_path, root, no_client, capsys):
    add_item(conn, root)
    add_item(conn, root, folder="1349", post_id=1349, photos=("a.jpg",))

    code = main(fill_args(db_path, root, "--dry-run"))

    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0].startswith("1342 (пост 1342): фото 2, ≈ ")
    assert out[1].startswith("1349 (пост 1349): фото 1, ≈ ")
    assert out[2].startswith("Карток: 2 · орієнтовно ")
    assert out[2].endswith("· нічого не надіслано")
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT DISTINCT status FROM items")] == ["new"]


def test_fill_without_key_explains_and_sends_nothing(conn, db_path, root, no_client, capsys):
    add_item(conn, root)
    config.ENV_FILE.write_text("OTHER=test-secret-value\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(fill_args(db_path, root))

    captured = capsys.readouterr()
    assert "Немає ANTHROPIC_API_KEY у .env" in captured.err
    assert "test-secret-value" not in captured.err + captured.out


def test_fill_uses_key_from_env_file(conn, db_path, root, monkeypatch, capsys):
    add_item(conn, root)
    config.ENV_FILE.write_text("ANTHROPIC_API_KEY=test-key-z-faylu\n", encoding="utf-8")
    keys = []
    client = FakeClient(message({**GOOD, "photo_order": ["IMG_1.JPG", "IMG_2.JPG"]}))
    monkeypatch.setattr(ai, "make_client", lambda key: keys.append(key) or client)

    code = main(fill_args(db_path, root))

    out = capsys.readouterr().out
    assert code == 0
    assert keys == ["test-key-z-faylu"]
    assert "test-key-z-faylu" not in out
    assert out == (
        "1342: бланк готовий, жовтих полів 0 (3,60 ¢)\nБланків: 1 з 1 · вартість 3,60 ¢\n"
    )


def test_fill_reports_api_error(conn, db_path, root, monkeypatch, capsys):
    add_item(conn, root)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = FakeClient(message("не json"))
    monkeypatch.setattr(ai, "make_client", lambda key: client)

    main(fill_args(db_path, root))

    assert capsys.readouterr().out.splitlines()[0] == (
        "1342: помилка — Відповідь ШІ не читається (3,60 ¢)"
    )


@pytest.mark.parametrize("limit", ["0", "11"])
def test_fill_limit_capped(db_path, root, no_client, limit, capsys):
    with pytest.raises(SystemExit):
        main(fill_args(db_path, root, "--limit", limit, "--dry-run"))
    assert "--limit від 1 до 10" in capsys.readouterr().err


def test_fill_nothing_to_do(db_path, root, no_client, capsys):
    main(fill_args(db_path, root, "--post", "5", "--dry-run"))
    assert capsys.readouterr().out == "Немає Картки поста 5 у статусі new\n"


def test_fill_missing_db_not_created(tmp_path, root, no_client, capsys):
    path = tmp_path / "немає.sqlite"
    with pytest.raises(SystemExit):
        main(fill_args(path, root, "--dry-run"))
    assert not path.exists()
    assert "Базу не знайдено" in capsys.readouterr().err


def test_settings_from_env_file(tmp_path, root, export_path, capsys):
    make_folder(root, CHANNEL_ID + "10", ["1.jpg"])
    config.ENV_FILE.write_text(f"SHAFA_ROOT_DIR={root}\n", encoding="utf-8")

    main(["scan", "--export", str(export_path), "--db", str(tmp_path / "б.sqlite")])

    assert capsys.readouterr().out == "Папок: 1 · нових: 1 · у Проблемах: 0\n"
