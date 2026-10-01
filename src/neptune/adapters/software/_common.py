"""What every software-identity format shares (ADR 0040).

A ``Reading`` is one ``ingest`` of one source: its findings, its documents and the
``SoftwareConfiguration`` its drafted items become. A ``Draft`` is one software item being read,
with the place its entry is (where an ``Unknown`` cites) and the fields the format filled. Each
format module only finds values and their places; the rules for states, kinds and findings live
here, so every format applies them the same way:

- a value the file gives is ``Known``, citing the bytes (or the decoded document's pointer) that
  hold it; a key the file leaves out, or a blank, is ``Unknown`` citing where the adapter looked;
- a field the format has no place for is ``NotCovered``; ``digest`` is ``NotApplicable`` except
  for checkpoints and container images, the only artifacts its kinds describe (ADR 0019 §5);
- a value that is not a valid member of its kind is ``Unknown`` plus ``software.invalid_value``;
- an item with no identity (commit, release, build or digest) and a field the format could have
  filled is ``software.software_identity_missing``, unless another finding explains every gap.
"""

import json
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn, TypeVar

from neptune.adapters.contract import (
    AdapterConfig,
    FormatSpec,
    ProbeReason,
    SourceReader,
    read_pieces,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId, check_text
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.machine import ArtifactDigest, Release, SoftwareConfiguration, SoftwareItem
from neptune.model.provenance import ByteRange, EvidenceRef, JsonPointer, Locator, Provenance
from neptune.model.versions import (
    BuildId,
    DeclaredVersion,
    GitCommit,
    SemanticVersion,
)

ADAPTER_ID: Final = "software"
# The fields that identify a software item. A name alone does not: two builds share one.
IDENTITY_FIELDS: Final = ("commit", "release", "build", "digest")

V = TypeVar("V")


def text_value(value: str) -> str:
    """A declared name or device, as text: non-empty valid Unicode, kept verbatim."""
    return check_text("text", value)


def pointer(*parts: str | int) -> str:
    """The RFC 6901 pointer to ``parts`` in a decoded document."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


@dataclass
class Draft:
    """One software item being read. ``entry`` is where it is declared: what an ``Unknown`` of it
    cites, and what its missing-identity finding is about. ``explained`` names the fields another
    finding already accounts for."""

    entry: EvidenceRef
    name: Knowledge[str] = field(default_factory=NotCovered)
    device: Knowledge[str] = field(default_factory=NotCovered)
    commit: Knowledge[GitCommit] = field(default_factory=NotCovered)
    release: Knowledge[Release] = field(default_factory=NotCovered)
    build: Knowledge[BuildId] = field(default_factory=NotCovered)
    digest: Knowledge[ArtifactDigest] = field(default_factory=NotApplicable)
    explained: set[str] = field(default_factory=set)

    def item(self) -> SoftwareItem:
        return SoftwareItem(
            name=self.name,
            device=self.device,
            commit=self.commit,
            release=self.release,
            build=self.build,
            digest=self.digest,
        )

    def identity(self) -> dict[str, Knowledge[Any]]:
        return {name: getattr(self, name) for name in IDENTITY_FIELDS}


CANDIDATE_LIMIT: Final = 32


class _DuplicateKey(ValueError):
    """A JSON object repeats a key: which value is meant is not decidable."""


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _no_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def loads_json(text: str) -> Any:
    """Strict JSON: a repeated key, ``NaN`` or an infinity raises ``ValueError``, never a silent
    last value. Nesting past the parser's guard raises ``RecursionError``."""
    # A leading byte-order mark is tolerated (Windows tools write one); citations are JSON pointers,
    # so dropping it moves none.
    return json.loads(
        text.removeprefix("\ufeff"), object_pairs_hook=_object_pairs, parse_constant=_no_constant
    )


def json_head(head: bytes, size: int) -> Any:
    """The head as strict JSON when it is the whole source, else ``None``: a probe that parses
    and checks its document claims ``VERIFIED``, above a generic JSON reader's ``STRUCTURE``."""
    if len(head) != size:
        return None
    try:
        return loads_json(head.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


_TABLE_HEADER: Final = re.compile(rb"^[ \t]*\[", re.MULTILINE)


def toml_head(head: bytes, size: int) -> dict[str, Any] | None:
    """The head as TOML: the whole source when the head is all of it, else the part before one of
    its last table headers, so a long lockfile or manifest is checked, not just matched."""
    if len(head) == size:
        cuts = [size]
    else:
        cuts = sorted((m.start() for m in _TABLE_HEADER.finditer(head)), reverse=True)[:4]
    for cut in cuts:
        if cut <= 0:
            continue
        try:
            return tomllib.loads(head[:cut].decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
            continue
    return None


_DESCRIBE: Final = re.compile(r"(?:(?P<tag>.+)-[0-9]+-g)?(?P<sha>[0-9a-f]{7,64})(?:-dirty)?")


def describe(text: str) -> tuple[bool, tuple[int, int] | None]:
    """Read ``git describe`` output: whether it names a tag, and the span of the commit it names.

    ``<tag>-<n>-g<abbrev>[-dirty]`` names both; ``<abbrev>[-dirty]`` (``--always`` with no tag)
    names only the commit; anything else is an exact tag and names no commit (ADR 0014 §5).
    """
    match = _DESCRIBE.fullmatch(text)
    if match is None:
        return True, None
    return match["tag"] is not None, match.span("sha")


class Reading:
    """One ``ingest`` of one source: its citations, findings and the record they amount to."""

    def __init__(
        self, source: SourceReader, config: AdapterConfig, label: str, assertion: AssertionKind
    ) -> None:
        self.source = source
        self.config = config
        self.label = label
        self.assertion = assertion
        self.whole = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        self.record_id: RecordId = evidence_record_id(
            SoftwareConfiguration.kind, self.whole, config.transform
        )
        self.findings: list[IngestFinding] = []
        self._entries_reported = 0
        # Findings about a value of the record: they name it only if it is emitted.
        self._deferred: list[dict[str, Any]] = []

    # --- Citations and findings ----------------------------------------------------------------

    def at(self, *steps: Locator) -> EvidenceRef:
        return EvidenceRef(self.source.content_id, steps)

    def span(self, offset: int, length: int) -> EvidenceRef:
        return self.at(ByteRange(offset, length))

    def provenance(self, evidence: EvidenceRef) -> Provenance:
        return Provenance(evidence, self.config.transform.id, self.assertion)

    def report(
        self,
        name: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        message: str,
        details: Mapping[str, JsonValue] | None = None,
        *,
        related: Sequence[EvidenceRef] = (),
        about_record: bool = False,
    ) -> None:
        """Report ``software.<name>``; ``about_record`` ones name the record if it is emitted."""
        found: dict[str, Any] = {
            "code": f"{ADAPTER_ID}.{name}",
            "category": category,
            "severity": severity,
            "subject": subject,
            "transform": self.config.transform,
            "message": message,
            "details": dict(details or {}),
            "related": tuple(related),
        }
        if about_record:
            self._deferred.append(found)
        else:
            self.findings.append(ingest_finding(**found))

    def malformed(
        self,
        problem: str,
        details: Mapping[str, JsonValue] | None = None,
        subject: EvidenceRef | None = None,
    ) -> None:
        """The source breaks its format: nothing is read from it."""
        self.report(
            "malformed",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            subject or self.whole,
            f"the {self.label} {problem}; no software is read from it",
            details,
        )

    def malformed_entry(self, subject: EvidenceRef, problem: str) -> None:
        """One entry breaks its format: that entry is skipped, the others are read.

        Bounded: past ``max_items`` entries the rest are skipped unreported, with one finding.
        """
        self._entries_reported += 1
        limit = self.config.integer("max_items")
        if self._entries_reported > limit + 1:
            return
        if self._entries_reported > limit:
            self.too_many_entries(subject, limit)
            return
        self.report(
            "malformed_entry",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            subject,
            f"an entry of the {self.label} {problem}; it is skipped",
        )

    def too_many_entries(self, subject: EvidenceRef, limit: int) -> None:
        self.report(
            "too_many_entries",
            FindingCategory.LIMIT,
            Severity.ERROR,
            subject,
            f"the {self.label} holds more than max_items ({limit}) malformed entries or notes;"
            " the rest are not read or reported",
            {"max_items": limit},
        )

    def truncated(
        self, subject: EvidenceRef, message: str, details: Mapping[str, JsonValue]
    ) -> None:
        self.report("truncated", FindingCategory.CORRUPT, Severity.ERROR, subject, message, details)

    # --- Values ----------------------------------------------------------------------------------

    def value(
        self,
        draft: Draft,
        name: str,
        raw: object,
        at: EvidenceRef,
        kind: Callable[[str], V],
        kind_name: str,
    ) -> Knowledge[V]:
        """A declared text as ``kind``: ``Known`` citing ``at``, ``Unknown`` if absent or blank.

        ``at`` is where the value is, or where the adapter looked when ``raw`` is ``None``.
        Text that is not a valid ``kind`` is ``Unknown`` plus ``software.invalid_value``.
        """
        if raw is None:
            return Unknown(self.provenance(at))
        if not isinstance(raw, str):
            return self.invalid(draft, name, at, "is not text")
        if not raw.strip():
            return Unknown(self.provenance(at))
        try:
            return Known(kind(raw), self.provenance(at))
        except ValueError:
            return self.invalid(draft, name, at, f"is not a valid {kind_name}")

    def text(self, draft: Draft, name: str, raw: object, at: EvidenceRef) -> Knowledge[str]:
        return self.value(draft, name, raw, at, text_value, "text")

    def declared(self, draft: Draft, raw: object, at: EvidenceRef) -> Knowledge[Release]:
        """A release under no scheme the format names: ``DeclaredVersion``."""
        version: Knowledge[Release] = self.value(
            draft, "release", raw, at, DeclaredVersion, "version"
        )
        return version

    def semver(self, draft: Draft, raw: object, at: EvidenceRef) -> Knowledge[Release]:
        """A release the format declares SemVer: ``SemanticVersion``, else the declared text plus
        ``software.version_not_semver`` (the kind is the format's claim, the text the file's)."""
        version = self.declared(draft, raw, at)
        if isinstance(version, Known) and isinstance(raw, str):
            try:
                return Known(SemanticVersion(raw), version.provenance)
            except ValueError:
                self.report(
                    "version_not_semver",
                    FindingCategory.INCONSISTENT,
                    Severity.INFO,
                    at,
                    f"the {self.label} format declares this version SemVer, but it is not;"
                    " it is kept as declared text",
                    about_record=True,
                )
        return version

    def invalid(self, draft: Draft, name: str, at: EvidenceRef, why: str) -> Unknown:
        draft.explained.add(name)
        self.report(
            "invalid_value",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            at,
            f"the {self.label} declares a {name} that {why}; it is unknown",
            {"field": name},
            about_record=True,
        )
        return Unknown(self.provenance(at))

    def unevaluated(self, draft: Draft, name: str, at: EvidenceRef, what: str) -> Unknown:
        """A value the file gives as an expression (a variable, an inherited or dynamic value)."""
        draft.explained.add(name)
        self.report(
            "unevaluated",
            FindingCategory.UNSUPPORTED,
            Severity.WARNING,
            at,
            f"the {self.label} gives the {name} as {what}, which is never evaluated; it is unknown",
            {"field": name},
            about_record=True,
        )
        return Unknown(self.provenance(at))

    def choose(
        self, draft: Draft, name: str, found: Sequence[Knowledge[V]], absent: Knowledge[V]
    ) -> Knowledge[V]:
        """One field read from several places: the same value is ``Known`` (first place), differing
        values are ``Ambiguous`` plus ``software.conflicting_identity``, none is ``absent``."""
        known = [state for state in found if isinstance(state, Known)]
        if not known:
            others = [state for state in found if not isinstance(state, Known)]
            return others[0] if others else absent
        # Values are frozen dataclasses or text: hashable, so this is linear; the first place of
        # each value is kept, in the order found.
        first: dict[Any, Known[V]] = {}
        for state in known:
            first.setdefault(state.value, state)
        distinct = list(first.values())
        if len(distinct) == 1:
            return distinct[0]
        total = len(distinct)
        # Ambiguous checks its candidates pairwise: a hostile file with thousands of distinct
        # values keeps the first CANDIDATE_LIMIT, the finding says how many there were.
        distinct = distinct[:CANDIDATE_LIMIT]
        places = [state.provenance for state in distinct]
        evidence = [place.evidence for place in places if isinstance(place, Provenance)]
        self.report(
            "conflicting_identity",
            FindingCategory.AMBIGUOUS,
            Severity.WARNING,
            evidence[0],
            f"the {self.label} gives this item {total} different values of {name}",
            {"distinct": total, "field": name, "kept": len(distinct)},
            related=evidence[1:],
            about_record=True,
        )
        return Ambiguous(tuple(Candidate(state.value, state.provenance) for state in distinct))

    # --- Documents -------------------------------------------------------------------------------

    def read(self, offset: int, length: int) -> bytes:
        """Up to ``length`` bytes from ``offset``, fewer only where the source ends."""
        end = min(self.source.size, offset + length)
        if offset >= end:
            return b""
        return b"".join(read_pieces(self.source, offset, end))

    def document(self, option: str = "max_document_bytes") -> bytes | None:
        """The whole source, or ``None`` past ``option``'s size (``software.too_large``).

        ``max_script_bytes`` is for sources decoded into a much larger tree (Python syntax,
        CMake commands); ``max_document_bytes`` for data documents.
        """
        limit = self.config.integer(option)
        if self.source.size > limit:
            self.too_large(self.whole, self.source.size, limit, option)
            return None
        return b"".join(read_pieces(self.source, 0, self.source.size))

    def too_large(self, subject: EvidenceRef, size: int, limit: int, option: str) -> None:
        self.report(
            "too_large",
            FindingCategory.LIMIT,
            Severity.ERROR,
            subject,
            f"the {self.label} holds {size} bytes to decode, over {option} ({limit});"
            " it is not read",
            {"bytes": size, option: limit},
        )

    def utf8(self, data: bytes) -> str | None:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.malformed(f"is not UTF-8 from byte {exc.start}", {"byte": exc.start})
            return None

    def toml(self) -> dict[str, Any] | None:
        """The source as a TOML document, or ``None`` with a finding."""
        data = self.document()
        text = None if data is None else self.utf8(data)
        if text is None:
            return None
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            self.malformed("does not parse as TOML")
        except RecursionError:
            self.malformed("nests too deeply to parse")
        return None

    def json(self, data: bytes | None, subject: EvidenceRef | None = None) -> Any:
        """``data`` as a JSON document, or ``None`` with a finding.

        Strict: a repeated key, ``NaN`` or an infinity is malformed, never a silent last value.
        """
        if data is None:
            return None
        text = self.utf8(data)
        if text is None:
            return None
        try:
            return loads_json(text)
        except _DuplicateKey:
            self.malformed("repeats a key in one object", subject=subject)
        except json.JSONDecodeError as exc:
            self.malformed(
                f"does not parse as JSON at character {exc.pos}", {"char": exc.pos}, subject
            )
        except ValueError:
            self.malformed("holds a value JSON does not allow", subject=subject)
        except RecursionError:
            self.malformed("nests too deeply to parse", subject=subject)
        return None

    # --- The record ------------------------------------------------------------------------------

    def full(self, drafts: Sequence[Draft]) -> bool:
        """Whether ``drafts`` is one past ``max_items``: a reader stops there, so a hostile file
        costs ``max_items`` drafts, not one per byte (``configuration`` then refuses the record)."""
        return len(drafts) > self.config.integer("max_items")

    def configuration(self, drafts: Sequence[Draft]) -> tuple[SoftwareConfiguration, ...]:
        """The ``SoftwareConfiguration`` of the drafted items, and the findings about them."""
        record: SoftwareConfiguration | None = None
        limit = self.config.integer("max_items")
        if not drafts:
            if not self.findings and not self._deferred:
                self.report(
                    "no_software_declared",
                    FindingCategory.MISSING,
                    Severity.INFO,
                    self.whole,
                    f"the {self.label} declares no software item",
                )
        elif len(drafts) > limit:
            self.report(
                "too_many_items",
                FindingCategory.LIMIT,
                Severity.ERROR,
                self.whole,
                f"the {self.label} declares more than max_items ({limit}) software items;"
                " no record is made",
                {"max_items": limit},
            )
        else:
            for draft in drafts:
                self._identity_check(draft)
            record = SoftwareConfiguration(
                id=self.record_id,
                provenance=self.provenance(self.whole),
                machine=NotCovered(),
                software=tuple(draft.item() for draft in drafts),
            )
        records = (self.record_id,) if record is not None else ()
        self.findings += [ingest_finding(**found, records=records) for found in self._deferred]
        self._deferred.clear()
        unique = {finding.id: finding for finding in self.findings}
        self.findings = list(unique.values())
        return (record,) if record is not None else ()

    def _identity_check(self, draft: Draft) -> None:
        identity = draft.identity()
        if any(isinstance(state, Known | Ambiguous) for state in identity.values()):
            return
        missing = [
            name
            for name, state in identity.items()
            if isinstance(state, Unknown) and name not in draft.explained
        ]
        if missing:
            self.report(
                "software_identity_missing",
                FindingCategory.MISSING,
                Severity.WARNING,
                draft.entry,
                f"the {self.label} gives this item no identity: no {' or '.join(missing)},"
                " though the format has a place for it",
                {"fields": list(missing)},
                about_record=True,
            )


@dataclass(frozen=True)
class Detected:
    """A format's claim on a head: the probe's confidence, reasons and declared version."""

    confidence: float
    reasons: tuple[ProbeReason, ...]
    version: str | None = None


def reason(key: str, message: str) -> tuple[ProbeReason, ...]:
    return (ProbeReason(f"{ADAPTER_ID}.{key}", message),)


@dataclass(frozen=True)
class Format:
    """One format the adapter reads: how its head is recognised and how a source is read."""

    key: str
    label: str
    spec: FormatSpec
    assertion: AssertionKind
    detect: Callable[[bytes, int], Detected | None]
    read: Callable[[Reading], list[Draft]]


@dataclass(frozen=True)
class Doc:
    """A decoded document inside ``base`` (the bytes it was decoded from): its pointers."""

    reading: Reading
    base: tuple[Locator, ...]

    def ref(self, *path: str | int) -> EvidenceRef:
        return self.reading.at(*self.base, JsonPointer(pointer(*path)))
