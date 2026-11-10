"""OPS-005: файли розгортання сервера (`ops/server/`) і `docs/install-server.md`.

Без сервера, мережі й sudo: шаблони рендеряться в тимчасову теку (`install.sh --render`),
нічна копія — на тимчасовій базі, примусова команда ключа обміну перевіряється справжнім розбирачем
аргументів. `shellcheck` і `systemd-analyze` — лише якщо є на машині."""

import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shafa.__main__ import build_parser
from shafa.bot import outbox

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "ops" / "server"
DOC = ROOT / "docs" / "install-server.md"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None, reason="скрипти сервера — для Linux"
)

PATHS = {
    "OPS_USER": "ubuntu",
    "OPS_REPO": "/home/ubuntu/shafa",
    "OPS_DATA": "/home/ubuntu/shafa-data",
}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOpenSshTestKeyOnlyForTestsNoSecrets shafa-exchange-mac"


def sh(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, **PATHS, **(env or {})}
    return subprocess.run(
        ["sh", *args], capture_output=True, text=True, env=full, check=False, timeout=60
    )


@pytest.fixture
def rendered(tmp_path: Path) -> Path:
    out = tmp_path / "units"
    done = sh(str(SERVER / "install.sh"), "--render", str(out))
    assert done.returncode == 0, done.stderr
    return out


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def exec_args(command: str) -> list[str]:
    """Частина команди після `-m shafa` (аргументи для справжнього розбирача)."""
    parts = shlex.split(command)
    return parts[parts.index("shafa") + 1 :]


# --- шаблони служб ---------------------------------------------------------------------------


def test_templates_render_without_leftover_placeholders(rendered: Path):
    names = {p.name for p in rendered.iterdir()}
    assert names == {
        "shafa-bot.service",
        "shafa-backup.service",
        "shafa-backup.timer",
        "shafa-cli",
    }
    for path in rendered.iterdir():
        assert not re.search(r"@[A-Z]+@", read(path)), path.name


def test_bot_service_matches_ticket(rendered: Path):
    text = read(rendered / "shafa-bot.service")
    for line in (
        "User=ubuntu",
        "Environment=SHAFA_DATA_DIR=/home/ubuntu/shafa-data",
        "Restart=always",
        "RestartSec=10",
        "MemoryMax=400M",
    ):
        assert line in text.splitlines(), line
    # секретів і ключів у юніті немає: усе — у .env з правами 600
    assert not re.search(r"TOKEN|API_KEY|INTEGRATION|SECRET", text.replace("`.env`", ""))
    # служба сама не вмикається до переходу: install.sh не робить `enable shafa-bot`
    assert not re.search(r"enable[^\n]*shafa-bot", read(SERVER / "install.sh"))


def test_bot_service_command_is_valid_server_mode(rendered: Path):
    line = next(
        item for item in read(rendered / "shafa-bot.service").splitlines()
        if item.startswith("ExecStart=")
    )  # fmt: skip
    assert line.startswith("ExecStart=/home/ubuntu/shafa/.venv/bin/python -m shafa bot ")
    args = build_parser().parse_args(exec_args(line.removeprefix("ExecStart=")))
    assert args.command == "bot"
    assert args.server is True  # без сайту, звіту й тривог (OPS-003)
    assert args.db == Path("/home/ubuntu/shafa-data/shafa.sqlite")
    assert not args.check


def test_backup_timer_and_service(rendered: Path):
    timer = read(rendered / "shafa-backup.timer")
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer
    service = read(rendered / "shafa-backup.service")
    assert "Type=oneshot" in service
    assert (
        "ExecStart=/bin/sh /home/ubuntu/shafa/ops/server/backup.sh "
        "/home/ubuntu/shafa-data/shafa.sqlite /home/ubuntu/shafa-data/backups"
    ) in service


def test_cli_helper_sets_data_dir(rendered: Path):
    text = read(rendered / "shafa-cli")
    assert "SHAFA_DATA_DIR=/home/ubuntu/shafa-data" in text
    assert 'exec /home/ubuntu/shafa/.venv/bin/python -m shafa "$@"' in text


def test_install_refuses_unsafe_paths(tmp_path: Path):
    """Шлях з пробілом чи лапкою зламав би юніт і рядок authorized_keys — відмова до запису."""
    for bad in ("/home/u s/shafa", '/home/u"x', "/home/u&x", "відносний/шлях", "/home/ю/shafa"):
        done = sh(
            str(SERVER / "install.sh"), "--render", str(tmp_path / "out"), env={"OPS_REPO": bad}
        )
        assert done.returncode != 0, bad
        assert not (tmp_path / "out").exists(), bad


def test_install_unknown_option_fails():
    assert sh(str(SERVER / "install.sh"), "--wat").returncode != 0
    assert sh(str(SERVER / "install.sh"), "--render").returncode != 0


# --- ключ обміну -----------------------------------------------------------------------------


def add_key(tmp_path: Path, content: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    pub = tmp_path / "exchange.pub"
    pub.write_text(content, encoding="utf-8")
    target = tmp_path / "home" / ".ssh" / "authorized_keys"
    done = sh(
        str(SERVER / "install.sh"), "--add-key", str(pub),
        env={"OPS_AUTHORIZED_KEYS": str(target)},
    )  # fmt: skip
    return done, target


def test_add_key_writes_forced_command_that_only_serves_outbox(tmp_path: Path):
    done, target = add_key(tmp_path, KEY + "\n")
    assert done.returncode == 0, done.stderr
    line = read(target).strip()
    assert line.count("\n") == 0
    match = re.fullmatch(r'restrict,command="([^"]+)" (ssh-ed25519 \S+ \S+)', line)
    assert match, line
    assert match.group(2) == KEY
    command = match.group(1)
    assert command.startswith("env SHAFA_DATA_DIR=/home/ubuntu/shafa-data ")
    assert "/home/ubuntu/shafa/.venv/bin/python -m shafa outbox --ssh" in command
    args = build_parser().parse_args(exec_args(command))
    assert args.command == "outbox" and args.ssh is True
    assert args.db == Path("/home/ubuntu/shafa-data/shafa.sqlite")
    # а дозволені дії — лише «give N» / «ack N» (це вже перевіряє сам outbox)
    assert outbox.parse_ssh("give 5") == ("give", 5)
    with pytest.raises(outbox.OutboxError):
        outbox.parse_ssh("bash -c id")
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_add_key_is_idempotent_and_keeps_other_keys(tmp_path: Path):
    target = tmp_path / "home" / ".ssh" / "authorized_keys"
    target.parent.mkdir(parents=True)
    target.write_text("ssh-ed25519 AAAAOWNERKEY owner@mac\n", encoding="utf-8")
    for _ in range(2):
        done, _ = add_key(tmp_path, KEY)
        assert done.returncode == 0, done.stderr
    lines = read(target).splitlines()
    assert lines[0] == "ssh-ed25519 AAAAOWNERKEY owner@mac"  # ключ Власника не чіпаємо
    assert len(lines) == 2


@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n",  # приватний
        KEY + "\n" + KEY.replace("Test", "Else") + "\n",  # два ключі
        "ssh-rsa AAAAB3NzaC1yc2E x\n",  # не ed25519
        "",  # порожній
        KEY + "|x\n",  # символ, що ламає підстановку
    ],
)
def test_add_key_rejects_bad_files(tmp_path: Path, content: str):
    done, target = add_key(tmp_path, content)
    assert done.returncode != 0
    assert not target.exists()


# --- нічна копія -----------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="потрібен CLI sqlite3")
class TestBackup:
    script = str(SERVER / "backup.sh")

    @staticmethod
    def make_db(path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

    def test_copy_is_valid_and_keeps_seven(self, tmp_path: Path):
        base, dest = tmp_path / "shafa.sqlite", tmp_path / "backups"
        self.make_db(base)
        dest.mkdir()
        for day in range(1, 10):
            (dest / f"shafa-2026-01-0{day}.sqlite").write_bytes(b"old")
        done = sh(self.script, str(base), str(dest))
        assert done.returncode == 0, done.stderr
        names = sorted(p.name for p in dest.iterdir())
        assert len(names) == 7
        assert names[0] == "shafa-2026-01-04.sqlite"  # найстаріші 01–03 прибрано
        fresh = [n for n in names if not n.startswith("shafa-2026-01-")]
        assert len(fresh) == 1
        copy = sqlite3.connect(dest / fresh[0])
        assert copy.execute("SELECT x FROM t").fetchall() == [(42,)]
        copy.close()
        assert oct((dest / fresh[0]).stat().st_mode & 0o777) == "0o600"
        # повтор того ж дня замінює копію, не множить
        assert sh(self.script, str(base), str(dest)).returncode == 0
        assert len(list(dest.iterdir())) == 7

    def test_broken_base_does_not_replace_copies(self, tmp_path: Path):
        base, dest = tmp_path / "shafa.sqlite", tmp_path / "backups"
        base.write_bytes(b"this is not a database" * 100)
        dest.mkdir()
        old = dest / "shafa-2026-01-01.sqlite"
        old.write_bytes(b"old")
        done = sh(self.script, str(base), str(dest))
        assert done.returncode != 0
        assert [p.name for p in dest.iterdir()] == [old.name]  # ні нової копії, ні .tmp
        assert old.read_bytes() == b"old"

    def test_missing_base_and_bad_usage(self, tmp_path: Path):
        assert sh(self.script, str(tmp_path / "немає.sqlite"), str(tmp_path / "b")).returncode == 1
        assert sh(self.script).returncode == 2


# --- статичні перевірки ----------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="немає shellcheck")
def test_shellcheck_clean():
    files = [SERVER / "install.sh", SERVER / "backup.sh", SERVER / "shafa-cli.in"]
    done = subprocess.run(
        ["shellcheck", "-s", "sh", *map(str, files)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout[-1500:]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="немає systemd-analyze")
def test_systemd_analyze_finds_no_directive_errors(rendered: Path, tmp_path: Path):
    # юніти посилаються на venv, якого тут немає: підставляємо порожню заглушку, щоб `verify`
    # не скаржився на відсутній файл, і дивимося лише на помилки самих директив
    fake = tmp_path / "shafa"
    (fake / ".venv" / "bin").mkdir(parents=True)
    (fake / ".venv" / "bin" / "python").symlink_to(Path(sys.executable))
    (fake / "ops" / "server").mkdir(parents=True)
    out = tmp_path / "verify"
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    env = {"OPS_USER": me, "OPS_REPO": str(fake), "OPS_DATA": str(tmp_path / "data")}
    assert sh(str(SERVER / "install.sh"), "--render", str(out), env=env).returncode == 0
    done = subprocess.run(
        ["systemd-analyze", "verify", *map(str, out.glob("*.service")), *map(str, out.glob("*.timer"))],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    bad = re.compile(r"Unknown (key|section)|Invalid|Failed to parse|Missing", re.IGNORECASE)
    problems = [line for line in (done.stdout + done.stderr).splitlines() if bad.search(line)]
    assert not problems, problems


# --- документ ---------------------------------------------------------------------------------


def test_doc_commands_exist_in_the_program():
    """Кожна команда `shafa-cli …` / `-m shafa …` з документа — справжня підкоманда, а ті, що
    ми даємо Власнику з прапорами, розбираються справжнім розбирачем."""
    doc = read(DOC)
    parser = build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    used = set(re.findall(r"(?:shafa-cli|-m shafa) ([a-z][a-z-]+)", doc))
    assert {"bot", "bot-pack", "bot-unpack", "quit", "app"} <= used
    assert used <= commands, used - commands
    checked = {
        "bot --db ~/shafa-data/shafa.sqlite --check": ["bot", "--db", "x", "--check"],
        "bot-unpack --db ~/shafa-data/shafa.sqlite --zip ~/bot-pack.zip": [
            "bot-unpack", "--db", "x", "--zip", "y",
        ],
        'bot-pack --db "$SHAFA_DATA_DIR/shafa.sqlite" --out ~/bot-pack.zip': [
            "bot-pack", "--db", "x", "--out", "y",
        ],
        "app --no-browser --port 8766": ["app", "--no-browser", "--port", "8766"],
    }  # fmt: skip
    for text, argv in checked.items():
        assert text in doc, text
        parser.parse_args(argv)


def test_doc_mentions_every_script_and_unit():
    doc = read(DOC)
    for name in ("install.sh", "--add-key", "shafa-bot", "shafa-backup.timer", "shafa-cli"):
        assert name in doc, name
    for must in ("Europe/Kyiv", "swap", "chmod 600", "journalctl -u shafa-bot -n 50", "--ff-only"):
        assert must in doc, must
    # у документі немає справжніх секретів: лише назви змінних
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", doc)  # токен бота
    assert "sk-ant-" not in doc and "BEGIN OPENSSH PRIVATE KEY" not in doc
