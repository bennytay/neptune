"""The consolidator contract, claim stamping and rebuild (ADR 0003 §2-§4).

A consolidator is a pure function ``(LedgerReader, previous claims, resolved config) -> drafts +
findings``. It never sets ids or provenance: ``run_consolidator`` hashes the config, stamps the
transform and the Ledger transaction onto each draft to build a ``schema.Claim`` (whose id covers
the transform, so a new version or config is a sibling claim), canonicalises evidence and record
order, refuses drafts that break the contract or the vocabulary as findings, and sorts the output.
``rebuild`` folds an ordered consolidator set over one Ledger snapshot; same snapshot, set,
versions and configs give byte-identical canonical JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from neptune.identity import canonical_json
from neptune.identity.ids import config_hash, record_id
from neptune.model.finding import Severity
from neptune.model.ids import (
    ConfigHash,
    RecordId,
    check_text,
    check_token,
    parse_config_hash,
)
from neptune.model.knowledge import Knowledge, NotApplicable
from neptune_memory.schema.claim import Claim, ClaimProvenance, is_inferred
from neptune_memory.schema.interval import OPEN, LedgerTx, Open
from neptune_memory.schema.predicates import CORE_PREDICATES, PredicateRegistry, violations

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.provenance import EvidenceRef
    from neptune.model.time import Timestamp
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import ClaimAssertionKind, ClaimId, ClaimObject
    from neptune_memory.schema.nodes import NodeRef

# Record kind hashed into finding ids. Changing it re-lineages every finding: new ADR.
FINDING_KIND: Final = "memory.finding"
MAX_MESSAGE: Final = 1000


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
    """What produced a build's claims: consolidator id, version, config hash and model, if any."""

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


def _not_applicable() -> Knowledge[float]:
    return NotApplicable()


@dataclass(frozen=True)
class ClaimDraft:
    """A ``Claim`` without what the runner stamps: transform, transaction time, canonical order.

    ``evidence`` and ``records`` may come in any order and with repeats; the runner de-duplicates
    and sorts them, so the order a consolidator found its inputs in never changes a claim id.
    Validation is ``Claim``'s: a draft it refuses becomes a finding.
    """

    subject: NodeRef
    predicate: str
    object: ClaimObject
    valid_from: Timestamp
    assertion_kind: ClaimAssertionKind
    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    valid_to: Timestamp | Open = OPEN
    confidence: Knowledge[float] = field(default_factory=_not_applicable)


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
class Consolidator(Protocol):
    """A pure function of the Ledger view, earlier claims and its resolved config (ADR 0003 §2).

    ``config`` is resolved: defaults already filled in, so an explicit default and an omitted one
    hash the same. A model-based consolidator's config holds ``"model": model.to_json()`` so the
    model is in every claim's ``config_hash``. Implementations must not read the clock,
    randomness, the network or files.
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
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput: ...


@dataclass(frozen=True)
class Consolidation:
    """One consolidator's output in one build: claims and findings, each sorted by id."""

    transform: ConsolidatorTransform
    claims: tuple[Claim, ...]
    findings: tuple[ConsolidationFinding, ...]

    def to_json(self) -> JsonObject:
        return {
            "claims": [claim.to_json() for claim in self.claims],
            "findings": [finding.to_json() for finding in self.findings],
            "transform": self.transform.to_json(),
        }


def _safe_text(text: str) -> str:
    """Untrusted text (an exception message) kept only if it is one printable line of Unicode."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "(unprintable)"
    return text[:MAX_MESSAGE] if text.isprintable() else "(unprintable)"


def _finding(
    code: str, transform: ConsolidatorTransform, message: str, records: Sequence[RecordId] = ()
) -> ConsolidationFinding:
    return ConsolidationFinding(
        code=f"consolidate.{code}",
        severity=Severity.ERROR,
        message=_safe_text(message),
        records=tuple(records),
        details={"consolidator_id": transform.consolidator_id, "version": transform.version},
    )


def _evidence_key(ref: EvidenceRef) -> bytes:
    return canonical_json.dumps(ref.to_json())


def _stamp(
    draft: ClaimDraft,
    transform: ConsolidatorTransform,
    recorded_at: LedgerTx,
    registry: PredicateRegistry,
) -> Claim | ConsolidationFinding:
    try:
        records = tuple(sorted(set(draft.records)))
        if is_inferred(draft.assertion_kind) != (transform.model is not None):
            allowed = "inferred" if transform.model is not None else "observed or stated"
            message = f"{draft.predicate} claim is {draft.assertion_kind}; expected {allowed}"
            return _finding("wrong_assertion_kind", transform, message, records)
        claim = Claim(
            subject=draft.subject,
            predicate=draft.predicate,
            object=draft.object,
            valid_from=draft.valid_from,
            valid_to=draft.valid_to,
            recorded_at=recorded_at,
            assertion_kind=draft.assertion_kind,
            confidence=draft.confidence,
            provenance=ClaimProvenance(
                evidence=tuple(sorted(set(draft.evidence), key=_evidence_key)),
                records=records,
                consolidator_id=transform.consolidator_id,
                consolidator_version=transform.version,
                config_hash=transform.config_hash,
            ),
        )
    except (TypeError, ValueError) as exc:
        message = f"{draft.predicate!r} claim refused: {type(exc).__name__}: {exc}"
        return _finding("invalid_claim", transform, message)
    found = violations(claim, registry)
    if found:
        message = "; ".join(f"{v.code}: {v.message}" for v in found)
        return _finding("schema_violation", transform, message, records)
    return claim


def run_consolidator(
    consolidator: Consolidator,
    ledger: LedgerReader,
    previous: Sequence[Claim],
    config: Mapping[str, JsonValue],
    *,
    recorded_at: LedgerTx,
    registry: PredicateRegistry = CORE_PREDICATES,
) -> Consolidation:
    """Run one consolidator and stamp its output. Contract breaches become findings, not errors.

    Raises only for a caller error: a config that is not canonical JSON, or a model-based
    consolidator whose resolved config does not name its model.
    """
    model = consolidator.model
    if model is not None and config.get("model") != model.to_json():
        raise ValueError("a model-based consolidator's resolved config must hold its model")
    transform = ConsolidatorTransform(
        consolidator_id=consolidator.consolidator_id,
        version=consolidator.version,
        config_hash=config_hash(config),
        model=model,
    )
    try:
        output = consolidator.consolidate(ledger, tuple(previous), config)
    except Exception as exc:  # partial success: a crashing consolidator is a finding
        message = f"{type(exc).__name__}: {exc}"
        if _safe_text(message) != message:
            message = type(exc).__name__
        return Consolidation(transform, (), (_finding("failed", transform, message),))
    if not (
        isinstance(output, ConsolidatorOutput)
        and all(isinstance(d, ClaimDraft) for d in output.drafts)
        and all(isinstance(f, ConsolidationFinding) for f in output.findings)
    ):
        message = "consolidate() must return a ConsolidatorOutput of ClaimDrafts and findings"
        return Consolidation(transform, (), (_finding("bad_output", transform, message),))
    claims: dict[ClaimId, Claim] = {}
    findings: dict[RecordId, ConsolidationFinding] = {f.id: f for f in output.findings}
    for draft in output.drafts:
        stamped = _stamp(draft, transform, recorded_at, registry)
        if isinstance(stamped, Claim):
            claims[stamped.id] = stamped
        else:
            findings[stamped.id] = stamped
    return Consolidation(
        transform,
        tuple(claims[key] for key in sorted(claims)),
        tuple(findings[key] for key in sorted(findings)),
    )


def rebuild(
    ledger: LedgerReader,
    plan: Sequence[tuple[Consolidator, Mapping[str, JsonValue]]],
    *,
    recorded_at: LedgerTx,
    registry: PredicateRegistry = CORE_PREDICATES,
) -> tuple[Consolidation, ...]:
    """ADR 0003 §4: run ``plan`` in order; each consolidator sees only earlier ones' claims.

    ``recorded_at`` is the Ledger snapshot's transaction: when Memory learned these claims.
    """
    ids = [consolidator.consolidator_id for consolidator, _ in plan]
    if len(set(ids)) != len(ids):
        raise ValueError(f"a consolidator appears twice in the plan: {ids}")
    previous: list[Claim] = []
    results: list[Consolidation] = []
    for consolidator, config in plan:
        result = run_consolidator(
            consolidator, ledger, previous, config, recorded_at=recorded_at, registry=registry
        )
        results.append(result)
        previous.extend(result.claims)
    return tuple(results)
