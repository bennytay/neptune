"""A fixture model-based consolidator for the golden graph (ADR 0006 §10). Not a real model.

It stands in for a model that guesses which machine recorded a run, so the golden graph has
inferred claims to filter and to supersede. Its "model" is its config: a guess per package id,
applied to that package's runs whose ``machine`` is not ``Known``. It is deterministic, calls no
model and reads only through the ``LedgerReader``; it lives here because what it emits is inferred.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known
from neptune_memory.consolidate.base import ClaimDraft, ConsolidatorOutput
from neptune_memory.contract.worked_examples import machine_node, runs
from neptune_memory.schema.claim import ModelRef

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

GUESS_ID: Final = "golden.recorder_guess"
GUESS_MODEL: Final = ModelRef("golden-fixture-guesser", "1")


class RecorderGuess:
    """``run recorded_by machine``, inferred, for runs that name no machine.

    Config: ``{"model": GUESS_MODEL.to_json(), "guesses": {package_id: {"namespace", "value",
    "confidence"}}}``. A package with no guess gets no claim.
    """

    consolidator_id: Final = GUESS_ID
    version: Final = "1"
    model: Final[ModelRef | None] = GUESS_MODEL

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        guesses = config.get("guesses", {})
        if not isinstance(guesses, Mapping):
            raise TypeError("guesses must map package ids to guesses")
        drafts: list[ClaimDraft] = []
        for run in runs(ledger):
            guess = guesses.get(run.package_id)
            if run.machine is not None or not isinstance(guess, Mapping):
                continue
            confidence = guess["confidence"]
            if not isinstance(confidence, float):
                raise TypeError("confidence must be a float")
            machine = LogicalId(str(guess["namespace"]), str(guess["value"]))
            drafts.append(
                ClaimDraft(
                    subject=run.node,
                    predicate="recorded_by",
                    object=machine_node(machine),
                    valid_from=run.first,
                    valid_to=run.last,
                    assertion_kind="inferred",
                    confidence=Known(confidence),
                    evidence=(run.evidence,),
                    records=(run.record,),
                )
            )
        return ConsolidatorOutput(tuple(drafts))
