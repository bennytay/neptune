"""The live view of ``neptune ingest``: one readable line per source, then a summary.

It reads only what the job already says: the SDK's events while it runs, and, once it has
committed, the package's findings. Nothing here parses a source, decides anything or touches the
package, so the view cannot change what is written; it goes to stderr and the result on stdout
stays what ``--json`` and scripts read (ADR 0043 §3, ADR 0074).

A source's line appears when its last chunk is committed (``chunk_committed``), or at once for a
source no adapter claims; a source whose chunk failed waits for the verdict the job reaches after
parsing (salvaged or quarantined). Findings carry no source until the package exists, so the
lines say how many a source raised and the views after the last line say which matter, in
words::

    Neptune · ingesting acceptance-2.2.0/                          56 files
    ✓ sites/PLANT-2/cell3/calibration/CAL-ARM3A-0911.yaml  calibration     12 records
    ✓ sites/PLANT-2/cell3/bags/pallet_2026-09-14_0.mcap    mcap           214 records
    ✗ sites/PLANT-2/legged/.../patrol_0.mcap               truncated: what was read is kept, ...
    ── 55 ingested · 1,444 records · 1 error · 36 warnings · 5.6 s ──
    unknown: 13 runs with no software identity (not guessed)

Colour only when the stream is a terminal (``NO_COLOR`` respected); a stream that cannot encode
the glyphs gets ASCII ones.
"""

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, TextIO

from neptune.model.finding import FindingCategory, Severity
from neptune.model.jsonvalue import JsonObject
from neptune.model.provenance import EvidenceRef
from neptune.sdk import JobEvent

ADAPTER_WIDTH: Final = 12
COUNT_WIDTH: Final = 15  # "1,234,567 records"
MAX_SOURCE_NOTES: Final = 8  # sources with a warning or an error listed after the last line
MAX_SUMMARY_KINDS: Final = 3  # finding kinds named per summary line


class FindingLike(Protocol):
    """What the view needs of a finding: a receipt's, or the package's own."""

    @property
    def code(self) -> str: ...
    @property
    def category(self) -> FindingCategory: ...
    @property
    def severity(self) -> Severity: ...
    @property
    def message(self) -> str: ...


# A finding code in plain words: (singular, plural, rest), shown as "<n> <noun> <rest>".
_UNKNOWN: Final[Mapping[str, tuple[str, str, str]]] = {
    "neptune.bindings.no_software_identity": (
        "run",
        "runs",
        "with no software identity (not guessed)",
    ),
    "neptune.bindings.snapshot_unresolved": (
        "run binding",
        "run bindings",
        "with no calibration or configuration record (left unresolved)",
    ),
    "neptune.clocks.latency_unbounded": (
        "clock mapping",
        "clock mappings",
        "with unbounded latency (not assumed zero)",
    ),
    "neptune.clocks.anchors_absent": (
        "clock pair",
        "clock pairs",
        "with no shared reading (left unrelated)",
    ),
    "neptune.clocks.single_instant": (
        "clock",
        "clocks",
        "anchored at one instant only (rate unknown)",
    ),
    "neptune.clocks.unsynchronised": (
        "package",
        "packages",
        "with clocks no mapping joins (times not comparable)",
    ),
    "neptune.frames.disconnected": (
        "run",
        "runs",
        "with frames no transform joins (not guessed)",
    ),
    "neptune.manifest.run_unrecorded": (
        "manifest run",
        "manifest runs",
        "whose files declare no run (stated, not evidenced)",
    ),
    "tabular.csv_dialect": (
        "CSV file",
        "CSV files",
        "with no declared dialect (sniffed, recorded as such)",
    ),
    "neptune.validate.dangling_reference": (
        "record",
        "records",
        "naming a record the package does not hold",
    ),
}

# Everything else worth a line: what was flagged, not unknown.
_FLAGGED: Final[Mapping[str, tuple[str, str, str]]] = {
    "mcap.truncated": ("MCAP file", "MCAP files", "cut off mid-chunk (what was read is kept)"),
    "mcap.chunk_truncated": ("MCAP chunk", "MCAP chunks", "cut short (whole records kept)"),
    "mcap.payload_not_decoded": (
        "MCAP channel",
        "MCAP channels",
        "with payloads kept raw (encoding not decoded)",
    ),
    "mcap.payload_partly_decoded": (
        "MCAP channel",
        "MCAP channels",
        "only partly decoded (fields without a column)",
    ),
    "neptune.bindings.shared_snapshot": (
        "snapshot",
        "snapshots",
        "shared by several runs (bound to none)",
    ),
    "neptune.validate.duplicate_id": ("id", "ids", "stated more than once"),
    "neptune.validate.source_incomplete": ("source", "sources", "incomplete (cut-off bytes)"),
    "neptune.introspection.definition_unreadable": (
        "stream definition",
        "stream definitions",
        "unusable",
    ),
    "declared.section_not_entries": (
        "declared section",
        "declared sections",
        "not entries (left unread)",
    ),
    "geojson.crs_legacy": ("GeoJSON file", "GeoJSON files", "with a legacy CRS member"),
    "text.invalid_utf8": ("text block", "text blocks", "not valid UTF-8 (text unknown)"),
    "mcap.attachment_not_extracted": (
        "MCAP attachment",
        "MCAP attachments",
        "cited, not extracted (no record kind holds them yet)",
    ),
}

# A source's own note, in a few words, for the codes that say most.
_SHORT: Final[Mapping[str, str]] = {
    "mcap.truncated": "truncated: what was read is kept, the rest flagged",
    "mcap.chunk_truncated": "a chunk is cut short: whole records kept, the rest flagged",
    "neptune.validate.source_incomplete": "incomplete: cut-off bytes reported above",
    "declared.section_not_entries": "a declared section is not entries: left unread",
    "neptune.introspection.definition_unreadable": "a stream definition cannot be used",
    "neptune.validate.duplicate_id": "an id is stated more than once",
}

_SEVERITY_ORDER: Final = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
_UNKNOWN_CATEGORIES: Final = frozenset({FindingCategory.MISSING})


@dataclass
class _Source:
    """What the view knows of one source while the job runs."""

    paths: list[str] = field(default_factory=list)
    adapter: str | None = None
    chunks: int | None = None
    resolved: int = 0
    reused: int = 0
    records: int = 0
    findings: int = 0
    failed: bool = False
    printed: bool = False


@dataclass(frozen=True)
class _Glyphs:
    ok: str
    warn: str
    error: str
    skip: str
    dot: str
    rule: str
    ellipsis: str


_UNICODE: Final = _Glyphs("✓", "⚠", "✗", "·", "·", "──", "…")
_ASCII: Final = _Glyphs("+", "!", "x", "-", "-", "--", "...")


def _glyphs_for(stream: TextIO) -> _Glyphs:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "✓⚠✗·─…".encode(encoding)
    except (UnicodeError, LookupError):
        return _ASCII
    return _UNICODE


def wants_colour(stream: TextIO) -> bool:
    """Colour only for a terminal, and never if ``NO_COLOR`` is set."""
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty()) and not os.environ.get("NO_COLOR")


def terminal_width(stream: TextIO, default: int = 120) -> int:
    """The terminal's width in columns, else ``default``; clamped to something readable."""
    try:
        width = os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, AttributeError):
        width = default
    return max(72, min(width, 160))


class LiveView:
    """Turns a job's events into the readable view; ``finish`` writes the summary.

    ``out`` is where lines go (stderr); ``width`` the columns available; ``colour`` whether to
    use ANSI colour. ``dry_run`` makes a planned source the end of its line.
    """

    def __init__(
        self,
        out: TextIO,
        *,
        name: str,
        dry_run: bool = False,
        colour: bool = False,
        width: int = 100,
    ) -> None:
        self._out = out
        self._name = name
        self._dry_run = dry_run
        self._colour = colour
        self._width = width
        self._glyphs = _glyphs_for(out)
        self._sources: dict[str, _Source] = {}
        self._order: list[str] = []
        self._path_width: int | None = None
        self._unread = 0

    # --- events ----------------------------------------------------------------------------

    def event(self, event: JobEvent) -> None:
        """One job event; writes a line if it finishes a source."""
        details = event.details
        kind = event.kind
        if kind == "phase_finished" and str(event.phase) == "discover":
            self._header(_int(details.get("files")))
        elif kind in ("source_hashed", "source_recognised"):
            self._hashed(details)
        elif kind == "source_selected":
            self._source(details).adapter = _str(details.get("adapter"))
        elif kind in ("source_unsupported", "source_ambiguous", "source_unreadable"):
            self._unclaimed(kind, details)
        elif kind == "source_planned":
            self._planned(details)
        elif kind == "chunk_committed":
            self._chunk(details, committed=True)
        elif kind == "chunk_skipped":
            self._chunk(details, committed=False)
        elif kind in ("chunk_failed", "plan_failed"):
            self._source(details).failed = True
        elif kind == "source_admitted":
            self._admitted(details)
        elif kind == "source_salvaged":
            self._salvaged(details)
        elif kind == "source_quarantined":
            self._quarantined(details)

    def _source(self, details: JsonObject) -> _Source:
        key = _str(details.get("source")) or ""
        if key not in self._sources:
            self._sources[key] = _Source()
            self._order.append(key)
        return self._sources[key]

    def _hashed(self, details: JsonObject) -> None:
        source = self._source(details)
        location = details.get("location")
        path = _location(location)
        if path not in source.paths:
            source.paths.append(path)

    def _unclaimed(self, kind: str, details: JsonObject) -> None:
        source = self._source(details)
        source.printed = True
        self._unread += 1
        if kind == "source_ambiguous":
            tied = details.get("adapters")
            names = " or ".join(str(n) for n in tied) if isinstance(tied, list) else "several"
            note = f"ambiguous: {names} claim it equally; not guessed"
        elif kind == "source_unreadable":
            note = "unreadable: left to the receipt"
        else:
            note = "no adapter claims it: kept as evidence only"
        self._line(self._glyphs.skip, source, "", note, colour="dim")

    def _planned(self, details: JsonObject) -> None:
        source = self._source(details)
        source.adapter = _str(details.get("adapter")) or source.adapter
        source.chunks = _int(details.get("chunks")) or 0
        if self._dry_run:
            source.printed = True
            word = "chunk" if source.chunks == 1 else "chunks"
            count = f"would read {source.chunks} {word}"
            self._line(self._glyphs.ok, source, source.adapter or "", count, colour="green")
        elif source.chunks == 0:
            self._finished(source)

    def _chunk(self, details: JsonObject, *, committed: bool) -> None:
        source = self._source(details)
        source.resolved += 1
        if committed:
            source.records += _int(details.get("records")) or 0
            source.findings += _int(details.get("findings")) or 0
        else:
            source.reused += 1
        if source.chunks is not None and source.resolved >= source.chunks and not source.failed:
            self._finished(source)

    def _admitted(self, details: JsonObject) -> None:
        source = self._source(details)
        if not source.printed:  # admitted without its last chunk event: still a finished source
            self._finished(source)

    def _finished(self, source: _Source) -> None:
        if source.printed:
            return
        source.printed = True
        self._line(self._glyphs.ok, source, source.adapter or "", self._count(source), "green")

    def _salvaged(self, details: JsonObject) -> None:
        source = self._source(details)
        kept, total, lost = (
            _int(details.get("committed")),
            _int(details.get("chunks")),
            _int(details.get("lost")),
        )
        note = f"salvaged: {kept} of {total} chunks kept, {lost} lost and flagged"
        source.printed = True
        self._line(self._glyphs.warn, source, source.adapter or "", note, colour="yellow")

    def _quarantined(self, details: JsonObject) -> None:
        source = self._source(details)
        codes = details.get("codes")
        why = ", ".join(str(c) for c in codes) if isinstance(codes, list) and codes else "failed"
        source.printed = True
        note = f"quarantined: {why}; nothing of it is canonical"
        self._line(self._glyphs.error, source, source.adapter or "", note, colour="red")

    # --- lines -----------------------------------------------------------------------------

    def _header(self, files: int | None) -> None:
        left = f"Neptune {self._glyphs.dot} ingesting {self._name}"
        right = "" if files is None else f"{files:,} file{'' if files == 1 else 's'}"
        pad = max(2, self._width - len(left) - len(right))
        self._write(self._style(left, "bold") + " " * pad + right)

    def _layout(self) -> int:
        if self._path_width is None:
            longest = max((len(p) for s in self._sources.values() for p in s.paths[:1]), default=20)
            room = self._width - 2 - 2 - ADAPTER_WIDTH - 2 - COUNT_WIDTH - 12
            self._path_width = max(20, min(longest, room))
        return self._path_width

    def _line(self, mark: str, source: _Source, adapter: str, tail: str, colour: str) -> None:
        width = self._layout()
        path = _fit(source.paths[0] if source.paths else "(unnamed source)", width, self._glyphs)
        extra = len(source.paths) - 1
        note = f"  (+{extra} identical cop{'y' if extra == 1 else 'ies'})" if extra > 0 else ""
        text = (
            f"{self._style(mark, colour)} {path:<{width}}  "
            f"{self._style(f'{adapter:<{ADAPTER_WIDTH}}', 'dim')}  {tail}{note}"
        )
        self._write(text.rstrip())

    def _count(self, source: _Source) -> str:
        if source.chunks and source.reused == source.chunks:
            count = "reused".rjust(COUNT_WIDTH)
        else:
            word = "record " if source.records == 1 else "records"
            count = f"{source.records:>7,} {word}"
            if source.reused:
                count += " + reused"
        if source.findings:
            word = "finding" if source.findings == 1 else "findings"
            count += f"  {self._glyphs.dot} {source.findings} {word}"
        return count

    # --- the end ---------------------------------------------------------------------------

    def finish(
        self,
        *,
        ingested: int,
        records: int | None,
        findings: Sequence[FindingLike],
        by_source: Mapping[str, Sequence[FindingLike]],
        elapsed: float | None,
    ) -> None:
        """The notes on sources that need a look, the summary rule and the unknowns.

        ``findings`` are the receipt's (all of them); ``by_source`` the package's findings by the
        content id of the source they cite, empty if the package could not be read.
        """
        for key in self._order:  # a source that never reached its line: say so, do not hide it
            source = self._sources[key]
            if not source.printed and source.adapter is not None:
                source.printed = True
                self._line(self._glyphs.skip, source, source.adapter, "not finished", "dim")
        self._notes(by_source)
        severities = Counter(f.severity for f in findings)
        parts = [
            f"{ingested:,} {'planned' if self._dry_run else 'ingested'}",
            "nothing written" if self._dry_run else f"{records:,} records" if records else "",
            _plural(severities[Severity.ERROR], "error"),
            _plural(severities[Severity.WARNING], "warning"),
            f"{self._unread} unread" if self._unread else "",
            "" if elapsed is None else f"{elapsed:.1f} s",
        ]
        rule = self._glyphs.rule
        middle = f" {self._glyphs.dot} ".join(p for p in parts if p)
        self._write(self._style(f"{rule} {middle} {rule}", "bold"))
        for label, unknown in (("unknown", True), ("flagged", False)):
            for text in self._kinds(findings, unknown):
                self._write(f"{label}: {text}")

    def _kinds(self, findings: Sequence[FindingLike], unknown: bool) -> list[str]:
        """The top finding kinds in plain words: unknowns (missing evidence) or what was flagged."""
        table = _UNKNOWN if unknown else _FLAGGED
        counts: Counter[str] = Counter()
        worst: dict[str, Severity] = {}
        samples: dict[str, str] = {}
        for f in findings:
            if (f.category in _UNKNOWN_CATEGORIES) is not unknown:
                continue
            counts[f.code] += 1
            samples.setdefault(f.code, f.message)
            if f.code not in worst or _SEVERITY_ORDER[f.severity] < _SEVERITY_ORDER[worst[f.code]]:
                worst[f.code] = f.severity
        ranked = sorted(counts, key=lambda c: (_SEVERITY_ORDER[worst[c]], -counts[c], c))
        lines = []
        for code in ranked[:MAX_SUMMARY_KINDS]:
            n = counts[code]
            if code in table:
                singular, plural, rest = table[code]
                lines.append(f"{n:,} {singular if n == 1 else plural} {rest}")
            else:
                lines.append(f"{n:,} x {code}: {_cut(samples[code], self._width - 20)}")
        if len(ranked) > MAX_SUMMARY_KINDS:
            lines.append(f"+{len(ranked) - MAX_SUMMARY_KINDS} more kinds: see receipt.md")
        return lines

    def _notes(self, by_source: Mapping[str, Sequence[FindingLike]]) -> None:
        """One line per source the package flagged with an error or warning, worst first."""
        noted: list[tuple[int, str, str, int, FindingLike]] = []
        for key in self._order:
            source = self._sources[key]
            flagged = sorted(
                (
                    f
                    for f in by_source.get(key, ())
                    if f.severity is not Severity.INFO and f.category not in _UNKNOWN_CATEGORIES
                ),
                key=lambda f: (_SEVERITY_ORDER[f.severity], f.code),
            )
            if flagged and source.paths:
                top = flagged[0]
                noted.append(
                    (_SEVERITY_ORDER[top.severity], source.paths[0], key, len(flagged), top)
                )
        noted.sort(key=lambda n: (n[0], n[1]))
        for _, _, key, count, top in noted[:MAX_SOURCE_NOTES]:
            source = self._sources[key]
            room = max(20, self._width - self._layout() - ADAPTER_WIDTH - 8)
            text = _SHORT.get(top.code) or _cut(top.message, room)
            if count > 1:
                text += f" (+{count - 1} more)"
            error = top.severity is Severity.ERROR
            mark = self._glyphs.error if error else self._glyphs.warn
            self._line(mark, source, source.adapter or "", text, "red" if error else "yellow")
        if len(noted) > MAX_SOURCE_NOTES:
            more = len(noted) - MAX_SOURCE_NOTES
            noun = "source" if more == 1 else "sources"
            self._write(f"  {self._glyphs.dot} {more} more {noun} with warnings: see receipt.md")

    # --- output ----------------------------------------------------------------------------

    def _style(self, text: str, style: str) -> str:
        if not self._colour:
            return text
        code = {"bold": "1", "dim": "2", "green": "32", "yellow": "33", "red": "31"}[style]
        return f"\x1b[{code}m{text}\x1b[0m"

    def _write(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()


def finding_sources(findings: Sequence[object]) -> dict[str, list[FindingLike]]:
    """The package's findings by the content id of the source they cite (those about evidence)."""
    grouped: dict[str, list[FindingLike]] = {}
    for finding in findings:
        subject = getattr(finding, "subject", None)
        if isinstance(subject, EvidenceRef):
            grouped.setdefault(str(subject.source), []).append(finding)  # type: ignore[arg-type]
    return grouped


def source_label(source: str, folder: bool) -> str:
    """A source argument as the header names it: its last component, a folder with a slash."""
    text = source.rstrip("/")
    name = text.rsplit("/", 1)[-1] or text
    return name + "/" if folder else name


def _location(location: object) -> str:
    if isinstance(location, dict):
        for key in ("path", "key", "uri"):
            value = location.get(key)
            if isinstance(value, str):
                return value
    return "(unnamed source)"


def _fit(path: str, width: int, glyphs: _Glyphs) -> str:
    """``path`` in ``width`` columns: whole, else with the middle directories elided."""
    if len(path) <= width:
        return path
    parts = path.split("/")
    head, tail = parts[:1], parts[-1:]
    if len("/".join([*head, glyphs.ellipsis, *tail])) > width:
        return glyphs.ellipsis + path[-(width - len(glyphs.ellipsis)) :]
    low, high = 1, len(parts) - 1  # parts[:low] and parts[high:] are kept
    turn = 0
    while low < high - 1:
        nxt = (low + 1, high) if turn % 2 == 0 else (low, high - 1)
        if len("/".join([*parts[: nxt[0]], glyphs.ellipsis, *parts[nxt[1] :]])) > width:
            break
        low, high = nxt
        turn += 1
    return "/".join([*parts[:low], glyphs.ellipsis, *parts[high:]])


def _cut(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: max(1, width - 3)].rstrip() + "..."


def _plural(n: int, word: str) -> str:
    return "" if n == 0 else f"{n:,} {word}{'' if n == 1 else 's'}"


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
