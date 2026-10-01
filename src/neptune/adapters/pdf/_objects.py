"""Reading pypdf objects defensively: every accessor answers ``None`` for a value of the wrong type.

A hostile file can put anything anywhere: a font where a number belongs, a reference to an object
that does not exist, a reference chain that loops. These helpers resolve references with a bound
and never raise for a wrong type, so callers decide what a missing value means. pypdf may still
raise while fetching an object (a broken offset, a recursion bomb); the page and document readers
catch that and turn it into a finding.
"""

import math
from typing import Final

from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    StreamObject,
    TextStringObject,
)

# A reference to a reference to ... is legal but never deep; a loop is hostile.
MAX_INDIRECTION: Final = 32


def resolve(obj: object) -> object:
    """``obj`` with its references followed; ``None`` past ``MAX_INDIRECTION`` hops."""
    for _ in range(MAX_INDIRECTION):
        if not isinstance(obj, IndirectObject):
            return obj
        obj = obj.get_object()
    return None


def entry(container: object, key: str) -> object:
    """The resolved value of ``key`` in a dictionary (or a stream's dictionary), or ``None``."""
    found = dictionary(container)
    if found is None:
        return None
    return resolve(dict.get(found, key))


def dictionary(obj: object) -> DictionaryObject | None:
    obj = resolve(obj)
    return obj if isinstance(obj, DictionaryObject) else None


def stream(obj: object) -> StreamObject | None:
    obj = resolve(obj)
    return obj if isinstance(obj, StreamObject) else None


def array(obj: object) -> ArrayObject | None:
    obj = resolve(obj)
    return obj if isinstance(obj, ArrayObject) else None


def number(obj: object) -> float | None:
    """A finite integer or real, as a float."""
    obj = resolve(obj)
    if isinstance(obj, bool) or not isinstance(obj, int | float):
        return None
    value = float(obj)
    return value if math.isfinite(value) else None


def integer(obj: object) -> int | None:
    obj = resolve(obj)
    if isinstance(obj, bool) or not isinstance(obj, int):
        return None
    return int(obj)


def name(obj: object) -> str | None:
    """A name without its slash: ``/Helvetica`` is ``Helvetica``."""
    obj = resolve(obj)
    if not isinstance(obj, NameObject):
        return None
    return str(obj)[1:]


def string_bytes(obj: object) -> bytes | None:
    """A string's bytes exactly as the file stores them (after decryption), never decoded."""
    obj = resolve(obj)
    if isinstance(obj, TextStringObject):
        return obj.original_bytes
    if isinstance(obj, ByteStringObject):
        return bytes(obj)
    return None


def reference(obj: object) -> tuple[int, int] | None:
    """The object number and generation ``obj`` was read from, if it is an indirect object."""
    found = getattr(obj, "indirect_reference", None)
    if isinstance(obj, IndirectObject):
        found = obj
    if isinstance(found, IndirectObject):
        return (int(found.idnum), int(found.generation))
    return None
