# Canonical commands. Agents and humans use these; CI runs `make check PKG=<name>` per job.
# `>` is the recipe prefix so this file does not depend on literal tabs.
#
# The repository is a uv workspace: the compiler (`neptune`) is the root project and every
# packages/<name>/ is a member. Targets run for all of them; `PKG=<name>` selects one.
.RECIPEPREFIX := >
.DEFAULT_GOAL := help
UV ?= uv

COMPILER := neptune
MEMBERS := $(sort $(patsubst packages/%/pyproject.toml,%,$(wildcard packages/*/pyproject.toml)))
PKG ?=
ifneq ($(PKG),)
ifeq ($(filter $(PKG),$(COMPILER) $(MEMBERS)),)
$(error PKG=$(PKG) is not a workspace package; choose one of: $(COMPILER) $(MEMBERS))
endif
endif
SELECTED := $(if $(PKG),$(PKG),$(COMPILER) $(MEMBERS))
# Every tool runs against the whole workspace environment, from the package's own directory so its
# own ruff / mypy / pytest configuration applies. Members are linted only by their own job.
RUN := $(UV) run --all-packages --all-groups
EACH = set -ef; for p in $(SELECTED); do \
  if [ "$$p" = $(COMPILER) ]; then cd "$(CURDIR)"; x="--extend-exclude packages/*"; \
  else cd "$(CURDIR)/packages/$$p"; x=""; fi; echo "--- $$p" >&2;

.PHONY: help setup fmt lint type test test-fast check schema examples \
  check-all site-demo site-replay site-live site-web site-show

help: ## Show available targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup: ## Install the compiler, every workspace member and the dev tools into .venv, plus git hooks
> $(UV) sync --all-packages --all-groups
> @if git rev-parse --git-dir >/dev/null 2>&1; then $(UV) run pre-commit install >/dev/null || true; fi

fmt: ## Format code and auto-fix lint findings
> $(UV) run ruff format .
> $(UV) run ruff check --fix .

lint: ## Formatting and lint check without modifying files
# `set -e` ignores a failure on the left of `&&`, so each step exits explicitly.
> @$(EACH) $(RUN) ruff format --check $$x . || exit 1; $(RUN) ruff check $$x . || exit 1; done

type: ## Static type check (mypy --strict)
> @$(EACH) $(RUN) mypy; done

# Tests run in parallel (pytest-xdist). PYTEST_WORKERS caps the workers (CI sets `auto`); tests
# that share state carry an `xdist_group` and stay on one worker. PYTEST_ARGS adds flags.
PYTEST_WORKERS ?= 4
PYTEST = $(RUN) pytest -n $(PYTEST_WORKERS) --dist loadgroup $(PYTEST_ARGS)

test: ## Full test suite, slow tests included
> @$(EACH) $(PYTEST); done

test-fast: ## Test suite excluding tests marked slow
> @$(EACH) $(PYTEST) -m "not slow"; done

check: lint type test-fast ## What PR CI runs: lint, types, fast tests; must pass before a PR (PKG=<name> for one)

check-all: lint type test ## Everything, slow tests included; main and the nightly run it (PKG=<name> for one)

schema: ## Regenerate docs/schema/canonical.schema.json from the model's types
> $(UV) run python -m neptune.model.schema docs/schema/canonical.schema.json

examples: ## Regenerate the worked examples and their golden package documents
> $(UV) run python tests/fixtures/model/make_examples.py
> $(UV) run python tests/golden/packages/make_packages.py
> $(UV) run python tests/golden/mcap/make_mcap_golden.py
> $(UV) run python tests/golden/config/make_config_golden.py
> $(UV) run python tests/golden/assertion/make_assertion_golden.py
> $(UV) run python tests/golden/urdf/make_urdf_golden.py
> $(UV) run python tests/golden/manifest/make_manifest_golden.py

# Site model v3 (docs/site-model/spec.md §9): a demo corpus -> fact log -> the MCP command.
# SITE=p7 (Plant 7, default) or SITE=harbor (Harbor DC). site-demo confirms the identity proposals
# (amr-02 / MR-1142; gtp-03 / AMR-17) as the operator would (spec §7). SITE=public fetches real public
# robot data instead of generating a corpus (docs/site-model/real-data.md).
SITE ?= p7
SITE_DIR ?= $(if $(filter p7,$(SITE)),site-demo,site-$(SITE))
SPEED ?= 60
# --extra anthropic: the dashboard's improvement runs call Claude with the person's own key (spec §16).
SITE_CMD := $(RUN) --extra anthropic python -m neptune_memory.site
SITE_MCP := uv run --directory "$(CURDIR)" --all-packages python -m neptune_context.mcp --site "$(abspath $(SITE_DIR))"
SITE_SOURCE = $(if $(filter public,$(SITE)),scripts/fetch_public_site.sh "$(SITE_DIR)",$(SITE_CMD) generate --site "$(SITE)" "$(SITE_DIR)")
site-demo: ## Site model: generate the SITE corpus (p7 | harbor) or fetch it (public), build, confirm, log insights, print the MCP command
> $(SITE_SOURCE)
> $(SITE_CMD) build "$(SITE_DIR)"
> $(SITE_CMD) confirm "$(SITE_DIR)" --yes-all --by demo-operator
> $(SITE_CMD) insights "$(SITE_DIR)"
> @echo "MCP: $(SITE_MCP)"
> @echo "Claude Code: claude mcp add neptune-site -- $(SITE_MCP)"

site-replay: ## Site model: the same, live (SPEED=60 is 60x real time)
> $(SITE_CMD) generate --site "$(SITE)" "$(SITE_DIR)"
> @echo "MCP: $(SITE_MCP)"
> $(SITE_CMD) replay "$(SITE_DIR)" --speed "$(SPEED)"

# site-live (#202): a local MQTT broker, `live` subscribed to it, then `publish` at SPEED x from START
# (earlier messages go at once). Ctrl-C stops all three. The MCP server tails the log as it grows.
BROKER ?= mqtt://127.0.0.1:1883
START ?= $(if $(filter harbor,$(SITE)),2026-11-04T10:02:00,2026-10-20T09:55:00)
SITE_READY = $(SITE_DIR)/.neptune/live.ready
WEB_PORT ?= 8765
SITE_URL = http://127.0.0.1:$(WEB_PORT)/
site-live: ## Site model, live over MQTT: broker + live + publish at SPEED x from START
> $(SITE_CMD) generate --site "$(SITE)" "$(SITE_DIR)"
> rm -rf "$(SITE_DIR)/feed/live" "$(SITE_DIR)/actions" "$(SITE_DIR)/improvements" "$(SITE_READY)"
> @echo "MCP: $(SITE_MCP)"
> @echo "Claude Code: claude mcp add neptune-site -- $(SITE_MCP)"
> @echo "Web view: $(SITE_URL)"
> @$(SITE_CMD) broker --broker "$(BROKER)" & b=$$!; \
  $(SITE_CMD) live "$(SITE_DIR)" --broker "$(BROKER)" & l=$$!; \
  $(SITE_CMD) web "$(SITE_DIR)" --port "$(WEB_PORT)" --broker "$(BROKER)" $(WEB_OPEN) & w=$$!; \
  trap 'kill $$l $$b $$w 2>/dev/null; wait' EXIT; trap 'exit 130' INT TERM; \
  until [ -f "$(SITE_READY)" ]; do kill -0 $$l 2>/dev/null || exit 1; sleep 0.2; done; \
  $(SITE_CMD) publish "$(SITE_DIR)" --broker "$(BROKER)" --speed "$(SPEED)" --start "$(START)"; \
  echo "live keeps running; drop a file into $(SITE_DIR)/static/ to see it ingested. Ctrl-C stops."; \
  wait

# site-web (#210): the live web view of SITE_DIR (map, states, timeline, findings, cited sources).
site-web: ## Site model: serve the web view of SITE_DIR at WEB_PORT and open it (run site-demo first)
> @test -d "$(SITE_DIR)/.neptune/log" || { echo "no fact log in $(SITE_DIR): run make site-demo first" >&2; exit 2; }
> $(SITE_CMD) web "$(SITE_DIR)" --port "$(WEB_PORT)" --open

# site-show (#214): the demo in one command. site-live at a demo-friendly SPEED (10: the Plant 7
# story runs about ten minutes instead of 95 s) with the web view opened, after a 3-minute script.
SHOW_SCRIPT = docs/site-model/show/$(SITE).txt
SHOW_OPEN ?= --open
site-show: SPEED = 10
site-show: ## Site model demo: live site + web view at SPEED=10, prints a 3-minute script (SITE=p7 | harbor)
> @test -f "$(SHOW_SCRIPT)" || { echo "site-show runs SITE=p7 or SITE=harbor" >&2; exit 2; }
> @sed 's/SPEED x/$(SPEED)x/' "$(SHOW_SCRIPT)"
> @$(MAKE) --no-print-directory site-live SITE="$(SITE)" SPEED="$(SPEED)" WEB_OPEN="$(SHOW_OPEN)"
