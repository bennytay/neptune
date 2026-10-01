"""Content sniffing: what a source's first bytes say about its format before any adapter speaks.

Sniffing is observation, never a claim (ADR 0027). A matched signature says "these bytes begin
the way MCAP files do"; only an adapter's ``probe`` says "I read this". The engine uses what it
sniffs for two things: to open containers (a zip, a tar, a gzip stream) and look inside them,
and to say *what* a source is when no adapter claims it, so the receipt reads "an MCAP file no
registered adapter reads" instead of "unsupported".

The table holds formats that turn up in robotics evidence and the containers they travel in.
Every entry is a fixed byte string at a fixed offset, so matching is exact and cheap. Adapters
add their own declared magic (``FormatSpec.magic``) through ``declared_signatures``. Names are
never consulted here: a renamed file sniffs the same as the original.
"""

import codecs
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from neptune.adapters.contract import AdapterDescriptor, Magic
from neptune.model.jsonvalue import JsonObject, JsonValue


class ContainerKind(StrEnum):
    """A container the engine knows how to look inside, or at least to name."""

    ZIP = "zip"
    TAR = "tar"
    GZIP = "gzip"
    BZIP2 = "bzip2"
    XZ = "xz"
    ZSTD = "zstd"  # recognised, not opened: no decoder in the standard library
    SEVEN_ZIP = "7z"  # recognised, not opened


@dataclass(frozen=True)
class Signature:
    """A format's fixed bytes. Every ``Magic`` part must match for the signature to match.

    ``adapter`` names the adapter whose descriptor declares it; built-in entries have none.
    """

    name: str
    magic: tuple[Magic, ...]
    container: ContainerKind | None = None
    adapter: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.magic:
            raise ValueError("a signature has a name and at least one magic part")
        if any(not isinstance(part, Magic) for part in self.magic):
            raise TypeError("signature parts must be Magic")

    @property
    def weight(self) -> int:
        """Bytes compared: a longer match is the more specific one."""
        return sum(len(part.data) for part in self.magic)

    def matches(self, head: bytes) -> bool:
        return all(
            head[part.offset : part.offset + len(part.data)] == part.data for part in self.magic
        )

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"name": self.name}
        if self.container is not None:
            out["container"] = str(self.container)
        if self.adapter is not None:
            out["adapter"] = self.adapter
        return out


def _sig(name: str, *parts: tuple[int, bytes], container: ContainerKind | None = None) -> Signature:
    return Signature(name, tuple(Magic(offset, data) for offset, data in parts), container)


# Robotics logs and telemetry, point clouds and geometry, documents and media, data files, and
# the containers they arrive in. A format-specific probe (does this MCAP parse?) is the adapter's.
SIGNATURES: Final[tuple[Signature, ...]] = (
    _sig("MCAP", (0, b"\x89MCAP0\r\n")),
    _sig("ROS bag 2.0", (0, b"#ROSBAG V2.0\n")),
    _sig("SQLite 3 database", (0, b"SQLite format 3\x00")),
    _sig("ULog", (0, b"ULog\x01\x12\x35")),
    _sig("pcap", (0, b"\xd4\xc3\xb2\xa1")),
    _sig("pcap", (0, b"\xa1\xb2\xc3\xd4")),
    _sig("pcapng", (0, b"\x0a\x0d\x0d\x0a")),
    _sig("LAS point cloud", (0, b"LASF")),
    _sig("PCD point cloud", (0, b"# .PCD")),
    _sig("PLY", (0, b"ply\n")),
    _sig("PLY", (0, b"ply\r\n")),
    _sig("E57", (0, b"ASTM-E57")),
    _sig("glTF binary", (0, b"glTF")),
    _sig("XML", (0, b"<?xml")),
    _sig("XML", (0, b"\xef\xbb\xbf<?xml")),
    _sig("PDF", (0, b"%PDF-")),
    _sig("PNG", (0, b"\x89PNG\r\n\x1a\n")),
    _sig("JPEG", (0, b"\xff\xd8\xff")),
    _sig("TIFF", (0, b"II*\x00")),
    _sig("TIFF", (0, b"MM\x00*")),
    _sig("GIF", (0, b"GIF87a")),
    _sig("GIF", (0, b"GIF89a")),
    _sig("WebP", (0, b"RIFF"), (8, b"WEBP")),
    _sig("WAVE audio", (0, b"RIFF"), (8, b"WAVE")),
    _sig("AVI", (0, b"RIFF"), (8, b"AVI ")),
    _sig("ISO base media (MP4, MOV, HEIF)", (4, b"ftyp")),
    _sig("Matroska / WebM", (0, b"\x1a\x45\xdf\xa3")),
    _sig("Ogg", (0, b"OggS")),
    _sig("FLAC", (0, b"fLaC")),
    _sig("HDF5", (0, b"\x89HDF\r\n\x1a\n")),
    _sig("Parquet", (0, b"PAR1")),
    _sig("NumPy array", (0, b"\x93NUMPY")),
    _sig("ELF executable", (0, b"\x7fELF")),
    _sig("ZIP archive", (0, b"PK\x03\x04"), container=ContainerKind.ZIP),
    _sig("ZIP archive (empty)", (0, b"PK\x05\x06"), container=ContainerKind.ZIP),
    _sig("ZIP archive (spanned)", (0, b"PK\x07\x08"), container=ContainerKind.ZIP),
    _sig("tar archive", (257, b"ustar"), container=ContainerKind.TAR),
    _sig("gzip", (0, b"\x1f\x8b\x08"), container=ContainerKind.GZIP),
    _sig("bzip2", (0, b"BZh"), container=ContainerKind.BZIP2),
    _sig("xz", (0, b"\xfd7zXZ\x00"), container=ContainerKind.XZ),
    _sig("zstd", (0, b"\x28\xb5\x2f\xfd"), container=ContainerKind.ZSTD),
    _sig("7-Zip archive", (0, b"7z\xbc\xaf\x27\x1c"), container=ContainerKind.SEVEN_ZIP),
)


def declared_signatures(descriptors: Iterable[AdapterDescriptor]) -> tuple[Signature, ...]:
    """The magic every adapter declares for its formats, as signatures naming the adapter."""
    found = [
        Signature(spec.name, spec.magic, adapter=descriptor.id)
        for descriptor in descriptors
        for spec in descriptor.formats
        if spec.magic
    ]
    return tuple(sorted(found, key=lambda s: (s.adapter or "", s.name)))


class TextClass(StrEnum):
    """What the head's bytes are as text, if anything. A classification, not a decoding."""

    EMPTY = "empty"
    BINARY = "binary"  # a NUL or a control byte text does not use
    UTF8 = "utf8"
    UTF8_BOM = "utf8_bom"
    UTF16_BOM = "utf16_bom"
    DAMAGED_UTF8 = "damaged_utf8"  # no binary bytes, but not valid UTF-8 either


_UTF8_BOM: Final = b"\xef\xbb\xbf"
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")
# C0 controls that plain text uses: tab, LF, VT, FF, CR, and ESC for terminal colours in logs.
_TEXT_CONTROLS: Final = frozenset(b"\t\n\x0b\x0c\r\x1b")
_BINARY_CONTROLS: Final = bytes(b for b in range(0x20) if b not in _TEXT_CONTROLS)


def classify_text(head: bytes, size: int) -> TextClass:
    """Classify ``head``, the first ``len(head)`` of a ``size``-byte source."""
    if not head:
        return TextClass.EMPTY
    if head.startswith(_UTF16_BOMS):
        return TextClass.UTF16_BOM
    if b"\x00" in head or len(head.translate(None, _BINARY_CONTROLS)) != len(head):
        return TextClass.BINARY
    try:
        # A head cut short of the source may end inside a character: decode it as unfinished.
        codecs.getincrementaldecoder("utf-8")().decode(head, final=len(head) == size)
    except UnicodeDecodeError:
        return TextClass.DAMAGED_UTF8
    return TextClass.UTF8_BOM if head.startswith(_UTF8_BOM) else TextClass.UTF8


@dataclass(frozen=True)
class Sniff:
    """What the head says: matched signatures, most specific first, and its text class."""

    signatures: tuple[Signature, ...]
    text: TextClass

    @property
    def container(self) -> ContainerKind | None:
        """The container the most specific matched signature names, if any."""
        for signature in self.signatures:
            if signature.container is not None:
                return signature.container
        return None

    def describe(self) -> str:
        """One line for a finding: ``MCAP signature; binary``."""
        names = sorted({signature.name for signature in self.signatures})
        found = ", ".join(names) + " signature" if names else "no known signature"
        return f"{found}; {self.text.replace('_', ' ')}"

    def to_json(self) -> JsonObject:
        return {
            "signatures": [signature.to_json() for signature in self.signatures],
            "text": str(self.text),
        }


def sniff(head: bytes, size: int, extra: Iterable[Signature] = ()) -> Sniff:
    """Match ``head`` against the built-in table and ``extra`` (adapters' declared magic).

    Matches are ordered most specific first (bytes compared, then name, then adapter), so the
    order is total and never depends on the table's order.
    """
    matched = [s for s in (*SIGNATURES, *extra) if s.matches(head)]
    matched.sort(key=lambda s: (-s.weight, s.name, s.adapter or ""))
    return Sniff(tuple(matched), classify_text(head, size))


def sniff_from_json(data: JsonValue, extra: Iterable[Signature] = ()) -> Sniff:
    """A ``Sniff`` from its JSON, each signature found in the built-in table or ``extra`` (the
    adapters' declared magic). Strict: an unknown signature or text class is a ``ValueError``.

    The JSON names a signature, not its bytes, and a few formats have two (pcap's byte orders,
    PLY's line endings); the first with that name, container and adapter stands for both, and
    reads back as the same JSON.
    """
    if not isinstance(data, dict) or data.keys() != {"signatures", "text"}:
        raise ValueError("a sniff is exactly signatures and text")
    found, text = data["signatures"], data["text"]
    if not isinstance(found, list) or not isinstance(text, str):
        raise ValueError("a sniff's signatures are a list and its text a class")
    by_json: dict[str, Signature] = {}
    for signature in (*SIGNATURES, *extra):
        by_json.setdefault(repr(sorted(signature.to_json().items())), signature)
    signatures = []
    for item in found:
        if not isinstance(item, dict):
            raise ValueError("a signature is an object")
        known = by_json.get(repr(sorted(item.items())))
        if known is None:
            raise ValueError(f"not a known signature: {item!r}")
        signatures.append(known)
    return Sniff(tuple(signatures), TextClass(text))
