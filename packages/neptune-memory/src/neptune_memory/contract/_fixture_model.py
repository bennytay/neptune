"""Golden-graph only: a stand-in for a model-based consolidator. Not exported; never register it.

It plays a model so that the golden graph contains inferred claims to filter and to supersede.
Its "model" is its config, and it calls no model: a ``recorded_by`` guess per package, for that
package's runs whose ``machine`` is not ``Known``, and ``same_as_candidate`` pairs (one claim each
way, ADR 0003 §1.3), which a ``derived/`` consolidator may emit. Deterministic, and reads only
through the ``LedgerReader``. Real model-based consolidators live under ``derived/``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from neptune.model.ids import LogicalId
from neptune.model.knowledge import Known
from neptune_memory.consolidate.base import ClaimDraft, ConsolidatorOutput
from neptune_memory.contract.worked_examples import machine_node, runs
from neptune_memory.schema.claim import ModelRef
from neptune_memory.schema.predicates import SAME_AS_CANDIDATE

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

FIXTURE_MODEL_ID: Final = "golden.fixture_model"
FIXTURE_MODEL: Final = ModelRef("golden-fixture-model", "1")


def _machine(spec: JsonValue) -> LogicalId:
    if not isinstance(spec, Mapping):
        raise TypeError("a machine is {namespace, value}")
    return LogicalId(str(spec["namespace"]), str(spec["value"]))


def _confidence(spec: Mapping[str, JsonValue]) -> float:
    value = spec["confidence"]
    if not isinstance(value, float):
        raise TypeError("confidence must be a float")
    return value


class _FixtureModel:
    """Config: ``{"model", "guesses": {package: {namespace, value, confidence}}, "candidates":
    {package: [{left, right, confidence}]}}``; a package's entries apply once its runs land."""

    consolidator_id: Final = FIXTURE_MODEL_ID
    version: Final = "1"
    model: Final[ModelRef | None] = FIXTURE_MODEL

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        guesses, candidates = config.get("guesses", {}), config.get("candidates", {})
        if not isinstance(guesses, Mapping) or not isinstance(candidates, Mapping):
            raise TypeError("guesses and candidates map package ids to entries")
        drafts: list[ClaimDraft] = []
        for run in runs(ledger):
            guess = guesses.get(run.package_id)
            if run.machine is None and isinstance(guess, Mapping):
                drafts.append(
                    ClaimDraft(
                        subject=run.node,
                        predicate="recorded_by",
                        object=machine_node(_machine(guess)),
                        valid_from=run.first,
                        valid_to=run.last,
                        assertion_kind="inferred",
                        confidence=Known(_confidence(guess)),
                        evidence=(run.evidence,),
                        records=(run.record,),
                    )
                )
            pairs = candidates.get(run.package_id, [])
            if not isinstance(pairs, Sequence):
                raise TypeError("candidates are a list")
            for pair in pairs:
                if not isinstance(pair, Mapping):
                    raise TypeError("a candidate is {left, right, confidence}")
                left, right = (
                    machine_node(_machine(pair["left"])),
                    machine_node(_machine(pair["right"])),
                )
                for subject, obj in ((left, right), (right, left)):
                    drafts.append(
                        ClaimDraft(
                            subject=subject,
                            predicate=SAME_AS_CANDIDATE,
                            object=obj,
                            valid_from=run.first,
                            assertion_kind="inferred",
                            confidence=Known(_confidence(pair)),
                            evidence=(run.evidence,),
                            records=(run.record,),
                        )
                    )
        return ConsolidatorOutput(tuple(drafts))
