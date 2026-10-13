PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup lint test ci

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
