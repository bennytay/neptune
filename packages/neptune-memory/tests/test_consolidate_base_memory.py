"""The consolidator contract (ADR 0003 §2-§4), exercised with a trivial consolidator."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.ids import config_hash, record_id
from neptune.model.finding import Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.time import Timestamp
from neptune_memory.consolidate.base import (
    ClaimDraft,
    Consolidation,
    ConsolidationFinding,
    Consolidator,
    ConsolidatorOutput,
    ModelRef,
    rebuild,
    run_consolidator,
)
from neptune_memory.ledger import LedgerReader, StubLedger
from neptune_memory.schema.claim import Claim, LedgerRecordRef
from neptune_memory.schema.interval import LedgerTx, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

TX = ledger_tx(7)
CLOCK = record_id("test.clock", {"name": "arm-controller"})
ARM = NodeRef(NodeType.MACHINE, "serial:UR10E-2041")
MODEL = ModelRef("vlm-x", "2026-09")


def _rid(n: int) -> RecordId:
    return record_id("test.record", {"n": n})


def _ref(n: int) -> EvidenceRef:
    return EvidenceRef(content_id(f"mcap {n}".encode()), (ByteRange(0, 16),))


def _ledger() -> StubLedger:
    return StubLedger(
        {
            "pkg-arm": (1, [{"kind": "observation", "id": _rid(1), "n": 1}]),
            "pkg-amr": (1, [{"kind": "observation", "id": _rid(2), "n": 2}]),
        }
    )


@dataclass(frozen=True)
class EvidencedBy:
    """Trivial consolidator: one ``evidenced_by`` claim per observation record."""

    consolidator_id: str = "test.evidenced"
    version: str = "1"
    model: ModelRef | None = None
    kind: object = AssertionKind.OBSERVED
    reverse: bool = False

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        drafts = []
        for ref in ledger.list_packages():
            for record in ledger.read_records(ref.package_id, "observation") or ():
                rid, n = record["id"], record["n"]
                assert isinstance(rid, str) and isinstance(n, int)
                evidence = (_ref(n), _ref(n + 10), _ref(n))
                records = (RecordId(rid), _rid(100))
                if self.reverse:
                    evidence, records = evidence[::-1], records[::-1]
                drafts.append(
                    ClaimDraft(
                        subject=ARM,
                        predicate="evidenced_by",
                        object=LedgerRecordRef(RecordId(rid)),
                        valid_from=Timestamp(n, CLOCK),
                        assertion_kind=self.kind,  # type: ignore[arg-type]
                        evidence=evidence,
                        records=records,
                        confidence=Known(0.5) if self.model else NotApplicable(),
                    )
                )
        return ConsolidatorOutput(tuple(reversed(drafts)))


@dataclass(frozen=True)
class Raises:
    error: Exception
    consolidator_id: str = "test.raises"
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        raise self.error


@dataclass(frozen=True)
class Returns:
    output: object
    consolidator_id: str = "test.returns"
    version: str = "1"
    model: ModelRef | None = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        return self.output  # type: ignore[return-value]


def _run(
    c: Consolidator, config: Mapping[str, JsonValue] | None = None, tx: LedgerTx = TX
) -> Consolidation:
    return run_consolidator(c, _ledger(), (), config or {}, recorded_at=tx)


def _draft(**changes: object) -> ClaimDraft:
    base = ClaimDraft(
        subject=ARM,
        predicate="evidenced_by",
        object=LedgerRecordRef(_rid(1)),
        valid_from=Timestamp(1, CLOCK),
        assertion_kind=AssertionKind.OBSERVED,
        evidence=(_ref(1),),
        records=(_rid(1),),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_trivial_consolidator_satisfies_the_protocol() -> None:
    assert isinstance(EvidencedBy(), Consolidator)


def test_run_is_idempotent_and_sorted() -> None:
    first, second = _run(EvidencedBy()), _run(EvidencedBy())
    assert canonical_json.dumps(first.to_json()) == canonical_json.dumps(second.to_json())
    assert [c.id for c in first.claims] == sorted(c.id for c in first.claims)
    assert len(first.claims) == 2 and not first.findings


def test_input_order_and_repeats_do_not_change_ids() -> None:
    forward, backward = _run(EvidencedBy()), _run(EvidencedBy(reverse=True))
    assert [c.id for c in forward.claims] == [c.id for c in backward.claims]
    claim = forward.claims[0]
    assert len(claim.provenance.evidence) == 2  # the repeat is gone
    assert list(claim.provenance.records) == sorted(claim.provenance.records)


def test_transform_and_transaction_are_stamped() -> None:
    result = _run(EvidencedBy(), {"window": 5})
    assert result.transform.config_hash == config_hash({"window": 5})
    for claim in result.claims:
        assert claim.provenance.consolidator_id == "test.evidenced"
        assert claim.provenance.consolidator_version == "1"
        assert claim.provenance.config_hash == config_hash({"window": 5})
        assert claim.recorded_at == TX


@pytest.mark.parametrize(
    ("other", "config"),
    [(EvidencedBy(version="2"), {}), (EvidencedBy(), {"window": 5})],
    ids=["version", "config"],
)
def test_lineage_change_gives_sibling_claims(
    other: EvidencedBy, config: Mapping[str, JsonValue]
) -> None:
    v1 = _run(EvidencedBy())
    v2 = _run(other, config)
    assert {c.id for c in v1.claims}.isdisjoint(c.id for c in v2.claims)

    def asserted(result: Consolidation) -> list[bytes]:
        return sorted(
            canonical_json.dumps([c.subject.to_json(), c.predicate, c.object.to_json()])
            for c in result.claims
        )

    assert asserted(v1) == asserted(v2)
    assert v1 == _run(EvidencedBy())  # the old lineage is untouched


def test_transaction_time_is_bookkeeping_not_identity() -> None:
    one = _run(EvidencedBy(), tx=ledger_tx(1))
    two = _run(EvidencedBy(), tx=ledger_tx(2))
    assert [c.id for c in one.claims] == [c.id for c in two.claims]


def test_deterministic_consolidator_may_not_emit_inferred() -> None:
    result = _run(EvidencedBy(kind="inferred"))
    assert not result.claims
    assert {f.code for f in result.findings} == {"consolidate.wrong_assertion_kind"}


def test_model_based_consolidator_emits_only_inferred_and_hashes_its_model() -> None:
    config: dict[str, JsonValue] = {"model": MODEL.to_json()}
    observed = _run(EvidencedBy(model=MODEL), config)
    assert not observed.claims and observed.findings
    inferred = _run(EvidencedBy(model=MODEL, kind="inferred"), config)
    assert len(inferred.claims) == 2
    assert inferred.transform.model == MODEL
    newer_model = ModelRef("vlm-x", "2026-10")
    newer = _run(EvidencedBy(model=newer_model, kind="inferred"), {"model": newer_model.to_json()})
    assert {c.id for c in inferred.claims}.isdisjoint(c.id for c in newer.claims)


def test_model_based_consolidator_must_name_its_model_in_config() -> None:
    with pytest.raises(ValueError, match="model"):
        _run(EvidencedBy(model=MODEL, kind="inferred"))


@pytest.mark.parametrize(
    ("draft", "code"),
    [
        (_draft(evidence=()), "consolidate.invalid_claim"),
        (_draft(predicate="Not A Token"), "consolidate.invalid_claim"),
        (_draft(records=("not-a-record-id",)), "consolidate.invalid_claim"),
        (_draft(valid_to=Timestamp(0, CLOCK)), "consolidate.invalid_claim"),
        (_draft(subject="serial:x"), "consolidate.invalid_claim"),
        (_draft(predicate="no_such_predicate"), "consolidate.schema_violation"),
        (_draft(predicate="rated_payload"), "consolidate.schema_violation"),
    ],
    ids=[
        "no-evidence",
        "bad-predicate",
        "bad-record",
        "empty-interval",
        "bad-subject",
        "unknown",
        "range",
    ],
)
def test_bad_draft_is_a_finding_not_a_claim(draft: ClaimDraft, code: str) -> None:
    result = _run(Returns(ConsolidatorOutput((draft, _draft()))))
    assert len(result.claims) == 1
    assert [f.code for f in result.findings] == [code]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (KeyError("boom"), "KeyError: 'boom'"),
        (RuntimeError("line\nbreak"), "RuntimeError"),
        (RuntimeError("lone \ud800 surrogate"), "RuntimeError"),
    ],
)
def test_crashing_consolidator_is_a_finding(error: Exception, message: str) -> None:
    result = _run(Raises(error))
    assert not result.claims
    assert [(f.code, f.message) for f in result.findings] == [("consolidate.failed", message)]


@pytest.mark.parametrize(
    "output",
    [None, ConsolidatorOutput(("not a draft",)), ConsolidatorOutput((), ("x",))],  # type: ignore[arg-type]
)
def test_wrong_output_type_is_a_finding(output: object) -> None:
    result = _run(Returns(output))
    assert [f.code for f in result.findings] == ["consolidate.bad_output"]


PLAN: list[tuple[Consolidator, Mapping[str, JsonValue]]] = [
    (EvidencedBy(), {}),
    (EvidencedBy(consolidator_id="test.second"), {}),
]


def test_rebuild_reproduces_the_graph_byte_for_byte() -> None:
    def build() -> bytes:
        return canonical_json.dumps([r.to_json() for r in rebuild(_ledger(), PLAN, recorded_at=TX)])

    assert build() == build()


def test_rebuild_feeds_only_earlier_claims_forward() -> None:
    seen: list[tuple[str, int]] = []

    @dataclass(frozen=True)
    class Spy(EvidencedBy):
        def consolidate(
            self,
            ledger: LedgerReader,
            previous: Sequence[Claim],
            config: Mapping[str, JsonValue],
        ) -> ConsolidatorOutput:
            seen.append((self.consolidator_id, len(previous)))
            return super().consolidate(ledger, previous, config)

    rebuild(_ledger(), [(Spy(), {}), (Spy(consolidator_id="test.spy"), {})], recorded_at=TX)
    assert seen == [("test.evidenced", 0), ("test.spy", 2)]


def test_rebuild_rejects_a_duplicate_consolidator() -> None:
    with pytest.raises(ValueError, match="twice"):
        rebuild(_ledger(), [(EvidencedBy(), {}), (EvidencedBy(version="2"), {})], recorded_at=TX)


def test_finding_id_is_content_derived_and_records_are_sorted() -> None:
    a = ConsolidationFinding("x.y", Severity.INFO, "m", (_rid(2), _rid(1)))
    b = ConsolidationFinding("x.y", Severity.INFO, "m", (_rid(1), _rid(2), _rid(1)))
    assert a.id == b.id and a.records == tuple(sorted({_rid(1), _rid(2)}))
    assert a.id != ConsolidationFinding("x.y", Severity.INFO, "other", a.records).id
    with pytest.raises(ValueError, match="producer"):
        ConsolidationFinding("nodot", Severity.INFO, "m")
    with pytest.raises(ValueError, match="representable"):
        ConsolidationFinding("x.y", Severity.INFO, "m", details={"v": float("nan")})
