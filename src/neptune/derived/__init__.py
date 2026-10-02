"""Model-generated or inferred annotations (ADR 0006 §5).

Everything here references canonical evidence; nothing in ``model/`` references this package.

- ``provenance``: ``InferredProvenance``, which no canonical ``Knowledge`` state accepts.
- ``sessions``: the session proposal and unassigned-file records, a package's derived tables.
- ``grouping``: the ``Grouper`` interface and v0's ``LayoutGrouper`` rules (ADR 0036).
- ``schemas``: the schema registry, declared definitions parsed into ``stream_layout`` lines
  (observed: a decoding of declared bytes, kept here as a derivative; ADR 0049).
- ``semantics``: the ``stream_semantic`` kind and the rules that infer what a stream carries.
- ``introspection``: both over a package's streams, under the ``neptune.introspection`` transform.
"""
