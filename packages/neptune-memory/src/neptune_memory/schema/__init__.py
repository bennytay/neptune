"""Graph schema and claim model (ADR 0002). ``GRAPH_SCHEMA_VERSION`` stays 0 until MVL-105.

Rule: a claim without provenance and ``assertion_kind`` is a bug. Nothing here infers; inferred
claims are produced under ``derived/`` and merely carry ``assertion_kind = inferred``.

- ``nodes``: tiers, node types, ``NodeRef``.
- ``interval``: valid time (``Interval``, ``CivilClock``) and transaction time (``LedgerTx``).
- ``claim``: ``Claim`` and its objects and provenance.
- ``predicates``: the registered vocabulary and claim validation.
- ``supersede``: the deterministic superseding resolver and ``as_of``.
"""
