"""Model checkpoints: their headers read deterministically, their weights never loaded (ADR 0040).

A checkpoint's identity is its bytes. Neptune's tier-1 content id is their sha256, computed when
the source is fingerprinted, and the record cites it as its source; the adapter never hashes a
checkpoint again in its sandbox. ``digest`` holds only a digest a source *states*
(``ModelCheckpointHash`` is a claim, ADR 0014 §6), and none of these formats has a place for one,
so it is ``NotCovered`` here; an SBOM or a manifest that states one is what binding compares with
the content id. Each checkpoint is one item, ``observed``.

- **safetensors**: an 8-byte little-endian header length, then a JSON header naming each tensor's
  dtype, shape and data offsets. The header is checked, and a tensor whose data runs past the end
  is ``software.truncated``. ``__metadata__`` stays cited in the bytes: it has no defined keys.
- **ONNX** (``ModelProto``): top-level protobuf fields are walked by tag and length, so the graph
  and its weights are skipped unread. ``model_version`` is ``release`` (declared text of the
  int64); producer, domain and metadata stay cited in the bytes. ``ModelProto`` has no name field.
- **PyTorch** (``torch.save`` zip archives, TorchScript included): the first member is
  ``<archive>/data.pkl``, which is never unpickled. The zip directory is read to check the
  archive is whole; the format records no name or version.
"""

import io
import struct
import zipfile
from typing import Final

from neptune.adapters.contract import (
    SIGNATURE,
    STRUCTURE,
    VERIFIED,
    FormatSpec,
    ShortReadError,
    SourceReader,
)
from neptune.adapters.software._common import (
    Detected,
    Doc,
    Draft,
    Format,
    Reading,
    loads_json,
    reason,
)
from neptune.model.knowledge import AssertionKind, NotCovered, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.versions import DeclaredVersion

# --- safetensors -------------------------------------------------------------------------------

_DTYPES: Final = frozenset(
    {
        "BOOL",
        "U8",
        "I8",
        "F8_E5M2",
        "F8_E4M3",
        "I16",
        "U16",
        "F16",
        "BF16",
        "I32",
        "U32",
        "F32",
        "F64",
        "I64",
        "U64",
        "F4",
        "F6_E2M3",
        "F6_E3M2",
        "F8_E8M0",
        "C64",
    }
)


def _checkpoint_draft(entry: EvidenceRef) -> Draft:
    """A checkpoint's item: every field NotCovered, the formats having no place for any."""
    draft = Draft(entry=entry)
    draft.digest = NotCovered()
    return draft


def _detect_safetensors(head: bytes, size: int) -> Detected | None:
    if len(head) < 10:
        return None
    (length,) = struct.unpack_from("<Q", head)
    if length < 2 or 8 + length > size or head[8] != ord("{"):
        return None
    if 8 + length <= len(head):
        try:
            header = loads_json(head[8 : 8 + length].decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        if isinstance(header, dict) and all(
            key == "__metadata__" or (isinstance(value, dict) and "data_offsets" in value)
            for key, value in header.items()
        ):
            return Detected(VERIFIED, reason("safetensors", "a safetensors header that parses"))
        return None
    return Detected(SIGNATURE, reason("safetensors", "a safetensors header length and JSON start"))


def _tensor_end(entry: object) -> int | None:
    """A tensor entry's data end, or ``None`` if it is not ``{dtype, shape, data_offsets}``."""
    if not isinstance(entry, dict):
        return None
    offsets, shape = entry.get("data_offsets"), entry.get("shape")
    if entry.get("dtype") not in _DTYPES or not isinstance(shape, list):
        return None
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in shape):
        return None
    if (
        not isinstance(offsets, list)
        or len(offsets) != 2
        or not all(isinstance(n, int) and not isinstance(n, bool) for n in offsets)
        or not 0 <= offsets[0] <= offsets[1]
    ):
        return None
    end: int = offsets[1]
    return end


def _read_safetensors(reading: Reading) -> list[Draft]:
    size = reading.source.size
    prefix = reading.read(0, 8)
    if len(prefix) < 8:
        reading.truncated(
            reading.whole, "the safetensors header length is cut short", {"size": size}
        )
        return []
    (length,) = struct.unpack("<Q", prefix)
    limit = reading.config.integer("max_header_bytes")
    if length > limit:
        reading.too_large(reading.span(0, 8), length, limit, "max_header_bytes")
        return []
    if 8 + length > size:
        reading.truncated(
            reading.span(0, 8),
            "the safetensors header runs past the end of the file",
            {"end": 8 + length, "size": size},
        )
        return []
    header_at = reading.span(8, length)
    header = reading.json(reading.read(8, length), header_at)
    if header is None:
        return []
    if not isinstance(header, dict):
        reading.malformed("header is not a JSON object", subject=header_at)
        return []
    doc = Doc(reading, (ByteRange(8, length),))
    data_size = size - 8 - length
    declared = 0
    for name, entry in header.items():
        if name == "__metadata__":
            if not isinstance(entry, dict) or not all(isinstance(v, str) for v in entry.values()):
                reading.malformed_entry(doc.ref(name), "is __metadata__ that is not text to text")
            continue
        end = _tensor_end(entry)
        if end is None:
            reading.malformed_entry(doc.ref(name), "is not a tensor's dtype, shape and offsets")
            continue
        declared = max(declared, end)
    if declared > data_size:
        reading.truncated(
            reading.span(8 + length, data_size),
            f"the safetensors header declares {declared} bytes of tensor data; the file holds"
            f" {data_size}",
            {"data_bytes": data_size, "declared_bytes": declared},
        )
    return [_checkpoint_draft(header_at)]


# --- ONNX --------------------------------------------------------------------------------------

# ModelProto's fields and wire types (onnx.proto): 0 is a varint, 2 is length-delimited.
_ONNX_FIELDS: Final = {1: 0, 2: 2, 3: 2, 4: 2, 5: 0, 6: 2, 7: 2, 8: 2, 14: 2, 20: 2, 25: 2, 26: 2}
_ONNX_MAX_FIELDS: Final = 65536


def _varint(data: bytes, at: int) -> tuple[int, int] | None:
    value = shift = 0
    for index in range(at, min(at + 10, len(data))):
        byte = data[index]
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index + 1
        shift += 7
    return None


def _detect_onnx(head: bytes, size: int) -> Detected | None:
    first = _varint(head, 0)
    if first is None or first[0] != 0x08:  # field 1 (ir_version), a varint
        return None
    ir_version = _varint(head, first[1])
    if ir_version is None or not 1 <= ir_version[0] <= 64:
        return None
    at, seen = ir_version[1], set()
    while at < len(head):
        tag = _varint(head, at)
        if tag is None:
            break
        number, wire = tag[0] >> 3, tag[0] & 7
        if _ONNX_FIELDS.get(number) != wire:
            return None
        seen.add(number)
        value = _varint(head, tag[1])
        if value is None:
            break
        at = value[1] + (value[0] if wire == 2 else 0)
        if number == 2 and at <= len(head):
            try:
                head[value[1] : at].decode("utf-8")
            except UnicodeDecodeError:
                return None
    if not seen & {7, 8} and len(head) != size:
        return None
    return Detected(
        STRUCTURE, reason("onnx", "protobuf fields of an ONNX ModelProto"), str(ir_version[0])
    )


class _Reader:
    """Small reads at increasing offsets, a window at a time."""

    def __init__(self, reading: Reading) -> None:
        self.reading = reading
        self.start = 0
        self.data = b""

    def window(self, at: int) -> tuple[bytes, int]:
        if not self.start <= at <= self.start + len(self.data) - 10:
            self.start, self.data = at, self.reading.read(at, 4096)
        return self.data, at - self.start


def _read_onnx(reading: Reading) -> list[Draft]:
    size, reader = reading.source.size, _Reader(reading)
    model_version: tuple[int, int, int] | None = None
    at = fields = field_at = 0
    while at < size:
        fields += 1
        if fields > _ONNX_MAX_FIELDS:
            reading.malformed(f"has more than {_ONNX_MAX_FIELDS} top-level fields")
            return []
        data, local = reader.window(at)
        tag = _varint(data, local)
        if tag is None:
            break
        number, wire = tag[0] >> 3, tag[0] & 7
        field_at = at
        at += tag[1] - local
        if wire == 0:
            data, local = reader.window(at)
            value = _varint(data, local)
            if value is None:
                break
            if number == 5:
                model_version = (value[0], at, value[1] - local)
            at += value[1] - local
        elif wire == 2:
            data, local = reader.window(at)
            value = _varint(data, local)
            if value is None:
                break
            at += value[1] - local + value[0]
        elif wire in (1, 5):
            at += 8 if wire == 1 else 4
        else:
            reading.malformed(
                f"has a protobuf field of wire type {wire} at byte {field_at}", {"byte": field_at}
            )
            return []
    if at != size:
        reading.truncated(
            reading.span(field_at, size - field_at),
            "an ONNX field runs past the end of the file",
            {"field_byte": field_at, "size": size},
        )
    draft = _checkpoint_draft(reading.whole)
    if model_version is None:
        draft.release = Unknown(reading.provenance(reading.whole))
    else:
        number, start, length = model_version
        signed = number - (1 << 64) if number >= 1 << 63 else number
        version_at = reading.span(start, length)
        draft.release = reading.value(
            draft, "release", str(signed), version_at, DeclaredVersion, "version"
        )
    return [draft]


# --- PyTorch -----------------------------------------------------------------------------------

_ZIP_LOCAL: Final = b"PK\x03\x04"
_ZIP_END: Final = b"PK\x05\x06"


def _first_member(head: bytes) -> str | None:
    if len(head) < 30 or not head.startswith(_ZIP_LOCAL):
        return None
    (name_length,) = struct.unpack_from("<H", head, 26)
    raw = head[30 : 30 + name_length]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_pickle_member(name: str) -> bool:
    prefix, _, base = name.rpartition("/")
    return base == "data.pkl" and "/" not in prefix and prefix != ""


def _detect_pytorch(head: bytes, size: int) -> Detected | None:
    name = _first_member(head)
    if name is None or not _is_pickle_member(name):
        return None
    return Detected(SIGNATURE, reason("pytorch", "a zip whose first member is <archive>/data.pkl"))


class _SourceFile(io.RawIOBase):
    """A read-only, seekable file over a source, for ``zipfile``."""

    def __init__(self, source: SourceReader) -> None:
        self._source = source
        self._at = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._at

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._at, io.SEEK_END: self._source.size}[whence]
        self._at = max(0, base + offset)
        return self._at

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        size = self._source.size
        if self._at >= size:
            return 0
        data = self._source.read(self._at, min(len(buffer), size - self._at))
        if not data:
            raise ShortReadError(self._source.content_id, self._at, size - self._at)
        buffer[: len(data)] = data
        self._at += len(data)
        return len(data)


def _directory_size(reading: Reading) -> int | None:
    """The zip directory's size from its end record (zip64 included), or ``None`` without one."""
    size = reading.source.size
    tail_at = max(0, size - (22 + 65535))
    tail = reading.read(tail_at, size - tail_at)
    end = tail.rfind(_ZIP_END)
    if end < 0 or end + 22 > len(tail):
        return None
    (directory,) = struct.unpack_from("<I", tail, end + 12)
    locator = end - 20
    if directory == 0xFFFFFFFF and locator >= 0 and tail[locator : locator + 4] == b"PK\x06\x07":
        (record_at,) = struct.unpack_from("<Q", tail, locator + 8)
        record = reading.read(record_at, 56)
        if len(record) == 56 and record.startswith(b"PK\x06\x06"):
            (directory,) = struct.unpack_from("<Q", record, 40)
    return int(directory)


def _read_pytorch(reading: Reading) -> list[Draft]:
    name = _first_member(reading.read(0, 30 + 65535))
    if name is None or not _is_pickle_member(name):
        reading.malformed("does not start with an <archive>/data.pkl member")
        return []
    draft = _checkpoint_draft(reading.whole)
    directory = _directory_size(reading)
    limit = reading.config.integer("max_header_bytes")
    if directory is None:
        reading.truncated(
            reading.whole,
            "the PyTorch archive has no zip end record: it is truncated",
            {"size": reading.source.size},
        )
    elif directory > limit:
        reading.too_large(reading.whole, directory, limit, "max_header_bytes")
    else:
        try:
            with zipfile.ZipFile(io.BufferedReader(_SourceFile(reading.source))) as archive:
                names = archive.namelist()
        except (
            zipfile.BadZipFile,
            ValueError,
            OSError,
            EOFError,
            struct.error,
            NotImplementedError,
        ):
            reading.truncated(
                reading.whole,
                "the PyTorch archive's zip directory does not read: it is truncated or damaged",
                {"size": reading.source.size},
            )
        else:
            if name not in names:
                reading.truncated(
                    reading.whole,
                    "the PyTorch archive's zip directory does not list its first member",
                    {"size": reading.source.size},
                )
    return [draft]


SAFETENSORS: Final = Format(
    key="safetensors",
    label="safetensors checkpoint",
    spec=FormatSpec("safetensors checkpoint", extensions=(".safetensors",)),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_safetensors,
    read=_read_safetensors,
)
ONNX: Final = Format(
    key="onnx",
    label="ONNX model",
    spec=FormatSpec("ONNX model (ModelProto)", extensions=(".onnx",)),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_onnx,
    read=_read_onnx,
)
PYTORCH: Final = Format(
    key="pytorch",
    label="PyTorch archive",
    spec=FormatSpec("PyTorch checkpoint (torch.save zip archive)", extensions=(".pt", ".pth")),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_pytorch,
    read=_read_pytorch,
)
