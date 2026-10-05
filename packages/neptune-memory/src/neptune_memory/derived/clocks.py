"""Estimated clock mappings as inferred claims (ADR 0011 §4).

The compiler's ``neptune.clocks`` pass fits clock mappings from sync anchors and writes them to
``derived/clock_mapping`` with ``assertion_kind: inferred`` (root ADR 0060). Memory relays them;
it never fits, adjusts or extends one. This consolidator applies ``consolidate.time``'s policy to
them: each estimate is a ``maps_to`` + ``clock_map`` pair over its validity, revised by later
estimates of its pair (never by declared mappings, which are another lineage), and every chain
with at least one estimated hop (declared hops included) is a composed pair. All of its claims are
``inferred``; the model is the compiler pass whose estimates they are, and each claim cites the
estimate's record and its transform record, which name the exact fit.

Its ``confidence`` is ``Unknown``: the compiler states a residual bound, not a probability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from neptune.derived.temporal import CLOCKS_ID, CLOCKS_VERSION
from neptune.model.knowledge import Unknown
from neptune_memory.consolidate.base import ConsolidatorOutput, ModelRef
from neptune_memory.consolidate.time import chains, piece_drafts, read, revise, unknown_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import ClaimDraft
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

ESTIMATES_CONSOLIDATOR_ID: Final = "memory.time_estimates"
# The fit whose output this relays: the compiler's clock-alignment pass (root ADR 0060).
CLOCKS_MODEL: Final = ModelRef(CLOCKS_ID, CLOCKS_VERSION)


class EstimatedClocksConsolidator:
    """Inferred ``maps_to`` and ``clock_map`` claims from the compiler's estimated mappings.

    Its resolved config is ``{"model": CLOCKS_MODEL.to_json()}`` and nothing else.
    """

    consolidator_id: Final = ESTIMATES_CONSOLIDATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = CLOCKS_MODEL

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        view = read(ledger, timing=False, estimated=True)
        extra = {key: value for key, value in config.items() if key != "model"}
        if extra:
            view.findings.append(unknown_config(extra, self.consolidator_id))
        unknown = Unknown()
        pieces = revise(view.hops, view.findings, lambda hop: hop.estimated)
        drafts: list[ClaimDraft] = []
        for piece in pieces:
            if piece.hop.estimated:
                drafts.extend(piece_drafts(piece, unknown))
        for chain in chains(pieces):
            if chain.estimated:
                drafts.extend(chain.drafts(unknown))
        return ConsolidatorOutput(tuple(drafts), tuple(view.findings))
