"""Graph schema and claim model (planned: MVL-105).

Rule: a claim without provenance and ``assertion_kind`` is a bug. Nothing here infers; inferred
claims are produced under ``derived/`` and merely carry ``assertion_kind = inferred``.
"""
