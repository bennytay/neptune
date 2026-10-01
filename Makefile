# Canonical commands. Agents and humans use these; CI runs `make check PKG=<name>` per job.
# `>` is the recipe prefix so this file does not depend on literal tabs.
#
# The repository is a uv workspace: the compiler (`neptune`) is the root project and every
# packages/<name>/ is a member. Targets run for all of them; `PKG=<name>` selects one.
.RECIPEPREFIX := >
.DEFAULT_GOAL := help
UV ?= uv

COMPILER := neptune
MEMBERS := $(sort $(patsubst packages/%/pyproject.toml,%,$(filter-out packages/_template/%,$(wildcard packages/*/pyproject.toml))))
PKG ?=
ifneq ($(PKG),)
ifeq ($(filter $(PKG),$(COMPILER) $(MEMBERS)),)
$(error PKG=$(PKG) is not a workspace package; choose one of: $(COMPILER) $(MEMBERS))
endif
endif
SELECTED := $(if $(PKG),$(PKG),$(COMPILER) $(MEMBERS))
SELECTED_MEMBERS := $(filter $(MEMBERS),$(SELECTED))
# Every tool runs against the whole workspace environment, from the package's own directory so its
# own ruff / mypy / pytest configuration applies. Members are linted only by their own job.
RUN := $(UV) run --all-packages --all-groups
EACH = set -ef; for p in $(SELECTED); do \
  if [ "$$p" = $(COMPILER) ]; then cd "$(CURDIR)"; x="--extend-exclude packages/*"; \
  else cd "$(CURDIR)/packages/$$p"; x=""; fi; echo "--- $$p" >&2;
ADR_DIRS = $(SELECTED_MEMBERS:%=packages/%/docs/adr)

.PHONY: help setup fmt lint type test test-fast check schema examples adr-index adr-index-check \
  contracts-check harness

help: ## Show available targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup: ## Install the compiler, every workspace member and the dev tools into .venv
> $(UV) sync --all-packages --all-groups

fmt: ## Format code and auto-fix lint findings
> $(UV) run ruff format .
> $(UV) run ruff check --fix .

lint: adr-index-check ## Formatting, lint and ADR-index check without modifying files
# `set -e` ignores a failure on the left of `&&`, so each step exits explicitly.
> @$(EACH) $(RUN) ruff format --check $$x . || exit 1; $(RUN) ruff check $$x . || exit 1; done

type: ## Static type check (mypy --strict)
> @$(EACH) $(RUN) mypy; done

test: ## Full test suite
> @$(EACH) $(RUN) pytest; done

test-fast: ## Test suite excluding tests marked slow
> @$(EACH) $(RUN) pytest -m "not slow"; done

check: lint type test ## Everything CI runs; must pass before opening a PR (PKG=<name> for one)

adr-index: ## Regenerate each member's docs/adr/README.md
> $(if $(ADR_DIRS),python3 .github/scripts/adr_index.py $(ADR_DIRS),@true)

adr-index-check: ## Fail if a member's ADR index is stale
> $(if $(ADR_DIRS),python3 .github/scripts/adr_index.py --check $(ADR_DIRS),@true)

schema: ## Regenerate docs/schema/canonical.schema.json from the model's types
> $(UV) run python -m neptune.model.schema docs/schema/canonical.schema.json

examples: ## Regenerate the worked examples and their golden package documents
> $(UV) run python tests/fixtures/model/make_examples.py
> $(UV) run python tests/golden/packages/make_packages.py
> $(UV) run python tests/golden/mcap/make_mcap_golden.py

# One `check` call covers every selected member (and, without PKG, every package in lock.toml), so
# each upstream owner's contract tests run once; the matrix must match the registry.
CONTRACT_CONSUMERS = $(if $(PKG),,--all) $(SELECTED_MEMBERS:%=--package %)
contracts-check: ## Owner rule, lock + upstream contract tests, matrix freshness (PKG=<name> for one)
> @set -e; for p in $(SELECTED); do echo "--- contracts $$p" >&2; \
  $(RUN) python scripts/contracts.py check-owner --package "$$p"; done
> $(if $(strip $(CONTRACT_CONSUMERS)),$(RUN) python scripts/contracts.py check $(CONTRACT_CONSUMERS),@true)
> $(RUN) python scripts/contracts.py matrix --check

HARNESS_RUN_DIR ?= harness/.run
harness: ## Integration harness: contracts check, corpus through the stages, smoke query, report
> $(RUN) python -m harness --run-dir "$(HARNESS_RUN_DIR)" $(HARNESS_ARGS)
