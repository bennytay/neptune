"""Machine configuration as typed snapshots: what one configuration document declares (ADR 0037).

Two evidence records of the ``machine`` family, added in schema version 2:

- ``ConfigurationSnapshot``: one configuration document as its bytes declare it: a JSON or TOML
  file, or one document of a YAML stream. It records how the document is written (its format, the
  YAML version it declares, the text encoding, byte-order mark and line endings), its comments
  verbatim and uninterpreted, how many values it has, and ``digest``: the identity of its values,
  equal for two documents that declare equal values however they are laid out.
- ``ConfigurationValue``: one node of the document's tree: a mapping, a sequence, an alias or a
  scalar. ``path`` is where it sits (keys verbatim, sequence positions as integers),
  ``occurrence`` which of the entries sharing each key it passes through, and ``order`` its
  position among its parent's entries, so key order survives. ``text`` is a scalar as written,
  read by no schema; ``value`` is the reading the format's own schema gives it: ``KnownAbsent``
  for a null the format defines, ``Ambiguous`` where the schemas that could apply disagree (a
  YAML ``on`` is a boolean in YAML 1.1 and text in YAML 1.2), ``Unknown`` where no reading can be
  held (an application's own tag, a number beyond binary64).

Nothing is inferred. A key named ``wheel_radius`` is a declared number with no unit; a document
that states a unit states it in a value of its own. Which snapshot applied to which run is a
binding (MVL-38). ``compare_configurations`` compares two snapshots field by field, and
``neptune.identity.configuration.configuration_digest`` hashes the same comparison.
"""

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import ClassVar, Final, NewType, TypeAlias

from neptune.model._fields import (
    check_text_values,
    check_type,
    enum_decoder,
    exact_object,
    is_int,
    json_array,
    json_bool,
    json_int,
    json_str,
    text_decoder,
    values_of,
)
from neptune.model.ids import RecordId, check_text, check_verbatim, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Inherited,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    from_json,
    to_json,
)
from neptune.model.provenance import (
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.scalars import NonFinite, Real, real_from_json, real_to_json
from neptune.model.time import INT64_MAX
from neptune.model.versions import DeclaredVersion, version_from_json, version_to_json

# The schema version that added these kinds (ADR 0037 §1).
CONFIGURATION_SINCE: Final = 2

# The identity of a snapshot's values: "sha256:<64 hex>" over their comparison (ADR 0037 §5).
ValueDigest = NewType("ValueDigest", str)
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


def parse_value_digest(text: str) -> ValueDigest:
    if not isinstance(text, str) or not _DIGEST.fullmatch(text):
        raise ValueError(f"not a value digest (want 'sha256:<64 lowercase hex>'): {text!r}")
    return ValueDigest(text)


class ConfigFormat(StrEnum):
    """The format a document is written in, as its bytes parse."""

    JSON = "json"  # RFC 8259
    TOML = "toml"  # TOML 1.0
    YAML = "yaml"  # YAML 1.1 or 1.2: one snapshot per document of the stream


class TextEncoding(StrEnum):
    """The encoding the bytes were decoded with: UTF-8, or the one a byte-order mark names."""

    UTF_8 = "utf-8"
    UTF_16_LE = "utf-16-le"
    UTF_16_BE = "utf-16-be"
    UTF_32_LE = "utf-32-le"
    UTF_32_BE = "utf-32-be"


class LineEndings(StrEnum):
    """The line breaks the bytes use: LF, CR LF, a lone CR, more than one of those, or none."""

    NONE = "none"
    LF = "lf"
    CRLF = "crlf"
    CR = "cr"
    MIXED = "mixed"


# --- Values ------------------------------------------------------------------------------------


class ScalarType(StrEnum):
    """A scalar's type in the format's own schema (JSON, TOML, the YAML type repository)."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    BINARY = "binary"  # YAML !!binary: base64 with canonical padding and no whitespace
    OFFSET_DATETIME = "offset_datetime"  # a date and time with a stated UTC offset
    LOCAL_DATETIME = "local_datetime"  # a date and time with no offset: never assumed UTC
    LOCAL_DATE = "local_date"
    LOCAL_TIME = "local_time"


ScalarValue: TypeAlias = bool | int | Real | str

# ISO 8601 as Python's ``isoformat`` writes it: what a date-time reading normalises to. The
# declared form stays in ``text``.
_DATETIME_FORMS: Final[Mapping[ScalarType, re.Pattern[str]]] = {
    ScalarType.OFFSET_DATETIME: re.compile(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?[+-]\d{2}:\d{2}(:\d{2}(\.\d{6})?)?"
    ),
    ScalarType.LOCAL_DATETIME: re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?"),
    ScalarType.LOCAL_DATE: re.compile(r"\d{4}-\d{2}-\d{2}"),
    ScalarType.LOCAL_TIME: re.compile(r"\d{2}:\d{2}:\d{2}(\.\d{6})?"),
}
_BASE64: Final = re.compile(r"([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")


def _check_scalar(kind: ScalarType, value: ScalarValue) -> None:
    if kind is ScalarType.BOOL:
        ok = isinstance(value, bool)
    elif kind is ScalarType.INT:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind is ScalarType.FLOAT:
        ok = isinstance(value, NonFinite) or (isinstance(value, float) and math.isfinite(value))
    else:
        ok = isinstance(value, str) and not isinstance(value, NonFinite)
    if not ok:
        raise ValueError(f"a {kind} scalar cannot hold {value!r}")
    if isinstance(value, str) and not isinstance(value, NonFinite):
        check_verbatim(f"{kind} value", value)
        form = _DATETIME_FORMS.get(kind)
        if form is not None and not form.fullmatch(value):
            raise ValueError(f"a {kind} is written as ISO 8601 isoformat, got {value!r}")
        if kind is ScalarType.BINARY and not _BASE64.fullmatch(value):
            raise ValueError(f"binary is canonical base64, got {value!r}")


@dataclass(frozen=True)
class ConfigScalar:
    """A scalar as the format's schema reads it: its type and its value in that type.

    Integers are exact; floats are the nearest binary64, non-finite ones a ``NonFinite``; text is
    any Unicode, empty included (``""`` is a declared value, not a blank); date-times are ISO 8601
    with the fields the source states and nothing assumed.
    """

    type: ScalarType
    value: ScalarValue

    def __post_init__(self) -> None:
        if not isinstance(self.type, ScalarType):
            raise TypeError(f"type must be a ScalarType, got {self.type!r}")
        _check_scalar(self.type, self.value)

    def to_json(self) -> JsonObject:
        value: JsonValue
        if self.type is ScalarType.FLOAT:
            assert isinstance(self.value, float | NonFinite)  # checked on construction
            value = real_to_json(self.value)
        else:
            value = self.value
        return {"type": str(self.type), "value": value}


class CollectionType(StrEnum):
    MAPPING = "mapping"  # a JSON object, a TOML table, a YAML mapping
    SEQUENCE = "sequence"  # a JSON or TOML array, a YAML sequence


@dataclass(frozen=True)
class ConfigCollection:
    """A mapping or sequence and how many entries or items it declares; each is its own value."""

    type: CollectionType
    length: int

    def __post_init__(self) -> None:
        if not isinstance(self.type, CollectionType):
            raise TypeError(f"type must be a CollectionType, got {self.type!r}")
        if not is_int(self.length) or not 0 <= self.length <= INT64_MAX:
            raise ValueError(f"length must be a count, got {self.length!r}")

    def to_json(self) -> JsonObject:
        return {"length": self.length, "type": str(self.type)}


# A position in a document: keys verbatim, sequence positions as integers; () is the root.
Path: TypeAlias = tuple[str | int, ...]


def _check_path(field: str, path: Path) -> None:
    if not isinstance(path, tuple):
        raise TypeError(f"{field} must be a tuple, got {type(path).__name__}")
    for segment in path:
        if isinstance(segment, str):
            check_verbatim(f"{field} key", segment)
        elif not is_int(segment) or not 0 <= segment <= INT64_MAX:
            raise ValueError(f"{field} holds keys and positions, got {segment!r}")


def _path_from_json(data: JsonValue, field: str) -> Path:
    segments: list[str | int] = []
    for segment in json_array(data, field):
        if isinstance(segment, str):
            segments.append(segment)
        else:
            segments.append(json_int(segment, field))
    return tuple(segments)


@dataclass(frozen=True)
class ConfigAlias:
    """A YAML alias (``*name``): the node the anchor ``name`` marks, at ``target``, never copied.

    An alias is the same node again in YAML's own model. It is recorded as a reference, so a
    document of nested aliases (a "billion laughs") costs one value per alias, never an expansion.
    ``key``: the anchor marks the key of the entry at ``target`` (``&k name: 1``), not a node:
    the alias is that key's scalar again, still a reference.
    """

    anchor: str
    target: Path
    key: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, str):
            raise TypeError(f"anchor must be a str, got {type(self.anchor).__name__}")
        check_text("anchor", self.anchor)
        _check_path("target", self.target)
        if not isinstance(self.key, bool):
            raise TypeError(f"key must be a bool, got {self.key!r}")
        if self.key and not (self.target and isinstance(self.target[-1], str)):
            raise ValueError("an alias to a key targets the entry the key names")

    def to_json(self) -> JsonObject:
        return {
            "anchor": self.anchor,
            "key": self.key,
            "target": list(self.target),
            "type": "alias",
        }


ConfigNode: TypeAlias = ConfigScalar | ConfigCollection | ConfigAlias


def _node_json(node: ConfigNode) -> JsonValue:
    return node.to_json()


def _node_from_json(data: JsonValue) -> ConfigNode:
    if not isinstance(data, Mapping) or "type" not in data:
        raise ValueError(f"a configuration value is a JSON object with a type, got {data!r}")
    kind = json_str(data["type"], "value type")
    if kind == "alias":
        obj = exact_object(data, "alias", {"anchor", "key", "target", "type"})
        anchor = json_str(obj["anchor"], "anchor")
        target = _path_from_json(obj["target"], "target")
        return ConfigAlias(anchor, target, json_bool(obj["key"]))
    if kind in CollectionType.__members__.values():
        obj = exact_object(data, "collection", {"length", "type"})
        return ConfigCollection(CollectionType(kind), json_int(obj["length"], "length"))
    obj = exact_object(data, "scalar", {"type", "value"})
    scalar = ScalarType(kind)
    raw = obj["value"]
    value: ScalarValue
    if scalar is ScalarType.BOOL:
        value = json_bool(raw)
    elif scalar is ScalarType.INT:
        value = json_int(raw, "int value")
    elif scalar is ScalarType.FLOAT:
        value = real_from_json(raw)
    else:
        value = json_str(raw, f"{scalar} value")
    return ConfigScalar(scalar, value)


# --- Records -----------------------------------------------------------------------------------


def _check_comments(comments: tuple[Knowledge[str], ...]) -> None:
    if not isinstance(comments, tuple):
        raise TypeError(f"comments must be a tuple, got {type(comments).__name__}")
    for comment in comments:
        if not isinstance(comment, Known) or isinstance(comment.provenance, Inherited):
            raise ValueError(f"a comment is Known text citing its own span, got {comment!r}")
        check_text_values("comment", comment)


@dataclass(frozen=True)
class ConfigurationSnapshot:
    """One configuration document as its bytes declare it (ADR 0037 §2).

    ``provenance`` cites the document's root: ``[JsonPointer("")]`` in a JSON or TOML file, and
    ``[<adapter>:document, JsonPointer("")]`` for one document of a YAML stream. Its values are
    ``ConfigurationValue`` records that name it.

    - ``format``: JSON, TOML or YAML. ``format_version``: the version the document declares (a
      YAML ``%YAML 1.2`` directive), ``Unknown`` where it could and does not, ``NotCovered`` where
      the format has no place for one (JSON, TOML).
    - ``encoding``, ``byte_order_mark``, ``line_endings``: how the file's bytes are written. The
      bytes stay the evidence; nothing is re-encoded or normalised.
    - ``comments``: every comment the document holds, in source order, each ``Known`` with its
      exact text, ``#`` included, citing its own span. Recorded, never attached to a value.
    - ``values``: how many ``ConfigurationValue`` records belong to this snapshot, the root
      included, so a package that lost some (a failed chunk) shows it.
    - ``digest``: the identity of those values (``configuration_digest``): two documents that
      declare equal values at equal paths have equal digests, whatever their format, layout,
      comments, key order, quoting or number spelling.
    """

    kind: ClassVar[str] = "configuration_snapshot"
    family: ClassVar[Family] = Family.MACHINE
    since: ClassVar[int] = CONFIGURATION_SINCE
    id: RecordId
    provenance: Provenance
    format: ConfigFormat
    format_version: Knowledge[DeclaredVersion]
    encoding: TextEncoding
    byte_order_mark: bool
    line_endings: LineEndings
    comments: tuple[Knowledge[str], ...]
    values: int
    digest: ValueDigest

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        for name, enum in (
            ("format", ConfigFormat),
            ("encoding", TextEncoding),
            ("line_endings", LineEndings),
        ):
            if not isinstance(getattr(self, name), enum):
                raise TypeError(f"{name} must be a {enum.__name__}, got {getattr(self, name)!r}")
        check_type("format_version", self.format_version, DeclaredVersion)
        if not isinstance(self.byte_order_mark, bool):
            raise TypeError(f"byte_order_mark must be a bool, got {self.byte_order_mark!r}")
        _check_comments(self.comments)
        if not is_int(self.values) or not 1 <= self.values <= INT64_MAX:
            raise ValueError(f"a snapshot has at least its root value, got {self.values!r}")
        parse_value_digest(self.digest)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "byte_order_mark": self.byte_order_mark,
                "comments": [to_json(comment) for comment in self.comments],
                "digest": self.digest,
                "encoding": str(self.encoding),
                "format": str(self.format),
                "format_version": to_json(self.format_version, version_to_json),
                "line_endings": str(self.line_endings),
                "values": self.values,
            },
            self.since,
        )


def _declared_version(data: JsonValue) -> DeclaredVersion:
    version = version_from_json(data)
    if not isinstance(version, DeclaredVersion):
        raise ValueError(f"a format version is declared text, not a {version.kind}")
    return version


def configuration_snapshot_from_json(data: JsonValue) -> ConfigurationSnapshot:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        ConfigurationSnapshot.kind,
        {
            "byte_order_mark",
            "comments",
            "digest",
            "encoding",
            "format",
            "format_version",
            "line_endings",
            "values",
        },
        ConfigurationSnapshot.since,
    )
    return ConfigurationSnapshot(
        id=record_id,
        provenance=provenance,
        format=enum_decoder(ConfigFormat)(obj["format"]),
        format_version=from_json(obj["format_version"], _declared_version, provenance_from_json),
        encoding=enum_decoder(TextEncoding)(obj["encoding"]),
        byte_order_mark=json_bool(obj["byte_order_mark"]),
        line_endings=enum_decoder(LineEndings)(obj["line_endings"]),
        comments=tuple(
            from_json(comment, text_decoder("comment"), provenance_from_json)
            for comment in json_array(obj["comments"], "comments")
        ),
        values=json_int(obj["values"], "values"),
        digest=parse_value_digest(json_str(obj["digest"], "digest")),
    )


def _check_occurrence(path: Path, occurrence: tuple[int, ...]) -> None:
    if not isinstance(occurrence, tuple):
        raise TypeError(f"occurrence must be a tuple, got {type(occurrence).__name__}")
    if len(occurrence) != len(path):
        raise ValueError(f"occurrence has one rank per step of the path, got {occurrence!r}")
    for segment, rank in zip(path, occurrence, strict=True):
        if not is_int(rank) or not 0 <= rank <= INT64_MAX:
            raise ValueError(f"occurrence holds ranks, got {rank!r}")
        if isinstance(segment, int) and rank:
            raise ValueError(f"a sequence position occurs once, got rank {rank} at {segment}")


def _verbatim_decoder(data: JsonValue) -> str:
    return json_str(data, "text")


@dataclass(frozen=True)
class ConfigurationValue:
    """One node of a configuration document, as declared (ADR 0037 §3).

    ``provenance`` cites the node: an RFC 6901 ``JsonPointer`` to it in the document as parsed,
    after the YAML document step; an entry whose key repeats in its mapping is addressed by its
    position there instead (ADR 0037 §4). ``snapshot`` is the ``ConfigurationSnapshot`` the same
    transform says it belongs to.

    - ``path``: its keys verbatim and its sequence positions; ``()`` for the root. Two values of
      one snapshot share a path only when a key repeats in one mapping.
    - ``occurrence``: one rank per step of ``path``: which of its parent's entries with that key
      the step passes through (0 for the first, and for every sequence position). ``(path,
      occurrence)`` is unique in a snapshot, so values sort, compare and hash in one order however
      a key repeats.
    - ``order``: its position among its parent's entries or items, in source order.
    - ``key_tag``: the type of its key where that is not a string: the tag a YAML key resolves
      to (``tag:yaml.org,2002:int`` for ``1``, ``...:bool`` for ``true``, an application's
      ``!tag``), ``Ambiguous`` where the YAML versions disagree (``on``). ``NotApplicable`` for a
      string key (every JSON and TOML key, a quoted or plain-text YAML key), the root and sequence
      items, so ``1`` and ``"1"``, one path, still declare two keys.
    - ``tag``: a YAML node's tag: the explicit one, expanded, or the non-specific ``?`` (plain
      scalars and collections) or ``!`` (quoted and block scalars). ``NotCovered`` in JSON and
      TOML, which have none.
    - ``text``: a scalar as written: a string's content, any other scalar's token verbatim.
      ``NotApplicable`` for collections and aliases.
    - ``value``: the node as the format's schema reads it: a ``ConfigCollection``, a
      ``ConfigAlias`` or a ``ConfigScalar``, citing its exact span where the reader locates it;
      ``KnownAbsent`` for a null the format defines, citing the document; ``Ambiguous`` between
      scalar readings; ``Unknown`` where none can be held.
    """

    kind: ClassVar[str] = "configuration_value"
    family: ClassVar[Family] = Family.MACHINE
    since: ClassVar[int] = CONFIGURATION_SINCE
    id: RecordId
    provenance: Provenance
    snapshot: RecordId
    path: Path
    occurrence: tuple[int, ...]
    order: int
    key_tag: Knowledge[str]
    tag: Knowledge[str]
    text: Knowledge[str]
    value: Knowledge[ConfigNode]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.snapshot)
        _check_path("path", self.path)
        _check_occurrence(self.path, self.occurrence)
        check_text_values("key_tag", self.key_tag)
        keyless = not self.path or isinstance(self.path[-1], int)
        if keyless and not isinstance(self.key_tag, NotApplicable):
            raise ValueError("key_tag is NotApplicable for the root and sequence items")
        if not is_int(self.order) or not 0 <= self.order <= INT64_MAX:
            raise ValueError(f"order must be a position, got {self.order!r}")
        if not self.path and self.order:
            raise ValueError("the root is at order 0")
        check_text_values("tag", self.tag)
        check_type("text", self.text, str)
        for text in values_of(self.text):
            check_verbatim("text", text)
        check_type("value", self.value, ConfigScalar | ConfigCollection | ConfigAlias)
        if isinstance(self.value, NotApplicable):
            raise ValueError("every node has a value, even an unknown one")
        if isinstance(self.value, Ambiguous) and not all(
            isinstance(value, ConfigScalar) for value in values_of(self.value)
        ):
            raise ValueError("only scalar readings can be ambiguous")
        structural = isinstance(self.value, Known) and not isinstance(
            self.value.value, ConfigScalar
        )
        if structural != isinstance(self.text, NotApplicable):
            raise ValueError("text is NotApplicable exactly for collections and aliases")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "key_tag": to_json(self.key_tag),
                "occurrence": list(self.occurrence),
                "order": self.order,
                "path": list(self.path),
                "snapshot": self.snapshot,
                "tag": to_json(self.tag),
                "text": to_json(self.text),
                "value": to_json(self.value, _node_json),
            },
            self.since,
        )


def configuration_value_from_json(data: JsonValue) -> ConfigurationValue:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        ConfigurationValue.kind,
        {"key_tag", "occurrence", "order", "path", "snapshot", "tag", "text", "value"},
        ConfigurationValue.since,
    )
    return ConfigurationValue(
        id=record_id,
        provenance=provenance,
        snapshot=parse_record_id(json_str(obj["snapshot"], "snapshot")),
        path=_path_from_json(obj["path"], "path"),
        occurrence=tuple(
            json_int(rank, "occurrence") for rank in json_array(obj["occurrence"], "occurrence")
        ),
        order=json_int(obj["order"], "order"),
        key_tag=from_json(obj["key_tag"], text_decoder("key_tag"), provenance_from_json),
        tag=from_json(obj["tag"], text_decoder("tag"), provenance_from_json),
        text=from_json(obj["text"], _verbatim_decoder, provenance_from_json),
        value=from_json(obj["value"], _node_from_json, provenance_from_json),
    )


# --- Comparing snapshots (ADR 0037 §5) ---------------------------------------------------------


def path_sort_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    """A total order on paths: segment by segment, positions before keys, keys by code point."""
    return tuple((0, segment) if isinstance(segment, int) else (1, segment) for segment in path)


def _stripped(knowledge: Knowledge[str]) -> JsonValue:
    """A text state without its citation: what it says, not where."""
    match knowledge:
        case Known(value=value):
            return {"knowledge": "known", "value": value}
        case Ambiguous(candidates=candidates):
            return {"knowledge": "ambiguous", "values": [c.value for c in candidates]}
        case _:
            return {"knowledge": str(knowledge.state)}


def comparison_key(value: ConfigurationValue) -> JsonValue:
    """What a value declares, without where: equal keys mean equal declared values.

    A collection compares by its type (its entries are values of their own), an alias by the path
    it refers to, a scalar by its reading. A value with no reading compares by its text and tag,
    which are then all the evidence says. A key that is not a string adds its type, so YAML's
    ``1: x`` and ``'1': x`` differ. Citations, spellings (``0x1F`` and ``31``, ``'a'`` and
    ``a``), anchors' names and key order never count.
    """
    declared = _declared_value(value)
    if isinstance(value.key_tag, NotApplicable):  # a string key: what JSON and TOML have too
        return declared
    return {"key_tag": _stripped(value.key_tag), "value": declared}


def _declared_value(value: ConfigurationValue) -> JsonValue:
    match value.value:
        case Known(value=ConfigCollection(type=kind)):
            return {"knowledge": "known", "type": str(kind)}
        case Known(value=ConfigAlias(target=target, key=key)):
            return {"key": key, "knowledge": "known", "target": list(target), "type": "alias"}
        case Known(value=ConfigScalar() as scalar):
            return {"knowledge": "known", "value": scalar.to_json()}
        case Ambiguous(candidates=candidates):
            readings: list[JsonValue] = [_node_json(candidate.value) for candidate in candidates]
            return {"knowledge": "ambiguous", "values": readings}
        case KnownAbsent():
            return {"knowledge": "known_absent"}
        case state:
            return {
                "knowledge": str(state.state),
                "tag": _stripped(value.tag),
                "text": _stripped(value.text),
            }


def address(value: ConfigurationValue) -> tuple[tuple[tuple[int, int | str], ...], tuple[int, ...]]:
    """Where a value sits, as a sort key: its path, then which repeated entries it passes through.

    Unique in a snapshot, so the order of a snapshot's values never depends on the order they
    were given in (a package's tables are sorted by id).
    """
    return path_sort_key(value.path), value.occurrence


def snapshot_of(values: Iterable[ConfigurationValue]) -> tuple[ConfigurationValue, ...]:
    """``values`` sorted by ``address``, after checking they are of one snapshot, each address
    once."""
    found = tuple(values)
    snapshots = {value.snapshot for value in found}
    if len(snapshots) > 1:
        raise ValueError(f"values of {len(snapshots)} snapshots; compare one snapshot at a time")
    ordered = tuple(sorted(found, key=address))
    for before, after in pairwise(ordered):
        if address(before) == address(after):
            raise ValueError(
                f"two values at path {list(after.path)}, occurrence {list(after.occurrence)}"
            )
    return ordered


class ChangeKind(StrEnum):
    ADDED = "added"  # only the right snapshot declares the path
    REMOVED = "removed"  # only the left one does
    CHANGED = "changed"  # both do, and what they declare there differs


@dataclass(frozen=True)
class ConfigurationChange:
    """One path at which two snapshots differ, with the values each declares there.

    ``left`` and ``right`` list the value records at ``path`` in source order: one each, unless a
    key repeats in a mapping, and none on the side that does not declare the path.
    """

    path: Path
    change: ChangeKind
    left: tuple[RecordId, ...]
    right: tuple[RecordId, ...]


def _declared(values: list[ConfigurationValue]) -> str:
    """The values' occurrences and comparison keys as exact text: Python's ``==`` takes ``-0.0``
    for ``0.0``, and the digest, which hashes the keys' canonical JSON, does not."""
    return json.dumps(
        [[list(value.occurrence), comparison_key(value)] for value in values], sort_keys=True
    )


def compare_configurations(
    left: Iterable[ConfigurationValue], right: Iterable[ConfigurationValue]
) -> tuple[ConfigurationChange, ...]:
    """Every path at which two snapshots' values differ, field by field, sorted by path.

    Each side is the values of one snapshot. Paths are joined exactly: keys verbatim, positions
    as positions. At a repeated key, the values there compare by occurrence, entry by entry. Two
    snapshots with no change have equal digests (``configuration_digest``), and the converse holds
    too.
    """
    sides: list[dict[Path, list[ConfigurationValue]]] = []
    for values in (left, right):
        by_path: dict[Path, list[ConfigurationValue]] = defaultdict(list)
        for value in snapshot_of(values):
            by_path[value.path].append(value)
        sides.append(by_path)
    before, after = sides
    changes: list[ConfigurationChange] = []
    for path in sorted(before.keys() | after.keys(), key=path_sort_key):
        old, new = before.get(path, []), after.get(path, [])
        if not old:
            change = ChangeKind.ADDED
        elif not new:
            change = ChangeKind.REMOVED
        elif _declared(old) != _declared(new):
            change = ChangeKind.CHANGED
        else:
            continue
        changes.append(
            ConfigurationChange(path, change, tuple(v.id for v in old), tuple(v.id for v in new))
        )
    return tuple(changes)
