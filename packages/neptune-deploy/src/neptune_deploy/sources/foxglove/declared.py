"""What the API *states* about a recording, as evidence (ADR 0007 §4).

Every value is a ``Knowledge`` with ``stated`` provenance that cites the one response object it came
from: the call, the SHA-256 of that object in canonical JSON (nulls dropped, so it does not depend
on page size or order) and a JSON pointer into it. Nothing here is inferred, merged, ranked or
reconciled:

- Device data is *declared identifiers* (the device id and name, the recording's key, session and
  project, and only the device properties the operator declared to be identifiers) and verbatim
  values. Two devices, or two recordings, that share a name or a property are not thereby the same
  thing: consolidation is Memory's decision, from these declarations.
- A recording and the device list that give one device id two names are ``Ambiguous`` with both,
  and a finding. A rename is exactly that, until the next index agrees.
- An absent value is ``Unknown``; a topic list that was not read is ``Unknown`` (the call failed)
  or ``NotCovered`` (the ``topics`` option is off). A blank is never a fact.
"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from neptune.identity.canonical_json import CanonicalJsonError
from neptune.identity.canonical_json import dumps as canonical_dumps
from neptune.identity.hashing import content_id
from neptune.model.ids import ExternalObjectRef, LogicalId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    NotCovered,
    Unknown,
)
from neptune.model.knowledge import to_json as knowledge_json
from neptune.model.provenance import (
    EvidenceRef,
    JsonPointer,
    Provenance,
    TransformRecord,
    adapter_locator,
)
from neptune_deploy.sources.foxglove.client import FoxgloveClient
from neptune_deploy.sources.foxglove.codes import failure
from neptune_deploy.sources.foxglove.config import Options, valid_id
from neptune_deploy.sources.foxglove.validation import (
    MAX_DOCUMENT_BYTES,
    Invalid,
    item_digest,
    json_pointer,
    strip_nulls,
    text,
)
from neptune_deploy.sources.object_store.source import MAX_PAGES
from neptune_deploy.sources.object_store.transport import TransportError

if TYPE_CHECKING:
    from neptune_deploy.sources.foxglove.source import Recording

MAX_TOPIC_PAGES: Final = 50
MAX_TOPICS: Final = 10_000  # topics one recording may declare
MAX_TOPIC_BYTES: Final = 16 * 1024 * 1024  # canonical JSON bytes of one recording's topics
MAX_DECLARED_CACHE: Final = 256  # recordings whose declared metadata is kept for repeat calls
LOCATOR_KIND: Final = "deploy_foxglove:response"

# Namespaces of the declared identifiers (``LogicalId.namespace``), one per thing Foxglove names.
NS_RECORDING_ID: Final = "foxglove.recording_id"
NS_RECORDING_KEY: Final = "foxglove.recording_key"
NS_DEVICE_ID: Final = "foxglove.device_id"
NS_DEVICE_NAME: Final = "foxglove.device_name"
NS_SESSION_ID: Final = "foxglove.session_id"
NS_PROJECT_ID: Final = "foxglove.project_id"
NS_DEVICE_PROPERTY: Final = "foxglove.device_property."

# The recording's own fields that become facts: (fact name, field in the API's recording).
FACT_FIELDS: Final = (
    ("created_at", "createdAt"),
    ("end", "end"),
    ("import_status", "importStatus"),
    ("imported_at", "importedAt"),
    ("path", "path"),
    ("size", "size"),
    ("start", "start"),
)


@dataclass(frozen=True)
class DeclaredTopic:
    """A topic as ``GET /data/topics`` states it (the schema itself is not requested)."""

    topic: str
    schema_name: str
    schema_encoding: str
    encoding: str
    version: str

    def to_json(self) -> JsonObject:
        return {
            "encoding": self.encoding,
            "schema_encoding": self.schema_encoding,
            "schema_name": self.schema_name,
            "topic": self.topic,
            "version": self.version,
        }


@dataclass(frozen=True)
class DeclaredEntry:
    """A named value exactly as the API states it: a device property, an MCAP metadata record."""

    name: str
    value: JsonValue

    def to_json(self) -> JsonObject:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True)
class DeclaredRecording:
    """What the API states about one recording at one revision. Evidence only.

    - ``identifiers``: ids the API gives the recording, its device, session and project, plus the
      device properties the operator declared to be identifiers. Two values for one thing in one
      namespace are ``Ambiguous``. Sorted by (namespace, value), each once.
    - ``facts``: named values (``created_at``, ``end``, ``import_status``, ``imported_at``,
      ``path``, ``size``, ``start``), verbatim: the API's time strings are not converted.
    - ``topics`` and ``topics_coverage``: the topics the recording declares, and how many
      (``Known``), or that they were not read (``Unknown``: the call failed; ``NotCovered``: the
      ``topics`` option is off).
    - ``device_properties`` and ``metadata``: the device's custom properties and the recording's
      MCAP metadata records, verbatim.
    """

    location: ExternalObjectRef
    identifiers: tuple[Knowledge[LogicalId], ...]
    facts: tuple[tuple[str, Knowledge[JsonValue]], ...]
    topics: tuple[Knowledge[DeclaredTopic], ...]
    topics_coverage: Knowledge[int]
    device_properties: tuple[Knowledge[DeclaredEntry], ...]
    metadata: tuple[Knowledge[DeclaredEntry], ...]

    def to_json(self) -> JsonObject:
        """A canonical JSON form (for tests, goldens and receipts)."""

        def many(items: tuple[Knowledge[DeclaredEntry], ...]) -> list[JsonValue]:
            return [knowledge_json(k, DeclaredEntry.to_json) for k in items]

        return {
            "device_properties": many(self.device_properties),
            "facts": [{"name": name, **knowledge_json(k)} for name, k in self.facts],
            "identifiers": [knowledge_json(k, LogicalId.to_json) for k in self.identifiers],
            "location": self.location.to_json(),
            "metadata": many(self.metadata),
            "topics": [knowledge_json(k, DeclaredTopic.to_json) for k in self.topics],
            "topics_coverage": knowledge_json(self.topics_coverage),
        }


class DeclarerHost(Protocol):
    """What declaring needs of its source: the API, the options and the finding log."""

    client: FoxgloveClient
    options: Options
    project: str | None

    @property
    def transform(self) -> TransformRecord: ...

    @property
    def listing_ref(self) -> ExternalObjectRef: ...

    def report(
        self, code: str, subject: ExternalObjectRef, details: dict[str, JsonValue]
    ) -> None: ...


class Declarer:
    """Builds ``DeclaredRecording``s for one source. Reads the device list once, and a recording's
    topics once per recording, on demand."""

    def __init__(self, host: DeclarerHost) -> None:
        self._host = host
        self._devices: dict[str, JsonObject] | None = None
        self._cache: OrderedDict[ExternalObjectRef, DeclaredRecording] = OrderedDict()

    def declared(self, recording: "Recording") -> DeclaredRecording:
        """Declared metadata of ``recording``; the last few are kept, so a million-recording index
        does not become a million kept results."""
        if recording.location in self._cache:
            self._cache.move_to_end(recording.location)
        else:
            self._cache[recording.location] = self._declare(recording)
            if len(self._cache) > MAX_DECLARED_CACHE:
                self._cache.popitem(last=False)
        return self._cache[recording.location]

    # --- Provenance ----------------------------------------------------------------------------

    def _grounding(
        self, where: ExternalObjectRef, call: str, document: JsonValue, pointer: str, rid: str
    ) -> Provenance:
        """``stated`` provenance citing one response object (see the module docstring)."""
        locator = adapter_locator(
            LOCATOR_KIND,
            {
                "call": call,
                "object_sha256": content_id(canonical_dumps(document)),
                "recording": rid,
            },
        )
        evidence = EvidenceRef(where, (locator, JsonPointer(pointer)))
        return Provenance(evidence, self._host.transform.id, AssertionKind.STATED)

    # --- Devices -------------------------------------------------------------------------------

    def devices(self) -> dict[str, JsonObject]:
        """Devices by id, from every page of ``GET /devices`` (empty, with a finding on failure)."""
        if self._devices is None:
            self._devices = self._read_devices()
        return self._devices

    def _read_devices(self) -> dict[str, JsonObject]:
        host = self._host
        found: dict[str, JsonObject] = {}
        offset = pages = invalid = held = 0
        seen: set[str] = set()  # ids of every device entry seen, to notice a page served again
        while pages < MAX_PAGES:
            try:
                page = host.client.devices(host.project, offset, host.options.page_size)
            except TransportError as exc:
                code, details = failure(exc, "devices_failed")
                host.report(code, host.listing_ref, {"page": pages, **details})
                break
            pages += 1
            if not page:
                break
            offset += len(page)
            before = len(seen)
            for item in page:
                doc = strip_nulls(item)
                seen.add(item_digest(item))
                try:
                    if not isinstance(doc, dict) or not valid_id(doc.get("id")):
                        raise Invalid("record_invalid")
                    text(doc.get("name"), 100)
                    if not isinstance(doc.get("properties", {}), dict):
                        raise Invalid("record_invalid")
                    weight = len(canonical_dumps(doc))
                    if weight > MAX_DOCUMENT_BYTES:
                        raise Invalid("record_invalid")
                except (Invalid, CanonicalJsonError):
                    invalid += 1
                    continue
                if held + weight > host.options.max_listing_bytes:
                    host.report("listing_limit", host.listing_ref, {"call": "devices"})
                    return found  # the devices after this one are not covered
                if str(doc["id"]) not in found:
                    found[str(doc["id"])] = doc
                    held += weight
            if len(seen) == before:
                host.report("pagination_loop", host.listing_ref, {"call": "devices"})
                break
        if invalid:
            host.report("record_invalid", host.listing_ref, {"call": "devices", "count": invalid})
        return found

    # --- One recording -------------------------------------------------------------------------

    def _declare(self, recording: "Recording") -> DeclaredRecording:
        where, doc, rid = recording.location, recording.document, recording.recording_id

        def stated(pointer: str) -> Provenance:
            return self._grounding(where, "GET /recordings", doc, pointer, rid)

        identifiers: dict[tuple[str, str], Knowledge[LogicalId]] = {}

        def declare(namespace: str, value: str, grounding: Provenance) -> None:
            identifiers[(namespace, value)] = Known(LogicalId(namespace, value), grounding)

        declare(NS_RECORDING_ID, rid, stated("/id"))
        for name, namespace in (
            ("key", NS_RECORDING_KEY),
            ("sessionId", NS_SESSION_ID),
            ("projectId", NS_PROJECT_ID),
        ):
            if name in doc:
                declare(namespace, str(doc[name]), stated(json_pointer(name)))
        properties: list[Knowledge[DeclaredEntry]] = []
        device = doc.get("device")
        if isinstance(device, dict):
            device_id = str(device["id"])
            declare(NS_DEVICE_ID, device_id, stated("/device/id"))
            device_doc = self.devices().get(device_id)
            self._declare_name(identifiers, device, device_doc, recording)
            if device_doc is not None:
                properties = self._declare_properties(identifiers, device_doc, recording)
        topics, coverage = self._declare_topics(recording)
        metadata = tuple(
            Known(
                DeclaredEntry(str(record["name"]), record.get("metadata", {})),
                stated(json_pointer("metadata", index)),
            )
            for index, record in enumerate(_records(doc.get("metadata")))
        )
        facts: list[tuple[str, Knowledge[JsonValue]]] = []
        for name, field_name in FACT_FIELDS:
            if field_name in doc:
                facts.append((name, Known(doc[field_name], stated(json_pointer(field_name)))))
            else:
                facts.append((name, Unknown(stated(""))))
        return DeclaredRecording(
            location=where,
            identifiers=tuple(identifiers[key] for key in sorted(identifiers)),
            facts=tuple(facts),
            topics=topics,
            topics_coverage=coverage,
            device_properties=tuple(sorted(properties, key=_entry_name)),
            metadata=metadata,
        )

    def _declare_name(
        self,
        identifiers: dict[tuple[str, str], Knowledge[LogicalId]],
        device: JsonObject,
        device_doc: JsonObject | None,
        recording: "Recording",
    ) -> None:
        """The device's name: ``Known`` if every response gives one name, else ``Ambiguous``."""
        where, rid = recording.location, recording.recording_id
        names: list[Candidate[LogicalId]] = []
        if "name" in device:
            grounding = self._grounding(
                where, "GET /recordings", recording.document, "/device/name", rid
            )
            names.append(Candidate(LogicalId(NS_DEVICE_NAME, str(device["name"])), grounding))
        if device_doc is not None:
            grounding = self._grounding(where, "GET /devices", device_doc, "/name", rid)
            listed = Candidate(LogicalId(NS_DEVICE_NAME, str(device_doc["name"])), grounding)
            if not names or names[0].value != listed.value:
                names.append(listed)
        if len(names) == 1:
            only = names[0]
            identifiers[(only.value.namespace, only.value.value)] = Known(
                only.value, only.provenance
            )
        elif len(names) > 1:
            self._host.report("device_name_differs", where, {"device": str(device["id"])})
            first = names[0].value
            identifiers[(first.namespace, first.value)] = Ambiguous(tuple(names))

    def _declare_properties(
        self,
        identifiers: dict[tuple[str, str], Knowledge[LogicalId]],
        device_doc: JsonObject,
        recording: "Recording",
    ) -> list[Knowledge[DeclaredEntry]]:
        """The device's custom properties verbatim; only the ones the operator declared to be
        identifiers (``identifier_properties``) also become declared identifiers."""
        where, rid = recording.location, recording.recording_id
        properties = device_doc.get("properties", {})
        assert isinstance(properties, dict)
        out: list[Knowledge[DeclaredEntry]] = []
        for name, value in sorted(properties.items()):
            grounding = self._grounding(
                where, "GET /devices", device_doc, json_pointer("properties", name), rid
            )
            out.append(Known(DeclaredEntry(name, value), grounding))
            if name not in self._host.options.identifier_properties:
                continue
            if isinstance(value, str) and value and value.isprintable():
                namespace = NS_DEVICE_PROPERTY + name.lower()
                identifiers[(namespace, value)] = Known(LogicalId(namespace, value), grounding)
            else:
                self._host.report("identifier_property_unusable", where, {"property": name})
        return out

    def _declare_topics(
        self, recording: "Recording"
    ) -> tuple[tuple[Knowledge[DeclaredTopic], ...], Knowledge[int]]:
        host = self._host
        where, rid = recording.location, recording.recording_id
        whole = self._grounding(where, "GET /recordings", recording.document, "", rid)
        if not host.options.topics:
            return (), NotCovered(whole)
        topics: dict[tuple[str, ...], Knowledge[DeclaredTopic]] = {}
        offset = pages = invalid = held = 0
        try:
            while pages < MAX_TOPIC_PAGES:
                page = host.client.topics(rid, offset, host.options.page_size)
                pages += 1
                if not page:
                    break
                offset += len(page)
                for raw in page:
                    item = strip_nulls(raw)
                    try:
                        topic = _topic(item)
                        grounding = self._grounding(where, "GET /data/topics", item, "", rid)
                    except (Invalid, CanonicalJsonError):
                        invalid += 1
                        continue
                    held += len(canonical_dumps(item))
                    if len(topics) >= MAX_TOPICS or held > MAX_TOPIC_BYTES:
                        host.report("listing_limit", where, {"call": "topics"})
                        return (), Unknown(whole)
                    key = (
                        topic.topic,
                        topic.schema_name,
                        topic.schema_encoding,
                        topic.encoding,
                        topic.version,
                    )
                    topics[key] = Known(topic, grounding)
            else:
                host.report("listing_limit", where, {"pages": pages})
                return (), Unknown(whole)
        except TransportError as exc:
            code, details = failure(exc, "topics_failed")
            host.report(code, where, details)
            return (), Unknown(whole)
        if invalid:
            host.report("record_invalid", where, {"call": "topics", "count": invalid})
        ordered = tuple(topics[key] for key in sorted(topics))
        return ordered, Known(len(ordered), whole)


def _entry_name(knowledge: Knowledge[DeclaredEntry]) -> str:
    assert isinstance(knowledge, Known)
    return knowledge.value.name


def _records(value: "JsonValue | None") -> list[JsonObject]:
    """The MCAP metadata records of a recording: validated when the recording was admitted."""
    return [record for record in (value or []) if isinstance(record, dict)]  # type: ignore[union-attr]


def _topic(item: JsonValue) -> DeclaredTopic:
    if not isinstance(item, dict):
        raise Invalid("record_invalid")
    # A schemaless channel states an empty schema name and encoding: that is what it declares.
    return DeclaredTopic(
        topic=text(item.get("topic")),
        schema_name=text(item.get("schemaName"), empty=True),
        schema_encoding=text(item.get("schemaEncoding"), empty=True),
        encoding=text(item.get("encoding"), empty=True),
        version=text(item.get("version"), empty=True),
    )
