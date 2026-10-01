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
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from neptune.adapters.contract import AdapterDescriptor, ProbeReason
from neptune.adapters.registry import SelectionStatus
from neptune.derived.grouping import Grouping
from neptune.derived.sessions import SessionProposal, Status, UnassignedFile
from neptune.discovery.probe import PROBE_ID, SourceProbe
from neptune.discovery.source import SkipReason
from neptune.identity import canonical_json
from neptune.model.finding import FindingCategory, IngestFinding
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import LocalPath, RawLocalPath
from neptune.runtime.cache import Rule
from neptune.runtime.sandbox import Limits

SCHEMA: Final = "neptune.explanation/1"
# A source is heavy when what is left of it to parse reads at least this many bytes...
HEAVY_BYTES: Final = 256 * 1024 * 1024
# ... or takes at least this many ``ingest`` calls, each a sandboxed fork.
HEAVY_CHUNKS: Final = 1024
ADAPTER_FAILED: Final = f"{PROBE_ID}.adapter_failed"

Location = LocalPath | RawLocalPath


def show(location: Location) -> str:
    """A location for people: its path, with bytes that are not UTF-8 escaped."""
    if isinstance(location, LocalPath):
        return location.path
    return location.path.decode("utf-8", errors="backslashreplace")


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{count} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


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

    @property
    def bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    @property
    def sources(self) -> int:
        """Distinct sources: identical bytes at two locations are one."""
        return len({entry.source for entry in self.files})

    def to_json(self) -> JsonObject:
        return {
            "bytes": self.bytes,
            "files": [entry.to_json() for entry in self.files],
            "links": [link.to_json() for link in self.links],
            "skipped": [entry.to_json() for entry in self.skipped],
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

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "adapter": self.adapter,
            "reasons": [reason.to_json() for reason in self.reasons],
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

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"findings": [f.to_json() for f in self.findings]}
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

    def format_line(self) -> str:
        if self.probe is None:
            return "not read"
        container = self.probe.container
        line = self.probe.sniff.describe()
        if container is not None:
            line += f"; a {container.kind} of {len(container.members)} listed members"
        return line

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "heavy": [reason.to_json() for reason in self.heavy],
            "locations": [location.to_json() for location in self.locations],
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
                detected["container"] = self.probe.container.to_json()
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
    summary: JsonObject

    @classmethod
    def of(cls, grouping: Grouping) -> "GroupingExplanation":
        return cls(
            grouping.transform.id, grouping.ranked(), grouping.unassigned, grouping.summary()
        )

    def to_json(self) -> JsonObject:
        return {
            "proposals": [proposal.to_json() for proposal in self.proposals],
            "summary": self.summary,
            "transform": self.transform,
            "unassigned": [entry.to_json() for entry in self.unassigned],
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
class Explanation:
    """What a run of this job would do, and why (ADR 0044).

    ``sources`` are sorted by their first location's bytes; ``adapters`` names every registered
    adapter with the sources it was selected for; ``ambiguities`` are the findings that leave a
    reading open (category ``ambiguous``): tied adapters, contested sessions, files several
    sessions could hold. ``findings`` are every finding the dry run made, by id.
    """

    inventory: Inventory
    sources: tuple[SourceExplanation, ...]
    adapters: tuple[tuple[AdapterDescriptor, tuple[ContentId, ...]], ...]
    grouping: GroupingExplanation
    work: WorkEstimate
    left_out: tuple[LeftOut, ...]
    findings: tuple[IngestFinding, ...]

    @property
    def ambiguities(self) -> tuple[IngestFinding, ...]:
        return tuple(f for f in self.findings if f.category is FindingCategory.AMBIGUOUS)

    @property
    def heavy(self) -> tuple[SourceExplanation, ...]:
        return tuple(item for item in self.sources if item.heavy)

    def to_json(self) -> JsonObject:
        return {
            "adapters": [
                {
                    "id": descriptor.id,
                    "selected": list(selected),
                    "summary": descriptor.summary,
                    "version": descriptor.version,
                }
                for descriptor, selected in self.adapters
            ],
            "ambiguities": [finding.id for finding in self.ambiguities],
            "findings": [finding.to_json() for finding in self.findings],
            "grouping": self.grouping.to_json(),
            "heavy": [item.source for item in self.heavy],
            "inventory": self.inventory.to_json(),
            "left_out": [entry.to_json() for entry in self.left_out],
            "schema": SCHEMA,
            "sources": [item.to_json() for item in self.sources],
            "work": self.work.to_json(),
        }

    def dumps(self) -> bytes:
        """Canonical JSON: byte-identical for the same root, adapters, config and workspace."""
        return canonical_json.dumps(self.to_json())

    def render(self) -> str:
        """The explanation as lines for people; the same facts as ``to_json``, abridged."""
        inv, work = self.inventory, self.work
        lines = [
            f"Inventory: {len(inv.files)} files ({_size(inv.bytes)}) holding {inv.sources}"
            f" distinct sources; {len(inv.links)} links; {len(inv.skipped)} skipped",
            "",
            "Sources:",
        ]
        for item in self.sources:
            where = show(item.locations[0]) + (
                f" (+{len(item.locations) - 1} copies)" if len(item.locations) > 1 else ""
            )
            lines.append(f"  {where}  [{item.status}]  {item.format_line()}")
            for verdict in item.verdicts:
                lines.append(f"    {verdict.adapter:<12} {verdict.verdict:<9} {verdict.why}")
            if item.inspection is not None and item.inspection.summary is not None:
                summary = canonical_json.dumps(item.inspection.summary).decode()
                lines.append(f"    inspect: {summary}")
            elif item.inspection is not None and item.inspection.failure is not None:
                failure = canonical_json.dumps(item.inspection.failure).decode()
                lines.append(f"    inspect failed: {failure}")
            if item.plan is not None:
                plan = item.plan
                lines.append(
                    f"    plan ({plan.rule}): {plan.chunks} chunks, {plan.committed} committed,"
                    f" {_size(plan.bytes_to_read)} to read"
                )
        counts = self.grouping.summary
        lines += [
            "",
            f"Sessions: {counts['proposals']} proposals ({counts['contested']} contested),"
            f" {counts['ambiguous']} ambiguous and {counts['unknown']} unplaced files",
        ]
        for proposal in self.grouping.proposals:
            place = proposal.directory
            where = "the root" if not isinstance(place, LocalPath | RawLocalPath) else show(place)
            contested = " contested" if proposal.status is Status.CONTESTED else ""
            lines.append(
                f"  [{proposal.confidence} {proposal.rule}{contested}] {where}:"
                f" {len(proposal.members)} files; "
                + "; ".join(reason.message for reason in proposal.reasons)
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
        return "\n".join(lines) + "\n"
