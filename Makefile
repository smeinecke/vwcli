# Makefile for vwcli

.PHONY: all format reformat-ruff check fix-ruff fix test test-cov integration-test vulture xenon bandit pyright validate

# Default target: run validation and tests
all: validate test

# Format the code using ruff
format:
	uv run ruff format --check --diff .

reformat-ruff:
	uv run ruff format .

# Check the code using ruff
check:
	uv run ruff check .

fix-ruff:
	uv run ruff check . --fix

fix: reformat-ruff fix-ruff
	@echo "Updated code."

test:
	uv run pytest tests

test-cov:
	uv run pytest tests --cov --cov-report=xml --cov-report=term-missing

integration-test:
	uv run pytest tests/integration -m integration -v

vulture:
	uv run vulture . --exclude .venv,tests --make-whitelist

xenon:
	uv run xenon -b D -m B -a B .

bandit:
	uv run bandit -c pyproject.toml -r .

pyright:
	uv run pyright

# Validate the code (format + check + security + type check + dead code + complexity)
validate: format check bandit pyright vulture xenon
	@echo "Validation passed. Your code is ready to push."
