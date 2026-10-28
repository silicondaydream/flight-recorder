.PHONY: setup fmt lint typecheck test check audit clean

PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

setup:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || { \
		echo "ERROR: Python 3.11+ required, found $$($(PYTHON) -V)"; exit 1; }
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

# Every gate, in the order to run them.
check: lint typecheck test

fmt:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest -q

# Advisories appear without anyone touching this repository, so this reports
# rather than gates.
audit:
	$(BIN)/pip-audit || true

clean:
	rm -rf $(VENV) .mypy_cache .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
