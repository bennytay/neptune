"""An independent reading of ROS 1 bag citations, for the adapter's tests.

``resolve`` turns an ``EvidenceRef`` the adapter emitted back into bytes the way a consumer
would: the first step is bytes of the file; a second step is bytes of the uncompressed data of
the Chunk record the first step covers, decompressed here with the libraries directly, not through
the adapter's code. ``message`` reads a Message Data record's fields.
"""

import bz2
import struct
from dataclasses import dataclass

import lz4.frame

from neptune.model.provenance import ByteRange, EvidenceRef


def fields(record: bytes) -> tuple[dict[str, bytes], bytes]:
    """A whole record's header fields by name, and its data."""
    (header_length,) = struct.unpack_from("<I", record)
    out: dict[str, bytes] = {}
    pos = 4
    while pos < 4 + header_length:
        (size,) = struct.unpack_from("<I", record, pos)
        field = record[pos + 4 : pos + 4 + size]
        name, _, value = field.partition(b"=")
        out[name.decode()] = value
        pos += 4 + size
    assert pos == 4 + header_length
    (data_length,) = struct.unpack_from("<I", record, pos)
    data = record[pos + 4 : pos + 4 + data_length]
    assert len(data) == data_length and pos + 4 + data_length == len(record)
    return out, data


def _chunk_data(record: bytes) -> bytes:
    header, stored = fields(record)
    assert header["op"] == b"\x05", "a nested citation's first step covers a Chunk record"
    (size,) = struct.unpack("<I", header["size"])
    compression = header["compression"]
    if compression == b"bz2":
        return bz2.decompress(stored)[:size]
    if compression == b"lz4":
        return bytes(lz4.frame.decompress(stored))[:size]
    assert compression == b"none", compression
    return stored[:size]


def resolve(data: bytes, evidence: EvidenceRef) -> bytes:
    """The exact bytes a citation names."""
    outer, *inner = evidence.locator
    assert isinstance(outer, ByteRange)
    span = data[outer.offset : outer.offset + outer.length]
    if not inner:
        return span
    (step,) = inner
    assert isinstance(step, ByteRange)
    return _chunk_data(span)[step.offset : step.offset + step.length]


@dataclass(frozen=True)
class Message:
    conn: int
    time: int  # ns
    payload: bytes


def message(record: bytes) -> Message:
    """A whole Message Data record."""
    header, payload = fields(record)
    assert header["op"] == b"\x02"
    sec, nsec = struct.unpack("<II", header["time"])
    (conn,) = struct.unpack("<I", header["conn"])
    return Message(conn, sec * 10**9 + nsec, payload)


def record_at(data: bytes, evidence: EvidenceRef) -> tuple[int, bytes]:
    """The op of the whole record a citation covers, and the record's bytes."""
    found = resolve(data, evidence)
    header, _ = fields(found)  # raises unless the citation covers exactly one whole record
    return header["op"][0], found
