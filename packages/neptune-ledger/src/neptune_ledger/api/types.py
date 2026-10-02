"""Request and response records of the catalog API (docs/catalog-api.md, ADR 0004).

Every record is a frozen dataclass whose JSON form and JSON Schema come from its type hints
(``neptune_ledger.api.codec``). Two kinds of absence, never mixed (ADR 0004 §3):

- ``Knowledge[T]`` where the catalog or the evidence may not know: the compiler's states, carrying
  no provenance because they are the Ledger's own determinations.
- ``X | None`` where a field does not apply to this request or response shape (no merge was asked
  for, no window was given). The key is omitted from JSON; there is never a ``null``.

Names follow Ledger ADR 0002 (transaction key, registration key, refusal findings) and ADR 0003
(thread key, declared key, evidence anchor, partitions, lineage sets, preferences).
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Final, Literal, TypeAlias

from neptune.identity import canonical_json
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotCovered

# The catalog API's registry version (contracts/catalog-api). It equals the registry version
# exactly (platform ADR 0002 §3); a reader-incompatible change raises the major (ADR 0004 §5).
CATALOG_API_VERSION: Final = "1.6.0"
API_MAJOR: Final = int(CATALOG_API_VERSION.split(".", 1)[0])


@dataclass(frozen=True)
class Constraint:
    """JSON Schema keywords for an ``Annotated`` type; the codec enforces them both ways.

    A named constraint becomes its own ``$defs`` entry, so a schema reader sees ``ContentId``
    rather than an anonymous pattern.
    """

    name: str | None = None
    description: str | None = None
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    min_length: int | None = None
    min_items: int | None = None
    unique_items: bool = False
    required: tuple[str, ...] = ()
    # On a ``Knowledge[T]`` field: the state restates a package field verbatim, so it keeps the
    # package's provenance (``StatedProvenance``) and may be ``known_absent`` (ADR 0004 §3).
    as_stated: bool = False


# A JSON object exactly as a package states it (a locator step, a location, libraries). Not the
# compiler's recursive ``JsonObject`` alias, whose forward reference type hints cannot resolve here.
JsonObject: TypeAlias = Mapping[str, Any]

_SHA256: Final = "sha256:[0-9a-f]{64}"

ContentId: TypeAlias = Annotated[
    str, Constraint("ContentId", "A tier-1 id: sha256 of complete bytes.", f"^{_SHA256}$")
]
PackageId: TypeAlias = Annotated[
    str,
    Constraint("PackageId", "A package's id: the sha256 of its manifest.json.", f"^{_SHA256}$"),
]
RecordId: TypeAlias = Annotated[
    str,
    Constraint(
        "RecordId",
        "A record's key in its table: a tier-2 id, or a content id for source_artifact.",
        f"^(rec:)?{_SHA256}$",
    ),
]
TierTwoId: TypeAlias = Annotated[
    str,
    Constraint(
        "TierTwoId",
        "A tier-2 id: a transform, a timestamp domain (clock key) or a clock mapping.",
        f"^rec:{_SHA256}$",
    ),
]
# A record kind is named by the package-schema contract, not listed here (Ledger ADR 0011 §4): a
# package-schema version that adds kinds changes no catalog-api version. The kind is checked
# against the package-schema version a package declares, where it enters the catalog.
RecordKind: TypeAlias = Annotated[
    str,
    Constraint(
        "RecordKind",
        "A record kind, by the package-schema contract (contracts/package-schema): the kind of a"
        " record table at the schema version its package declares. Not enumerated here, so a"
        " package-schema version that adds kinds changes no catalog-api version.",
        r"^[a-z][a-z0-9_]*$",
    ),
]
Token: TypeAlias = Annotated[str, Constraint("Token", "A machine token.", r"^[a-z][a-z0-9_.\-]*$")]
Text: TypeAlias = Annotated[str, Constraint(min_length=1)]
Ticks: TypeAlias = Annotated[
    int,
    Constraint(
        "Ticks",
        "Integer ticks on one clock, exactly as the package states them; never converted.",
        minimum=-(2**63),
        maximum=2**63 - 1,
    ),
]
TxSeq: TypeAlias = Annotated[
    int, Constraint("TxSeq", "A catalog transaction sequence number (ADR 0002 §4).", minimum=1)
]
Count: TypeAlias = Annotated[int, Constraint(minimum=0)]
ApiVersion: TypeAlias = Annotated[
    str,
    Constraint(
        "ApiVersion",
        "The catalog API version that produced the document; any version of this major.",
        rf"^{API_MAJOR}\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
    ),
]
LocatorStep: TypeAlias = Annotated[
    JsonObject,
    Constraint(
        "LocatorStep",
        "One locator step exactly as the package states it (package-schema EvidenceRef).",
        required=("kind",),
    ),
]
Locator: TypeAlias = Annotated[tuple[LocatorStep, ...], Constraint(min_items=1)]

FindingCode: TypeAlias = Literal[
    "as_of_out_of_range",
    "conflicting_id",
    "file_digest_mismatch",
    "file_missing",
    "invalid_request",
    "manifest_digest_mismatch",
    "manifest_invalid",
    "mapping_out_of_range",
    "package_unreadable",
    "preference_required",
    "record_invalid",
    "unexpected_file",
    "unknown_clock",
    "unknown_mapping",
    "unknown_package",
    "unknown_record",
    "unresolvable_evidence",
    "unsafe_entry",
    "unsupported_mapping",
    "unsupported_schema_version",
]
ThreadKind: TypeAlias = Literal[
    "asset",
    "configuration",
    "document",
    "machine",
    "person",
    "run",
    "sensor",
    "site",
    "software_version",
    "stream",
    "task",
    "zone",
]
# The kind of thing an identity link's two ids name, as the evidence states it (ADR 0010 §8).
EntityKind: TypeAlias = Annotated[
    ThreadKind, Constraint("EntityKind", "The kind of entity a link's ids name (a thread kind).")
]
Role: TypeAlias = Literal["cites", "part_of", "subject"]
Order: TypeAlias = Literal["transaction", "world"]


# --- Shared ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransactionKey:
    """A point in the catalog's transaction order (ADR 0002 §4): one registration's tick.

    ``tx_time`` is RFC 3339 UTC with six fractional digits and never decreases as ``tx_seq``
    grows, so transaction order is ``tx_seq`` order.
    """

    tx_seq: TxSeq
    tx_time: Annotated[
        str,
        Constraint(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"),
    ]


@dataclass(frozen=True)
class CatalogFinding:
    """A structured refusal or warning. Calls report evidence and request problems this way.

    ``subject`` names what it concerns (a package id, a package-relative path, a record id, a
    mapping id); ``detail`` is deterministic text for people and is never parsed.
    """

    code: FindingCode
    subject: Text
    detail: str
    paths_tried: tuple["MappingPath", ...] | None = None


@dataclass(frozen=True)
class MappingPath:
    """One path of ``ClockMapping`` record ids, from an entry's clock to the reference clock."""

    mappings: Annotated[tuple[TierTwoId, ...], Constraint(min_items=1)]


@dataclass(frozen=True)
class StatedProvenance:
    """A package field's provenance exactly as the package states it, kept as its JSON."""

    document: JsonObject

    def __post_init__(self) -> None:
        if self.document.get("assertion_kind") not in ("observed", "stated"):
            raise ValueError("stated provenance must be observed or stated evidence")

    @property
    def assertion_kind(self) -> AssertionKind:
        return AssertionKind(self.document["assertion_kind"])

    def to_json(self) -> JsonObject:
        return self.document

    def __hash__(self) -> int:
        return hash(canonical_json.dumps(self.document))


@dataclass(frozen=True)
class RecordRef:
    """One catalogued record in one package: ``line`` is its 1-based line in its table."""

    package_id: PackageId
    kind: RecordKind
    record_id: RecordId
    line: Annotated[int, Constraint(minimum=1)]


@dataclass(frozen=True)
class EvidenceAnchor:
    """A source content id plus a locator: a record-level ``EvidenceRef`` (ADR 0003 §1).

    Compared by exact canonical-JSON equality of the locator; never normalised.
    """

    source: ContentId
    locator: Locator

    def __hash__(self) -> int:
        # Locator steps are JSON objects (dicts); hash their canonical bytes, as equality compares.
        return hash((self.source, canonical_json.dumps(list(self.locator))))


@dataclass(frozen=True)
class DeclaredKey:
    """A declared identifier: a ``LogicalId`` or a software content identifier (ADR 0003 §1).

    Namespace and value compare as exact code point sequences: no folding or trimming.
    """

    namespace: Token
    value: Text


@dataclass(frozen=True)
class ThreadKey:
    """``ThreadKey(kind, key)`` (ADR 0003 §1). Its JSON form is what ``thread_id`` hashes."""

    kind: ThreadKind
    key: DeclaredKey | EvidenceAnchor

    @property
    def thread_id(self) -> str:
        """``"sha256:" + hex(sha256(canonical JSON of {"key": …, "kind": …}))`` (ADR 0003 §1.3)."""
        from neptune_ledger.api.codec import to_json  # the codec imports this module

        return "sha256:" + hashlib.sha256(canonical_json.dumps(to_json(self))).hexdigest()


# --- register ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisterRequest:
    """``register(package_root)``: catalogue the package at a local directory."""

    package_root: Text


@dataclass(frozen=True)
class KindCount:
    """How many records of one kind a registered package holds."""

    kind: RecordKind
    count: Count


@dataclass(frozen=True)
class Registration:
    """The outcome of one ``register`` call (ADR 0002 §4, §6).

    - ``registered``: one transaction wrote the log row, the package row and every index row.
    - ``already_registered``: the package id was in the tenant; nothing was written and the stored
      ``registration_key``, ``root_locator`` and ``ledger_version`` are returned.
    - ``refused``: nothing was written; ``findings`` say why, and ``registration_key`` is
      ``NotApplicable``. ``package_id`` is ``Unknown`` when no manifest could be read.

    ``schema_version`` is the package-schema version the manifest declares, and every kind in
    ``record_counts`` is a kind of that version (1.6.0: kinds are named by the package-schema
    contract, Ledger ADR 0011 §4).
    """

    outcome: Literal["already_registered", "refused", "registered"]
    package_id: Knowledge[PackageId]
    registration_key: Knowledge[TransactionKey]
    root_locator: Text
    ledger_version: Text
    schema_version: Knowledge[int]
    record_counts: tuple[KindCount, ...]
    findings: tuple[CatalogFinding, ...]
    api_version: ApiVersion = CATALOG_API_VERSION


# --- verify ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyRequest:
    """``verify(package_id)``: re-hash a registered package where it was registered from."""

    package_id: Text
    as_of: TxSeq | None = None


@dataclass(frozen=True)
class VerifyReport:
    """Whether a registered package's bytes still match its id and its manifest.

    ``unknown_package``: the tenant never registered the id; ``registration_key`` and
    ``root_locator`` are ``NotCovered``. ``damaged``: ``findings`` list every mismatch.
    ``unreachable`` (1.1.0): the stored root locator cannot be read (moved, removed, not a
    directory or a symlink); nothing was compared, ``files_checked`` is 0 and a
    ``package_unreadable`` finding names the root. Verify reads and reports; it never repairs,
    re-registers or edits the catalog (Ledger ADR 0006 §2).
    """

    package_id: Text
    verdict: Literal["damaged", "intact", "unknown_package", "unreachable"]
    registration_key: Knowledge[TransactionKey]
    root_locator: Knowledge[str]
    files_checked: Count
    as_of: Knowledge[TransactionKey]
    findings: tuple[CatalogFinding, ...]
    api_version: ApiVersion = CATALOG_API_VERSION


# --- resolve -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolveRequest:
    """``resolve(evidence_ref)``: where cited evidence lives and how to fetch it."""

    evidence_ref: EvidenceAnchor
    as_of: TxSeq | None = None


@dataclass(frozen=True)
class Region:
    """The innermost locator step, which says what part of the source is cited.

    ``addressing`` is the step's kind for the package schema's core steps (byte range, record
    range, row, page, video frame …) and ``adapter`` for an adapter-specific step.
    """

    addressing: Literal[
        "adapter",
        "byte_range",
        "frame",
        "image_region",
        "json_pointer",
        "object",
        "page",
        "page_region",
        "record_range",
        "row",
        "row_cell",
        "span",
        "video_frame",
    ]
    step: LocatorStep


@dataclass(frozen=True)
class SourceLocation:
    """One package's route to the source bytes (ADR 0002 ``package_source``, ``source_location``).

    ``materialised``: the bytes are in the package at ``blob_path`` (relative to the package
    root). ``referenced``: the bytes stayed outside; ``locations`` are the locations the package
    states, verbatim, and ``blob_path`` is ``NotApplicable``. The Ledger never fetches them.
    """

    package_id: PackageId
    storage: Literal["materialised", "referenced"]
    blob_path: Knowledge[str]
    locations: tuple[JsonObject, ...]


@dataclass(frozen=True)
class Resolution:
    """What the catalog knows about one evidence anchor.

    ``unresolvable``: no registered package holds the source; ``size`` is ``NotCovered`` and
    ``fetch`` and ``cited_by`` are empty. ``cited_by`` lists the records whose record-level
    evidence anchor equals this one exactly, sorted by kind, record id, package id.
    """

    evidence_ref: EvidenceAnchor
    status: Literal["resolved", "unresolvable"]
    size: Knowledge[int]
    region: Region
    fetch: tuple[SourceLocation, ...]
    cited_by: tuple[RecordRef, ...]
    as_of: Knowledge[TransactionKey]
    findings: tuple[CatalogFinding, ...]
    api_version: ApiVersion = CATALOG_API_VERSION


# --- thread and threads_of ---------------------------------------------------------------------


@dataclass(frozen=True)
class History:
    """``history()``: every entry, nothing collapsed or hidden (ADR 0003 §4.2)."""

    tag_field: ClassVar[str] = "preference"
    tag: ClassVar[str] = "history"


@dataclass(frozen=True)
class LatestTransform:
    """``current(latest_transform)``: the undominated transform per lineage set (ADR 0003 §4.4)."""

    tag_field: ClassVar[str] = "preference"
    tag: ClassVar[str] = "latest_transform"


@dataclass(frozen=True)
class Pinned:
    """``current(pinned(transform id))``: that transform where it appears; ``NotCovered`` else."""

    tag_field: ClassVar[str] = "preference"
    tag: ClassVar[str] = "pinned"
    transform_id: TierTwoId


@dataclass(frozen=True)
class AsRegisteredBy:
    """``current(as_registered_by(package id))``: the transform that package used per set."""

    tag_field: ClassVar[str] = "preference"
    tag: ClassVar[str] = "as_registered_by"
    package_id: PackageId


# The ADR 0003 §4.4 preferences, and the thread() argument that also admits History. No default.
Preference: TypeAlias = LatestTransform | Pinned | AsRegisteredBy
ThreadPreference: TypeAlias = History | LatestTransform | Pinned | AsRegisteredBy


@dataclass(frozen=True)
class ClockMerge:
    """A caller-named reference clock and the ``ClockMapping`` record ids to merge with (§3)."""

    reference_clock: TierTwoId
    mappings: Annotated[tuple[TierTwoId, ...], Constraint(min_items=1, unique_items=True)]


@dataclass(frozen=True)
class ThreadRequest:
    """``thread(key, order, preference)``; ``merge`` and ``as_of`` are optional."""

    key: ThreadKey
    order: Order
    preference: ThreadPreference
    merge: ClockMerge | None = None
    as_of: TxSeq | None = None


@dataclass(frozen=True)
class TimePoint:
    """A timestamp as a package states it: ticks on one clock (root ADR 0005). Never converted."""

    domain_id: TierTwoId
    ticks: Ticks


@dataclass(frozen=True)
class WorldTime:
    """An entry's world time (ADR 0003 §3): its start ``s`` and its end ``e`` as stated.

    ``start`` is the Known ``s`` (or ``e`` when only the end is Known); its clock is the entry's
    ordering clock. ``end`` restates the package field the end comes from, state and provenance
    included: ``Unknown`` and ``NotCovered`` stay distinct, and an end on another clock is
    ``Known`` with its own ``domain_id``. The end is **open** unless it is ``Known`` on the start's
    clock; it is never dropped or converted.
    """

    start: TimePoint
    end: Annotated[Knowledge[TimePoint], Constraint(as_stated=True)]

    @property
    def clock(self) -> str:
        return self.start.domain_id

    @property
    def closed_end(self) -> int | None:
        """The end's ticks when it is Known on the start's clock; ``None`` when it is open."""
        match self.end:
            case Known(value=TimePoint(domain_id=clock, ticks=ticks)) if clock == self.clock:
                return ticks
        return None


@dataclass(frozen=True)
class MappedInterval:
    """An entry's interval on the reference clock and the mapping ids of its path (§3.4-3.5)."""

    reference_clock: TierTwoId
    lo: Ticks
    hi: Ticks
    path: tuple[TierTwoId, ...]


@dataclass(frozen=True)
class ThreadEntry:
    """One entry: a record id, its roles, and every package that registered it in this view.

    In ``history`` an entry is one ``(package id, record id)`` and ``packages`` has one element.
    In a current view the same record id from several packages appears once, ``packages`` in
    registration order. ``registration_key`` is that of ``packages[0]``. ``world`` is
    ``NotApplicable`` for kinds without world time and ``Unknown`` when no bound is ``Known``.
    """

    record_id: RecordId
    kind: RecordKind
    roles: Annotated[tuple[Role, ...], Constraint(min_items=1, unique_items=True)]
    packages: Annotated[tuple[PackageId, ...], Constraint(min_items=1, unique_items=True)]
    registration_key: TransactionKey
    transform_id: TierTwoId
    lineage_source: ContentId
    world: Knowledge[WorldTime]
    mapped: MappedInterval | None = None


@dataclass(frozen=True)
class Partition:
    """A run of entries whose adjacency means something (ADR 0003 §3).

    ``clock``: one ordering clock, ``clock_key`` its domain id. ``merged``: entries mapped onto
    ``clock_key``, the reference clock. ``untimed``: entries without world time. ``transaction``:
    the single partition of transaction order. Only inside one partition is order temporal.
    """

    kind: Literal["clock", "merged", "transaction", "untimed"]
    entries: tuple[ThreadEntry, ...]
    clock_key: TierTwoId | None = None


@dataclass(frozen=True)
class LineageSet:
    """``(thread, record kind, source content id)`` and how this view resolved it (§4.1, §4.3).

    ``resolution`` is ``Known(transform)``, ``Ambiguous(candidates by transform id)`` or
    ``NotCovered`` in a current view, and ``NotApplicable`` in ``history``.
    """

    kind: RecordKind
    source: ContentId
    transforms: Annotated[tuple[TierTwoId, ...], Constraint(min_items=1, unique_items=True)]
    resolution: Knowledge[TierTwoId]


@dataclass(frozen=True)
class RevisionEdge:
    """``(thread, kind, source) revises (thread, kind, revises)`` through two source revisions."""

    kind: RecordKind
    source: ContentId
    revises: ContentId
    source_revision: RecordId
    revised_revision: RecordId


@dataclass(frozen=True)
class UnresolvedMember:
    """A record whose ``Ambiguous`` field names this thread among its candidates (§2)."""

    package_id: PackageId
    record_id: RecordId
    kind: RecordKind
    pointer: str


@dataclass(frozen=True)
class ThreadLink:
    """An ``IdentityLink`` record relating two declared ids; never a merge (§1.5).

    ``from_key`` and ``to_key`` are the link's left and right ids as keys of the thread that
    lists it: their ``kind`` is the kind the caller asked for, a lookup, not something the link
    states. ``entity_kind`` is what the evidence states about the kind of thing the two ids
    name: ``NotCovered`` for every link of package schema 3, which has no field to state it in
    (ADR 0010 §8).
    """

    link_record_id: RecordId
    package_id: PackageId
    from_key: ThreadKey
    to_key: ThreadKey
    assertion_kind: Literal["observed", "stated"]
    state: Literal["ambiguous", "known", "not_applicable", "not_covered", "unknown"]
    entity_kind: Knowledge[EntityKind] = field(default_factory=NotCovered)


@dataclass(frozen=True)
class Thread:
    """One thread at one catalog point, in the requested order and view (ADR 0003 §3-§5).

    A pure function of ``(catalog as of as_of, key, preference, order, merge)``. ``preference`` is
    absent only when the call was rejected with ``preference_required``; then every collection is
    empty. Unknown keys are not errors: the thread is simply empty.
    """

    thread_id: ContentId
    key: ThreadKey
    order: Order
    as_of: Knowledge[TransactionKey]
    partitions: tuple[Partition, ...]
    lineage_sets: tuple[LineageSet, ...]
    revisions: tuple[RevisionEdge, ...]
    unresolved: tuple[UnresolvedMember, ...]
    links: tuple[ThreadLink, ...]
    findings: tuple[CatalogFinding, ...]
    preference: ThreadPreference | None = None
    merge: ClockMerge | None = None
    api_version: ApiVersion = CATALOG_API_VERSION


@dataclass(frozen=True)
class ThreadsOfRequest:
    """``threads_of(record_id)``: every thread a record belongs to (ADR 0003 §1.4)."""

    record_id: Text
    as_of: TxSeq | None = None


@dataclass(frozen=True)
class Membership:
    """One thread a record is a member of, in one registering package, with its roles."""

    thread_id: ContentId
    key: ThreadKey
    package_id: PackageId
    roles: Annotated[tuple[Role, ...], Constraint(min_items=1, unique_items=True)]


@dataclass(frozen=True)
class UnresolvedMembership:
    """A thread a record's ``Ambiguous`` field (at ``pointer``) names as a candidate."""

    thread_id: ContentId
    key: ThreadKey
    package_id: PackageId
    pointer: str


@dataclass(frozen=True)
class ThreadsOf:
    """Every thread of one record id, sorted by thread id then package id. Co-declared keys
    give several memberships; the Ledger reports them and never unions the threads."""

    record_id: Text
    status: Literal["found", "unknown_record"]
    memberships: tuple[Membership, ...]
    unresolved: tuple[UnresolvedMembership, ...]
    as_of: Knowledge[TransactionKey]
    findings: tuple[CatalogFinding, ...]
    api_version: ApiVersion = CATALOG_API_VERSION


# --- lineage -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LineageRequest:
    """``lineage(record_id)``: the transform DAG behind one record and its lineage siblings."""

    record_id: Text
    as_of: TxSeq | None = None


@dataclass(frozen=True)
class TransformInfo:
    """A registered ``TransformRecord``'s fields, as stated (ADR 0002 ``transform``)."""

    adapter_id: Token
    adapter_version: Text
    config_hash: ContentId
    libraries: JsonObject


@dataclass(frozen=True)
class LineageNode:
    """A transform in the DAG; ``transform`` is ``NotCovered`` when an edge names a transform
    that no registered package holds (the edge is kept, ADR 0002 §5)."""

    transform_id: TierTwoId
    transform: Knowledge[TransformInfo]


@dataclass(frozen=True)
class LineageEdge:
    """``transform_id`` consumed ``upstream_id``; ``position`` is its place in ``upstream``."""

    transform_id: TierTwoId
    upstream_id: TierTwoId
    position: Count


@dataclass(frozen=True)
class LineageGraph:
    """The lineage behind one record id (root ADR 0016; Ledger ADR 0003 §4.1).

    ``registered_by``: every package holding the record, in registration order. ``nodes``: the
    record's transform and everything upstream of it, sorted by transform id; ``edges`` sorted
    by ``(transform_id, position)``. ``siblings``: records of the same kind and evidence anchor
    produced by other transforms. ``unknown_record`` leaves every collection empty.
    """

    record_id: Text
    status: Literal["found", "unknown_record"]
    kind: Knowledge[RecordKind]
    transform_id: Knowledge[TierTwoId]
    registered_by: tuple[RecordRef, ...]
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]
    siblings: tuple[RecordRef, ...]
    as_of: Knowledge[TransactionKey]
    findings: tuple[CatalogFinding, ...]
    api_version: ApiVersion = CATALOG_API_VERSION


# --- query -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeWindow:
    """``[first, last]`` ticks, both inclusive, on one clock. Never applied across clocks."""

    clock: TierTwoId
    first: Ticks
    last: Ticks


@dataclass(frozen=True)
class QueryCursor:
    """A ``query`` row's sort key; ``QuerySpec.after`` returns the rows strictly after it."""

    kind: RecordKind
    record_id: RecordId
    package_id: PackageId


@dataclass(frozen=True)
class QuerySpec:
    """The filter contract ``query`` implements (MVL-98): kinds, a window, a thread, packages.

    Filters combine with AND. ``kinds`` is required. ``window`` keeps records whose world clock
    is ``window.clock`` and whose stated extent ``[world_first, world_last or world_first]``
    meets it; records without world time never match a window. ``thread_id`` keeps the thread's
    history entries. ``packages`` absent means every registered package. Rows are sorted by
    ``(kind, record_id, package_id)`` as UTF-8 bytes; ``after`` (1.1.0) keeps the rows whose key
    is strictly greater than the cursor, and ``limit`` keeps the first rows, so the last row of
    one page is the next page's cursor (Ledger ADR 0006 §6).
    """

    kinds: Annotated[tuple[RecordKind, ...], Constraint(min_items=1, unique_items=True)]
    window: TimeWindow | None = None
    thread_id: ContentId | None = None
    packages: Annotated[tuple[PackageId, ...], Constraint(unique_items=True)] | None = None
    as_of: TxSeq | None = None
    limit: Annotated[int, Constraint(minimum=1)] | None = None
    after: QueryCursor | None = None


@dataclass(frozen=True)
class QueryRequest:
    """``query(spec)``."""

    spec: QuerySpec


@dataclass(frozen=True)
class QueryRow:
    """One row of a ``query`` result; the Arrow columns are these fields, in this order.

    The catalog ``record`` row (ADR 0002 §5) with its locator as canonical JSON text. An absent
    field is a NULL index column: not ``Known`` in the record, never "absent" in the world.
    """

    kind: RecordKind
    record_id: RecordId
    package_id: PackageId
    line: Annotated[int, Constraint(minimum=1)]
    registration_seq: TxSeq
    transform_id: TierTwoId | None = None
    source_content_id: ContentId | None = None
    source_locator: str | None = None
    assertion_kind: Literal["observed", "stated"] | None = None
    world_clock: TierTwoId | None = None
    world_first: Ticks | None = None
    world_last: Ticks | None = None


@dataclass(frozen=True)
class QueryMeta:
    """What a ``query`` result's Arrow schema metadata carries besides its rows."""

    as_of: Knowledge[TransactionKey]
    findings: tuple[CatalogFinding, ...]
    api_version: ApiVersion = CATALOG_API_VERSION


REQUEST_TYPES: Final = (
    RegisterRequest,
    VerifyRequest,
    ResolveRequest,
    ThreadRequest,
    ThreadsOfRequest,
    LineageRequest,
    QueryRequest,
)
RESPONSE_TYPES: Final = (
    Registration,
    VerifyReport,
    Resolution,
    Thread,
    ThreadsOf,
    LineageGraph,
    QueryRow,
    QueryMeta,
)
