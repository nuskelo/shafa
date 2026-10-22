PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup lint test ci monkey

setup:
	$(PYTHON) -c "import sys; sys.exit(sys.version_info < (3, 12))" || { echo "Потрібен Python >= 3.12"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet pip==26.2.1
	$(BIN)/python -m pip install --quiet -e . --group dev

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

test:
	$(BIN)/python -m pytest -q

ci: lint test

# QA-004: «мавпа» по програмі й роботові на підставній Шафі (порт 8799, дані — в /tmp). Не в `ci`.
SEED ?= 1
ACTIONS ?= 500
CYCLES ?= 100

# На Mac `caffeinate` не дає ноутбуку заснути посеред прогону (робот зупиняється з причиною «sleep»).
CAFFEINATE := $(shell command -v caffeinate 2>/dev/null && echo -i)

monkey:
	$(CAFFEINATE) $(BIN)/python ops/monkey.py --seed $(SEED) --actions $(ACTIONS) --cycles $(CYCLES)
