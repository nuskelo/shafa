#!/bin/sh
# Вмикає git-хуки з .githooks/ у цій копії репозиторію. Можна запускати повторно.
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

chmod +x .githooks/*
echo "chmod +x: $(ls .githooks | tr '\n' ' ')"

current=$(git config --get core.hooksPath || true)
if [ "$current" = ".githooks" ]; then
    echo "core.hooksPath вже .githooks — нічого не змінено."
else
    git config core.hooksPath .githooks
    echo "core.hooksPath: '${current}' → '.githooks'"
fi

echo "Готово: хуки pre-commit і pre-push увімкнено в $root"
