#!/usr/bin/env bash
# Neptune's 15-minute quickstart (README.md, "15-minute quickstart"), as one script.
#
# The lines between the markers are the README's quickstart block, verbatim: CI runs this file on
# a clean runner (.github/workflows/harness.yml, job `quickstart`, 15-minute timeout) and
# packages/neptune-platform/tests/test_quickstart_readme.py fails when the two differ.
# Run it from a clone (or from an empty folder: it clones). Needs git, make and curl.
set -euo pipefail

# --- quickstart ---
command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh && . "$HOME/.local/bin/env"; }
[ -f harness/acceptance/gold.json ] || { git clone https://github.com/bennytay/neptune.git && cd neptune; }
make setup
make demo
ls demo/*.pdf
cp packages/neptune-context/claude/mcp.sample.json .mcp.json
mkdir -p .claude/skills && cp -R packages/neptune-context/claude/skills/neptune .claude/skills/
export NEPTUNE_MEMORY_GRAPH=demo/graph.json
# --- end ---
