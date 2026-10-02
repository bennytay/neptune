"""What a manifest declares, set against what the job observes (ADR 0047 §5, §6).

The job takes a ``LoadedManifest`` as ``JobOptions.manifest``. ``Declarations`` turns it into what
the job uses, checked before any work starts:

- each adapter's config: the job's own, with the manifest's ``adapters`` options added (a key set
  in both is a configuration error, never a silent winner);
- one resolved config per ``sources`` rule: its adapter's, with the rule's options on top;
- the grouping config: the manifest's runs as declared sessions, under a grouping transform whose
  upstream is the manifest's transform, so every declared proposal leads back to the manifest.

Then, per source, ``choose`` applies the rules to what the probe engine observed. The last rule
matching a location applies to it; every location holding the artifact must get the same rule.

- The rule's adapter is one the probe engine ranked first: it is selected, nothing to say.
- It is among adapters that tie: it resolves the tie (``adapter_pinned``, info), and the probe's
  ``ambiguous`` finding, now answered, gives way to it (its candidates are in the details).
- It accepted the source but another ranked higher: it is selected, and
  ``pin_overrides_probe`` (inconsistent, warning) says whose ranking it overrode.
- Its probe declined the source: that contradicts the evidence, so it is not applied and
  ``pin_refused`` (inconsistent, warning) says so; the probe's own selection stands.
- Locations of one artifact matching different rules: ``rules_conflict`` (ambiguous, warning),
  and no rule applies.

A rule that matches no file is ``rule_unmatched`` (missing, warning). Every finding is the
manifest transform's, and cites the rule in the manifest's bytes by JSON pointer.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import AdapterConfig, ConfigError, configure
from neptune.adapters.registry import AdapterRegistry, Candidate, Selection, SelectionStatus
from neptune.derived.grouping import DEFAULT_GAP_SECONDS, GroupingConfig, LayoutGrouper
from neptune.derived.sessions import DeclaredSession
from neptune.identity.findings import ingest_finding
from neptune.manifest import MANIFEST_ID, LoadedManifest, ManifestError, SourceRule
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import LocalPath, RawLocalPath

ADAPTER_PINNED: Final = f"{MANIFEST_ID}.adapter_pinned"
PIN_OVERRIDES_PROBE: Final = f"{MANIFEST_ID}.pin_overrides_probe"
PIN_REFUSED: Final = f"{MANIFEST_ID}.pin_refused"
RULES_CONFLICT: Final = f"{MANIFEST_ID}.rules_conflict"
RULE_UNMATCHED: Final = f"{MANIFEST_ID}.rule_unmatched"
FINDING_CODES: Final = (
    ADAPTER_PINNED,
    PIN_OVERRIDES_PROBE,
    PIN_REFUSED,
    RULE_UNMATCHED,
    RULES_CONFLICT,
)

Location = LocalPath | RawLocalPath


@dataclass(frozen=True)
class Choice:
    """What the rules decided for one source: an adapter and config, or nothing to change.

    ``answers_tie`` is set when the choice settles a tie the probe engine reported, whose
    ``ambiguous`` finding then gives way to the manifest's.
    """

    candidate: Candidate | None = None
    config: AdapterConfig | None = None
    rule: SourceRule | None = None
    findings: tuple[IngestFinding, ...] = ()
    answers_tie: bool = False


@dataclass
class Declarations:
    """A manifest made ready for one job: configs resolved, rules compiled, grouping built."""

    loaded: LoadedManifest
    config: dict[str, dict[str, JsonValue]]
    rules: tuple[tuple[SourceRule, AdapterConfig], ...]
    grouping: GroupingConfig
    matched: set[int] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        loaded: LoadedManifest,
        registry: AdapterRegistry,
        config: Mapping[str, Mapping[str, JsonValue]],
        grouping: GroupingConfig,
    ) -> "Declarations":
        """Check the manifest against the registry and the job's own options; ``ManifestError``
        (or the adapter's ``ConfigError``) for anything that cannot be used."""
        manifest = loaded.manifest
        if grouping != GroupingConfig():
            raise ManifestError(
                "both the job's options and the manifest configure grouping; declare runs and "
                "gap_seconds in the manifest"
            )
        descriptors = registry.descriptors()
        merged = {adapter: dict(values) for adapter, values in config.items()}
        for adapter, options in manifest.adapters:
            if adapter not in descriptors:
                raise ManifestError(f"/adapters/{adapter}: no adapter {adapter!r} is registered")
            given = merged.setdefault(adapter, {})
            if both := sorted(set(given) & set(options)):
                raise ManifestError(
                    f"/adapters/{adapter}: options {both} are set by the job and the manifest"
                )
            given.update(options)
        rules: list[tuple[SourceRule, AdapterConfig]] = []
        for rule in manifest.sources:
            if rule.adapter not in descriptors:
                raise ManifestError(
                    f"{rule.pointer}/adapter: no adapter {rule.adapter!r} is registered; "
                    f"registered: {sorted(descriptors)}"
                )
            values = {**merged.get(rule.adapter, {}), **rule.options}
            try:
                resolved = configure(descriptors[rule.adapter], values)
            except ConfigError as exc:
                raise ConfigError(f"{rule.pointer}/options: {exc}") from exc
            rules.append((rule, resolved))
        sessions = tuple(DeclaredSession(run.name, run.paths) for run in manifest.runs)
        gap = manifest.gap_seconds if manifest.gap_seconds is not None else DEFAULT_GAP_SECONDS
        return cls(loaded, merged, tuple(rules), GroupingConfig(gap, sessions))

    def grouper(self) -> LayoutGrouper:
        return LayoutGrouper(self.grouping, upstream=(self.loaded.transform.id,))

    def configs(self) -> Iterable[AdapterConfig]:
        """Every config a rule resolves: plans made under them are reusable (``collect``)."""
        return (config for _, config in self.rules)

    def _finding(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        rule: SourceRule,
        message: str,
        details: Mapping[str, JsonValue],
        *,
        cite: bool,
    ) -> IngestFinding:
        cited = self.loaded.cite(rule.pointer)
        return ingest_finding(
            code=code,
            category=category,
            severity=severity,
            subject=subject,
            transform=self.loaded.transform,
            message=message,
            details={"manifest_pointer": rule.pointer, **details},
            related=(cited,) if cite else (),
        )

    def choose(
        self,
        source: ContentId,
        size: int,
        locations: Sequence[Location],
        selection: Selection,
    ) -> Choice:
        """Apply the rules to one source the probe engine selected for (module docstring)."""
        applied: dict[int, tuple[SourceRule, AdapterConfig]] = {}
        for location in locations:
            last: int | None = None
            for index, (rule, _) in enumerate(self.rules):
                if rule.matches(location):
                    self.matched.add(index)
                    last = index
            if last is not None:
                applied[last] = self.rules[last]
        if not applied:
            return Choice()
        whole = EvidenceRef(source, (ByteRange(0, size),))
        if len(applied) > 1:
            rules = [self.rules[i][0] for i in sorted(applied)]
            finding = self._finding(
                RULES_CONFLICT,
                FindingCategory.AMBIGUOUS,
                Severity.WARNING,
                whole,
                rules[0],
                "locations holding these bytes match manifest rules that disagree; none applies",
                {"rules": [r.pointer for r in rules]},
                cite=True,
            )
            return Choice(findings=(finding,))
        rule, config = next(iter(applied.values()))
        candidate = next((c for c in selection.candidates if c.adapter == rule.adapter), None)
        probe: dict[str, JsonValue] = {
            "adapter": rule.adapter,
            "probe_adapters": [c.adapter for c in selection.tied],
            "probe_confidence": selection.candidates[0].confidence if selection.candidates else 0,
            "probe_status": str(selection.status),
        }
        if candidate is None:
            finding = self._finding(
                PIN_REFUSED,
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                whole,
                rule,
                f"the manifest names adapter {rule.adapter} for this source, but its probe "
                "declines it; the probe's own selection stands",
                probe,
                cite=True,
            )
            return Choice(rule=rule, findings=(finding,))
        tie = selection.status is SelectionStatus.AMBIGUOUS
        findings: tuple[IngestFinding, ...] = ()
        if tie and candidate in selection.tied:
            findings = (
                self._finding(
                    ADAPTER_PINNED,
                    FindingCategory.AMBIGUOUS,
                    Severity.INFO,
                    whole,
                    rule,
                    f"adapters tie for this source; the manifest names {rule.adapter}",
                    probe,
                    cite=True,
                ),
            )
        elif candidate.adapter != selection.adapter:
            findings = (
                self._finding(
                    PIN_OVERRIDES_PROBE,
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    whole,
                    rule,
                    f"the manifest names {rule.adapter} (confidence {candidate.confidence}) over "
                    f"the probe's ranking ({', '.join(c.adapter for c in selection.tied)} at "
                    f"{selection.candidates[0].confidence})",
                    {**probe, "confidence": candidate.confidence},
                    cite=True,
                ),
            )
        return Choice(candidate, config, rule, findings, answers_tie=tie)

    def unmatched(self, locations: Iterable[Location]) -> tuple[IngestFinding, ...]:
        """A finding per rule that matches none of ``locations`` (every file the job read)."""
        listed = tuple(locations)
        out = []
        for index, (rule, _) in enumerate(self.rules):
            if index in self.matched or any(rule.matches(location) for location in listed):
                continue
            out.append(
                self._finding(
                    RULE_UNMATCHED,
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    self.loaded.cite(rule.pointer),
                    rule,
                    f"the manifest's rule for {rule.pattern!r} matches no file",
                    {"pattern": rule.pattern},
                    cite=False,
                )
            )
        return tuple(out)
