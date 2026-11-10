#!/bin/sh
# OPS-005: встановлення серверного бота (запускає Власник на сервері; docs/install-server.md).
#
#   sh ops/server/install.sh                     встановити: venv, пакет, теки, база, служби
#   sh ops/server/install.sh --render ТЕКА       лише записати файли служб у ТЕКУ — без sudo і
#                                                без змін системи (перевірка й тести)
#   sh ops/server/install.sh --add-key ФАЙЛ.pub  додати ключ обміну з примусовою командою
#                                                в ~/.ssh/authorized_keys
#
# Змінні (необов'язкові): OPS_USER (типово — поточний), OPS_REPO (типово — клон, де лежить скрипт),
# OPS_DATA (~/shafa-data), OPS_PYTHON (python3.12), OPS_CLI (~/shafa-cli), OPS_AUTHORIZED_KEYS.
#
# Що НЕ робить: не качає браузер Playwright (боту не потрібен), не пише секрети в `.env`,
# не вмикає й не запускає службу бота (її вмикає Власник після `.env` і переходу), не чіпає
# ssh Власника. Повторний запуск безпечний.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo_default=$(cd "$here/../.." && pwd)

USER_NAME=${OPS_USER:-$(id -un)}
REPO=${OPS_REPO:-$repo_default}
DATA=${OPS_DATA:-$HOME/shafa-data}
PYTHON=${OPS_PYTHON:-python3.12}
CLI=${OPS_CLI:-$HOME/shafa-cli}
ROOT=$DATA/root

die() {
    echo "install.sh: $*" >&2
    exit 1
}

# Шляхи потрапляють у systemd-юніти й у рядок authorized_keys: лише безпечні символи,
# без пробілів, лапок, `&`, `|`, `\`, `$` (інакше юніт чи примусова команда зламаються).
check_value() {
    case $2 in
        "" | *[!A-Za-z0-9_./-]*) die "$1 містить неприпустимі символи або порожнє: «$2» (дозволено A-Z a-z 0-9 _ . / -)" ;;
    esac
}
check_absolute() {
    check_value "$1" "$2"
    case $2 in
        /*) ;;
        *) die "$1 має бути повним шляхом: «$2»" ;;
    esac
}

check_value OPS_USER "$USER_NAME"
check_absolute OPS_REPO "$REPO"
check_absolute OPS_DATA "$DATA"
check_absolute OPS_CLI "$CLI"

# Підставити @USER@ @REPO@ @DATA@ @PUBKEY@ (останній — лише якщо задано) у шаблон.
render() {
    sed -e "s|@USER@|$USER_NAME|g" -e "s|@REPO@|$REPO|g" -e "s|@DATA@|$DATA|g" \
        -e "s|@PUBKEY@|${PUBKEY:-}|g" "$1" >"$2"
}

render_units() {
    out=$1
    mkdir -p "$out"
    render "$here/shafa-bot.service.in" "$out/shafa-bot.service"
    render "$here/shafa-backup.service.in" "$out/shafa-backup.service"
    render "$here/shafa-backup.timer.in" "$out/shafa-backup.timer"
    render "$here/shafa-cli.in" "$out/shafa-cli"
}

add_key() {
    keyfile=$1
    [ -f "$keyfile" ] || die "файлу ключа немає: $keyfile"
    lines=$(grep -c . "$keyfile" || true)
    [ "$lines" -eq 1 ] || die "у $keyfile має бути рівно один публічний ключ (знайдено рядків: $lines)"
    case $(cat "$keyfile") in
        "ssh-ed25519 "*) ;;
        *) die "очікується публічний ключ ssh-ed25519 (файл .pub, не приватний!)" ;;
    esac
    PUBKEY=$(cat "$keyfile")
    case $PUBKEY in
        *"|"* | *"&"* | *\\*) die "у ключі є недозволені символи" ;;
    esac
    target=${OPS_AUTHORIZED_KEYS:-$HOME/.ssh/authorized_keys}
    tmp=$(mktemp)
    render "$here/authorized_keys.in" "$tmp"
    line=$(cat "$tmp")
    rm -f "$tmp"
    mkdir -p "$(dirname "$target")"
    chmod 700 "$(dirname "$target")" 2>/dev/null || true
    touch "$target"
    chmod 600 "$target"
    if grep -qxF "$line" "$target"; then
        echo "Ключ уже є в $target — нічого не змінено."
    else
        printf '%s\n' "$line" >>"$target"
        echo "Ключ обміну додано в $target (лише команда outbox, без оболонки)."
    fi
}

install_all() {
    command -v "$PYTHON" >/dev/null 2>&1 ||
        die "немає $PYTHON. Потрібен Python 3.12 (Ubuntu 24.04: sudo apt install python3.12 python3.12-venv). Старіший Ubuntu — не продовжуй, напиши Архітектору."
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' ||
        die "$PYTHON — не Python 3.12 (у продакшні саме 3.12)"
    command -v sudo >/dev/null 2>&1 || die "потрібен sudo (для служб systemd)"
    command -v sqlite3 >/dev/null 2>&1 || die "немає sqlite3 (sudo apt install sqlite3) — потрібен для нічної копії"

    if [ ! -d "$REPO/.venv" ]; then
        "$PYTHON" -m venv "$REPO/.venv" ||
            die "не вдалося створити venv (sudo apt install python3.12-venv)"
    fi
    # Версія pip — як у `make setup`. Браузер Playwright не ставимо: боту він не потрібен.
    "$REPO/.venv/bin/python" -m pip install --quiet pip==26.2.1
    "$REPO/.venv/bin/python" -m pip install --quiet -e "$REPO"

    mkdir -p "$DATA" "$ROOT"
    chmod 700 "$DATA"
    if [ ! -f "$DATA/.env" ]; then
        (
            umask 077
            cat >"$DATA/.env" <<EOF
# Налаштування серверного бота. Секрети вводить Власник сам (nano), не в чат і не в git.
# Порожнє значення = «не задано». Права файлу — 600.
SHAFA_ROOT_DIR=$ROOT
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_CHANNEL_ID=
TELEGRAM_CHANNEL_ID=
TELEGRAM_SELLER_CHAT_ID=
TELEGRAM_OWNER_CHAT_ID=
SHAFA_ASK_BUTTON=
SHAFA_SITE_BUTTON=
SHAFA_AI_WAIT_DAYS=
SHAFA_SITE_WAIT_DAYS=
EOF
        )
        echo "Створено $DATA/.env (порожній, права 600) — заповни його."
    fi
    chmod 600 "$DATA/.env"

    # Порожня база зі схемою: `bot` не стартує без файлу, а `bot-unpack` дописує в неї.
    SHAFA_DATA_DIR=$DATA "$REPO/.venv/bin/python" -c \
        'import sys; from pathlib import Path; from shafa import db; db.connect(Path(sys.argv[1])).close()' \
        "$DATA/shafa.sqlite"

    stage=$(mktemp -d)
    render_units "$stage"
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "$stage/shafa-bot.service" "$stage/shafa-backup.service" \
            "$stage/shafa-backup.timer" || echo "УВАГА: systemd-analyze verify мав зауваження (див. вище)."
    fi
    install -m 755 "$stage/shafa-cli" "$CLI"
    sudo install -m 644 "$stage/shafa-bot.service" "$stage/shafa-backup.service" \
        "$stage/shafa-backup.timer" /etc/systemd/system/
    rm -rf "$stage"
    sudo systemctl daemon-reload
    sudo systemctl enable --now shafa-backup.timer

    cat <<EOF

Готово. Служба бота встановлена, але НЕ ввімкнена й не запущена.
  Нічна копія бази: увімкнена (shafa-backup.timer, 03:30, 7 копій у $DATA/backups).
  Команди програми на сервері: $CLI <команда>
Далі — за docs/install-server.md: .env, ключ обміну (--add-key), перехід з Mac.
EOF
}

case ${1:-} in
    "") install_all ;;
    --render)
        [ "$#" -eq 2 ] || die "використання: --render ТЕКА"
        render_units "$2"
        ;;
    --add-key)
        [ "$#" -eq 2 ] || die "використання: --add-key ФАЙЛ.pub"
        add_key "$2"
        ;;
    *) die "невідомий параметр: $1 (є: --render ТЕКА, --add-key ФАЙЛ.pub)" ;;
esac
