"""Which records a record names, read from the model's own field types (ADR 0069 §2).

One walker serves both checks that a package's references resolve: the runtime's salvage check
(a kept record must not name one only a lost chunk held) and validation's ``dangling_reference``
rule. Neither keeps a hand list of reference fields: every field the model types as a
``RecordId`` (alone, in a tuple, in a ``Knowledge``, or inside a nested value such as a
``Timestamp``'s ``domain_id`` or a ``FrameRef``'s ``frame_graph_id``) is one, so a kind added
later is covered without an edit here. A finding's ``records`` are ``RecordId``\\s too.

Not references: the record's own ``id``, its ``provenance`` (evidence and the transform that read
it), and a finding's ``transform``. Fields typed as data (cells, configuration values) are never
read, so text that looks like an id names nothing. Fields marked ``EXTERNAL`` (``ids.EXTERNAL``
field metadata) may name records of another package, such as an assertion's ``scope`` or a
revision's ``supersedes``: ``named`` skips them, so neither check calls them dangling.
"""

import dataclasses
from collections.abc import Iterator
from typing import Any, Final

from neptune.model.ids import is_external
from neptune.model.provenance import EvidenceRef, Provenance, TransformRecord

_PREFIX: Final = "rec:sha256:"
_NOT_REFERENCES: Final = frozenset({"id", "provenance", "transform"})
_OPAQUE: Final = (Provenance, EvidenceRef, TransformRecord)


def _typed_as_id(field: dataclasses.Field[Any]) -> bool:
    return "RecordId" in str(field.type)


def _ids(value: object) -> Iterator[str]:
    """Every record id inside a value whose field the model types as one."""
    if isinstance(value, str):
        if value.startswith(_PREFIX):
            yield value
    elif isinstance(value, tuple):
        for item in value:
            yield from _ids(item)
    elif dataclasses.is_dataclass(value) and not isinstance(value, (type, *_OPAQUE)):
        for field in dataclasses.fields(value):  # a Known's value; its provenance is no id
            if field.name != "provenance":
                yield from _ids(getattr(value, field.name))


def _nested(value: object) -> Iterator[str]:
    """The ids that fields typed as ``RecordId`` hold anywhere inside ``value``."""
    if isinstance(value, tuple):
        for item in value:
            yield from _nested(item)
    elif dataclasses.is_dataclass(value) and not isinstance(value, (type, *_OPAQUE)):
        for field in dataclasses.fields(value):
            if is_external(field):
                continue
            inner = getattr(value, field.name)
            if _typed_as_id(field):
                yield from _ids(inner)
            elif field.name != "provenance":
                yield from _nested(inner)


def named(record: object) -> Iterator[tuple[str, str]]:
    """``(top-level field, record id)`` for every record ``record`` names in this package, in
    field order. Fields marked external are not read."""
    if not dataclasses.is_dataclass(record) or isinstance(record, type):
        return
    for field in dataclasses.fields(record):
        if field.name in _NOT_REFERENCES or is_external(field):
            continue
        value = getattr(record, field.name)
        targets = _ids(value) if _typed_as_id(field) else _nested(value)
        for target in targets:
            yield field.name, target
