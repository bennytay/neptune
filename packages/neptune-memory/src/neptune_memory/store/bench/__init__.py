"""Benchmark support for the store decision (MVL-104): a deterministic deployment generator.

Rule: benchmark-only code. Nothing in the runtime store imports this subpackage; engine drivers
used by the harness (``packages/neptune-memory/bench/``) are never package dependencies.
"""
