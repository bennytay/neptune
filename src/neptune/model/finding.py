"""What went wrong, was skipped, or could not be represented, as records (ADR 0017 §9).

An ``IngestFinding`` is Neptune's own statement about the evidence or about a location: a
truncated chunk, a cell that does not parse, a unit the catalogue cannot read, a clock that runs
backwards, a symlink that was not followed. Adapters return findings instead of raising
(ADR 0008); discovery, the runtime and validation emit them too.

A finding names what it is about (``subject``) and the transform that found it. The subject is a
place in some bytes (an ``EvidenceRef``) or, when there are no bytes to cite (an unreadable
directory, an object that could not be fetched), a location. Its id is derived from its whole
content by ``neptune.identity.findings``, so two identical findings are one finding.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from neptune.model._fields import enum_decoder, exact_object, json_array, json_str
from neptune.model.ids import ExternalObjectRef, RecordId, check_text, check_token, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.record import Family, envelope, record_object
from neptune.model.source import LocalPath, RawLocalPath, SourceLocation, location_from_json

# One line a person reads in a receipt. The facts a program needs go in ``details``.
MAX_MESSAGE_LENGTH: Final = 1000
_CODE: Final = re.compile(r"[a-z][a-z0-9_\-]*(?:\.[a-z][a-z0-9_\-]*)+")


class Severity(StrEnum):
    """What the problem cost, judged by what reached the canonical output."""

    ERROR = "error"  # some evidence produced no canonical output: corrupt, unsupported, crashed
    WARNING = "warning"  # the output exists, but a value in it is unknown, ambiguous or in conflict
    INFO = "info"  # nothing was lost or put in doubt; recorded so the receipt can say so


class FindingCategory(StrEnum):
    """What kind of problem it is. Receipts group findings by category (MVL-5)."""

    CORRUPT = "corrupt"  # the bytes violate their format: bad checksum, truncation, bad structure
    UNSUPPORTED = "unsupported"  # well-formed bytes Neptune does not decode: an unknown encoding
    UNREPRESENTABLE = "unrepresentable"  # a decoded value has no canonical form: invalid Unicode
    MISSING = "missing"  # the evidence does not state what consumers rely on: no unit, no clock
    AMBIGUOUS = "ambiguous"  # the evidence supports more than one reading
    INCONSISTENT = "inconsistent"  # the evidence contradicts itself or other evidence
    SKIPPED = "skipped"  # not read, by policy: a symlink, a special file, an unreadable directory
    LIMIT = "limit"  # a resource or safety limit stopped processing: size, time, memory, ratio
    FAILED = "failed"  # the transform itself failed; the runtime quarantined what it was reading


# A place in fetched bytes, or a location with no bytes to cite.
FindingSubject: TypeAlias = EvidenceRef | SourceLocation
_SUBJECTS: Final = (EvidenceRef, LocalPath, RawLocalPath, ExternalObjectRef)


@dataclass(frozen=True)
class IngestFinding:
    """One problem, where it is, and what found it.

    - ``code`` is ``<producer>.<name>`` (``mcap.chunk_crc_mismatch``), stable, and documented by
      the producer. ``category`` and ``severity`` classify it for receipts.
    - ``transform`` is the ``TransformRecord`` id of what found it: an adapter, discovery, the
      runtime or a validator.
    - ``message`` is one deterministic line for people. ``details`` holds the facts a program
      needs (expected and actual values), keyed by lowercase tokens. Neither holds wall-clock
      times, host names, absolute paths or memory addresses.
    - ``related`` is other evidence involved (the second declaration of a conflicting value), in
      evidence order. ``records`` are the records the finding qualifies, such as one whose field
      is ``Unknown`` because of it, sorted by id.
    """

    kind: ClassVar[str] = "ingest_finding"
    family: ClassVar[Family] = Family.FINDING
    id: RecordId
    code: str
    category: FindingCategory
    severity: Severity
    subject: FindingSubject
    transform: RecordId
    message: str
    details: JsonObject
    related: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
            raise ValueError(f"code must be '<producer>.<name>' in lowercase tokens: {self.code!r}")
        if not isinstance(self.category, FindingCategory):
            raise TypeError(f"category must be a FindingCategory, got {self.category!r}")
        if not isinstance(self.severity, Severity):
            raise TypeError(f"severity must be a Severity, got {self.severity!r}")
        if not isinstance(self.subject, _SUBJECTS):
            raise TypeError(f"subject must be an EvidenceRef or a location, got {self.subject!r}")
        parse_record_id(self.transform)
        check_text("message", self.message)
        if len(self.message) > MAX_MESSAGE_LENGTH or "\n" in self.message or "\r" in self.message:
            raise ValueError(f"message must be one line of at most {MAX_MESSAGE_LENGTH} characters")
        if not isinstance(self.details, Mapping):
            raise TypeError(f"details must be a JSON object, got {type(self.details).__name__}")
        for key in self.details:
            check_token("details key", key)
        if not isinstance(self.related, tuple):
            raise TypeError(f"related must be a tuple, got {type(self.related).__name__}")
        for ref in self.related:
            if not isinstance(ref, EvidenceRef):
                raise TypeError(f"related must hold EvidenceRefs, got {ref!r}")
        if len(set(self.related)) != len(self.related) or self.subject in self.related:
            raise ValueError("related evidence must be distinct from each other and the subject")
        if not isinstance(self.records, tuple):
            raise TypeError(f"records must be a tuple, got {type(self.records).__name__}")
        for record in self.records:
            parse_record_id(record)
        if list(self.records) != sorted(set(self.records)):
            raise ValueError("records must be unique and sorted by id")

    def __hash__(self) -> int:
        return hash(self.id)

    def content_json(self) -> JsonObject:
        """Everything but ``id``: the input its id is derived from."""
        return {
            "category": str(self.category),
            "code": self.code,
            "details": self.details,
            "message": self.message,
            "records": list(self.records),
            "related": [ref.to_json() for ref in self.related],
            "severity": str(self.severity),
            "subject": subject_to_json(self.subject),
            "transform": self.transform,
        }

    def to_json(self) -> JsonObject:
        return envelope(self.kind, {**self.content_json(), "id": self.id})


def subject_to_json(subject: FindingSubject) -> JsonObject:
    """A location as itself (kind ``local``, ``local_raw`` or ``external``), evidence tagged."""
    if isinstance(subject, EvidenceRef):
        return {"kind": "evidence", "ref": subject.to_json()}
    return subject.to_json()


def subject_from_json(data: JsonValue) -> FindingSubject:
    if isinstance(data, Mapping) and data.get("kind") == "evidence":
        return evidence_ref_from_json(
            exact_object(data, "evidence subject", {"kind", "ref"})["ref"]
        )
    return location_from_json(data)


def ingest_finding_from_json(data: JsonValue) -> IngestFinding:
    """Parse strictly; ``identity.findings.check_ingest_finding`` checks the id."""
    obj = record_object(
        data,
        IngestFinding.kind,
        {
            "category",
            "code",
            "details",
            "id",
            "message",
            "records",
            "related",
            "severity",
            "subject",
            "transform",
        },
    )
    details = obj["details"]
    if not isinstance(details, Mapping):
        raise ValueError("details must be a JSON object")
    return IngestFinding(
        id=parse_record_id(json_str(obj["id"], "id")),
        code=json_str(obj["code"], "code"),
        category=enum_decoder(FindingCategory)(obj["category"]),
        severity=enum_decoder(Severity)(obj["severity"]),
        subject=subject_from_json(obj["subject"]),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        message=json_str(obj["message"], "message"),
        details=details,
        related=tuple(evidence_ref_from_json(ref) for ref in json_array(obj["related"], "related")),
        records=tuple(
            parse_record_id(json_str(record, "record"))
            for record in json_array(obj["records"], "records")
        ),
    )
