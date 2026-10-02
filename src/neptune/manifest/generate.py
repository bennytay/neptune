"""``neptune init-manifest``: a manifest written from what discovery found (ADR 0047 §7).

The generated manifest changes nothing as written. Every choice in it is a comment: the probe's
ties as one line per tied adapter, contested session readings as one block per reading, proposals
nobody contests, the probe's winners, and templates for the machines, sites, tasks and software
no file states. A developer uncomments the lines that are true; only then does the manifest
declare anything, and what it declares is theirs, stated. A guess is never written as a
declaration.

What it reads: one dry run of the folder through the SDK (every probe sandboxed, as an ingest
probes; no manifest applied), and the folder's layout grouped by the same grouper the job uses.
The same folder, adapters and options give byte-identical text.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from neptune.derived.grouping import GroupingConfig, LayoutGrouper
from neptune.derived.sessions import SessionProposal, Status
from neptune.discovery.layout import Layout, inside, layout_from_scan
from neptune.discovery.probe import PROBE_ID
from neptune.discovery.scan import fingerprint
from neptune.discovery.source import LocalSource
from neptune.identity.revisions import SourceLedger
from neptune.manifest.load import MANIFEST_NAMES
from neptune.manifest.reader import ManifestError
from neptune.manifest.schema import EMBODIMENTS, SCHEMA_VERSION
from neptune.model.ids import ContentId, RecordId
from neptune.model.provenance import EvidenceRef
from neptune.model.source import LocalPath, RawLocalPath

if TYPE_CHECKING:
    from neptune.sdk import JobEvent, Neptune

LISTED: Final = 200  # winners and proposals listed before the rest are only counted
PATHS: Final = 64  # paths a generated run lists before it says to declare a directory instead
_SELECTED: Final = "source_selected"
_AMBIGUOUS: Final = "source_ambiguous"
_UNSUPPORTED: Final = "source_unsupported"


@dataclass(frozen=True)
class Probed:
    """What the dry run's probe said about each source, by content id."""

    selected: Mapping[ContentId, tuple[str, float]]
    tied: Mapping[ContentId, tuple[tuple[str, ...], float | None]]
    unsupported: frozenset[ContentId]


def generate(root: Path, client: "Neptune") -> str:
    """The commented manifest for the folder ``root``: one dry run, then the text."""
    if not root.is_dir():
        raise ManifestError(f"{root} is not a folder; a manifest describes a folder")
    seen: list[JobEvent] = []
    result = client.dry_run(root, manifest=False, on_event=seen.append)
    confidence: dict[ContentId, float] = {}
    for finding in result.findings:
        if finding.code == f"{PROBE_ID}.ambiguous" and isinstance(finding.subject, EvidenceRef):
            value = finding.details.get("confidence")
            if isinstance(value, int | float) and isinstance(finding.subject.source, str):
                confidence[ContentId(finding.subject.source)] = float(value)
    selected: dict[ContentId, tuple[str, float]] = {}
    tied: dict[ContentId, tuple[tuple[str, ...], float | None]] = {}
    unsupported: set[ContentId] = set()
    for event in seen:
        details = event.details
        named = details.get("source")
        if not isinstance(named, str):
            continue
        cid = ContentId(named)
        if event.kind == _SELECTED:
            adapter, score = details.get("adapter"), details.get("confidence")
            if isinstance(adapter, str) and isinstance(score, int | float):
                selected[cid] = (adapter, float(score))
        elif event.kind == _AMBIGUOUS:
            adapters = details.get("adapters")
            if isinstance(adapters, list):
                names = tuple(sorted(a for a in adapters if isinstance(a, str)))
                tied[cid] = (names, confidence.get(cid))
        elif event.kind == _UNSUPPORTED:
            unsupported.add(cid)
    rules = client.options.ignore.rules(LocalSource(root))
    walked = LocalSource(root, ignore=rules)
    scan = fingerprint(walked, SourceLedger(), tuple(walked.walk()))
    layout = layout_from_scan(scan.observations, scan.symlinks)
    grouping = LayoutGrouper(GroupingConfig()).propose(layout)
    probed = Probed(selected, tied, frozenset(unsupported))
    return render(root.name or str(root), layout, grouping.ranked(), grouping.unassigned, probed)


def _quote(text: str) -> str:
    """A YAML double-quoted scalar (JSON's string syntax is a subset of it)."""
    return json.dumps(text, ensure_ascii=False)


def _is_manifest(location: LocalPath | RawLocalPath) -> bool:
    return isinstance(location, LocalPath) and location.path in MANIFEST_NAMES


def _extent(
    proposal: SessionProposal, by_id: Mapping[RecordId, SessionProposal]
) -> list[LocalPath | RawLocalPath]:
    out = [member.location for member in proposal.members]
    for included in proposal.includes:
        if included in by_id:
            out += _extent(by_id[included], by_id)
    return sorted(set(out), key=lambda location: location.raw)


def _paths(
    proposal: SessionProposal, layout: Layout, by_id: Mapping[RecordId, SessionProposal]
) -> list[str] | str:
    """The paths a run declaring ``proposal`` lists, or why it cannot be written."""
    extent = _extent(proposal, by_id)
    directory = proposal.directory
    if isinstance(directory, LocalPath):
        below = {f.location.raw for f in layout.files if inside(f.location.raw, directory.raw)}
        if below == {location.raw for location in extent}:
            return [directory.path]
    if any(isinstance(location, RawLocalPath) for location in extent):
        return "a path is not UTF-8, so it cannot be written in a manifest"
    if len(extent) > PATHS:
        return f"{len(extent)} files; declare their directory instead"
    return [location.path for location in extent if isinstance(location, LocalPath)]


def _name(paths: Sequence[str], taken: set[str]) -> str:
    """A run name from what it holds: its one path less the extension, or the first and a count."""
    first = paths[0]
    leaf = first.rsplit("/", 1)[-1]
    stem = first[: len(first) - len(leaf)] + (leaf.split(".", 1)[0] or leaf)
    base = stem if len(paths) == 1 else f"{stem} +{len(paths) - 1}"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    taken.add(name)
    return name


def _run_lines(
    proposal: SessionProposal,
    layout: Layout,
    by_id: Mapping[RecordId, SessionProposal],
    taken: set[str],
) -> list[str]:
    reason = proposal.reasons[0].message if proposal.reasons else proposal.rule
    head = f"  #   {proposal.rule}, confidence {proposal.confidence}: {reason}"
    paths = _paths(proposal, layout, by_id)
    if isinstance(paths, str):
        return [head, f"  #   (not offered: {paths})"]
    listed = ", ".join(_quote(path) for path in paths)
    return [head, f"  # - {{name: {_quote(_name(paths, taken))}, paths: [{listed}]}}"]


def _components(proposals: Sequence[SessionProposal]) -> list[list[SessionProposal]]:
    """Contested proposals in connected sets, each in rank order, sets in order of first rank."""
    by_id = {p.id: p for p in proposals}
    order = {p.id: n for n, p in enumerate(proposals)}
    seen: set[RecordId] = set()
    out: list[list[SessionProposal]] = []
    for proposal in proposals:
        if proposal.status is not Status.CONTESTED or proposal.id in seen:
            continue
        stack, members = [proposal.id], []
        seen.add(proposal.id)
        while stack:
            current = by_id[stack.pop()]
            members.append(current)
            for other in current.contested:
                if other in by_id and other not in seen:
                    seen.add(other)
                    stack.append(other)
        out.append(sorted(members, key=lambda p: order[p.id]))
    return out


def render(
    folder: str,
    layout: Layout,
    ranked: Sequence[SessionProposal],
    unassigned: Iterable[object],
    probed: Probed,
) -> str:
    """The manifest text (module docstring); a pure function of its inputs."""
    lines = [
        f"# Neptune manifest for {folder}, written by `neptune init-manifest`.",
        "# As written it declares nothing: every choice below is a comment. Uncomment the lines",
        "# that are true. What you declare is stated, with this file as its source, and set",
        "# against the evidence: a declaration the evidence contradicts is a finding, never a",
        "# silent override. Reference: docs/manifest.md; editor schema:",
        "# docs/schema/manifest.schema.json.",
        f"neptune: {SCHEMA_VERSION}",
        "",
        "# What no file here states: the machines, sites, tasks and software of the runs.",
        "machines:",
        f'  # - {{id: robot-1, name: "Robot 1", embodiment: {EMBODIMENTS[0]}}}'
        f"  # embodiment: {', '.join(EMBODIMENTS)}",
        "sites:",
        '  # - {id: site-1, name: "Site 1"}',
        "tasks:",
        '  # - {id: task-1, description: "What the runs were for"}',
        "software:",
        '  # - {id: software-1, name: "controller", version: "1.0.0"}',
        "",
        "# Runs: the sessions grouping proposes from names and folders (ADR 0036). Declaring one",
        "# states it; add machine:, site:, task: and software: to say what it involved.",
        "runs:",
    ]
    by_id = {p.id: p for p in ranked}
    taken: set[str] = set()
    contested = _components(ranked)
    for number, group in enumerate(contested, start=1):
        lines.append(
            f"  # Contested set {number} (neptune.grouping.contested): the folder supports"
            " each reading; keep the one that is true."
        )
        for proposal in group:
            lines += _run_lines(proposal, layout, by_id, taken)
    plain = [p for p in ranked if p.status is not Status.CONTESTED]
    if plain:
        lines.append("  # Proposed, uncontested (declare one to state it):")
    for proposal in plain[:LISTED]:
        lines += _run_lines(proposal, layout, by_id, taken)
    if len(plain) > LISTED:
        lines.append(f"  # ... and {len(plain) - LISTED} more proposals")
    for entry in unassigned:
        placement = getattr(entry, "placement", None)
        location = getattr(entry, "location", None)
        if str(placement) == "ambiguous" and isinstance(location, LocalPath):
            lines.append(
                f"  # {_quote(location.path)} could belong to several runs: add it to the"
                " paths of the run that holds it."
            )
    lines += [
        "",
        "# Adapters: which adapter reads which files, and with which options. The probe's",
        "# winners need no line; a pin applies only if that adapter's probe accepts the file.",
        "sources:",
    ]
    locations: dict[ContentId, list[LocalPath | RawLocalPath]] = defaultdict(list)
    for file in layout.files:
        if not _is_manifest(file.location):
            locations[file.content_id].append(file.location)

    def first(cid: ContentId) -> bytes:
        return min((location.raw for location in locations.get(cid, [])), default=b"")

    for cid in sorted(probed.tied, key=first):
        adapters, score = probed.tied[cid]
        for location in locations.get(cid, []):
            if not isinstance(location, LocalPath):
                continue
            at = f" at confidence {score}" if score is not None else ""
            lines.append(
                f"  # {_quote(location.path)}: {' and '.join(adapters)} tie{at}"
                " (neptune.probe.ambiguous); keep one line:"
            )
            path = _quote(location.path)
            lines += [f"  # - {{path: {path}, adapter: {adapter}}}" for adapter in adapters]
    winners = sorted(
        (location.path, adapter, score)
        for cid, (adapter, score) in probed.selected.items()
        for location in locations.get(cid, [])
        if isinstance(location, LocalPath)
    )
    if winners:
        lines.append("  # Selected by the probe (uncomment one to pin it):")
    for path, adapter, score in winners[:LISTED]:
        lines.append(f"  # - {{path: {_quote(path)}, adapter: {adapter}}}  # confidence {score}")
    if len(winners) > LISTED:
        lines.append(f"  # ... and {len(winners) - LISTED} more")
    lonely = sorted(
        location.path
        for cid in probed.unsupported
        for location in locations.get(cid, [])
        if isinstance(location, LocalPath)
    )
    if lonely:
        lines.append(f"  # No adapter claims {len(lonely)} files, e.g. {_quote(lonely[0])}.")
    lines += [
        "",
        "# Options for every file an adapter reads, by adapter id.",
        "adapters:",
        '  # tabular: {options: {csv_delimiter: ";"}}',
        "",
    ]
    return "\n".join(lines)
