# Canonical commands. Agents and humans use these; CI runs `make check`.
# `>` is the recipe prefix so this file does not depend on literal tabs.
.RECIPEPREFIX := >
.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help setup fmt lint type test test-fast check

help: ## Show available targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

setup: ## Install the project and dev tools into .venv
> $(UV) sync --all-groups

fmt: ## Format code and auto-fix lint findings
> $(UV) run ruff format .
> $(UV) run ruff check --fix .

lint: ## Formatting and lint check without modifying files
> $(UV) run ruff format --check .
> $(UV) run ruff check .

type: ## Static type check (mypy --strict)
> $(UV) run mypy

test: ## Full test suite
> $(UV) run pytest

test-fast: ## Test suite excluding tests marked slow
> $(UV) run pytest -m "not slow"

check: lint type test ## Everything CI runs; must pass before opening a PR
