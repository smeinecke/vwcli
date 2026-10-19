# Makefile for vwcli

.PHONY: all install install-tool install-completion install-service format reformat-ruff check fix-ruff fix test test-cov integration-test integration-test-all vulture xenon bandit pyright validate

# Default target: run validation and tests
all: validate test

# Install vwcli as a uv tool, shell completions (bash/zsh/fish) and the
# persistent bw serve service (systemd user unit on Linux, launchd
# LaunchAgent on macOS; the installers also set up nvm + @bitwarden/cli
# when missing). Optional installer flags go through INSTALL_ARGS, e.g.
#   make install INSTALL_ARGS=--install-backup   (Linux only)
install: install-tool install-completion install-service

install-tool:
	uv tool install --reinstall .

install-completion: install-tool
	./completion/install-completion.sh

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
install-service:
	./bitwarden-serve/install-bitwarden-cli-user-service.sh $(INSTALL_ARGS)
else ifeq ($(UNAME_S),Darwin)
install-service:
	./bitwarden-serve-macos/install-bitwarden-serve.sh $(INSTALL_ARGS)
else
install-service:
	@echo "Skipping bw serve service install (unsupported OS); vwcli falls back to a per-command bw serve."
endif

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
	uv run pytest tests/integration -m "integration and not slow" -v

integration-test-all:
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
