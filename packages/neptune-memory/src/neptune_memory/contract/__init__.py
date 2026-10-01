"""The graph-schema contract beyond its types: golden graph and downstream contract suite.

Rule: deterministic and test-facing. Nothing here is a production consolidator or a store: the
consolidators in ``golden`` exist only to build the published golden graph from the compiler's
four worked examples, and ``suite`` checks any ``schema.reader.MemoryReader`` against the
reference reader on that graph (ADR 0006 §8, §10). Reads packages only through a
``LedgerReader``; file access stays with the caller.

- ``worked_examples``: strict parsing of the worked examples' run records.
- ``golden``: the golden-graph consolidators, the Ledger overlay and ``build_golden``.
- ``suite``: ``CHECKS`` (run them against your reader) and ``StubReader`` (they fail on it).
"""
