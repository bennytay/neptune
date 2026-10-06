"""Explain (ADR 0010): why memory holds a claim, and what changed about a subject.

- ``explainer``: ``Explainer``, which answers a query's ``Why`` and ``Diff`` clauses for the
  local engine: claim and evidence hits fused like any channel's, plus one trail per clause.
- ``why`` / ``diff``: the two computations over Memory's snapshot (``run`` holds their shared
  reads and admission rules).
- ``history``: ``ClaimHistory``, claim versions by id, and ``IndexedReader``, Memory's
  reference reader with that index.
- ``markdown``: ``render_markdown``, the human rendering with console links (``links``).

Trails name claims Memory holds and relations it states; nothing here infers a cause.
"""

from neptune_context.explain.explainer import Explained, Explainer
from neptune_context.explain.history import ClaimHistory, IndexedReader
from neptune_context.explain.markdown import render_markdown
from neptune_context.explain.run import Caps

__all__ = [
    "Caps",
    "ClaimHistory",
    "Explained",
    "Explainer",
    "IndexedReader",
    "render_markdown",
]
