"""The integration harness (Linear MVL-123; packages/neptune-platform ADR 0004).

One environment that runs the contracts check, flows a corpus through compiler -> ledger ->
memory -> context and issues a smoke query, writing one deterministic report. Each stage runs the
real package when it is importable and built against the registry's contract version, and a
contract stub otherwise. ``uv run python -m harness`` runs it; the runbook is
``packages/neptune-platform/docs/harness.md``.

- ``run``: the CLI and the run itself.
- ``stages``: the four stages, how each resolves to real or stub, and their drivers.
- ``corpus``: the input cases, with the hook for the Deploy D1 archetypes.
- ``contracts``: the registry tool (``scripts/contracts.py``) loaded as a module.
- ``services``: the compose stack and the reachability check.
- ``report``: the report body and its Markdown form.
- ``publish``: the Linear gate-issue comments (the PR comment is a workflow step).
"""
