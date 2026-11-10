#!/bin/sh
# OPS-005: копія бази бота через `sqlite3 .backup` (бот працює — це безпечно), лишає 7 останніх.
# Використання: backup.sh <база.sqlite> <тека копій>
# Копія — shafa-РРРР-ММ-ДД.sqlite; повторний запуск того ж дня замінює її. Спершу пишемо у
# тимчасовий файл і перевіряємо `integrity_check`: зіпсована копія ніколи не стає «справжньою»
# й не витісняє стару.
set -eu

KEEP=7

if [ "$#" -ne 2 ]; then
    echo "Використання: backup.sh <база.sqlite> <тека копій>" >&2
    exit 2
fi
db=$1
dir=$2

if [ ! -f "$db" ]; then
    echo "Базу не знайдено: $db" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "Немає sqlite3 (sudo apt install sqlite3)" >&2
    exit 1
fi

umask 077
mkdir -p "$dir"
chmod 700 "$dir"

day=$(date +%Y-%m-%d)
tmp="$dir/.shafa-$day.sqlite.tmp"
final="$dir/shafa-$day.sqlite"
rm -f "$tmp"
# Будь-який збій (у т.ч. `set -e`) не залишає недописаної копії.
trap 'rm -f "$tmp"' EXIT

sqlite3 "$db" ".backup '$tmp'"
check=$(sqlite3 "$tmp" "PRAGMA integrity_check;")
if [ "$check" != "ok" ]; then
    echo "Копія не пройшла integrity_check: $check" >&2
    exit 1
fi
mv -f "$tmp" "$final"

# Старі копії: лишаємо KEEP найновіших (назви з датою сортуються як дати).
set -- "$dir"/shafa-????-??-??.sqlite
excess=$(($# - KEEP))
for file in "$@"; do
    if [ "$excess" -le 0 ]; then
        break
    fi
    rm -f "$file"
    excess=$((excess - 1))
done

echo "Копія: $final"
