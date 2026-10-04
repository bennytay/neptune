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
# MEMBER_DIRS (<member>:<root dir>) are root directories whose tests live in a member: that member
# lints them (`$$d`) and the compiler does not; .github/scripts/ci_plan.py routes them the same way.
RUN := $(UV) run --all-packages --all-groups
MEMBER_DIRS := neptune-platform:harness
EACH = set -ef; for p in $(SELECTED); do d=""; \
  if [ "$$p" = $(COMPILER) ]; then cd "$(CURDIR)"; \
    x="--extend-exclude packages/*$(foreach m,$(MEMBER_DIRS), --extend-exclude $(word 2,$(subst :, ,$(m))))"; \
  else cd "$(CURDIR)/packages/$$p"; x=""; \
    for m in $(MEMBER_DIRS); do if [ "$${m%%:*}" = "$$p" ]; then d="$$d $(CURDIR)/$${m\#*:}"; fi; done; \
  fi; echo "--- $$p" >&2;
# The compiler's own index (repository-wide numbers) is generated too; members' are local to the package.
ADR_DIRS = $(SELECTED_MEMBERS:%=packages/%/docs/adr)
COMPILER_ADR = $(if $(filter $(COMPILER),$(SELECTED)),--compiler docs/adr)

.PHONY: help setup fmt lint type test test-fast check schema examples adr-index adr-index-check \
  contracts-check harness

help: ## Show available targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup: ## Install the compiler, every workspace member and the dev tools into .venv
> $(UV) sync --all-packages --all-groups

fmt: adr-index ## Format code, auto-fix lint findings and regenerate the ADR index
> $(UV) run ruff format .
> $(UV) run ruff check --fix .

lint: adr-index-check ## Formatting, lint and ADR-index check without modifying files
# `set -e` ignores a failure on the left of `&&`, so each step exits explicitly.
> @$(EACH) $(RUN) ruff format --check $$x . $$d || exit 1; $(RUN) ruff check $$x . $$d || exit 1; done

type: ## Static type check (mypy --strict)
> @$(EACH) $(RUN) mypy; done

test: ## Full test suite
> @$(EACH) $(RUN) pytest; done

test-fast: ## Test suite excluding tests marked slow
> @$(EACH) $(RUN) pytest -m "not slow"; done

check: lint type test ## Everything CI runs; must pass before opening a PR (PKG=<name> for one)

adr-index: ## Regenerate docs/adr/README.md (compiler and each member); `make fmt` runs it
> $(if $(strip $(COMPILER_ADR) $(ADR_DIRS)),python3 .github/scripts/adr_index.py $(COMPILER_ADR) $(ADR_DIRS),@true)

adr-index-check: ## Fail if an ADR index is stale
> $(if $(strip $(COMPILER_ADR) $(ADR_DIRS)),python3 .github/scripts/adr_index.py --check $(COMPILER_ADR) $(ADR_DIRS),@true)

schema: ## Regenerate docs/schema/canonical.schema.json from the model's types
> $(UV) run python -m neptune.model.schema docs/schema/canonical.schema.json

examples: ## Regenerate the worked examples and their golden package documents
> $(UV) run python tests/fixtures/model/make_examples.py
> $(UV) run python tests/golden/packages/make_packages.py
> $(UV) run python tests/golden/mcap/make_mcap_golden.py
> $(UV) run python tests/golden/config/make_config_golden.py
> $(UV) run python tests/golden/assertion/make_assertion_golden.py

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
