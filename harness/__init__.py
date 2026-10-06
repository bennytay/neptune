"""The integration harness (Linear MVL-123; packages/neptune-platform ADR 0004).

One environment that runs the contracts check, flows a corpus through compiler -> deploy -> ledger
-> memory -> context and issues a smoke query, writing one deterministic report. Each stage runs the
real package when it is importable and built against the registry's contract version, and a
contract stub otherwise. ``uv run python -m harness`` runs it; the runbook is
``packages/neptune-platform/docs/harness.md``.

- ``run``: the CLI and the run itself.
- ``stages``: the five stages, how each resolves to real or stub, and their drivers.
- ``consolidate``: the memory stage's real driver (Memory's rebuild over the Ledger's packages).
- ``agent``: the context stage's real driver (the gold questions through Context's MCP tools).
- ``demo``: ``make demo``, Demo v1 in one command (the harness, then Deploy's evidence packs).
- ``corpus``: the input cases, with the hook for the Deploy D1 archetypes.
- ``contracts``: the registry tool (``scripts/contracts.py``) loaded as a module.
- ``services``: the compose stack and the reachability check.
- ``report``: the report body and its Markdown form.
- ``publish``: the Linear gate-issue comments (the PR comment is a workflow step).
"""
