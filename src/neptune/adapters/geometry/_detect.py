"""Which geometry format the first bytes start, and how sure the bytes alone make that.

PLY, GLB, USD (ASCII and crate) have signatures: a match is ``SIGNATURE``, and a header that also
parses is ``VERIFIED``. Binary STL has none, but its size is exactly ``84 + 50 * count``: that is
``SIGNATURE`` (a file that merely starts ``solid`` still is binary if its size says so, a known
trap). The text formats are claimed by their grammar: ASCII STL by ``solid`` then ``facet`` or
``endsolid``, glTF JSON by an ``asset`` object with a 2.x ``version``, OBJ by lines that are all OBJ
statements with at least one vertex of three numbers (``STRUCTURE``; glTF is ``SIGNATURE``, so it
is never left to a JSON reader). The name is only a last resort: ``NAME_ONLY`` for a non-empty
file of a geometry extension no signature or grammar explains (a truncated binary STL).
"""

import re
import struct
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import NAME_ONLY, SIGNATURE, STRUCTURE, VERIFIED

OBJ: Final = "obj"
STL_ASCII: Final = "stl_ascii"
STL_BINARY: Final = "stl_binary"
PLY: Final = "ply"
GLTF: Final = "gltf"
GLB: Final = "glb"
USDA: Final = "usda"
USDC: Final = "usdc"
EXTENSIONS: Final = {
    ".obj": OBJ, ".stl": STL_BINARY, ".ply": PLY, ".gltf": GLTF, ".glb": GLB,
    ".usd": USDA, ".usda": USDA, ".usdc": USDC,
}  # fmt: skip
_PLY_FORMAT: Final = re.compile(
    rb"\nformat (?:ascii|binary_little_endian|binary_big_endian) 1\.0\r?\n"
)
_GLTF_ASSET: Final = re.compile(rb'"asset"\s*:\s*\{[^{}]*"version"\s*:\s*"(2\.[0-9]+)"')
_USDA: Final = re.compile(rb"#usda ([0-9]+\.[0-9]+)")
OBJ_KEYWORDS: Final = frozenset(
    {b"v", b"vt", b"vn", b"vp", b"f", b"l", b"p", b"o", b"g", b"s", b"usemtl", b"mtllib"}
)
_BOM: Final = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class Detected:
    format: str
    confidence: float
    reason: str
    version: str | None = None


def detect(head: bytes, size: int) -> Detected | None:
    """The format ``head`` (the first bytes of a ``size``-byte source) starts, or ``None``."""
    if head.startswith(b"glTF"):
        version = struct.unpack("<I", head[4:8])[0] if len(head) >= 8 else None
        verified = len(head) >= 20 and head[16:20] == b"JSON" and version == 2
        return Detected(
            GLB, VERIFIED if verified else SIGNATURE, "the glTF binary signature" + (
                " and a JSON chunk" if verified else ""
            ), None if version is None else str(version),
        )  # fmt: skip
    if head.startswith(b"PXR-USDC"):
        return Detected(USDC, SIGNATURE, "the USD crate signature PXR-USDC")
    found = _USDA.match(head)
    if found is not None:
        return Detected(USDA, SIGNATURE, "the #usda layer signature", found.group(1).decode())
    if head[:3] == b"ply" and head[3:4] in (b"\n", b"\r"):
        verified = _PLY_FORMAT.search(head[:1024]) is not None
        return Detected(
            PLY, VERIFIED if verified else SIGNATURE,
            "the ply signature" + (" and a format line" if verified else ""),
        )  # fmt: skip
    if _binary_stl(head, size):
        return Detected(STL_BINARY, SIGNATURE, "an 84-byte header whose facet count fits the size")
    if head.lstrip().startswith(b"{"):  # JSON has no byte order mark (RFC 8259, glTF 2.0)
        asset = _GLTF_ASSET.search(head)
        if asset is not None:
            return Detected(GLTF, SIGNATURE, "a glTF asset object", asset.group(1).decode())
        return None
    text = head[len(_BOM) :] if head.startswith(_BOM) else head
    stripped = text.lstrip()
    if stripped[:5].lower() == b"solid" and re.search(
        rb"\b(?:facet|endsolid)\b", stripped[:4096], re.I
    ):
        return Detected(STL_ASCII, STRUCTURE, "a solid followed by facets")
    if _obj(text, size - (len(head) - len(text))):
        return Detected(OBJ, STRUCTURE, "OBJ statements with a vertex of three numbers")
    return None


def detect_by_name(name: str, size: int) -> Detected | None:
    """The last resort: a non-empty file whose extension is a geometry format's."""
    dot = name.rfind(".")
    kind = EXTENSIONS.get(name[dot:].lower()) if dot >= 0 else None
    if kind is None or size == 0:
        return None
    return Detected(kind, NAME_ONLY, f"only the extension {name[dot:].lower()!r} suggests it")


def _binary_stl(head: bytes, size: int) -> bool:
    if size < 84 or len(head) < 84:
        return False
    (count,) = struct.unpack("<I", head[80:84])
    return bool(size == 84 + 50 * count)


def _obj(head: bytes, size: int) -> bool:
    try:
        text = head.decode("utf-8", errors="ignore" if len(head) < size else "strict")
    except UnicodeDecodeError:
        return False
    if "\x00" in text:
        return False
    lines = text.split("\n")
    if len(head) < size:
        lines = lines[:-1]  # the head ends inside a line
    vertex = False
    for line in lines[:400]:
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        if words[0].encode() not in OBJ_KEYWORDS:
            return False
        if words[0] == "v" and len(words) >= 4:
            try:
                [float(word) for word in words[1:4]]
            except ValueError:
                return False
            vertex = True
    return vertex


def _plausible_facet(head: bytes, size: int) -> bool:
    """Binary STL with a size that disagrees with its count: is it still one? Its count is
    positive and its first facet, if any is present, has finite coordinates and a normal that is
    zero or of unit length (what writers emit). Random bytes almost never pass."""
    if size < 84 or len(head) < 84:
        return False
    (count,) = struct.unpack("<I", head[80:84])
    if count == 0:
        return False
    if len(head) < 134:
        return True
    facet = struct.unpack("<12f", head[84:132])
    if not all(abs(value) < 1e30 for value in facet):  # NaN fails every comparison
        return False
    length = sum(value * value for value in facet[:3]) ** 0.5
    return bool(length == 0.0 or abs(length - 1.0) < 0.05)


def _obj_like(head: bytes, size: int) -> bool:
    """A text head with at least one vertex of three numbers, whatever else its lines say."""
    if b"\x00" in head:
        return False
    lines = head.decode("utf-8", errors="ignore").split("\n")
    if len(head) < size:
        lines = lines[:-1]
    for line in lines[:400]:
        words = line.split("#", 1)[0].split()
        if len(words) >= 4 and words[0] == "v":
            try:
                [float(word) for word in words[1:4]]
            except ValueError:
                continue
            return True
    return False


def lenient(head: bytes, size: int) -> Detected | None:
    """What ``ingest`` reads a source as: a detected format, else a damaged one it can still read
    (a binary STL whose size disagrees with its count, an OBJ with statements of other kinds, an
    ASCII STL cut short). Only reached for a source this adapter was chosen for; a file is never
    called geometry for being damaged unless its first bytes are shaped like one."""
    found = detect(head, size)
    if found is not None:
        return found
    if _plausible_facet(head, size):
        return Detected(STL_BINARY, NAME_ONLY, "a damaged binary STL")
    if head.lstrip()[:5].lower() == b"solid" and b"\x00" not in head:
        return Detected(STL_ASCII, NAME_ONLY, "an ASCII STL with no facets")
    if _obj_like(head[len(_BOM) :] if head.startswith(_BOM) else head, size):
        return Detected(OBJ, NAME_ONLY, "an OBJ with statements that are not OBJ's")
    return None
