"""The consolidator contract, claim-id derivation and rebuild (ADR 0003 §2-§4).

A consolidator is a pure function ``(LedgerReader, previous claims, resolved config) -> drafts +
findings``. It never sets ids or provenance: ``run_consolidator`` hashes the config, derives every
claim id from the claim's lineage, rejects drafts that break the contract as findings, and sorts the
output. ``rebuild`` folds an ordered consolidator set over one Ledger snapshot; same snapshot, set,
versions and configs give byte-identical canonical JSON.

``ProposedClaim`` is the consolidator-side envelope until MVL-102's ``schema.Claim`` lands; the
schema builds its ``Claim`` from ``(id, draft, transform)``. It is not a second claim model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from neptune.identity import canonical_json
from neptune.identity.ids import config_hash, record_id
from neptune.model.finding import Severity
from neptune.model.ids import (
    ConfigHash,
    LogicalId,
    RecordId,
    check_text,
    check_token,
    parse_config_hash,
    parse_record_id,
)
from neptune.model.knowledge import KnowledgeState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune_memory.ledger import LedgerReader

# Record kinds hashed into claim and finding ids. Changing either re-lineages every claim: new ADR.
CLAIM_KIND: Final = "memory.claim"
FINDING_KIND: Final = "memory.finding"

AssertionKind = Literal["observed", "stated", "inferred"]
DETERMINISTIC_KINDS: Final[frozenset[str]] = frozenset({"observed", "stated"})
CLAIM_STATES: Final = (KnowledgeState.KNOWN, KnowledgeState.AMBIGUOUS)


@dataclass(frozen=True)
class ModelRef:
    """The model a ``derived/`` consolidator runs. Deterministic consolidators have none."""

    model_id: str
    model_version: str

    def __post_init__(self) -> None:
        check_text("model_id", self.model_id)
        check_text("model_version", self.model_version)

    def to_json(self) -> JsonObject:
        return {"model_id": self.model_id, "model_version": self.model_version}


@dataclass(frozen=True)
class ConsolidatorTransform:
    """What produced a claim: consolidator id, version, resolved-config hash and model, if any."""

    consolidator_id: str
    version: str
    config_hash: ConfigHash
    model: ModelRef | None = None

    def __post_init__(self) -> None:
        check_token("consolidator_id", self.consolidator_id)
        check_text("version", self.version)
        parse_config_hash(self.config_hash)

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "config_hash": self.config_hash,
            "consolidator_id": self.consolidator_id,
            "version": self.version,
        }
        if self.model is not None:
            out["model"] = self.model.to_json()
        return out


@dataclass(frozen=True)
class ClaimDraft:
    """What a consolidator asserts, before the runner stamps its id and transform.

    ``inputs`` are the Ledger record ids (and earlier claim ids) the claim rests on; they are
    stored sorted and de-duplicated, so the order a consolidator found them in never matters.
    """

    predicate: str
    subject: LogicalId
    object: JsonValue
    assertion_kind: AssertionKind
    inputs: tuple[RecordId, ...]
    state: KnowledgeState = KnowledgeState.KNOWN

    def __post_init__(self) -> None:
        check_token("predicate", self.predicate)
        if not isinstance(self.subject, LogicalId):
            raise TypeError(f"subject must be a LogicalId, got {type(self.subject).__name__}")
        canonical_json.dumps(self.object)  # CanonicalJsonError (a ValueError) if not representable
        if self.assertion_kind not in ("observed", "stated", "inferred"):
            raise ValueError(
                f"assertion_kind must be observed|stated|inferred: {self.assertion_kind!r}"
            )
        if self.state not in CLAIM_STATES:
            raise ValueError(f"a claim is known or ambiguous, not {self.state}")
        for value in self.inputs:
            parse_record_id(value)
        object.__setattr__(self, "inputs", tuple(sorted(set(self.inputs))))

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": self.assertion_kind,
            "inputs": list(self.inputs),
            "object": self.object,
            "predicate": self.predicate,
            "state": str(self.state),
            "subject": self.subject.to_json(),
        }


def claim_id(transform: ConsolidatorTransform, draft: ClaimDraft) -> RecordId:
    """ADR 0003 §3: the id over the claim's lineage. A version or config change is a sibling id."""
    inputs: dict[str, JsonValue] = {
        "config_hash": transform.config_hash,
        "consolidator_id": transform.consolidator_id,
        "consolidator_version": transform.version,
        "assertion_kind": draft.assertion_kind,
        "inputs": list(draft.inputs),
        "object": draft.object,
        "predicate": draft.predicate,
        "state": str(draft.state),
        "subject": draft.subject.to_json(),
    }
    if transform.model is not None:
        inputs["model"] = transform.model.to_json()
    return record_id(CLAIM_KIND, inputs)


@dataclass(frozen=True)
class ProposedClaim:
    """A draft with its derived id and transform: the triple MVL-102's ``Claim`` is built from."""

    id: RecordId
    draft: ClaimDraft
    transform: ConsolidatorTransform

    @property
    def predicate(self) -> str:
        return self.draft.predicate

    def to_json(self) -> JsonObject:
        return {"draft": self.draft.to_json(), "id": self.id, "transform": self.transform.to_json()}


@dataclass(frozen=True)
class ConsolidationFinding:
    """A problem a consolidator or the runner found. Its id is derived from its whole content."""

    code: str
    severity: Severity
    message: str
    records: tuple[RecordId, ...] = ()
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_token("code", self.code)
        if "." not in self.code:
            raise ValueError(f"finding code is <producer>.<name>: {self.code!r}")
        check_text("message", self.message)
        canonical_json.dumps(dict(self.details))  # CanonicalJsonError (a ValueError) if not
        object.__setattr__(self, "records", tuple(sorted(set(self.records))))
        object.__setattr__(self, "details", dict(self.details))

    @property
    def id(self) -> RecordId:
        return record_id(FINDING_KIND, self._content())

    def _content(self) -> JsonObject:
        return {
            "code": self.code,
            "details": dict(self.details),
            "message": self.message,
            "records": list(self.records),
            "severity": str(self.severity),
        }

    def to_json(self) -> JsonObject:
        return {"id": self.id, **self._content()}


@dataclass(frozen=True)
class ConsolidatorOutput:
    """What ``Consolidator.consolidate`` returns: drafts and findings, in any order."""

    drafts: tuple[ClaimDraft, ...] = ()
    findings: tuple[ConsolidationFinding, ...] = ()


@runtime_checkable
class PriorClaim(Protocol):
    """The fields of an earlier consolidator's claim a later one may rest on."""

    @property
    def id(self) -> RecordId: ...

    @property
    def predicate(self) -> str: ...


@runtime_checkable
class Consolidator(Protocol):
    """A pure function of the Ledger view, earlier claims and its resolved config (ADR 0003 §2).

    ``config`` is resolved: defaults already filled in, so an explicit default and an omitted one
    hash the same. Implementations must not read the clock, randomness, the network or files.
    """

    @property
    def consolidator_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def model(self) -> ModelRef | None: ...

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[PriorClaim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput: ...


@dataclass(frozen=True)
class Consolidation:
    """One consolidator's output in one build: claims sorted by id, findings sorted by id."""

    transform: ConsolidatorTransform
    claims: tuple[ProposedClaim, ...]
    findings: tuple[ConsolidationFinding, ...]

    def to_json(self) -> JsonObject:
        return {
            "claims": [claim.to_json() for claim in self.claims],
            "findings": [finding.to_json() for finding in self.findings],
            "transform": self.transform.to_json(),
        }


MAX_MESSAGE: Final = 1000


def _valid_unicode(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _runner_finding(
    code: str, transform: ConsolidatorTransform, message: str, records: Sequence[RecordId] = ()
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"consolidate.{code}",
        severity=Severity.ERROR,
        message=message,
        records=tuple(records),
        details={"consolidator_id": transform.consolidator_id, "version": transform.version},
    )


def _check_draft(
    draft: ClaimDraft, transform: ConsolidatorTransform
) -> ConsolidationFinding | None:
    if not draft.inputs:
        return _runner_finding(
            "claim_without_inputs", transform, f"{draft.predicate} claim names no input records"
        )
    allowed = DETERMINISTIC_KINDS if transform.model is None else frozenset({"inferred"})
    if draft.assertion_kind not in allowed:
        return _runner_finding(
            "wrong_assertion_kind",
            transform,
            f"{draft.predicate} claim is {draft.assertion_kind}; this consolidator may emit "
            + "|".join(sorted(allowed)),
            draft.inputs,
        )
    return None


def run_consolidator(
    consolidator: Consolidator,
    ledger: LedgerReader,
    previous: Sequence[PriorClaim],
    config: Mapping[str, JsonValue],
) -> Consolidation:
    """Run one consolidator and stamp its output. Contract breaches become findings, not errors."""
    transform = ConsolidatorTransform(
        consolidator_id=consolidator.consolidator_id,
        version=consolidator.version,
        config_hash=config_hash(config),
        model=consolidator.model,
    )
    try:
        output = consolidator.consolidate(ledger, tuple(previous), config)
    except Exception as exc:  # partial success: a crashing consolidator is a finding
        # The exception text is untrusted: keep it only if it is valid Unicode and one line.
        text = f"{type(exc).__name__}: {exc}"
        if "\n" in text or not text.isprintable() or not _valid_unicode(text):
            text = type(exc).__name__
        finding = _runner_finding("failed", transform, text[:MAX_MESSAGE])
        return Consolidation(transform, (), (finding,))
    if not (
        isinstance(output, ConsolidatorOutput)
        and all(isinstance(d, ClaimDraft) for d in output.drafts)
        and all(isinstance(f, ConsolidationFinding) for f in output.findings)
    ):
        message = "consolidate() must return ConsolidatorOutput of ClaimDrafts and findings"
        return Consolidation(transform, (), (_runner_finding("bad_output", transform, message),))
    claims: dict[RecordId, ProposedClaim] = {}
    findings: dict[RecordId, ConsolidationFinding] = {f.id: f for f in output.findings}
    for draft in output.drafts:
        problem = _check_draft(draft, transform)
        if problem is not None:
            findings[problem.id] = problem
            continue
        identifier = claim_id(transform, draft)
        claims[identifier] = ProposedClaim(identifier, draft, transform)
    return Consolidation(
        transform,
        tuple(claims[key] for key in sorted(claims)),
        tuple(findings[key] for key in sorted(findings)),
    )


def rebuild(
    ledger: LedgerReader, plan: Sequence[tuple[Consolidator, Mapping[str, JsonValue]]]
) -> tuple[Consolidation, ...]:
    """ADR 0003 §4: run ``plan`` in order; each consolidator sees only earlier ones' claims."""
    ids = [consolidator.consolidator_id for consolidator, _ in plan]
    if len(set(ids)) != len(ids):
        raise ValueError(f"a consolidator appears twice in the plan: {ids}")
    previous: list[ProposedClaim] = []
    results: list[Consolidation] = []
    for consolidator, config in plan:
        result = run_consolidator(consolidator, ledger, previous, config)
        results.append(result)
        previous.extend(result.claims)
    return tuple(results)
