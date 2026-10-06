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

Memory does not grow with the records (ADR 0070): rules read each kind from the package's table as
they iterate it, never an index of the whole package, and the one join over every record (which
references resolve) is sorted in a ``SpillSpace`` under the caller's scratch. A rule holds the few
records it groups (streams, runs, clocks, configurations, frames, entities) and what it reports.
"""

from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import (
    MAX_MESSAGE_LENGTH,
    FindingCategory,
    FindingSubject,
    IngestFinding,
    Severity,
)
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, TransformRecord
from neptune.store.package import IngestPackage, PackageError, records_of

VALIDATOR_ID: Final = "neptune.validate"
# Changes whenever a rule is added or removed; each rule's own version changes with its logic.
VALIDATOR_VERSION: Final = "0.2.0"
CODE_PREFIX: Final = f"{VALIDATOR_ID}."
FINDINGS_CAPPED: Final = f"{VALIDATOR_ID}.findings_capped"
RULE_FAILED: Final = f"{VALIDATOR_ID}.rule_failed"


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
class Omitted:
    """``count`` more findings a rule did not draft, past its cap; ``subject`` is the first's.

    A rule yields it after ``findings_per_rule`` drafts when counting the rest is cheaper than
    drafting them, so the engine never builds a draft only to count it.
    """

    count: int
    subject: FindingSubject


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
    """A package as every rule reads it: records by kind, read from their tables as they are
    iterated (never held whole, ADR 0070), source sizes, and the other producers' findings.

    ``spill`` is a directory of the caller's (a workspace's scratch space) where a rule that must
    sort what it reads, such as ``dangling_reference``, spills; without one it sorts in memory.
    """

    def __init__(
        self, package: IngestPackage, bounds: Bounds, inputs: Inputs, spill: Path | None = None
    ) -> None:
        self.package = package
        self.bounds = bounds
        self.inputs = inputs
        self.spill = spill
        self.memo: dict[str, Any] = {}  # work two rules share, done once
        self.sizes: dict[str, int] = {
            artifact.content_id: artifact.size for artifact in self.records("source_artifact")
        }
        # A rule reads the other producers' findings, never its own: validating twice adds nothing.
        self.own = frozenset(
            transform.id
            for transform in self.records("transform_record")
            if transform.adapter_id == VALIDATOR_ID
        )

    def records(self, kind: str) -> Collection[Any]:
        """The package's records of one kind, in table order (sorted by id), read as iterated."""
        return records_of(self.package.records, kind)

    @property
    def findings(self) -> Iterator[IngestFinding]:
        """The other producers' findings, in id order: a fresh pass each time it is read."""
        return (f for f in self.records("ingest_finding") if f.transform not in self.own)

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
    check: Callable[[Context], Iterable[Draft | Omitted]]
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


def _one_line(message: str) -> str:
    """A message as one printable line of at most ``MAX_MESSAGE_LENGTH`` characters: rules quote
    what the evidence states (ids, names), which may hold anything."""
    text = "".join(ch if ch.isprintable() else "\ufffd" for ch in message)
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[: MAX_MESSAGE_LENGTH - 1] + "\u2026"
    return text or "-"


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
        message=_one_line(draft.message),
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


def _type_name(value: object) -> str:
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


def _failed(
    rule: Rule, exc: Exception, context: Context, transform: TransformRecord
) -> IngestFinding | None:
    """A rule that raised: a finding about the package's first source; its drafts are dropped.

    It names the exception's class, never its text (which may hold paths or addresses). A
    package without sources has nothing to cite, and the report alone says the rule failed.
    """
    sources = sorted(context.sizes)
    subject = context.whole(sources[0]) if sources else None
    if subject is None:
        return None
    return ingest_finding(
        code=RULE_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.WARNING,
        subject=subject,
        transform=transform,
        message=f"{rule.code} failed ({_type_name(exc)}); what it checks is unchecked here",
        details={"exception": _type_name(exc), "rule": rule.key},
    )


def _drafts(rule: Rule, context: Context) -> Iterator[Draft | Omitted]:
    yield from rule.check(context)


def validate_package(
    package: IngestPackage,
    rules: Sequence[Rule] | None = None,
    *,
    bounds: Bounds | None = None,
    inputs: Inputs | None = None,
    spill: Path | None = None,
) -> ValidationReport:
    """Run ``rules`` (by default every rule whose inputs are on main) over a verified package.

    Deterministic: the same package, rules and bounds give the same findings, in id order. The
    records are read from the package's tables as each rule iterates them; ``spill`` is where a
    rule sorts what it must (``Context``), so memory does not grow with the records.
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
    context = Context(package, bounds, inputs, spill)
    transform = validator_transform(chosen, bounds)
    findings: dict[RecordId, IngestFinding] = {}
    outcomes: list[RuleOutcome] = []
    for rule in sorted(chosen, key=lambda r: r.code):
        made = omitted = 0
        first_omitted: FindingSubject | None = None
        mine: dict[RecordId, IngestFinding] = {}
        try:
            for draft in _drafts(rule, context):
                if isinstance(draft, Omitted):
                    omitted += draft.count
                    first_omitted = first_omitted or draft.subject
                elif made < bounds.findings_per_rule:
                    finding = _finding(rule, draft, transform, bounds)
                    if finding.id not in mine and finding.id not in findings:
                        mine[finding.id] = finding
                        made += 1
                else:
                    omitted += 1
                    first_omitted = first_omitted or draft.subject
        except PackageError:
            raise  # the package's files changed or vanished under a rule: not the rule's fault
        except Exception as exc:  # one rule's fault never costs the package (non-negotiable 7)
            failure = _failed(rule, exc, context, transform)
            if failure is not None:
                findings[failure.id] = failure
            outcomes.append(
                RuleOutcome(rule.code, rule.version, False, reason=f"failed: {_type_name(exc)}")
            )
            continue
        findings.update(mine)
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
