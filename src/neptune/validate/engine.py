"""The integrity and data-quality engine: rules run over a stored package, findings out (ADR 0054).

Validation reads a package the store has verified (``read_package``) and never a raw source or an
adapter: it checks what the canonical records and series say, across sources, after every chunk
has landed. Each rule is deterministic and versioned; each finding it makes is an
``IngestFinding`` from the validator's own ``TransformRecord`` (``neptune.validate`` at
``VALIDATOR_VERSION``, its config the rule versions and bounds), cites the evidence it rests on
(``subject`` and ``related``) and names the records it qualifies. The job adds the findings, and
the transform with them, to the package, so the receipt lists them like any other finding. A
package with nothing to report is unchanged: the transform enters only with a finding.

Severity is fixed per rule and ranked ``error > warning > info``
(``neptune.model.package.SEVERITY_ORDER``, the receipt's order). No rule
judges a value against a unit or limit the evidence does not declare.

The work is bounded by the package: every rule is linear (or ``n log n``) in the records it reads,
series are read in batches of a few columns, and the output is capped (``Bounds``): at most
``findings_per_rule`` findings a rule, each naming at most ``records_per_finding`` records and
``related_per_finding`` other citations. A cut is counted in the finding (``records_omitted``) or
reported once (``neptune.validate.findings_capped``).
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, FindingSubject, IngestFinding, Severity
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, TransformRecord
from neptune.store.package import IngestPackage

VALIDATOR_ID: Final = "neptune.validate"
# Changes whenever a rule is added or removed; each rule's own version changes with its logic.
VALIDATOR_VERSION: Final = "0.1.0"
CODE_PREFIX: Final = f"{VALIDATOR_ID}."
FINDINGS_CAPPED: Final = f"{VALIDATOR_ID}.findings_capped"


@dataclass(frozen=True)
class Bounds:
    """What one validation may emit, whatever the package holds (ADR 0054 §5)."""

    findings_per_rule: int = 256
    records_per_finding: int = 256
    related_per_finding: int = 16
    values_per_detail: int = 16
    batch_rows: int = 65_536

    def __post_init__(self) -> None:
        for name, value in self.to_json().items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def to_json(self) -> JsonObject:
        return {
            "batch_rows": self.batch_rows,
            "findings_per_rule": self.findings_per_rule,
            "records_per_finding": self.records_per_finding,
            "related_per_finding": self.related_per_finding,
            "values_per_detail": self.values_per_detail,
        }


@dataclass(frozen=True)
class Draft:
    """What a rule found, before the engine caps it and derives the finding's id."""

    subject: FindingSubject
    message: str
    details: Mapping[str, JsonValue]
    related: Sequence[EvidenceRef] = ()
    records: Iterable[RecordId] = ()


@dataclass(frozen=True)
class Inputs:
    """Inputs a rule needs that no record kind on main carries yet (ADR 0054 §4).

    Each is a ``Protocol`` in ``neptune.validate.pending``; a rule over one is off by default and
    reported as not covered. Tests, and the issue that lands the kind, supply them.
    """

    limits: tuple[Any, ...] = ()
    document_revisions: tuple[Any, ...] = ()
    run_software: tuple[Any, ...] = ()


class Context:
    """A package indexed once for every rule: records by kind and id, sizes, foreign findings."""

    def __init__(self, package: IngestPackage, bounds: Bounds, inputs: Inputs) -> None:
        self.package = package
        self.bounds = bounds
        self.inputs = inputs
        self.memo: dict[str, Any] = {}  # work two rules share, done once
        self.by_kind: dict[str, list[Any]] = defaultdict(list)
        self.by_id: dict[str, Any] = {}
        for record in package.records:
            self.by_kind[record.kind].append(record)
            identifier = getattr(record, "id", None)
            if isinstance(identifier, str):
                self.by_id[identifier] = record
        self.sizes: dict[str, int] = {
            artifact.content_id: artifact.size for artifact in self.by_kind["source_artifact"]
        }
        own = {
            transform.id
            for transform in self.by_kind["transform_record"]
            if transform.adapter_id == VALIDATOR_ID
        }
        # A rule reads the other producers' findings, never its own: validating twice adds nothing.
        self.findings: list[IngestFinding] = [
            finding for finding in self.by_kind["ingest_finding"] if finding.transform not in own
        ]

    def records(self, kind: str) -> list[Any]:
        return self.by_kind.get(kind, [])

    def whole(self, source: str) -> EvidenceRef | None:
        """The whole of a source the package lists, as evidence."""
        size = self.sizes.get(source)
        return None if size is None else EvidenceRef(ContentId(source), (ByteRange(0, size),))


def evidence_of(record: Any, knowledge: Any = None) -> EvidenceRef:
    """Where a field's value was read: its own provenance, else its record's."""
    provenance = getattr(knowledge, "provenance", None)
    if isinstance(provenance, Provenance):
        return provenance.evidence
    evidence: EvidenceRef = record.provenance.evidence
    return evidence


def source_of(record: Any) -> str | None:
    """The content id an evidence record was read from; ``None`` for an unfetched object."""
    provenance = getattr(record, "provenance", None)
    if not isinstance(provenance, Provenance):
        return None
    source = provenance.evidence.source
    return source if isinstance(source, str) else None


def short(identifier: str) -> str:
    """An id as people read it in a message: its prefix and 12 digits."""
    prefix, _, digest = identifier.rpartition(":")
    return f"{'rec' if prefix.startswith('rec') else prefix}:{digest[:12]}"


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True)
class Rule:
    """One check: its code, version, fixed category and severity, and what it reads.

    ``not_covered`` says why the rule is off: the record kind it reads is not on main. Such a rule
    runs only when its input is supplied (``Inputs``).
    """

    name: str
    version: int
    category: FindingCategory
    severity: Severity
    summary: str
    check: Callable[[Context], Iterable[Draft]]
    not_covered: str | None = None

    @property
    def code(self) -> str:
        return CODE_PREFIX + self.name

    @property
    def key(self) -> str:
        return f"{self.code}/{self.version}"


@dataclass(frozen=True)
class RuleOutcome:
    """What one rule did: ran (and how many findings it made, how many it left out) or not."""

    code: str
    version: int
    covered: bool
    findings: int = 0
    omitted: int = 0
    reason: str | None = None

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "code": self.code,
            "covered": self.covered,
            "findings": self.findings,
            "omitted": self.omitted,
            "version": self.version,
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class ValidationReport:
    """The findings, the transform that made them (``None`` when there are none), and coverage."""

    transform: TransformRecord
    findings: tuple[IngestFinding, ...]
    rules: tuple[RuleOutcome, ...] = field(default=())

    def records(self) -> tuple[Any, ...]:
        """What the package gains: the transform and its findings, or nothing."""
        return (self.transform, *self.findings) if self.findings else ()

    def summary(self) -> JsonObject:
        by_severity = {str(s): sum(1 for f in self.findings if f.severity is s) for s in Severity}
        return {
            "findings": len(self.findings),
            "not_covered": sorted(r.code for r in self.rules if not r.covered),
            "rules": sum(1 for r in self.rules if r.covered),
            "severity": by_severity,
        }


def validator_transform(rules: Iterable[Rule], bounds: Bounds) -> TransformRecord:
    """The validator under one rule set and bounds: who a validation finding names."""
    config: dict[str, JsonValue] = {
        "bounds": bounds.to_json(),
        "rules": {rule.code: rule.version for rule in sorted(rules, key=lambda r: r.code)},
    }
    return transform_record(
        adapter_id=VALIDATOR_ID, adapter_version=VALIDATOR_VERSION, config=config
    )


def _finding(rule: Rule, draft: Draft, transform: TransformRecord, bounds: Bounds) -> IngestFinding:
    records = sorted(set(draft.records))
    details = dict(draft.details)
    details["rule"] = rule.key
    if len(records) > bounds.records_per_finding:
        details["records_omitted"] = len(records) - bounds.records_per_finding
        records = records[: bounds.records_per_finding]
    related: list[EvidenceRef] = []
    seen = {draft.subject}
    dropped = 0
    for ref in draft.related:
        if ref in seen:
            continue
        seen.add(ref)
        if len(related) < bounds.related_per_finding:
            related.append(ref)
        else:
            dropped += 1
    if dropped:
        details["related_omitted"] = dropped
    return ingest_finding(
        code=rule.code,
        category=rule.category,
        severity=rule.severity,
        subject=draft.subject,
        transform=transform,
        message=draft.message,
        details=details,
        related=related,
        records=records,
    )


def _capped(
    rule: Rule, emitted: int, omitted: int, subject: FindingSubject, transform: TransformRecord
) -> IngestFinding:
    return ingest_finding(
        code=FINDINGS_CAPPED,
        category=FindingCategory.LIMIT,
        severity=Severity.INFO,
        subject=subject,
        transform=transform,
        message=f"{rule.code} reported {emitted} findings and left {omitted} more out",
        details={"emitted": emitted, "omitted": omitted, "rule": rule.key},
    )


def _drafts(rule: Rule, context: Context) -> Iterator[Draft]:
    yield from rule.check(context)


def validate_package(
    package: IngestPackage,
    rules: Sequence[Rule] | None = None,
    *,
    bounds: Bounds | None = None,
    inputs: Inputs | None = None,
) -> ValidationReport:
    """Run ``rules`` (by default every rule whose inputs are on main) over a verified package.

    Deterministic: the same package, rules and bounds give the same findings, in id order.
    """
    from neptune.validate import pending  # the rules import this module
    from neptune.validate.rules import ALL_RULES, DEFAULT_RULES

    inputs = inputs or Inputs()
    if rules is None:  # a pending rule runs when its input is supplied
        supplied = [r for r in pending.RULES if getattr(inputs, pending.NEEDS[r.name])]
        rules = (*DEFAULT_RULES, *supplied)
    chosen = tuple(rules)
    if len({rule.code for rule in chosen}) != len(chosen):
        raise ValueError("each rule may run once")
    bounds = bounds or Bounds()
    context = Context(package, bounds, inputs)
    transform = validator_transform(chosen, bounds)
    findings: dict[RecordId, IngestFinding] = {}
    outcomes: list[RuleOutcome] = []
    for rule in sorted(chosen, key=lambda r: r.code):
        made = omitted = 0
        first_omitted: FindingSubject | None = None
        for draft in _drafts(rule, context):
            if made < bounds.findings_per_rule:
                finding = _finding(rule, draft, transform, bounds)
                if finding.id not in findings:
                    findings[finding.id] = finding
                    made += 1
            else:
                omitted += 1
                first_omitted = first_omitted or draft.subject
        if first_omitted is not None:
            capped = _capped(rule, made, omitted, first_omitted, transform)
            findings[capped.id] = capped
        outcomes.append(RuleOutcome(rule.code, rule.version, True, made, omitted))
    ran = {rule.code for rule in chosen}
    for rule in ALL_RULES:
        if rule.code not in ran:
            outcomes.append(
                RuleOutcome(rule.code, rule.version, False, reason=rule.not_covered or "not run")
            )
    ordered = tuple(findings[key] for key in sorted(findings))
    return ValidationReport(transform, ordered, tuple(sorted(outcomes, key=lambda o: o.code)))
