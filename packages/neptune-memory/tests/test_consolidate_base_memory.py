"""The consolidator contract (ADR 0003 §2-§4), exercised with a trivial consolidator."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import pytest

from neptune.identity import canonical_json
from neptune.identity.ids import config_hash, record_id
from neptune.model.ids import LogicalId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import KnowledgeState
from neptune_memory.consolidate.base import (
    ClaimDraft,
    ConsolidationFinding,
    Consolidator,
    ConsolidatorOutput,
    ConsolidatorTransform,
    ModelRef,
    PriorClaim,
    claim_id,
    rebuild,
    run_consolidator,
)
from neptune_memory.ledger import LedgerReader, StubLedger


def _rid(n: int) -> RecordId:
    return record_id("test.record", {"n": n})


ARM = LogicalId("serial", "UR10E-2041")


def _ledger() -> StubLedger:
    return StubLedger(
        {
            "pkg-arm": (1, [{"kind": "observation", "id": _rid(1), "count": 2}]),
            "pkg-amr": (1, [{"kind": "observation", "id": _rid(2), "count": 3}]),
        }
    )


@dataclass(frozen=True)
class CountObservations:
    """Trivial deterministic consolidator: one claim per observation record, its count as object."""

    consolidator_id: str = "test.count"
    version: str = "1"
    model: ModelRef | None = None
    kind: str = "observed"

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[PriorClaim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        drafts = []
        for ref in ledger.list_packages():
            for record in ledger.read_records(ref.package_id, "observation") or ():
                rid = record["id"]
                count = record["count"]
                assert isinstance(rid, str) and isinstance(count, int)
                drafts.append(
                    ClaimDraft(
                        predicate="observation_count",
                        subject=ARM,
                        object=count * int(str(config.get("scale", 1))),
                        assertion_kind=self.kind,  # type: ignore[arg-type]
                        inputs=(RecordId(rid), *(p.id for p in previous)),
                    )
                )
        return ConsolidatorOutput(tuple(reversed(drafts)))


@dataclass(frozen=True)
class Crashes:
    consolidator_id: str = "test.crash"
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[PriorClaim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        raise KeyError("boom")


def _draft(inputs: tuple[RecordId, ...] = (_rid(1), _rid(2))) -> ClaimDraft:
    return ClaimDraft("observation_count", ARM, 2, "observed", inputs)


def _transform(
    version: str = "1", config: Mapping[str, JsonValue] | None = None
) -> ConsolidatorTransform:
    return ConsolidatorTransform("test.count", version, config_hash(config or {}))


def test_trivial_consolidator_satisfies_the_protocol() -> None:
    assert isinstance(CountObservations(), Consolidator)


def test_same_inputs_same_id() -> None:
    assert claim_id(_transform(), _draft()) == claim_id(_transform(), _draft())


def test_input_order_and_duplicates_are_irrelevant() -> None:
    a = _draft((_rid(1), _rid(2)))
    b = _draft((_rid(2), _rid(1), _rid(2)))
    assert a == b
    assert claim_id(_transform(), a) == claim_id(_transform(), b)


@pytest.mark.parametrize(
    "other",
    [
        _transform(version="2"),
        _transform(config={"scale": 2}),
        ConsolidatorTransform("test.other", "1", config_hash({})),
        replace(_transform(), model=ModelRef("vlm-x", "2026-09")),
    ],
    ids=["version", "config", "consolidator", "model"],
)
def test_lineage_change_gives_a_sibling_id(other: ConsolidatorTransform) -> None:
    assert claim_id(other, _draft()) != claim_id(_transform(), _draft())


@pytest.mark.parametrize(
    "draft",
    [
        _draft((_rid(1),)),
        replace(_draft(), predicate="other_predicate"),
        replace(_draft(), subject=LogicalId("serial", "UR10E-2042")),
        replace(_draft(), object=3),
    ],
    ids=["inputs", "predicate", "subject", "object"],
)
def test_claim_content_changes_the_id(draft: ClaimDraft) -> None:
    assert claim_id(_transform(), draft) != claim_id(_transform(), _draft())


def test_run_is_idempotent_and_sorted() -> None:
    first = run_consolidator(CountObservations(), _ledger(), (), {})
    second = run_consolidator(CountObservations(), _ledger(), (), {})
    assert canonical_json.dumps(first.to_json()) == canonical_json.dumps(second.to_json())
    assert [c.id for c in first.claims] == sorted(c.id for c in first.claims)
    assert len(first.claims) == 2 and not first.findings


def test_version_bump_creates_sibling_claims_and_leaves_old_ones_alone() -> None:
    v1 = run_consolidator(CountObservations(), _ledger(), (), {})
    v2 = run_consolidator(CountObservations(version="2"), _ledger(), (), {})
    assert {c.id for c in v1.claims}.isdisjoint(c.id for c in v2.claims)
    assert [c.draft for c in v1.claims] == [c.draft for c in v2.claims]
    assert v1 == run_consolidator(CountObservations(), _ledger(), (), {})


def test_transform_is_stamped_on_every_claim() -> None:
    result = run_consolidator(CountObservations(), _ledger(), (), {"scale": 2})
    assert result.transform == _transform(config={"scale": 2})
    assert all(c.transform == result.transform for c in result.claims)
    assert sorted(c.draft.object for c in result.claims) == [4, 6]  # type: ignore[type-var]


def test_deterministic_consolidator_may_not_emit_inferred() -> None:
    result = run_consolidator(CountObservations(kind="inferred"), _ledger(), (), {})
    assert not result.claims
    assert {f.code for f in result.findings} == {"consolidate.wrong_assertion_kind"}


def test_model_based_consolidator_emits_only_inferred_and_carries_the_model() -> None:
    model = ModelRef("vlm-x", "2026-09")
    observed = run_consolidator(CountObservations(model=model), _ledger(), (), {})
    assert not observed.claims and observed.findings
    inferred = run_consolidator(CountObservations(model=model, kind="inferred"), _ledger(), (), {})
    assert len(inferred.claims) == 2
    assert all(c.transform.model == model for c in inferred.claims)
    assert inferred.transform.to_json()["model"] == model.to_json()


def test_draft_without_inputs_is_a_finding_not_a_claim() -> None:
    @dataclass(frozen=True)
    class NoInputs(CountObservations):
        def consolidate(
            self,
            ledger: LedgerReader,
            previous: Sequence[PriorClaim],
            config: Mapping[str, JsonValue],
        ) -> ConsolidatorOutput:
            return ConsolidatorOutput((ClaimDraft("observation_count", ARM, 1, "observed", ()),))

    result = run_consolidator(NoInputs(), _ledger(), (), {})
    assert not result.claims
    assert [f.code for f in result.findings] == ["consolidate.claim_without_inputs"]


def test_crashing_consolidator_is_a_finding() -> None:
    result = run_consolidator(Crashes(), _ledger(), (), {})
    assert not result.claims
    assert [f.code for f in result.findings] == ["consolidate.failed"]
    assert result.findings[0].message == "KeyError: 'boom'"


@pytest.mark.parametrize("text", ["line\nbreak", "lone \ud800 surrogate"])
def test_crash_with_hostile_message_keeps_only_the_type(text: str) -> None:
    @dataclass(frozen=True)
    class Hostile(Crashes):
        def consolidate(
            self,
            ledger: LedgerReader,
            previous: Sequence[PriorClaim],
            config: Mapping[str, JsonValue],
        ) -> ConsolidatorOutput:
            raise RuntimeError(text)

    (finding,) = run_consolidator(Hostile(), _ledger(), (), {}).findings
    assert finding.message == "RuntimeError"


def test_wrong_output_type_is_a_finding() -> None:
    @dataclass(frozen=True)
    class Wrong(Crashes):
        def consolidate(
            self,
            ledger: LedgerReader,
            previous: Sequence[PriorClaim],
            config: Mapping[str, JsonValue],
        ) -> ConsolidatorOutput:
            return ConsolidatorOutput(("not a draft",))  # type: ignore[arg-type]

    result = run_consolidator(Wrong(), _ledger(), (), {})
    assert not result.claims
    assert [f.code for f in result.findings] == ["consolidate.bad_output"]


def test_state_and_assertion_kind_are_part_of_the_id() -> None:
    base = claim_id(_transform(), _draft())
    assert claim_id(_transform(), replace(_draft(), state=KnowledgeState.AMBIGUOUS)) != base
    assert claim_id(_transform(), replace(_draft(), assertion_kind="stated")) != base


def test_rebuild_reproduces_the_graph_byte_for_byte() -> None:
    plan: list[tuple[Consolidator, Mapping[str, JsonValue]]] = [
        (CountObservations(), {}),
        (CountObservations(consolidator_id="test.second"), {}),
    ]

    def build() -> bytes:
        return canonical_json.dumps([r.to_json() for r in rebuild(_ledger(), plan)])

    assert build() == build()


def test_rebuild_feeds_only_earlier_claims_forward() -> None:
    first, second = rebuild(
        _ledger(),
        [(CountObservations(), {}), (CountObservations(consolidator_id="test.second"), {})],
    )
    earlier = {c.id for c in first.claims}
    assert all(not (set(c.draft.inputs) & {c2.id for c2 in first.claims}) for c in first.claims)
    assert all(earlier <= set(c.draft.inputs) for c in second.claims)


def test_rebuild_rejects_a_duplicate_consolidator() -> None:
    with pytest.raises(ValueError, match="twice"):
        rebuild(_ledger(), [(CountObservations(), {}), (CountObservations(version="2"), {})])


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"predicate": "Not A Token"}, "predicate"),
        ({"assertion_kind": "guessed"}, "assertion_kind"),
        ({"state": KnowledgeState.UNKNOWN}, "known or ambiguous"),
        ({"inputs": ("not-a-record-id",)}, "record id"),
        ({"object": float("nan")}, "representable"),
        ({"subject": {"namespace": "serial", "value": "x"}}, "LogicalId"),
    ],
)
def test_malformed_draft_is_rejected(kwargs: dict[str, object], error: str) -> None:
    # TypeError for a wrong subject type; every other case is a ValueError.
    fields: dict[str, object] = {
        "predicate": "p",
        "subject": ARM,
        "object": 1,
        "assertion_kind": "observed",
        "inputs": (_rid(1),),
    }
    fields.update(kwargs)
    with pytest.raises((ValueError, TypeError), match=error):
        ClaimDraft(**fields)  # type: ignore[arg-type]


def test_finding_id_is_content_derived_and_records_are_sorted() -> None:
    from neptune.model.finding import Severity

    a = ConsolidationFinding("x.y", Severity.INFO, "m", (_rid(2), _rid(1)))
    b = ConsolidationFinding("x.y", Severity.INFO, "m", (_rid(1), _rid(2), _rid(1)))
    assert a.id == b.id and a.records == tuple(sorted({_rid(1), _rid(2)}))
    assert a.id != ConsolidationFinding("x.y", Severity.INFO, "other", a.records).id
    with pytest.raises(ValueError, match="producer"):
        ConsolidationFinding("nodot", Severity.INFO, "m")
    with pytest.raises(ValueError, match="representable"):
        ConsolidationFinding("x.y", Severity.INFO, "m", details={"v": float("nan")})
