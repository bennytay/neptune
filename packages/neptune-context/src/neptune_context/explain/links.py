"""Links a human rendering carries, for Deploy's console to resolve (ADR 0010 §6).

- ``neptune://evidence/<token>?as_of=N``: one evidence ref (its canonical JSON, base64url, no
  padding) at the answer's transaction. The console hydrates it with ``Client.hydrate(ref,
  as_of=N)``. The same URI the MCP server serves as a resource (ADR 0004), so one link works in
  both places.
- ``neptune://claim/<claim id>?as_of=N``: one claim as Memory knew it at transaction ``N``. The
  console opens it with ``Client.why(claim_id, as_of=N, ...)``. A claim a diff names but no
  longer holds links to the transaction before the change, where ``why`` can show it.

Both are deterministic functions of their inputs and carry no host, path or credential.
"""

from __future__ import annotations

import base64
from typing import Final

from neptune_memory.schema.claim import parse_claim_id

from neptune.identity.canonical_json import dumps
from neptune.model.provenance import EvidenceRef

EVIDENCE_SCHEME: Final = "neptune://evidence/"
CLAIM_SCHEME: Final = "neptune://claim/"


def evidence_link(ref: EvidenceRef, as_of: int | None = None) -> str:
    """The console (and MCP resource) URI of ``ref`` at transaction ``as_of``."""
    if not isinstance(ref, EvidenceRef):
        raise TypeError(f"an evidence link names an EvidenceRef, got {type(ref).__name__}")
    token = base64.urlsafe_b64encode(dumps(ref.to_json())).rstrip(b"=").decode("ascii")
    return EVIDENCE_SCHEME + token + ("" if as_of is None else f"?as_of={_tx(as_of)}")


def claim_link(claim_id: str, as_of: int) -> str:
    """The console URI that opens ``why`` for ``claim_id`` at transaction ``as_of``."""
    return f"{CLAIM_SCHEME}{parse_claim_id(claim_id)}?as_of={_tx(as_of)}"


def _tx(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"as_of is a Ledger transaction, got {value!r}")
    return value
