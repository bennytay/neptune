"""Graph schema and claim model: the graph-schema contract (ADR 0002, 0005, 0006).

Rule: a claim without provenance and ``assertion_kind`` is a bug. Nothing here infers; inferred
claims are produced under ``derived/`` and merely carry ``assertion_kind = inferred`` and a model.

- ``nodes``: tiers, node types, ``NodeRef``.
- ``interval``: valid time (``Interval``, ``CivilClock``) and transaction time (``LedgerTx``).
- ``claim``: ``Claim`` and its objects and provenance.
- ``predicates``: the registered vocabulary and claim validation.
- ``supersede``: the deterministic superseding resolver, its findings and ``as_of``.
- ``reader``: the ``MemoryReader`` read protocol and its typed results; ``reference``: the
  in-memory reference reader built on ``resolve`` and ``as_of``; ``traverse``: following
  ``same_as`` over any reader, never merging (ADR 0008 §5).
- ``codec``: strict JSON parsing of claims, findings and graph documents.
- ``export``: the JSON Schema published under ``contracts/graph-schema/``.

``GRAPH_SCHEMA_VERSION`` is the registry major of graph-schema: raise it for a breaking change.
"""

from typing import Final

GRAPH_SCHEMA_VERSION: Final = 1
