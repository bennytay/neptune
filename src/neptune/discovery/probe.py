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

The job runs ``probe`` for one source in one sandboxed call (ADR 0033 §1): every adapter's probe
and the container inspection, whose decoders read hostile bytes, run confined. The reply is
``SourceProbe.to_json()``; ``source_probe_from_json`` rebuilds it in the job and derives again
everything the job can (the sniff, the selection, the conclusion), refusing a reply that differs
from what the engine would have written. If the call dies or hits a limit, ``probe_head`` asks
each adapter again, one call each, and leaves the container unopened with an
``inspection_failed`` finding.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeAlias

from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    Adapter,
    Documented,
    ProbeHints,
    ProbeResult,
    SourceReader,
    probe_result_from_json,
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
    container_report_from_json,
    inspect_container,
    selection_to_json,
)
from neptune.discovery.sniff import Signature, Sniff, declared_signatures, sniff, sniff_from_json
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding, ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import (
    FindingCategory,
    IngestFinding,
    Severity,
    ingest_finding_from_json,
)
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath, SourceLocation

PROBE_ID: Final = "neptune.probe"
# 0.2.0: inspection_failed, and adapter_failed names a crash or a limit (ADR 0033 §1)
PROBE_VERSION: Final = "0.2.0"

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        f"{PROBE_ID}.adapter_failed",
        "an adapter's probe raised, returned the wrong type, crashed or hit a sandbox limit"
        " (details: error, signal, exit_status, reply, or limit and value); it is left out of"
        " this source's candidates (failed, error)",
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
        f"{PROBE_ID}.inspection_failed",
        "the sandboxed inspection of a container died or hit a limit; its adapters were asked"
        " again one by one and the container was not opened (failed, warning)",
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


# Why an adapter gave no probe result: its exception's class (``{"error": ...}``), or, when the
# probe ran in the sandbox, the cause the runner names (a signal, an exit status, a malformed
# reply, or a limit and its value).
ProbeFailure: TypeAlias = JsonObject
# How the engine asks one adapter to probe one head.
Ask: TypeAlias = Callable[[Adapter, bytes, ProbeHints], "ProbeResult | ProbeFailure"]
_CONCLUDED: Final = frozenset(
    f"{PROBE_ID}.{name}" for name in ("ambiguous", "name_mismatch", "unsupported")
)


def ask_in_process(adapter: Adapter, head: bytes, hints: ProbeHints) -> ProbeResult | ProbeFailure:
    """Ask ``adapter`` here, isolating whatever it raises: only the exception's class is kept."""
    try:
        result = adapter.probe(head, hints)
        if not isinstance(result, ProbeResult):
            raise TypeError(f"probe returned {type(result).__name__}")
    except Exception as exc:  # isolation is the point: a probe must not fail the job
        return {"error": type(exc).__name__}
    return result


def _failure_text(cause: ProbeFailure) -> str:
    if "signal" in cause:
        return f"killed by {cause['signal']}"
    if "exit_status" in cause:
        return f"exit status {cause['exit_status']}"
    if "limit" in cause:
        return f"stopped at its {cause['limit']} limit"
    if "reply" in cause:
        return "a reply that does not decode"
    return str(cause.get("error", "failed"))


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
        self._codes = frozenset(code.name for code in FINDING_CODES)
        self._extensions: dict[str, list[str]] = {}
        for adapter_id, descriptor in registry.descriptors().items():
            for spec in descriptor.formats:
                for extension in spec.extensions:
                    self._extensions.setdefault(extension, []).append(adapter_id)

    def probe(self, reader: SourceReader, name: str = "", head: bytes | None = None) -> SourceProbe:
        """Probe one source. ``name`` is its last location name, advisory; ``""`` if none.

        ``head`` is the source's first ``min(size, PROBE_HEAD_SIZE)`` bytes when the caller has
        read them already (the job, whose sandboxed child inherits them); otherwise they are read.
        """
        size = reader.size
        if head is None:
            head = reader.read(0, min(size, PROBE_HEAD_SIZE))
        if len(head) != min(size, PROBE_HEAD_SIZE):
            raise ValueError(
                f"{reader.content_id}: the reader gave {len(head)} head bytes of a {size}-byte"
                " source"
            )
        findings: list[IngestFinding] = []
        whole = EvidenceRef(reader.content_id, (ByteRange(0, size),))
        sniffed = sniff(head, size, self._signatures)
        probes, selection = self._select(
            head, ProbeHints(name, size), whole, findings, ask_in_process
        )
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

    def probe_head(
        self, source: ContentId, size: int, name: str, head: bytes, ask: Ask, failed: JsonObject
    ) -> SourceProbe:
        """Probe one source from its head alone, asking each adapter through ``ask``: what the job
        does when the sandboxed ``probe`` of the source died or hit a limit (``failed`` names the
        cause). A container is left unopened, with an ``inspection_failed`` finding; an adapter
        whose own probe fails again is an ``adapter_failed`` finding naming the cause.
        """
        if len(head) != min(size, PROBE_HEAD_SIZE):
            raise ValueError(f"{source}: {len(head)} head bytes of a {size}-byte source")
        findings: list[IngestFinding] = []
        whole = EvidenceRef(source, (ByteRange(0, size),))
        sniffed = sniff(head, size, self._signatures)
        probes, selection = self._select(head, ProbeHints(name, size), whole, findings, ask)
        if sniffed.container is not None:
            self._report(
                findings,
                "inspection_failed",
                FindingCategory.FAILED,
                Severity.WARNING,
                whole,
                f"the inspection of a {sniffed.container} container failed"
                f" ({_failure_text(failed)}); it was not opened",
                {"container": str(sniffed.container), **failed},
            )
        self._conclude(findings, sniffed, probes, selection, name, whole, None)
        return SourceProbe(source, size, name, sniffed, probes, selection, None, tuple(findings))

    # --- Reading a sandboxed probe back --------------------------------------------------------

    def _candidate(self, data: JsonValue) -> Candidate:
        if not isinstance(data, dict) or data.keys() != {"adapter", "result", "version"}:
            raise ValueError("a probe is exactly adapter, result and version")
        adapter, version = data["adapter"], data["version"]
        descriptors = self.registry.descriptors()
        if not isinstance(adapter, str) or adapter not in descriptors:
            raise ValueError(f"no registered adapter {adapter!r}")
        if version != descriptors[adapter].version:
            raise ValueError(f"adapter {adapter} is not at version {version!r}")
        result = probe_result_from_json(data["result"])
        return Candidate(adapter, descriptors[adapter].version, result)

    def _member_probe(self, data: JsonValue) -> MemberProbe:
        if not isinstance(data, dict) or data.keys() != {"selection", "sniff"}:
            raise ValueError("a member's probe is exactly selection and sniff")
        chosen = data["selection"]
        if not isinstance(chosen, dict) or not isinstance(chosen.get("candidates"), list):
            raise ValueError("a selection lists its candidates")
        candidates = [self._candidate(item) for item in chosen["candidates"]]
        return MemberProbe(sniff_from_json(data["sniff"], self._signatures), select(candidates))

    def _finding(self, data: JsonValue, source: ContentId) -> IngestFinding:
        finding = check_ingest_finding(ingest_finding_from_json(data))
        if finding.transform != self.transform.id or finding.code not in self._codes:
            raise ValueError(f"finding {finding.id} is not this engine's")
        subject = finding.subject
        if not isinstance(subject, EvidenceRef) or subject.source != source:
            raise ValueError(f"finding {finding.id} cites another source")
        return finding

    def source_probe_from_json(
        self, data: JsonValue, *, source: ContentId, size: int, name: str, head: bytes
    ) -> SourceProbe:
        """Read back the ``SourceProbe`` a sandboxed ``probe`` of this source returned.

        The reply comes from a process that read hostile bytes, so it is rebuilt from its parts
        and checked, never trusted: the sniff is taken again from ``head``, every adapter's
        result must come from a registered adapter at its version (or that adapter must have
        failed), the selection and the concluding findings are derived again, the container
        report and the other findings are parsed strictly and must cite this source, and the
        whole must be exactly what this engine writes. Anything else is a ``ValueError``.
        """
        keys = {"findings", "name", "probes", "selection", "size", "sniff", "source"}
        if not isinstance(data, dict) or not keys <= data.keys() <= keys | {"container"}:
            raise ValueError(f"a source's probe is exactly {sorted(keys)} and a container")
        if (data["source"], data["size"], data["name"]) != (source, size, name):
            raise ValueError("the probe is of another source")
        if not isinstance(data["probes"], list) or not isinstance(data["findings"], list):
            raise ValueError("a source's probes and findings are lists")
        whole = EvidenceRef(source, (ByteRange(0, size),))
        sniffed = sniff(head, size, self._signatures)
        probes = tuple(self._candidate(item) for item in data["probes"])
        container = (
            None
            if "container" not in data
            else container_report_from_json(
                data["container"], self._member_probe, self.policy.max_depth - 1, source
            )
        )
        findings = [self._finding(item, source) for item in data["findings"]]
        failed = sorted(
            str(f.details.get("adapter"))
            for f in findings
            if f.code == f"{PROBE_ID}.adapter_failed" and f.subject == whole
        )
        asked = [c.adapter for c in probes]
        if asked != sorted(asked) or sorted(asked + failed) != sorted(self.registry.descriptors()):
            raise ValueError("every registered adapter is asked once, in id order")
        kept = [f for f in findings if f.code not in _CONCLUDED]
        selection = select(probes)
        self._conclude(kept, sniffed, probes, selection, name, whole, container)
        probed = SourceProbe(source, size, name, sniffed, probes, selection, container, tuple(kept))
        if canonical_json.dumps(probed.to_json()) != canonical_json.dumps(data):
            raise ValueError("the probe is not what this engine writes for the source")
        return probed

    # --- Asking the adapters -------------------------------------------------------------------

    def _select(
        self,
        head: bytes,
        hints: ProbeHints,
        subject: EvidenceRef,
        findings: list[IngestFinding],
        ask: Ask,
    ) -> tuple[tuple[Candidate, ...], Selection]:
        """Every adapter's probe of ``head``, failures isolated, and the rule applied to them."""
        candidates: list[Candidate] = []
        for adapter in self.registry.adapters():
            descriptor = adapter.descriptor
            result = ask(adapter, head, hints)
            if not isinstance(result, ProbeResult):
                self._report(
                    findings,
                    "adapter_failed",
                    FindingCategory.FAILED,
                    Severity.ERROR,
                    subject,
                    f"adapter {descriptor.id} {descriptor.version} failed to probe"
                    f" ({_failure_text(result)}); it is not a candidate for this source",
                    {"adapter": descriptor.id, "version": descriptor.version, **result},
                )
                continue
            candidates.append(Candidate(descriptor.id, descriptor.version, result))
        return tuple(candidates), select(candidates)

    def _prober(
        self, findings: list[IngestFinding]
    ) -> Callable[[bytes, ProbeHints, EvidenceRef], MemberProbe]:
        def prober(head: bytes, hints: ProbeHints, subject: EvidenceRef) -> MemberProbe:
            _, selection = self._select(head, hints, subject, findings, ask_in_process)
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
