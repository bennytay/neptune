"""An independent reading of MCAP citations, for the adapter's tests.

``resolve`` turns an ``EvidenceRef`` the adapter emitted back into bytes the way a consumer
would: the first step is bytes of the file; a second step is bytes of the uncompressed records of
the Chunk record the first step covers, decompressed here with the libraries directly, not through
the adapter's code. ``message`` reads a Message record's fixed fields.
"""

import struct
from dataclasses import dataclass
from typing import Any

import lz4.frame
import zstandard

from neptune.model.provenance import ByteRange, EvidenceRef


def _chunk_records(record: bytes) -> bytes:
    opcode, _ = struct.unpack_from("<BQ", record)
    assert opcode == 0x06, "a nested citation's first step covers a Chunk record"
    content = record[9:]
    size = struct.unpack_from("<Q", content, 16)[0]
    (name_length,) = struct.unpack_from("<I", content, 28)
    compression = content[32 : 32 + name_length]
    at = 32 + name_length
    (stored_length,) = struct.unpack_from("<Q", content, at)
    stored = content[at + 8 : at + 8 + stored_length]
    if compression == b"zstd":
        return zstandard.ZstdDecompressor().decompressobj().decompress(stored)[:size]
    if compression == b"lz4":
        decompressor = lz4.frame.LZ4FrameDecompressor()
        return bytes(decompressor.decompress(stored))[:size]
    assert compression == b"", compression
    return stored


def resolve(data: bytes, evidence: EvidenceRef) -> bytes:
    """The exact bytes a citation names."""
    outer, *inner = evidence.locator
    assert isinstance(outer, ByteRange)
    span = data[outer.offset : outer.offset + outer.length]
    if not inner:
        return span
    (step,) = inner
    assert isinstance(step, ByteRange)
    return _chunk_records(span)[step.offset : step.offset + step.length]


@dataclass(frozen=True)
class Message:
    channel_id: int
    sequence: int
    log_time: int
    publish_time: int
    payload: bytes


def message(record: bytes) -> Message:
    """A whole Message record (opcode, length, fields, payload)."""
    opcode, length = struct.unpack_from("<BQ", record)
    assert opcode == 0x05 and len(record) == 9 + length
    channel_id, sequence, log_time, publish_time = struct.unpack_from("<HIQQ", record, 9)
    return Message(channel_id, sequence, log_time, publish_time, record[31:])


def record_at(data: bytes, evidence: EvidenceRef) -> tuple[int, Any]:
    """The opcode of the whole record a citation covers, and the record's bytes."""
    found = resolve(data, evidence)
    opcode, length = struct.unpack_from("<BQ", found)
    assert len(found) == 9 + length, "a citation covers exactly one whole record"
    return opcode, found
