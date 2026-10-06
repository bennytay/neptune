"""Retrieval channels and their fusion (ADR 0007): graph, catalog, lexical, vector and spatial.

- ``channel``: the one interface every channel implements (``RetrievalChannel``), what it is asked
  (``Retrieval`` at a ``Snapshot``) and what it answers (``ChannelAnswer``, built with ``answer``).
- ``fusion``: reciprocal-rank fusion over channel answers and the budget cut (v0; MVL-145).
- ``graph``: the graph channel over Memory's reader and the Ledger's indexes (MVL-144).

Every channel returns items with provenance; fusion keeps it and never ranks one channel above
another by construction. Read-only over Memory and the Ledger.
"""

from neptune_context.retrieve.channel import (
    ChannelAnswer,
    Retrieval,
    RetrievalChannel,
    Snapshot,
    answer,
)
from neptune_context.retrieve.fusion import RRF_K, Cut, cut, fuse

__all__ = [
    "RRF_K",
    "ChannelAnswer",
    "Cut",
    "Retrieval",
    "RetrievalChannel",
    "Snapshot",
    "answer",
    "cut",
    "fuse",
]
