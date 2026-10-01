"""The probe engine: sniff a source, ask every adapter, rank, open containers, report (ADR 0027).

Stage 3 of the pipeline. For one source the engine:

1. reads the head (the first ``min(size, PROBE_HEAD_SIZE)`` bytes) and nothing else of the
   source, except the bounded tail and member heads a container inspection needs;
2. sniffs it: signatures and text class, observations that name what the bytes look like;
3. gives the head to every registered adapter, isolating a probe that raises (a finding, and
   that adapter is out of this source's candidates), and applies the registry's selection rule
   unchanged: confidence, then adapter id; a tie is ``ambiguous``, no claim is ``unsupported``;
4. if the head sniffs as a container, lists its members within ``ProbePolicy`` and probes each
   member's head the same way, so the report says what the container holds;
5. reports: an ``ambiguous`` or ``unsupported`` source is a finding that says what was seen and
   who claimed what; a name whose extension belongs to another adapter's format is an
   ``info`` finding; container problems are findings cited to the bytes concerned.

The engine is a producer in its own right: its findings name its ``TransformRecord``
(``neptune.probe`` at ``PROBE_VERSION`` with the policy as config), so they enter a package like
any adapter's and the receipt shows who looked at a source nobody read. Everything is a pure
function of the bytes, the registry and the policy.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    Documented,
    ProbeHints,
    ProbeResult,
    SourceReader,
)
from neptune.adapters.registry import (
    AdapterRegistry,
    Candidate,
    Selection,
    SelectionStatus,
    select,
)
from neptune.discovery.containers import (
    ContainerReport,
    MemberProbe,
    ProbePolicy,
    inspect_container,
    selection_to_json,
)
from neptune.discovery.sniff import Signature, Sniff, declared_signatures, sniff
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath, SourceLocation

PROBE_ID: Final = "neptune.probe"
PROBE_VERSION: Final = "0.1.0"

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        f"{PROBE_ID}.adapter_failed",
        "an adapter's probe raised; it is left out of this source's candidates (failed, error)",
    ),
    Documented(
        f"{PROBE_ID}.ambiguous",
        "adapters tie at the top confidence; none is chosen until a manifest names one"
        " (ambiguous, error)",
    ),
    Documented(
        f"{PROBE_ID}.container_corrupt",
        "a container's structure does not parse; the listing stops there (corrupt, warning)",
    ),
    Documented(
        f"{PROBE_ID}.container_limit",
        "a policy limit stopped an inspection; details.limit is members, bytes, depth or ratio"
        " (limit, warning)",
    ),
    Documented(
        f"{PROBE_ID}.container_not_inspected",
        "a container or member is recognised but not decoded: no decoder, encrypted, or an"
        " unknown method (unsupported, info)",
    ),
    Documented(
        f"{PROBE_ID}.name_mismatch",
        "the name's extension belongs to another adapter's format; the bytes decided"
        " (inconsistent, info)",
    ),
    Documented(
        f"{PROBE_ID}.unsupported",
        "no adapter claims the source; the message says what sniffing saw (unsupported, error)",
    ),
)


def hint_name(location: SourceLocation) -> str:
    """The last name of a location, as ``ProbeHints`` wants it: text, whatever the bytes were."""
    if isinstance(location, LocalPath):
        return location.parts[-1]
    if isinstance(location, RawLocalPath):
        return location.path.rsplit(b"/", 1)[-1].decode("utf-8", errors="replace")
    return location.object_id.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class SourceProbe:
    """Everything the engine found out about one source.

    ``probes`` holds every adapter's result in adapter id order, confidence 0 included, so an
    explanation can say why each adapter declined. ``selection`` is the rule's outcome over them.
    ``container`` is present when the head sniffed as a container the policy let the engine open.
    """

    source: ContentId
    size: int
    name: str
    sniff: Sniff
    probes: tuple[Candidate, ...]
    selection: Selection
    container: ContainerReport | None
    findings: tuple[IngestFinding, ...]

    @property
    def adapter(self) -> str | None:
        """The selected adapter's id, or ``None`` when the source is ambiguous or unsupported."""
        return self.selection.adapter

    def to_json(self) -> JsonObject:
        """The probe for explanations and dry runs (MVL-15); findings are the records' JSON."""
        out: dict[str, JsonValue] = {
            "findings": [finding.to_json() for finding in self.findings],
            "name": self.name,
            "probes": [
                {"adapter": c.adapter, "result": c.result.to_json(), "version": c.version}
                for c in self.probes
            ],
            "selection": selection_to_json(self.selection),
            "size": self.size,
            "sniff": self.sniff.to_json(),
            "source": self.source,
        }
        if self.container is not None:
            out["container"] = self.container.to_json()
        return out


class ProbeEngine:
    """Probes sources against one registry under one policy. Build one per job."""

    def __init__(self, registry: AdapterRegistry, policy: ProbePolicy | None = None) -> None:
        self.registry = registry
        self.policy = policy if policy is not None else ProbePolicy()
        self.transform: TransformRecord = transform_record(
            adapter_id=PROBE_ID, adapter_version=PROBE_VERSION, config=self.policy.to_json()
        )
        self._signatures: tuple[Signature, ...] = declared_signatures(
            registry.descriptors().values()
        )
        self._extensions: dict[str, list[str]] = {}
        for adapter_id, descriptor in registry.descriptors().items():
            for spec in descriptor.formats:
                for extension in spec.extensions:
                    self._extensions.setdefault(extension, []).append(adapter_id)

    def probe(self, reader: SourceReader, name: str = "") -> SourceProbe:
        """Probe one source. ``name`` is its last location name, advisory; ``""`` if none."""
        size = reader.size
        head = reader.read(0, min(size, PROBE_HEAD_SIZE))
        if len(head) != min(size, PROBE_HEAD_SIZE):
            raise ValueError(
                f"{reader.content_id}: the reader gave {len(head)} head bytes of a {size}-byte"
                " source"
            )
        findings: list[IngestFinding] = []
        whole = EvidenceRef(reader.content_id, (ByteRange(0, size),))
        sniffed = sniff(head, size, self._signatures)
        probes, selection = self._select(head, ProbeHints(name, size), whole, findings)
        container: ContainerReport | None = None
        if sniffed.container is not None:
            if self.policy.max_depth >= 1:
                container = inspect_container(
                    reader,
                    sniffed.container,
                    policy=self.policy,
                    prober=self._prober(findings),
                    report=self._reporter(findings),
                )
            else:
                self._report(
                    findings,
                    "container_limit",
                    FindingCategory.LIMIT,
                    Severity.WARNING,
                    whole,
                    f"a {sniffed.container} container is not opened (max_depth 0)",
                    {"container": str(sniffed.container), "limit": "depth", "max_depth": 0},
                )
        self._conclude(findings, sniffed, probes, selection, name, whole, container)
        return SourceProbe(
            reader.content_id, size, name, sniffed, probes, selection, container, tuple(findings)
        )

    # --- Asking the adapters -------------------------------------------------------------------

    def _select(
        self,
        head: bytes,
        hints: ProbeHints,
        subject: EvidenceRef,
        findings: list[IngestFinding],
    ) -> tuple[tuple[Candidate, ...], Selection]:
        """Every adapter's probe of ``head``, crashes isolated, and the rule applied to them."""
        candidates: list[Candidate] = []
        for adapter in self.registry.adapters():
            descriptor = adapter.descriptor
            try:
                result = adapter.probe(head, hints)
                if not isinstance(result, ProbeResult):
                    raise TypeError(f"probe returned {type(result).__name__}")
            except Exception as exc:  # isolation is the point: a probe must not fail the job
                self._report(
                    findings,
                    "adapter_failed",
                    FindingCategory.FAILED,
                    Severity.ERROR,
                    subject,
                    f"adapter {descriptor.id} {descriptor.version} failed to probe"
                    f" ({type(exc).__name__}); it is not a candidate for this source",
                    {
                        "adapter": descriptor.id,
                        "error": type(exc).__name__,
                        "version": descriptor.version,
                    },
                )
                continue
            candidates.append(Candidate(descriptor.id, descriptor.version, result))
        return tuple(candidates), select(candidates)

    def _prober(
        self, findings: list[IngestFinding]
    ) -> Callable[[bytes, ProbeHints, EvidenceRef], MemberProbe]:
        def prober(head: bytes, hints: ProbeHints, subject: EvidenceRef) -> MemberProbe:
            _, selection = self._select(head, hints, subject, findings)
            return MemberProbe(sniff(head, hints.size, self._signatures), selection)

        return prober

    # --- Findings ------------------------------------------------------------------------------

    def _report(
        self,
        findings: list[IngestFinding],
        name: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        message: str,
        details: JsonObject,
    ) -> None:
        findings.append(
            ingest_finding(
                code=f"{PROBE_ID}.{name}",
                category=category,
                severity=severity,
                subject=subject,
                transform=self.transform,
                message=message,
                details=details,
            )
        )

    def _reporter(
        self, findings: list[IngestFinding]
    ) -> Callable[[str, FindingCategory, Severity, EvidenceRef, str, JsonObject], None]:
        def report(
            name: str,
            category: FindingCategory,
            severity: Severity,
            subject: EvidenceRef,
            message: str,
            details: JsonObject,
        ) -> None:
            self._report(findings, name, category, severity, subject, message, details)

        return report

    def _name_suggests(self, name: str) -> tuple[str, tuple[str, ...]] | None:
        """The longest declared extension ``name`` ends with, and the adapters declaring it."""
        lowered = name.lower()
        best = ""
        for extension in self._extensions:
            if lowered.endswith(extension) and len(lowered) > len(extension):
                best = max(best, extension, key=len)
        return (best, tuple(sorted(self._extensions[best]))) if best else None

    def _conclude(
        self,
        findings: list[IngestFinding],
        sniffed: Sniff,
        probes: tuple[Candidate, ...],
        selection: Selection,
        name: str,
        whole: EvidenceRef,
        container: ContainerReport | None,
    ) -> None:
        suggests = self._name_suggests(name)
        if selection.status is SelectionStatus.AMBIGUOUS:
            tied = selection.tied
            self._report(
                findings,
                "ambiguous",
                FindingCategory.AMBIGUOUS,
                Severity.ERROR,
                whole,
                f"adapters {', '.join(c.adapter for c in tied)} all claim the source at"
                f" confidence {tied[0].confidence}; none is chosen until a manifest names one",
                {
                    "adapters": [c.adapter for c in tied],
                    "confidence": tied[0].confidence,
                    "reasons": {c.adapter: [r.code for r in c.result.reasons] for c in tied},
                    "signatures": [s.to_json() for s in sniffed.signatures],
                    "text": str(sniffed.text),
                },
            )
        elif selection.status is SelectionStatus.UNSUPPORTED:
            message = f"no adapter claims the source ({sniffed.describe()})"
            if container is not None:
                count = len(container.members)
                more = "" if container.complete else " or more"
                message += (
                    f"; a {container.kind} container holding {count}{more}"
                    f" member{'s' if count != 1 else ''}"
                )
            if suggests is not None:
                message += f"; the name suggests {', '.join(suggests[1])}, which declined"
            details: dict[str, JsonValue] = {
                "declined": {c.adapter: [r.code for r in c.result.reasons] for c in probes},
                "signatures": [s.to_json() for s in sniffed.signatures],
                "text": str(sniffed.text),
            }
            if suggests is not None:
                details["extension"] = suggests[0]
                details["name_suggests"] = list(suggests[1])
            self._report(
                findings,
                "unsupported",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                whole,
                message,
                details,
            )
        elif suggests is not None and selection.adapter not in suggests[1]:
            extension, adapters = suggests
            self._report(
                findings,
                "name_mismatch",
                FindingCategory.INCONSISTENT,
                Severity.INFO,
                whole,
                f"the name ends with {extension}, an extension of {', '.join(adapters)}, but"
                f" the bytes were read as {selection.adapter}",
                {
                    "extension": extension,
                    "name_suggests": list(adapters),
                    "selected": selection.candidates[0].adapter,
                },
            )
