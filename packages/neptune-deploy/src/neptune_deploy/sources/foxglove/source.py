"""``FoxgloveSource``: a Foxglove Data Platform recording index as a read-only compiler ``Source``
(ADR 0007).

It has the shape of the compiler's ``Source`` protocol and of the object-store source (ADR 0006):
the same entry types, ``ObjectReadError``, ranged stream and adapter reader. What a recording
index needs that a bucket does not:

- ``index()``: every recording the API lists, sorted by id, with no request for its bytes. Identity
  is ``ExternalObjectRef("deploy_foxglove", [<store>:]recording/<recording id>, <revision token>)``;
  the token is built from the API's own ``importedAt``, ``createdAt`` and ``size``, so a recording
  imported again is a new revision of the same recording, never a new recording.
- ``discover(ledger)``: the index against the compiler's ledger, as for any connector. A recording
  the ledger holds and a complete listing lacks is asserted gone only after ``GET /recordings/{id}``
  says 404, because offset paging over a live index can skip entries.
- ``walk()``: what to fingerprint and probe. A stream's size is not the recording's stored ``size``
  (Foxglove re-encodes it as MCAP), so each entry's size is measured, with one one-byte ranged read.
  It is measured only for what ``walk`` yields: unchanged recordings cost no stream request.
- ``declared(location)``: what the API *states* about a recording (ids, device, session, time range,
  declared topics, MCAP metadata, the device's properties) as ``Knowledge`` values with ``stated``
  provenance, each citing the response object it came from. Device data is declared identifiers
  and values only; nothing is ever merged or picked (ADR 0007 §4).
- ``open(location)`` / ``reader(location, artifact)``: ranged reads of the recording's MCAP stream,
  through the object-store source's windowed stream and chunk-checked reader.

Every problem is a finding (``findings()``), deterministic and free of URLs, keys and error text.
"""

import io
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import BinaryIO, Final

from neptune.identity.canonical_json import CanonicalJsonError
from neptune.identity.canonical_json import dumps as canonical_dumps
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import IngestFinding
from neptune.model.ids import ExternalObjectRef
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import TransformRecord
from neptune.model.source import SourceArtifact, SourceLocation, SourceRevision
from neptune_deploy.sources.foxglove.client import FoxgloveClient
from neptune_deploy.sources.foxglove.codes import CODES, SKIP_REASONS, failure
from neptune_deploy.sources.foxglove.config import (
    CONNECTOR_ID,
    CONNECTOR_VERSION,
    MAX_ID,
    Options,
    valid_id,
)
from neptune_deploy.sources.foxglove.declared import DeclaredRecording, Declarer
from neptune_deploy.sources.foxglove.validation import (
    MAX_TOKEN_PART,
    Invalid,
    stable_name,
    strip_nulls,
    text,
)
from neptune_deploy.sources.object_store.clients import RangeInvalid
from neptune_deploy.sources.object_store.source import (
    MAX_EXAMPLES,
    MAX_PAGES,
    MIN_WINDOW,
    ObjectEntry,
    ObjectReader,
    ObjectReadError,
    ObjectStream,
    SkippedObject,
    absent_candidates,
    classify,
)
from neptune_deploy.sources.object_store.transport import (
    HttpStatusError,
    NetworkGate,
    ShortRead,
    TransportError,
)

MAX_STREAM_BYTES: Final = 1 << 44  # a stream claiming more than 16 TiB is refused
MAX_VERIFICATIONS: Final = 1000  # ``GET /recordings/{id}`` calls one ``discover`` makes
LISTING_TOKEN: Final = "listing"


# --- Entries ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Recording:
    """One recording as the index states it: its identity and the object the API returned."""

    location: ExternalObjectRef
    recording_id: str
    document: JsonObject = field(compare=False, hash=False, repr=False)

    @property
    def key(self) -> str:
        return self.recording_id


@dataclass(frozen=True)
class StreamEntry(ObjectEntry):
    """A recording's MCAP stream, measured: location, size in bytes, recording id as ``key``."""

    @property
    def name(self) -> str:
        """The probe's name hint. The stream is MCAP whatever the recording was uploaded as."""
        return f"{self.key}.mcap"


@dataclass(frozen=True)
class RecordingIndex:
    """Every usable recording, sorted by id, and what was not used."""

    recordings: tuple[Recording, ...]
    skipped: tuple[SkippedObject, ...]
    complete: bool  # the API said it had no more; only then is anything asserted gone


@dataclass(frozen=True)
class FoxgloveDiscovery:
    """The index against a ledger: what needs probing, what does not, and what is gone."""

    new: tuple[Recording, ...]
    changed: tuple[Recording, ...]
    unchanged: tuple[tuple[Recording, SourceRevision], ...]
    gone: tuple[SourceRevision, ...]
    complete: bool

    @property
    def to_probe(self) -> tuple[Recording, ...]:
        """New and changed recordings, in id order: the only ones a job measures and reads."""
        return tuple(sorted((*self.new, *self.changed), key=lambda r: r.recording_id))


class FoxgloveSource:
    """A Foxglove recording index, read only, as a compiler ``Source``."""

    def __init__(
        self,
        project: str | None,
        client: FoxgloveClient,
        network: NetworkGate,
        options: Options,
        *,
        ledger: SourceLedger | None = None,
    ) -> None:
        self.project = project
        self.client = client
        self.options = options
        self.connector_id = CONNECTOR_ID
        self._network = network
        self._ledger = ledger
        self._findings: dict[str, IngestFinding] = {}
        self._sizes: dict[ExternalObjectRef, StreamEntry | str] = {}
        self._declarer = Declarer(self)

    # --- Identity ------------------------------------------------------------------------------

    @property
    def scope(self) -> str:
        """What every object id starts with: ``recording/``, after ``<store>:`` for a declared
        endpoint, whose ids are its own (ADR 0007 §3). The project is not part of it."""
        store = f"{self.options.store}:" if self.options.store else ""
        return f"{store}recording/"

    def object_id(self, recording_id: str) -> str:
        return self.scope + recording_id

    @cached_property
    def transform(self) -> TransformRecord:
        """The connector as a producer: what decided which recordings were seen and what the
        stream's bytes are. No endpoint, key or link host: they say where, not what."""
        config: dict[str, JsonValue] = {
            "compression": self.options.compression,
            "identifier_properties": list(self.options.identifier_properties),
            "max_recordings": self.options.max_recordings,
            "project": self.project if self.project is not None else "-",
            "topics": self.options.topics,
        }
        for name in ("device_id", "device_name", "start", "end", "store"):
            value = getattr(self.options, name)
            if value is not None:
                config[name] = value
        return transform_record(
            adapter_id=CONNECTOR_ID, adapter_version=CONNECTOR_VERSION, config=config
        )

    @property
    def listing_ref(self) -> ExternalObjectRef:
        """The subject of a finding about the listing as a whole."""
        return ExternalObjectRef(CONNECTOR_ID, self.scope + "index", LISTING_TOKEN)

    def ref(self, recording_id: str, token: str) -> ExternalObjectRef:
        return ExternalObjectRef(CONNECTOR_ID, self.object_id(recording_id), token)

    # --- Findings ------------------------------------------------------------------------------

    def report(self, code: str, subject: ExternalObjectRef, details: dict[str, JsonValue]) -> None:
        category, severity, message = CODES[code]
        finding = ingest_finding(
            code=f"{CONNECTOR_ID}.{code}",
            category=category,
            severity=severity,
            subject=subject,
            transform=self.transform,
            message=message,
            details=details,
        )
        self._findings[finding.id] = finding

    def findings(self) -> tuple[IngestFinding, ...]:
        """Every finding so far, sorted by id."""
        return tuple(self._findings[key] for key in sorted(self._findings))

    # --- The index -----------------------------------------------------------------------------

    def _filters(self) -> list[tuple[str, str]]:
        found = [
            ("projectId", self.project),
            ("deviceId", self.options.device_id),
            ("deviceName", self.options.device_name),
            ("start", self.options.start),
            ("end", self.options.end),
        ]
        return [(name, value) for name, value in found if value is not None]

    def _recording(self, item: JsonValue) -> Recording:
        """``item`` as a usable recording, else ``Invalid(reason)``."""
        if not isinstance(item, dict):
            raise Invalid("record_invalid")
        doc = strip_nulls(item)
        assert isinstance(doc, dict)
        recording_id = doc.get("id")
        if not isinstance(recording_id, str):
            raise Invalid("record_invalid")
        if not valid_id(recording_id):
            raise Invalid("recording_id_invalid")
        size = doc.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise Invalid("record_invalid")
        for name in ("createdAt", "start", "end", "importStatus", "projectId"):
            text(doc.get(name), MAX_TOKEN_PART if name != "projectId" else MAX_ID)
        text(doc.get("path"))
        if "importedAt" in doc:
            text(doc["importedAt"], MAX_TOKEN_PART)
        self._check_optional(doc)
        try:
            canonical_dumps(doc)
        except CanonicalJsonError as exc:
            raise Invalid("record_invalid") from exc
        if doc["importStatus"] != "complete":
            raise Invalid("import_incomplete")
        token = f"import:{doc.get('importedAt', '-')};created:{doc['createdAt']};size:{size}"
        return Recording(self.ref(recording_id, token), recording_id, doc)

    @staticmethod
    def _check_optional(doc: JsonObject) -> None:
        """The optional parts a declared record reads (device, key, session, metadata) are well
        formed if present, so ``declared`` never meets a malformed one."""
        device = doc.get("device")
        if device is not None:
            if not isinstance(device, dict) or not valid_id(device.get("id")):
                raise Invalid("record_invalid")
            if "name" in device:
                text(device["name"], 100)
        for name in ("key", "sessionId"):
            if name in doc:
                text(doc[name])
        metadata = doc.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, list):
                raise Invalid("record_invalid")
            for record in metadata:
                if not isinstance(record, dict) or not isinstance(record.get("metadata", {}), dict):
                    raise Invalid("record_invalid")
                text(record.get("name"))

    @cached_property
    def _index(self) -> RecordingIndex:
        limit = self.options.max_recordings
        kept: dict[str, Recording] = {}
        duplicated: set[str] = set()
        skipped: dict[tuple[str, bytes], str] = {}  # (reason, raw) -> import status ("" if none)
        distinct: set[bytes] = set()
        complete = limited = False
        offset = pages = 0
        while not limited:
            if pages >= MAX_PAGES:
                self.report("listing_limit", self.listing_ref, {"pages": pages})
                break
            try:
                page = self.client.recordings(self._filters(), offset, self.options.page_size)
            except TransportError as exc:
                code, details = failure(exc, "listing_failed")
                self.report(code, self.listing_ref, {"page": pages, **details})
                break
            pages += 1
            if not page:
                complete = True
                break
            before = len(distinct)
            for item in page:
                raw = item.get("id") if isinstance(item, dict) else None
                name = (
                    raw.encode("utf-8", "backslashreplace")
                    if isinstance(raw, str)
                    else stable_name(item)
                )
                if name not in distinct and len(distinct) >= limit:
                    self.report("listing_limit", self.listing_ref, {"max_recordings": limit})
                    limited = True
                    break
                distinct.add(name)
                try:
                    recording = self._recording(item)
                except Invalid as bad:
                    status = ""
                    if bad.reason == "import_incomplete" and isinstance(item, dict):
                        found = item.get("importStatus")
                        status = found if isinstance(found, str) and len(found) <= 32 else "other"
                    skipped[(bad.reason, name)] = status
                    continue
                known = kept.get(recording.recording_id)
                if known is not None and known.document != recording.document:
                    duplicated.add(recording.recording_id)
                else:
                    kept[recording.recording_id] = recording
            if not limited and len(distinct) == before:
                self.report("pagination_loop", self.listing_ref, {"page": pages})
                break
            offset += len(page)
        for recording_id in duplicated:
            del kept[recording_id]
            skipped[("recording_duplicated", recording_id.encode("utf-8"))] = ""
        recordings = tuple(kept[key] for key in sorted(kept))
        unique = tuple(SkippedObject(raw, reason) for (reason, raw) in sorted(skipped))
        self._report_skipped(unique, skipped)
        return RecordingIndex(recordings, unique, complete)

    def _report_skipped(
        self, skipped: tuple[SkippedObject, ...], statuses: Mapping[tuple[str, bytes], str]
    ) -> None:
        """One finding per reason, citing the first ids (as hex) and counting them all."""
        by_reason: defaultdict[str, list[bytes]] = defaultdict(list)
        for item in skipped:
            by_reason[item.reason].append(item.raw_key)
        for reason in SKIP_REASONS:
            keys = by_reason.get(reason)
            if not keys:
                continue
            examples: list[JsonValue] = [key[:256].hex() for key in keys[:MAX_EXAMPLES]]
            details: dict[str, JsonValue] = {"count": len(keys), "keys_hex": examples}
            if reason == "import_incomplete":
                counts: defaultdict[str, int] = defaultdict(int)
                for key in keys:
                    counts[statuses[(reason, key)]] += 1
                details["statuses"] = dict(sorted(counts.items()))
            self.report(reason, self.listing_ref, details)

    def index(self) -> RecordingIndex:
        """Every page of the index, read once per source and kept."""
        return self._index

    # --- Discovery -----------------------------------------------------------------------------

    def discover(self, ledger: SourceLedger) -> FoxgloveDiscovery:
        """The index against ``ledger`` (a ledger of this ingest root, ADR 0009)."""
        index = self.index()
        new, changed, unchanged = classify(index.recordings, ledger)
        return FoxgloveDiscovery(new, changed, unchanged, self._gone(index, ledger), index.complete)

    def _gone(self, index: RecordingIndex, ledger: SourceLedger) -> tuple[SourceRevision, ...]:
        """Ledger revisions a complete index lacks *and* the API says do not exist (404).

        Offset paging over a live index can skip an entry when another is deleted mid-listing, and
        a ledger may hold recordings another project's or device's source ingested, so a blank in
        the listing is never enough: each candidate is asked for by id.
        """
        if not index.complete:
            return ()
        seen = {recording.location.object_id for recording in index.recordings}
        for item in index.skipped:  # seen but not used: nothing is known of them
            seen.add(self.object_id(item.raw_key.decode("utf-8", "backslashreplace")))
        candidates = absent_candidates(ledger, CONNECTOR_ID, self.scope, seen)
        gone: list[SourceRevision] = []
        unverified = 0
        for revision in candidates:
            where = revision.location
            assert isinstance(where, ExternalObjectRef)
            recording_id = where.object_id[len(self.scope) :]
            if len(gone) + unverified >= MAX_VERIFICATIONS or not valid_id(recording_id):
                unverified += 1
                continue
            try:
                found = self.client.recording(recording_id)
            except TransportError:
                unverified += 1
                continue
            if found is None:
                gone.append(revision)
        if unverified:
            self.report("gone_unverified", self.listing_ref, {"count": unverified})
        return tuple(gone)

    # --- Measuring streams ---------------------------------------------------------------------

    def _measure(self, recording: Recording) -> StreamEntry | str:
        """The recording's stream, with its size, or the finding code that says why it has none."""
        where = recording.location
        try:
            link = self.client.stream_link(recording.recording_id)
            got = self.client.get_range(link, 0, 1)
        except HttpStatusError as exc:
            if exc.status == 416:
                self.report("stream_empty", where, {"status": 416})
                return "stream_empty"
            code, details = failure(exc, "stream_unavailable")
            self.report(code, where, details)
            return code
        except ShortRead:
            self.report("stream_empty", where, {})
            return "stream_empty"
        except TransportError as exc:
            code, details = failure(exc, "stream_unavailable")
            self.report(code, where, details)
            return code
        if got.total is None:
            self.report("size_unknown", where, {})
            return "size_unknown"
        if got.total > MAX_STREAM_BYTES:
            self.report("stream_too_large", where, {"limit": MAX_STREAM_BYTES})
            return "stream_too_large"
        if got.total == 0:
            self.report("stream_empty", where, {})
            return "stream_empty"
        return StreamEntry(where, got.total, recording.recording_id)

    def resolve(self, recording: Recording) -> StreamEntry | str:
        """``recording``'s measured stream (once per source), or the code that says why not."""
        if recording.location not in self._sizes:
            self._sizes[recording.location] = self._measure(recording)
        return self._sizes[recording.location]

    # --- The Source protocol -------------------------------------------------------------------

    def walk(self) -> Iterator[StreamEntry | SkippedObject]:
        """What to fingerprint and probe, in id order, then what was not used.

        With a ledger, unchanged recordings are left out: they keep their ledger revision, and no
        stream is requested for them. A recording whose stream cannot be measured comes after, as a
        ``SkippedObject`` with the finding's code.
        """
        index = self.index()
        if self._ledger is None:
            todo: Sequence[Recording] = index.recordings
        else:
            new, changed, _ = classify(index.recordings, self._ledger)
            todo = sorted((*new, *changed), key=lambda r: r.recording_id)
        failed: list[SkippedObject] = []
        for recording in todo:
            entry = self.resolve(recording)
            if isinstance(entry, StreamEntry):
                yield entry
            else:
                failed.append(SkippedObject(recording.recording_id.encode("utf-8"), entry))
        yield from failed
        yield from index.skipped

    @cached_property
    def _by_location(self) -> dict[ExternalObjectRef, Recording]:
        return {recording.location: recording for recording in self.index().recordings}

    def entry(self, location: SourceLocation) -> StreamEntry:
        """The measured stream at ``location``, at the revision the index holds.

        Another connector's location is a ``TypeError``; one the index does not hold at that
        revision is ``ObjectReadError("not_listed")`` with no finding (the caller's mistake); a
        stream that cannot be measured raises ``ObjectReadError`` with the finding's code.
        """
        if (
            isinstance(location, ExternalObjectRef)
            and location.connector_id == CONNECTOR_ID
            and location.object_id.startswith(self.scope)
        ):
            recording = self._by_location.get(location)
            if recording is None:
                raise ObjectReadError("not_listed", location)
            entry = self.resolve(recording)
            if isinstance(entry, str):
                raise ObjectReadError(entry, location)
            return entry
        raise TypeError(f"{CONNECTOR_ID} cannot open {location!r}")

    def open(self, location: SourceLocation) -> BinaryIO:
        """A seekable, read-only stream over the recording's MCAP, fetched in ranges as read."""
        return io.BufferedReader(ObjectStream(self, self.entry(location)), MIN_WINDOW)

    def reader(self, location: SourceLocation, artifact: SourceArtifact) -> ObjectReader:
        """An adapter's reader over ``location``, whose bytes were fingerprinted as ``artifact``."""
        return ObjectReader(self, self.entry(location), artifact)

    def close(self) -> None:
        """Close every connection."""
        self.client.drop()

    # --- Ranged reads --------------------------------------------------------------------------

    def fetch(self, entry: ObjectEntry, start: int, length: int) -> bytes:
        """``length`` bytes of ``entry``'s stream from ``start``.

        One request for a fresh link (they expire in seconds, so none is kept) and one ranged read
        of it. The stream's total size, as the answer states it, must still be the measured one.
        """
        if start < 0 or length <= 0 or start + length > entry.size:
            raise ValueError(f"bytes {start}+{length} are outside a stream of {entry.size}")
        where = entry.location
        span: dict[str, JsonValue] = {"length": length, "offset": start}
        try:
            link = self.client.stream_link(entry.key)
            got = self.client.get_range(link, start, length)
        except ShortRead as exc:
            self.report("short_read", where, span)
            raise ObjectReadError("short_read", where) from exc
        except HttpStatusError as exc:
            code, details = failure(exc, "read_failed")
            if code == "read_failed":
                code = {404: "object_gone", 412: "object_changed", 416: "object_changed"}.get(
                    exc.status or 0, "read_failed"
                )
            self.report(code, where, {**span, **details})
            raise ObjectReadError(code, where) from exc
        except RangeInvalid as exc:
            self.report("read_failed", where, {**span, "cause": exc.code})
            raise ObjectReadError("read_failed", where) from exc
        except TransportError as exc:
            code, details = failure(exc, "read_failed")
            self.report(code, where, {**span, **details})
            raise ObjectReadError(code, where) from exc
        if got.total is not None and got.total != entry.size:
            self.report("object_changed", where, {**span, "size": got.total})
            raise ObjectReadError("object_changed", where)
        return got.data

    # --- Declared metadata ---------------------------------------------------------------------

    def declared(self, location: SourceLocation) -> DeclaredRecording:
        """What the API states about the recording at ``location`` (see ``DeclaredRecording``)."""
        if not isinstance(location, ExternalObjectRef):
            raise TypeError(f"{CONNECTOR_ID} does not describe {location!r}")
        if location not in self._by_location:
            raise ObjectReadError("not_listed", location)
        return self._declarer.declared(self._by_location[location])
