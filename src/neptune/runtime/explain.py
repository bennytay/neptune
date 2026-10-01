"""What an ingest would do, before it does it: the dry run's explanation (ADR 0044).

``IngestJob.dry_run`` builds an ``Explanation`` from what its first four phases saw: the inventory
the walk and the fingerprint found, every distinct source's detected format, every registered
adapter's verdict on it (probe confidence, the adapter's reasons, why it won or lost), the
adapter's ``inspect`` summary, the plan and what is left of it to parse, the session grouping with
its contested readings and ambiguous files, the work the run would do and the sources heavy enough
to say so, and everything the run would leave out, with why.

It is evidence about the plan, not a record: nothing here enters a package. ``to_json`` is a pure
function of the root's bytes and names, the adapters, the config and what the workspace already
holds (its committed chunks and saved plans); it carries no job id, clock, duration, absolute path
or host, so ``dumps`` is byte-identical for the same inputs. ``render`` is the same content as
lines for people.

It is bounded (``Bounds``, ADR 0044 §8): every list keeps at most ``entries`` items, each source
at most ``locations`` locations, each verdict ``reasons`` reasons, each container listing
``members`` members, each ``inspect`` summary ``summary_bytes`` canonical bytes and ``inspect``
findings; what a bound cuts is counted in a ``*_omitted`` field beside it, and one
``neptune.explain.truncated`` finding per bound that cut says where. Totals (bytes, sources, work)
are always over everything.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from neptune.adapters.contract import AdapterDescriptor, ProbeReason
from neptune.adapters.registry import SelectionStatus
from neptune.derived.grouping import Grouping
from neptune.derived.sessions import SessionProposal, Status, UnassignedFile
from neptune.discovery.probe import PROBE_ID, SourceProbe
from neptune.discovery.source import SkipReason
from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, FindingSubject, IngestFinding, Severity
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath
from neptune.runtime.cache import Rule
from neptune.runtime.sandbox import Limits

SCHEMA: Final = "neptune.explanation/1"
# A source is heavy when what is left of it to parse reads at least this many bytes...
HEAVY_BYTES: Final = 256 * 1024 * 1024
# ... or takes at least this many ``ingest`` calls, each a sandboxed fork.
HEAVY_CHUNKS: Final = 1024
ADAPTER_FAILED: Final = f"{PROBE_ID}.adapter_failed"
RENDER_WIDTH: Final = 160  # ``render`` abridges an inspect summary to this many characters
EXPLAIN_ID: Final = "neptune.explain"
EXPLAIN_VERSION: Final = "0.1.0"
TRUNCATED: Final = f"{EXPLAIN_ID}.truncated"


@dataclass(frozen=True)
class Bounds:
    """How much one explanation holds (ADR 0044 §8). ``entries`` bounds every list: inventory
    files, links and skipped entries, sources, left-out locations, session proposals, unassigned
    files, findings, and each adapter's selected sources."""

    entries: int = 10_000
    locations: int = 64  # per source, and per session proposal: members, links, peers, reasons
    reasons: int = 16
    members: int = 256
    summary_bytes: int = 16 * 1024
    inspect_findings: int = 64

    def __post_init__(self) -> None:
        for name in ("entries", "locations", "reasons", "members", "summary_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} is a positive integer, got {value!r}")
        found = self.inspect_findings
        if isinstance(found, bool) or not isinstance(found, int) or found < 0:
            raise ValueError(f"inspect_findings is a count, got {self.inspect_findings!r}")

    def to_json(self) -> JsonObject:
        return {
            "entries": self.entries,
            "inspect_findings": self.inspect_findings,
            "locations": self.locations,
            "members": self.members,
            "reasons": self.reasons,
            "summary_bytes": self.summary_bytes,
        }


DEFAULT_BOUNDS: Final = Bounds()


def explain_transform(bounds: Bounds) -> TransformRecord:
    """The explanation as a producer: its truncation findings name this transform."""
    return transform_record(
        adapter_id=EXPLAIN_ID, adapter_version=EXPLAIN_VERSION, config=bounds.to_json()
    )


Location = LocalPath | RawLocalPath


def _printable(text: str) -> str:
    """``text`` on one line with nothing a terminal acts on: C0 controls (a newline, ESC), DEL and
    C1 controls (U+009B CSI) as ``\\xNN`` escapes. Names and summaries are hostile."""
    return "".join(
        f"\\x{ord(ch):02x}" if ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 else ch for ch in text
    )


def show(location: Location) -> str:
    """A location for people, on one line: bytes that are not UTF-8, and control characters (a
    newline, an escape sequence: names are hostile), as ``\\xNN`` escapes."""
    text = (
        location.path
        if isinstance(location, LocalPath)
        else location.path.decode("utf-8", errors="backslashreplace")
    )
    return _printable(text)


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{count} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _abridge(text: str, width: int = RENDER_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


# --- The inventory -------------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryFile:
    """A location the scan found holding bytes, and the source those bytes are."""

    location: Location
    source: ContentId
    size: int

    def to_json(self) -> JsonObject:
        return {"location": self.location.to_json(), "size": self.size, "source": self.source}


@dataclass(frozen=True)
class InventoryLink:
    """A symlink the walk recorded and did not follow; ``target`` is its contents, unresolved."""

    location: Location
    target: bytes

    def to_json(self) -> JsonObject:
        return {"location": self.location.to_json(), "target_hex": self.target.hex()}


@dataclass(frozen=True)
class InventorySkipped:
    """An entry the walk or the fingerprint saw and did not read, and why (ADR 0029 §1)."""

    location: Location
    reason: SkipReason

    def to_json(self) -> JsonObject:
        return {"location": self.location.to_json(), "reason": str(self.reason)}


@dataclass(frozen=True)
class Inventory:
    """Everything the scan saw under the root, each list sorted by location bytes."""

    files: tuple[InventoryFile, ...]
    links: tuple[InventoryLink, ...]
    skipped: tuple[InventorySkipped, ...]
    bytes: int  # over every file, listed or omitted
    sources: int  # distinct sources: identical bytes at two locations are one
    files_omitted: int = 0
    links_omitted: int = 0
    skipped_omitted: int = 0

    @classmethod
    def of(
        cls,
        files: Iterable[InventoryFile],
        links: Iterable[InventoryLink],
        skipped: Iterable[InventorySkipped],
    ) -> "Inventory":
        """Everything, each list sorted by location bytes."""
        listed = tuple(sorted(files, key=lambda f: f.location.raw))
        return cls(
            listed,
            tuple(sorted(links, key=lambda link: link.location.raw)),
            tuple(sorted(skipped, key=lambda entry: (entry.location.raw, str(entry.reason)))),
            sum(entry.size for entry in listed),
            len({entry.source for entry in listed}),
        )

    def to_json(self) -> JsonObject:
        return {
            "bytes": self.bytes,
            "files": [entry.to_json() for entry in self.files],
            "files_omitted": self.files_omitted,
            "links": [link.to_json() for link in self.links],
            "links_omitted": self.links_omitted,
            "skipped": [entry.to_json() for entry in self.skipped],
            "skipped_omitted": self.skipped_omitted,
            "sources": self.sources,
        }


# --- One source ----------------------------------------------------------------------------------


class Verdict(StrEnum):
    """What the selection rule made of one adapter's probe of one source (ADR 0024 §7)."""

    SELECTED = "selected"  # the most confident claim, alone at the top
    TIED = "tied"  # shares the top confidence with another: none is chosen
    OUTRANKED = "outranked"  # claims the source, less confidently than the top
    DECLINED = "declined"  # confidence 0: not its format
    FAILED = "failed"  # its probe raised, crashed or hit a limit: out of the running


@dataclass(frozen=True)
class AdapterVerdict:
    """One registered adapter's word on one source, and why it won or lost."""

    adapter: str
    version: str
    verdict: Verdict
    confidence: float | None  # None when the probe failed
    format_version: str | None
    reasons: tuple[ProbeReason, ...]
    why: str
    failure: JsonObject | None = None
    reasons_omitted: int = 0

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "adapter": self.adapter,
            "reasons": [reason.to_json() for reason in self.reasons],
            "reasons_omitted": self.reasons_omitted,
            "verdict": str(self.verdict),
            "version": self.version,
            "why": self.why,
        }
        if self.confidence is not None:
            out["confidence"] = self.confidence
        if self.format_version is not None:
            out["format_version"] = self.format_version
        if self.failure is not None:
            out["failure"] = self.failure
        return out


def adapter_verdicts(
    probe: SourceProbe, descriptors: Mapping[str, AdapterDescriptor]
) -> tuple[AdapterVerdict, ...]:
    """Every registered adapter's verdict on ``probe``'s source, in adapter id order."""
    selection = probe.selection
    top = selection.candidates[0] if selection.candidates else None
    tied = (
        {c.adapter for c in selection.tied}
        if selection.status is SelectionStatus.AMBIGUOUS
        else set()
    )
    probes = {c.adapter: c for c in probe.probes}
    whole = EvidenceRef(probe.source, (ByteRange(0, probe.size),))
    failures: dict[str, JsonObject] = {}
    for finding in probe.findings:  # a member's failed probe is about the member, not this source
        named = finding.details.get("adapter")
        if finding.code == ADAPTER_FAILED and finding.subject == whole and isinstance(named, str):
            failures[named] = {
                k: v for k, v in finding.details.items() if k not in ("adapter", "version")
            }
    out = []
    for adapter_id in sorted(descriptors):
        descriptor = descriptors[adapter_id]
        candidate = probes.get(adapter_id)
        if candidate is None:
            cause: JsonObject = failures.get(adapter_id, {})
            why = (
                "its probe failed, so it is not a candidate ("
                + (", ".join(f"{k}={v}" for k, v in sorted(cause.items())) or "no reply")
                + ")"
            )
            out.append(
                AdapterVerdict(
                    adapter_id, descriptor.version, Verdict.FAILED, None, None, (), why, cause
                )
            )
            continue
        confidence = candidate.confidence
        if confidence == 0.0:
            codes = ", ".join(reason.code for reason in candidate.result.reasons)
            verdict = Verdict.DECLINED
            why = "confidence 0: not its format" + (f" ({codes})" if codes else "")
        elif adapter_id in tied:
            others = ", ".join(sorted(tied - {adapter_id}))
            verdict = Verdict.TIED
            why = f"ties at {confidence} with {others}; none is chosen until a manifest names one"
        elif top is not None and top.adapter == adapter_id:
            verdict = Verdict.SELECTED
            runner = next((c for c in selection.candidates[1:]), None)
            why = f"the most confident claim ({confidence})" + (
                f", above {runner.adapter} ({runner.confidence})" if runner else ", uncontested"
            )
        else:
            assert top is not None  # a positive claim is ranked, so there is a top
            verdict = Verdict.OUTRANKED
            why = f"claims it at {confidence}, below {top.adapter} at {top.confidence}"
        result = candidate.result
        out.append(
            AdapterVerdict(
                adapter_id,
                candidate.version,
                verdict,
                confidence,
                result.version,
                result.reasons,
                why,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class Inspection:
    """What the selected adapter's ``inspect`` said of the source, or why it said nothing.

    ``findings`` are what ``inspect`` saw on the way: shown here, never recorded, since an ingest
    finds them again (ADR 0024).
    """

    summary: JsonObject | None
    findings: tuple[IngestFinding, ...] = ()
    failure: JsonObject | None = None
    summary_omitted_bytes: int = 0  # the summary's canonical size, when a bound left it out
    findings_omitted: int = 0

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "findings": [f.to_json() for f in self.findings],
            "findings_omitted": self.findings_omitted,
            "summary_omitted_bytes": self.summary_omitted_bytes,
        }
        if self.summary is not None:
            out["summary"] = self.summary
        if self.failure is not None:
            out["failure"] = self.failure
        return out


@dataclass(frozen=True)
class PlanEstimate:
    """A source's plan, and how much of it a run would parse.

    ``cost`` is the bytes every chunk reads, as the adapter estimates them; ``to_parse`` the
    chunks the workspace has not committed and ``bytes_to_read`` their cost. ``rule`` is why the
    plan was made (``planned``: reused from the workspace, ADR 0031 §3).
    """

    transform: RecordId
    rule: Rule
    chunks: int
    committed: int
    cost: int
    bytes_to_read: int

    @property
    def to_parse(self) -> int:
        return self.chunks - self.committed

    def to_json(self) -> JsonObject:
        return {
            "bytes_to_read": self.bytes_to_read,
            "chunks": self.chunks,
            "committed": self.committed,
            "cost": self.cost,
            "rule": str(self.rule),
            "to_parse": self.to_parse,
            "transform": self.transform,
        }


@dataclass(frozen=True)
class Heavy:
    """Why one source's transform is heavy: a ``code`` (``large_input``, ``many_chunks``,
    ``memory_grows``, ``memory_limit``) and one line."""

    code: str
    message: str

    def to_json(self) -> JsonObject:
        return {"code": self.code, "message": self.message}


def heavy_reasons(
    size: int, plan: PlanEstimate, descriptor: AdapterDescriptor, limits: Limits | None
) -> tuple[Heavy, ...]:
    """Why parsing what is left of a source is expensive, by fixed thresholds (ADR 0044 §5).

    ``limits`` are the sandbox's, or ``None`` when adapters run in process.
    """
    if plan.to_parse == 0:
        return ()
    found = []
    if plan.bytes_to_read >= HEAVY_BYTES:
        found.append(
            Heavy("large_input", f"reads {_size(plan.bytes_to_read)} in {plan.to_parse} chunks")
        )
    if plan.to_parse >= HEAVY_CHUNKS:
        found.append(
            Heavy("many_chunks", f"{plan.to_parse} ingest calls, each a sandboxed process")
        )
    resources = descriptor.resources
    if not resources.streaming and size > resources.max_memory:
        found.append(
            Heavy(
                "memory_grows",
                f"{descriptor.id} does not stream: its memory grows with this {_size(size)}"
                f" source, past the {_size(resources.max_memory)} it declares",
            )
        )
    if limits is not None and resources.max_memory > limits.memory_bytes:
        found.append(
            Heavy(
                "memory_limit",
                f"{descriptor.id} declares {_size(resources.max_memory)} per call, above the"
                f" sandbox's {_size(limits.memory_bytes)}: chunks may hit the memory limit",
            )
        )
    return tuple(found)


class SourceStatus(StrEnum):
    """What a run would do with one source."""

    PLANNED = "planned"  # selected and planned: a run parses what is not committed
    QUARANTINED = "quarantined"  # selected, but planning or reading it failed: left out
    AMBIGUOUS = "ambiguous"  # adapters tie: left out until a manifest names one
    UNSUPPORTED = "unsupported"  # no adapter claims it
    UNREADABLE = "unreadable"  # it could not be read to probe


@dataclass(frozen=True)
class SourceExplanation:
    """One distinct source: where it is, what it is, who reads it and why, and what that costs."""

    source: ContentId
    size: int
    locations: tuple[Location, ...]
    status: SourceStatus
    probe: SourceProbe | None  # None when the source could not be read to probe
    adapter: str | None
    verdicts: tuple[AdapterVerdict, ...]
    inspection: Inspection | None
    plan: PlanEstimate | None
    heavy: tuple[Heavy, ...]
    quarantined: tuple[str, ...]  # the codes of the findings that took it out, in order
    locations_omitted: int = 0
    members_omitted: int = 0  # container members a bound left out of ``format.container``

    @property
    def whole(self) -> EvidenceRef:
        return EvidenceRef(self.source, (ByteRange(0, self.size),))

    def format_line(self) -> str:
        if self.probe is None:
            return "not read"
        container = self.probe.container
        line = self.probe.sniff.describe()
        if container is not None:
            line += f"; a {container.kind} container, {len(container.members)} members listed"
        return line

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "heavy": [reason.to_json() for reason in self.heavy],
            "locations": [location.to_json() for location in self.locations],
            "locations_omitted": self.locations_omitted,
            "quarantined": list(self.quarantined),
            "size": self.size,
            "source": self.source,
            "status": str(self.status),
            "verdicts": [verdict.to_json() for verdict in self.verdicts],
        }
        if self.adapter is not None:
            out["adapter"] = self.adapter
        if self.probe is not None:
            detected: dict[str, JsonValue] = {
                "description": self.format_line(),
                "sniff": self.probe.sniff.to_json(),
            }
            if self.probe.container is not None:
                container = dict(self.probe.container.to_json())
                members = container["members"]
                assert isinstance(members, Sequence)
                container["members"] = list(members[: len(members) - self.members_omitted])
                container["members_omitted"] = self.members_omitted
                detected["container"] = container
            out["format"] = detected
        if self.inspection is not None:
            out["inspection"] = self.inspection.to_json()
        if self.plan is not None:
            out["plan"] = self.plan.to_json()
        return out


# --- What is left out ----------------------------------------------------------------------------


class Disposition(StrEnum):
    """Why a run would not ingest something the scan saw."""

    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    UNREADABLE = "unreadable"
    QUARANTINED = "quarantined"
    SKIPPED = "skipped"  # the walk did not read it: a special file, an unreadable entry
    LINK = "link"  # a symlink: recorded, never followed


@dataclass(frozen=True)
class LeftOut:
    """One location a run would not ingest, and why."""

    location: Location
    disposition: Disposition
    reason: str
    source: ContentId | None = None

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "disposition": str(self.disposition),
            "location": self.location.to_json(),
            "reason": self.reason,
        }
        if self.source is not None:
            out["source"] = self.source
        return out


def left_out(sources: Iterable[SourceExplanation], inventory: Inventory) -> tuple[LeftOut, ...]:
    """Everything a run leaves out, by location bytes: sources no adapter reads, links and
    skipped entries."""
    found: list[LeftOut] = []
    for item in sources:
        if item.status is SourceStatus.PLANNED:
            continue
        if item.status is SourceStatus.AMBIGUOUS:
            tied = ", ".join(v.adapter for v in item.verdicts if v.verdict is Verdict.TIED)
            reason = f"adapters tie ({tied}); declare one in a manifest"
        elif item.status is SourceStatus.UNSUPPORTED:
            reason = f"no adapter claims it ({item.format_line()})"
            if item.probe is not None and item.probe.container is not None:
                reason += "; containers are listed, never extracted"
        else:
            reason = ", ".join(item.quarantined) or str(item.status)
        found.extend(
            LeftOut(location, Disposition(str(item.status)), reason, item.source)
            for location in item.locations
        )
    found.extend(
        LeftOut(link.location, Disposition.LINK, "a symlink: recorded, never followed")
        for link in inventory.links
    )
    found.extend(
        LeftOut(entry.location, Disposition.SKIPPED, f"not read: {entry.reason}")
        for entry in inventory.skipped
    )
    return tuple(sorted(found, key=lambda entry: (entry.location.raw, str(entry.disposition))))


# --- Grouping and work ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupingExplanation:
    """The proposed sessions, most confident first, and the files no reading places (ADR 0036)."""

    transform: RecordId
    proposals: tuple[SessionProposal, ...]
    unassigned: tuple[UnassignedFile, ...]
    summary: JsonObject  # over every proposal and file, listed or omitted
    proposals_omitted: int = 0
    unassigned_omitted: int = 0
    per_proposal: int | None = None  # the bound on each proposal's lists, once bounded

    def proposal_json(self, proposal: SessionProposal) -> JsonObject:
        """A proposal's record, every list in it (its own, and those inside its reasons' details)
        cut to ``per_proposal`` with a count beside it (``capped``)."""
        out = proposal.to_json()
        return out if self.per_proposal is None else capped(out, self.per_proposal)[0]

    def unassigned_json(self, entry: UnassignedFile) -> JsonObject:
        out = entry.to_json()
        return out if self.per_proposal is None else capped(out, self.per_proposal)[0]

    @classmethod
    def of(cls, grouping: Grouping) -> "GroupingExplanation":
        return cls(
            grouping.transform.id, grouping.ranked(), grouping.unassigned, grouping.summary()
        )

    def to_json(self) -> JsonObject:
        return {
            "proposals": [self.proposal_json(proposal) for proposal in self.proposals],
            "proposals_omitted": self.proposals_omitted,
            "summary": self.summary,
            "transform": self.transform,
            "unassigned": [self.unassigned_json(entry) for entry in self.unassigned],
            "unassigned_omitted": self.unassigned_omitted,
        }


@dataclass(frozen=True)
class WorkEstimate:
    """What a run would do after the dry run's phases, summed over planned sources.

    ``calls`` are the adapter calls this dry run made; ``ingest_calls`` the least a run makes
    (one per chunk left to parse; a retry is another).
    """

    sources: int
    sources_to_parse: int
    chunks: int
    committed: int
    bytes_to_read: int
    calls: JsonObject

    @property
    def ingest_calls(self) -> int:
        return self.chunks - self.committed

    def to_json(self) -> JsonObject:
        return {
            "bytes_to_read": self.bytes_to_read,
            "calls": self.calls,
            "chunks": self.chunks,
            "committed": self.committed,
            "ingest_calls": self.ingest_calls,
            "sources": self.sources,
            "sources_to_parse": self.sources_to_parse,
        }


def work_estimate(sources: Iterable[SourceExplanation], calls: JsonObject) -> WorkEstimate:
    plans = [item.plan for item in sources if item.plan is not None]
    return WorkEstimate(
        sources=len(plans),
        sources_to_parse=sum(1 for plan in plans if plan.to_parse),
        chunks=sum(plan.chunks for plan in plans),
        committed=sum(plan.committed for plan in plans),
        bytes_to_read=sum(plan.bytes_to_read for plan in plans),
        calls=calls,
    )


# --- The whole explanation -----------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterUse:
    """A registered adapter and the sources it was selected for, by id."""

    descriptor: AdapterDescriptor
    selected: tuple[ContentId, ...]
    selected_omitted: int = 0

    def to_json(self) -> JsonObject:
        return {
            "id": self.descriptor.id,
            "selected": list(self.selected),
            "selected_omitted": self.selected_omitted,
            "summary": self.descriptor.summary,
            "version": self.descriptor.version,
        }


@dataclass(frozen=True)
class Explanation:
    """What a run of this job would do, and why (ADR 0044).

    ``sources`` are sorted by their first location's bytes; ``adapters`` names every registered
    adapter with the sources it was selected for; ``ambiguities`` are the findings that leave a
    reading open (category ``ambiguous``): tied adapters, contested sessions, files several
    sessions could hold. ``findings`` are every finding the dry run made, by id.
    """

    inventory: Inventory
    sources: tuple[SourceExplanation, ...]
    adapters: tuple[AdapterUse, ...]
    grouping: GroupingExplanation
    work: WorkEstimate
    left_out: tuple[LeftOut, ...]
    findings: tuple[IngestFinding, ...]
    bounds: Bounds = DEFAULT_BOUNDS
    sources_omitted: int = 0
    left_out_omitted: int = 0
    findings_omitted: int = 0

    @property
    def ambiguities(self) -> tuple[IngestFinding, ...]:
        return tuple(f for f in self.findings if f.category is FindingCategory.AMBIGUOUS)

    @property
    def heavy(self) -> tuple[SourceExplanation, ...]:
        return tuple(item for item in self.sources if item.heavy)

    def to_json(self) -> JsonObject:
        return {
            "adapters": [use.to_json() for use in self.adapters],
            "ambiguities": [finding.id for finding in self.ambiguities],
            "bounds": self.bounds.to_json(),
            "findings": [finding.to_json() for finding in self.findings],
            "findings_omitted": self.findings_omitted,
            "grouping": self.grouping.to_json(),
            "heavy": [item.source for item in self.heavy],
            "inventory": self.inventory.to_json(),
            "left_out": [entry.to_json() for entry in self.left_out],
            "left_out_omitted": self.left_out_omitted,
            "schema": SCHEMA,
            "sources": [item.to_json() for item in self.sources],
            "sources_omitted": self.sources_omitted,
            "work": self.work.to_json(),
        }

    def dumps(self) -> bytes:
        """Canonical JSON: byte-identical for the same root, adapters, config and workspace."""
        return canonical_json.dumps(self.to_json())

    def render(self) -> str:
        """The explanation as lines for people; the same facts as ``to_json``, abridged."""
        inv, work = self.inventory, self.work
        files = len(inv.files) + inv.files_omitted
        links, skipped = len(inv.links) + inv.links_omitted, len(inv.skipped) + inv.skipped_omitted
        lines = [
            f"Inventory: {files} files ({_size(inv.bytes)}) holding {inv.sources}"
            f" distinct sources; {links} links; {skipped} skipped",
            "",
            "Sources:",
        ]
        for item in self.sources:
            where = show(item.locations[0]) + (
                f" (+{copies} copies)"
                if (copies := len(item.locations) + item.locations_omitted - 1)
                else ""
            )
            lines.append(f"  {where}  [{item.status}]  {item.format_line()}")
            for verdict in item.verdicts:
                lines.append(f"    {verdict.adapter:<12} {verdict.verdict:<9} {verdict.why}")
            if item.inspection is not None and item.inspection.summary is not None:
                summary = canonical_json.dumps(item.inspection.summary).decode()
                lines.append(f"    inspect: {_abridge(summary)}")
            elif item.inspection is not None and item.inspection.failure is not None:
                failure = canonical_json.dumps(item.inspection.failure).decode()
                lines.append(f"    inspect failed: {_abridge(failure)}")
            if item.plan is not None:
                plan = item.plan
                lines.append(
                    f"    plan ({plan.rule}): {plan.chunks} chunks, {plan.committed} committed,"
                    f" {_size(plan.bytes_to_read)} to read"
                )
        counts = self.grouping.summary
        lines += [
            "",
            f"Sessions (inferred from names and folders, ADR 0036): {counts['proposals']}"
            f" proposals ({counts['contested']} contested), {counts['ambiguous']} ambiguous and"
            f" {counts['unknown']} unplaced files",
        ]
        for proposal in self.grouping.proposals:
            place = proposal.directory
            where = "the root" if not isinstance(place, LocalPath | RawLocalPath) else show(place)
            contested = " contested" if proposal.status is Status.CONTESTED else ""
            lines.append(
                f"  [{proposal.assertion_kind} {proposal.confidence} {proposal.rule}{contested}]"
                f" {where}:"
                f" {len(proposal.members)} files; "
                + _abridge("; ".join(reason.message for reason in proposal.reasons))
            )
        for entry in self.grouping.unassigned:
            lines.append(f"  unplaced {show(entry.location)} ({entry.placement}): {entry.reason}")
        lines += [
            "",
            f"Work: {work.sources_to_parse} of {work.sources} planned sources to parse,"
            f" {work.ingest_calls} of {work.chunks} chunks ({_size(work.bytes_to_read)}) left",
        ]
        for item in self.heavy:
            for reason in item.heavy:
                lines.append(f"  heavy: {show(item.locations[0])}: {reason.code}: {reason.message}")
        if self.left_out:
            lines += ["", "Left out:"]
            lines += [
                f"  {show(entry.location)}  [{entry.disposition}]  {entry.reason}"
                for entry in self.left_out
            ]
        if self.ambiguities:
            lines += ["", "Ambiguities:"]
            lines += [f"  {finding.code}: {finding.message}" for finding in self.ambiguities]
        truncated = [f for f in self.findings if f.code == TRUNCATED]
        if truncated:
            lines += ["", "Truncated:"]
            lines += [f"  {finding.message}" for finding in truncated]
        return "\n".join(_printable(line) for line in lines) + "\n"


# --- Bounds --------------------------------------------------------------------------------------


class _Cuts:
    """The truncation findings one bounding makes: one per list a bound cut."""

    def __init__(self, transform: TransformRecord) -> None:
        self.transform = transform
        self.findings: list[IngestFinding] = []

    def cut(
        self,
        what: str,
        subject: FindingSubject,
        kept: int,
        omitted: int,
        limit: int,
        unit: str,
        sources: int | None = None,
        among: str = "sources",
    ) -> None:
        """``what`` kept ``kept`` and omitted ``omitted``; per-source bounds name how many
        ``sources`` they cut in, and ``kept`` is then the bound each was cut to."""
        if omitted <= 0:
            return
        where = f" in {sources} {among}" if sources is not None else ""
        details: dict[str, JsonValue] = {
            "kept": kept,
            "limit": limit,
            "list": what,
            "omitted": omitted,
        }
        if sources is not None:
            details["sources"] = sources
        self.findings.append(
            ingest_finding(
                code=TRUNCATED,
                category=FindingCategory.LIMIT,
                severity=Severity.INFO,
                subject=subject,
                transform=self.transform,
                message=f"{what}: {omitted} {unit} omitted{where} past the bound of {limit};"
                " the subject is the first one cut",
                details=details,
            )
        )


def bounded(explanation: Explanation, bounds: Bounds | None = None) -> Explanation:
    """``explanation`` within ``bounds`` (``DEFAULT_BOUNDS`` when ``None``, read at call time):
    each list cut to its bound, each cut counted beside it and named by one
    ``neptune.explain.truncated`` finding, which is always listed."""
    bounds = bounds if bounds is not None else DEFAULT_BOUNDS
    cuts = _Cuts(explain_transform(bounds))
    n = bounds.entries
    inv = explanation.inventory
    for what, entries in (("inventory.files", inv.files), ("inventory.links", inv.links),
                          ("inventory.skipped", inv.skipped)):  # fmt: skip
        if len(entries) > n:
            cuts.cut(what, entries[n].location, n, len(entries) - n, n, "entries")
    inventory = replace(
        inv,
        files=inv.files[:n],
        links=inv.links[:n],
        skipped=inv.skipped[:n],
        files_omitted=max(0, len(inv.files) - n),
        links_omitted=max(0, len(inv.links) - n),
        skipped_omitted=max(0, len(inv.skipped) - n),
    )

    per_source: dict[str, tuple[FindingSubject, int, int]] = {}  # what -> first, sources, total

    def note(what: str, item: SourceExplanation, omitted: int) -> None:
        if omitted > 0:
            first, count, total = per_source.get(what, (item.whole, 0, 0))
            per_source[what] = (first, count + 1, total + omitted)

    kept_sources = []
    for item in explanation.sources[:n]:
        locations = item.locations[: bounds.locations]
        note("source.locations", item, len(item.locations) - len(locations))
        verdicts = []
        for verdict in item.verdicts:
            reasons = verdict.reasons[: bounds.reasons]
            note("verdict.reasons", item, len(verdict.reasons) - len(reasons))
            verdicts.append(
                replace(
                    verdict, reasons=reasons, reasons_omitted=len(verdict.reasons) - len(reasons)
                )
            )
        members = 0
        if item.probe is not None and item.probe.container is not None:
            members = max(0, len(item.probe.container.members) - bounds.members)
            note("container.members", item, members)
        inspection = item.inspection
        if inspection is not None:
            if inspection.summary is not None:
                size = len(canonical_json.dumps(inspection.summary))
                if size > bounds.summary_bytes:
                    note("inspection.summary", item, 1)
                    inspection = replace(inspection, summary=None, summary_omitted_bytes=size)
            extra = len(inspection.findings) - bounds.inspect_findings
            if extra > 0:
                note("inspection.findings", item, extra)
                inspection = replace(
                    inspection,
                    findings=inspection.findings[: bounds.inspect_findings],
                    findings_omitted=extra,
                )
        kept_sources.append(
            replace(
                item,
                locations=locations,
                locations_omitted=len(item.locations) - len(locations),
                verdicts=tuple(verdicts),
                members_omitted=members,
                inspection=inspection,
            )
        )
    for what, (first, count, total) in sorted(per_source.items()):
        limit = getattr(bounds, _PER_SOURCE[what])
        cuts.cut(what, first, limit, total, limit, "items", sources=count)
    sources = explanation.sources
    if len(sources) > n:
        cuts.cut("sources", sources[n].whole, n, len(sources) - n, n, "sources")

    by_id = {item.source: item for item in sources}
    adapters = []
    for use in explanation.adapters:
        if len(use.selected) > n:
            chosen = by_id[use.selected[n]]
            cuts.cut(f"adapters.{use.descriptor.id}.selected", chosen.whole, n,
                     len(use.selected) - n, n, "sources")  # fmt: skip
        adapters.append(
            replace(use, selected=use.selected[:n], selected_omitted=max(0, len(use.selected) - n))
        )

    left = explanation.left_out
    if len(left) > n:
        cuts.cut("left_out", left[n].location, n, len(left) - n, n, "locations")
    grouping = explanation.grouping
    if len(grouping.proposals) > n:
        proposal = grouping.proposals[n]
        place = proposal.directory
        subject: FindingSubject | None = (
            proposal.members[0].location
            if proposal.members
            else place
            if isinstance(place, LocalPath | RawLocalPath)
            else None
        )
        if subject is None and inv.files:
            subject = inv.files[0].location
        if subject is not None:
            cuts.cut("grouping.proposals", subject, n, len(grouping.proposals) - n, n, "proposals")
    if len(grouping.unassigned) > n:
        cuts.cut("grouping.unassigned", grouping.unassigned[n].location, n,
                 len(grouping.unassigned) - n, n, "files")  # fmt: skip
    per = bounds.locations
    over = [
        (proposal, cut)
        for proposal in grouping.proposals[:n]
        if (cut := capped(proposal.to_json(), per)[1])
    ]
    first_cut = over[0][0] if over else None
    named: FindingSubject | None = None
    if first_cut is not None:
        named = first_cut.members[0].location if first_cut.members else None
        if named is None and inv.files:
            named = inv.files[0].location
    if named is not None:
        omitted = sum(cut for _, cut in over)
        cuts.cut("grouping.proposal_lists", named, per, omitted, per, "items",
                 sources=len(over), among="proposals")  # fmt: skip
    loose = [
        (entry, cut)
        for entry in grouping.unassigned[:n]
        if (cut := capped(entry.to_json(), per)[1])
    ]
    if loose:
        cuts.cut("grouping.unassigned_lists", loose[0][0].location, per,
                 sum(cut for _, cut in loose), per, "items", sources=len(loose),
                 among="files")  # fmt: skip
    grouping = replace(
        grouping,
        per_proposal=per,
        proposals=grouping.proposals[:n],
        unassigned=grouping.unassigned[:n],
        proposals_omitted=max(0, len(grouping.proposals) - n),
        unassigned_omitted=max(0, len(grouping.unassigned) - n),
    )
    findings = explanation.findings
    if len(findings) > n:
        cuts.cut("findings", findings[n].subject, n, len(findings) - n, n, "findings")
    kept = findings[:n] + tuple(cuts.findings)
    return replace(
        explanation,
        inventory=inventory,
        sources=tuple(kept_sources),
        adapters=tuple(adapters),
        grouping=grouping,
        left_out=left[:n],
        findings=tuple(sorted(kept, key=lambda f: f.id)),
        bounds=bounds,
        sources_omitted=max(0, len(sources) - n),
        left_out_omitted=max(0, len(left) - n),
        findings_omitted=max(0, len(findings) - n),
    )


def capped(value: JsonObject, bound: int) -> tuple[JsonObject, int]:
    """``value`` with every list in it, at any depth, cut to ``bound`` entries, and how many
    entries were cut. A list under a key gets ``<key>_omitted`` beside it, always; what is cut
    from a list inside a list counts toward the nearest such key."""

    def walk(node: JsonValue) -> tuple[JsonValue, int]:
        if isinstance(node, Mapping):
            out: dict[str, JsonValue] = {}
            total = 0
            for key, item in node.items():
                kept, cut = walk(item)
                out[key] = kept
                if isinstance(item, Sequence) and not isinstance(item, str):
                    out[f"{key}_omitted"] = cut
                total += cut
            return out, total
        if isinstance(node, Sequence) and not isinstance(node, str):
            kept_items, total = [], max(0, len(node) - bound)
            for item in node[:bound]:
                kept, cut = walk(item)
                kept_items.append(kept)
                total += cut
            return kept_items, total
        return node, 0

    out, total = walk(value)
    assert isinstance(out, Mapping)
    return out, total


_PER_SOURCE: Final = {
    "container.members": "members",
    "inspection.findings": "inspect_findings",
    "inspection.summary": "summary_bytes",
    "source.locations": "locations",
    "verdict.reasons": "reasons",
}
