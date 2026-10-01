"""Firmware images and binaries: the identity their own headers record (ADR 0040).

Each image is one item, ``observed`` (the bytes describe themselves). Binary fields are rendered
as their tools print them, and nothing is guessed from a file name or a string found elsewhere.

- **ELF** executables and shared objects: the GNU build-id note (``NT_GNU_BUILD_ID``) is
  ``build``, as lowercase hex; the systemd package-metadata note (``FDO``, type ``0xcafe1a7e``)
  gives ``name`` and ``release`` (declared text). Notes are found through the section headers,
  else the program headers. Without the notes those fields are ``Unknown`` (ELF has a place for
  them). Object files and core dumps are not software that ran and are not claimed.
- **ESP-IDF app images**: the ``esp_app_desc_t`` at byte 32 gives ``project_name`` (``name``),
  ``version`` (a ``FirmwareVersion``) and ``app_elf_sha256`` (``build``, lowercase hex: the build
  output's own hash). The SDK version, date and time stay cited in the bytes.
- **MCUboot images**: the header's ``ih_ver`` is ``release``, rendered
  ``major.minor.revision+build`` as ``imgtool`` prints it.
- **PX4 and ArduPilot firmware files** (``.px4``, ``.apj``: JSON with ``magic`` ``PX4FWv1`` or
  ``APJFWv1``): ``summary`` is the board (``device``), ``git_hash`` the commit, ``git_identity``
  (``git describe`` output) the release, and the commit it names when no ``git_hash`` is given.
  The base64 image is never decoded.
"""

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SIGNATURE, VERIFIED, FormatSpec, Magic
from neptune.adapters.software._common import (
    Detected,
    Doc,
    Draft,
    Format,
    Reading,
    describe,
    json_head,
    loads_json,
    reason,
)
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Span
from neptune.model.versions import BuildId, FirmwareVersion, GitCommit

# --- ELF ---------------------------------------------------------------------------------------

_ELF_MAGIC: Final = b"\x7fELF"
_ELF_TYPES: Final = {2: "executable", 3: "shared object"}
_SHT_NOTE: Final = 7
_PT_NOTE: Final = 4
_NT_GNU_BUILD_ID: Final = 3
_NT_FDO_PACKAGING_METADATA: Final = 0xCAFE1A7E


@dataclass(frozen=True)
class _ElfHeader:
    order: str  # struct byte order
    wide: bool  # 64-bit
    type: int
    phoff: int
    phentsize: int
    phnum: int
    shoff: int
    shentsize: int
    shnum: int


def _elf_header(head: bytes) -> _ElfHeader | None:
    if len(head) < 52 or not head.startswith(_ELF_MAGIC):
        return None
    klass, data, version = head[4], head[5], head[6]
    if klass not in (1, 2) or data not in (1, 2) or version != 1:
        return None
    order = "<" if data == 1 else ">"
    wide = klass == 2
    if wide and len(head) < 64:
        return None
    (kind,) = struct.unpack_from(order + "H", head, 16)
    if wide:
        phoff, shoff = struct.unpack_from(order + "QQ", head, 32)
        phentsize, phnum, shentsize, shnum = struct.unpack_from(order + "HHHH", head, 54)
    else:
        phoff, shoff = struct.unpack_from(order + "II", head, 28)
        phentsize, phnum, shentsize, shnum = struct.unpack_from(order + "HHHH", head, 42)
    return _ElfHeader(order, wide, kind, phoff, phentsize, phnum, shoff, shentsize, shnum)


def _detect_elf(head: bytes, size: int) -> Detected | None:
    header = _elf_header(head)
    if header is None or header.type not in _ELF_TYPES:
        return None
    what = f"an ELF {_ELF_TYPES[header.type]}"
    return Detected(SIGNATURE, reason("elf", f"ELF magic and header: {what}"))


@dataclass(frozen=True)
class _Region:
    offset: int
    size: int
    align: int


@dataclass(frozen=True)
class _Note:
    name: bytes
    type: int
    offset: int  # of the descriptor
    desc: bytes


def _note_regions(reading: Reading, header: _ElfHeader) -> tuple[list[_Region], ByteRange]:
    """The note sections (or, with no section headers, note segments) and the table looked at."""
    limit = reading.config.integer("max_header_bytes")
    sections = header.shoff > 0 and header.shnum > 0
    offset, count, entsize = (
        (header.shoff, header.shnum, header.shentsize)
        if sections
        else (header.phoff, header.phnum, header.phentsize)
    )
    minimum = (64 if header.wide else 40) if sections else (56 if header.wide else 32)
    table = ByteRange(min(offset, reading.source.size), 0)
    if count == 0 or entsize < minimum:
        return [], table
    length = count * entsize
    table = ByteRange(min(offset, reading.source.size), length)
    if offset + length > reading.source.size:
        reading.truncated(
            reading.whole,
            f"the ELF's {'section' if sections else 'program'} header table runs past the end",
            {"end": offset + length, "size": reading.source.size},
        )
        return [], ByteRange(min(offset, reading.source.size), 0)
    if length > limit:
        reading.too_large(reading.at(table), length, limit, "max_header_bytes")
        return [], table
    data = reading.read(offset, length)
    regions = []
    for index in range(count):
        entry = index * entsize
        if sections:
            (kind,) = struct.unpack_from(header.order + "I", data, entry + 4)
            fields = "QQ" if header.wide else "II"
            at = (24, 32, 48) if header.wide else (16, 20, 32)
            start, size = struct.unpack_from(header.order + fields, data, entry + at[0])
            (align,) = struct.unpack_from(header.order + fields[0], data, entry + at[2])
            note = kind == _SHT_NOTE
        else:
            (kind,) = struct.unpack_from(header.order + "I", data, entry)
            if header.wide:
                (start,) = struct.unpack_from(header.order + "Q", data, entry + 8)
                (size,) = struct.unpack_from(header.order + "Q", data, entry + 32)
                (align,) = struct.unpack_from(header.order + "Q", data, entry + 48)
            else:
                (start,) = struct.unpack_from(header.order + "I", data, entry + 4)
                (size,) = struct.unpack_from(header.order + "I", data, entry + 16)
                (align,) = struct.unpack_from(header.order + "I", data, entry + 28)
            note = kind == _PT_NOTE
        if note and size > 0:
            regions.append(_Region(start, size, 8 if align == 8 else 4))
    return regions, table


def _pad(length: int, align: int) -> int:
    return (length + align - 1) // align * align


def _notes(reading: Reading, header: _ElfHeader, region: _Region) -> Iterator[_Note]:
    limit = reading.config.integer("max_header_bytes")
    subject = reading.span(min(region.offset, reading.source.size), 0)
    if region.offset + region.size > reading.source.size:
        reading.truncated(
            subject,
            "an ELF note section runs past the end of the file",
            {"end": region.offset + region.size, "size": reading.source.size},
        )
        return
    if region.size > limit:
        reading.too_large(
            reading.span(region.offset, region.size), region.size, limit, "max_header_bytes"
        )
        return
    data = reading.read(region.offset, region.size)
    at = 0
    while at + 12 <= len(data):
        namesz, descsz, kind = struct.unpack_from(header.order + "III", data, at)
        name_start = at + 12
        desc_start = name_start + _pad(namesz, region.align)
        desc_end = desc_start + descsz
        if desc_end > len(data):
            reading.malformed_entry(
                reading.span(region.offset + at, len(data) - at),
                "is an ELF note that runs past its section",
            )
            return
        name = data[name_start : name_start + namesz].rstrip(b"\x00")
        yield _Note(name, kind, region.offset + desc_start, data[desc_start:desc_end])
        at = desc_start + _pad(descsz, region.align)


def _read_elf(reading: Reading) -> list[Draft]:
    header = _elf_header(reading.read(0, 64))
    if header is None:
        reading.malformed("has no ELF header")
        return []
    regions, table = _note_regions(reading, header)
    draft = Draft(entry=reading.at(table))
    looked = reading.provenance(draft.entry)
    builds, names, releases = [], [], []
    read_limit = reading.config.integer("max_header_bytes")
    note_limit = reading.config.integer("max_items")
    seen: set[_Region] = set()
    spent = notes = 0
    capped = False
    for region in regions:
        if capped:
            break
        if region in seen:  # the same bytes again add no note
            continue
        seen.add(region)
        spent += region.size
        if spent > read_limit:  # hostile tables point many regions at the same big range
            reading.too_large(
                reading.span(region.offset, region.size), spent, read_limit, "max_header_bytes"
            )
            break
        for note in _notes(reading, header, region):
            notes += 1
            if notes > note_limit:
                reading.too_many_entries(reading.span(note.offset, len(note.desc)), note_limit)
                capped = True
                break
            at = reading.span(note.offset, len(note.desc))
            if note.name == b"GNU" and note.type == _NT_GNU_BUILD_ID:
                builds.append(
                    reading.value(draft, "build", note.desc.hex(), at, BuildId, "build id")
                )
            elif note.name == b"FDO" and note.type == _NT_FDO_PACKAGING_METADATA:
                text = note.desc.rstrip(b"\x00")
                try:
                    metadata = loads_json(text.decode("utf-8"))
                except (ValueError, RecursionError):
                    reading.malformed_entry(at, "is a package-metadata note that is not JSON")
                    continue
                if not isinstance(metadata, dict):
                    reading.malformed_entry(at, "is a package-metadata note that is not an object")
                    continue
                doc = Doc(reading, (ByteRange(note.offset, len(text)),))
                if "name" in metadata:
                    names.append(reading.text(draft, "name", metadata["name"], doc.ref("name")))
                if "version" in metadata:
                    releases.append(
                        reading.declared(draft, metadata["version"], doc.ref("version"))
                    )
    draft.build = reading.choose(draft, "build", builds, Unknown(looked))
    draft.name = reading.choose(draft, "name", names, Unknown(looked))
    draft.release = reading.choose(draft, "release", releases, Unknown(looked))
    return [draft]


# --- ESP-IDF app image -------------------------------------------------------------------------

_ESP_IMAGE_MAGIC: Final = 0xE9
_ESP_APP_DESC_MAGIC: Final = 0xABCD5432
_ESP_DESC: Final = 32  # the app descriptor follows the image header and the first segment header
_ESP_DESC_SIZE: Final = 256


def _detect_esp(head: bytes, size: int) -> Detected | None:
    if len(head) < _ESP_DESC + 4 or head[0] != _ESP_IMAGE_MAGIC:
        return None
    (magic,) = struct.unpack_from("<I", head, _ESP_DESC)
    if magic != _ESP_APP_DESC_MAGIC:
        return None
    return Detected(
        SIGNATURE, reason("esp_app", "ESP image magic and ESP-IDF app-descriptor magic")
    )


def _c_string(field: bytes) -> bytes:
    return field.split(b"\x00", 1)[0]


def _read_esp(reading: Reading) -> list[Draft]:
    data = reading.read(0, _ESP_DESC + _ESP_DESC_SIZE)
    if len(data) < _ESP_DESC + _ESP_DESC_SIZE:
        reading.truncated(
            reading.whole,
            "the ESP-IDF app descriptor runs past the end of the image",
            {"end": _ESP_DESC + _ESP_DESC_SIZE, "size": reading.source.size},
        )
        return []
    draft = Draft(entry=reading.span(_ESP_DESC, _ESP_DESC_SIZE))

    def field(offset: int) -> tuple[str | None, EvidenceRef]:
        raw = _c_string(data[offset : offset + 32])
        at = reading.span(offset, len(raw) if raw else 32)
        try:
            return raw.decode("utf-8"), at
        except UnicodeDecodeError:
            return None, at

    name, name_at = field(_ESP_DESC + 48)
    draft.name = (
        reading.invalid(draft, "name", name_at, "is not UTF-8")
        if name is None
        else reading.text(draft, "name", name, name_at)
    )
    version, version_at = field(_ESP_DESC + 16)
    draft.release = (
        reading.invalid(draft, "release", version_at, "is not UTF-8")
        if version is None
        else reading.value(
            draft, "release", version, version_at, FirmwareVersion, "firmware version"
        )
    )
    digest_at = reading.span(_ESP_DESC + 144, 32)
    raw_digest = data[_ESP_DESC + 144 : _ESP_DESC + 176]
    digest = raw_digest.hex()
    if not any(raw_digest) or all(byte == 0xFF for byte in raw_digest):
        # An unfinalised or erased image: all zero or all 0xff is no hash, and two such images
        # must not look like one build.
        draft.build = reading.invalid(draft, "build", digest_at, "is unset (all zero or all 0xff)")
    else:
        draft.build = reading.value(draft, "build", digest, digest_at, BuildId, "build id")
    return [draft]


# --- MCUboot image -----------------------------------------------------------------------------

_MCUBOOT_MAGIC: Final = 0x96F3B83D
_MCUBOOT_HEADER: Final = 32


def _detect_mcuboot(head: bytes, size: int) -> Detected | None:
    if len(head) < _MCUBOOT_HEADER or struct.unpack_from("<I", head)[0] != _MCUBOOT_MAGIC:
        return None
    return Detected(SIGNATURE, reason("mcuboot", "MCUboot image header magic"))


def _read_mcuboot(reading: Reading) -> list[Draft]:
    data = reading.read(0, _MCUBOOT_HEADER)
    if len(data) < _MCUBOOT_HEADER:
        reading.truncated(
            reading.whole,
            "the MCUboot header runs past the end of the image",
            {"end": _MCUBOOT_HEADER, "size": reading.source.size},
        )
        return []
    header_size, _, image_size = struct.unpack_from("<HHI", data, 8)
    major, minor, revision, build = struct.unpack_from("<BBHI", data, 20)
    if header_size + image_size > reading.source.size:
        reading.truncated(
            reading.span(0, _MCUBOOT_HEADER),
            "the MCUboot header declares an image that runs past the end of the file",
            {"end": header_size + image_size, "size": reading.source.size},
        )
    draft = Draft(entry=reading.span(0, _MCUBOOT_HEADER))
    at = reading.span(20, 8)
    version = FirmwareVersion(f"{major}.{minor}.{revision}+{build}")
    draft.release = Known(version, reading.provenance(at))
    return [draft]


# --- PX4 and ArduPilot firmware files ----------------------------------------------------------

_PX4_MAGIC: Final = re.compile(rb'"magic"\s*:\s*"(PX4FWv1|APJFWv1)"')


def _detect_px4(head: bytes, size: int) -> Detected | None:
    if not head.lstrip().startswith(b"{"):
        return None
    match = _PX4_MAGIC.search(head)
    if match is None:
        return None
    what = "PX4" if match[1] == b"PX4FWv1" else "ArduPilot"
    document = json_head(head, size)
    if isinstance(document, dict) and document.get("magic") in ("PX4FWv1", "APJFWv1"):
        why = reason("px4_firmware", f"a {what} firmware file that parses")
        return Detected(VERIFIED, why)
    return Detected(
        SIGNATURE, reason("px4_firmware", f"a JSON object with {what}'s firmware magic")
    )


def _read_px4(reading: Reading) -> list[Draft]:
    data = reading.json(reading.document())
    if data is None:
        return []
    if not isinstance(data, dict) or data.get("magic") not in ("PX4FWv1", "APJFWv1"):
        reading.malformed("is not a JSON object with PX4 or ArduPilot firmware magic")
        return []
    doc = Doc(reading, (ByteRange(0, reading.source.size),))
    draft = Draft(entry=doc.ref())
    absent = Unknown(reading.provenance(draft.entry))

    def at(key: str) -> EvidenceRef:
        return doc.ref(key) if key in data else draft.entry

    draft.device = reading.text(draft, "device", data.get("summary"), at("summary"))
    commits = []
    if "git_hash" in data:
        commits.append(
            reading.value(
                draft, "commit", data["git_hash"], at("git_hash"), GitCommit, "git object name"
            )
        )
    identity = data.get("git_identity")
    draft.release = absent
    if isinstance(identity, str) and identity.strip():
        tagged, span = describe(identity)
        if tagged:
            draft.release = reading.declared(draft, identity, at("git_identity"))
        else:
            draft.release = Unknown(reading.provenance(at("git_identity")))
        if span is not None and not commits:
            spanned = reading.at(*at("git_identity").locator, Span(*span))
            commits.append(
                reading.value(
                    draft,
                    "commit",
                    identity[span[0] : span[1]],
                    spanned,
                    GitCommit,
                    "git object name",
                )
            )
    elif identity is not None:
        draft.release = reading.declared(draft, identity, at("git_identity"))
    draft.commit = reading.choose(draft, "commit", commits, absent)
    return [draft]


ELF: Final = Format(
    key="elf",
    label="ELF binary",
    spec=FormatSpec(
        "ELF executable or shared object", extensions=(".elf",), magic=(Magic(0, _ELF_MAGIC),)
    ),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_elf,
    read=_read_elf,
)
ESP_APP: Final = Format(
    key="esp_app",
    label="ESP-IDF app image",
    spec=FormatSpec("ESP-IDF application image"),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_esp,
    read=_read_esp,
)
MCUBOOT: Final = Format(
    key="mcuboot",
    label="MCUboot image",
    spec=FormatSpec("MCUboot firmware image", magic=(Magic(0, struct.pack("<I", _MCUBOOT_MAGIC)),)),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_mcuboot,
    read=_read_mcuboot,
)
PX4_FIRMWARE: Final = Format(
    key="px4_firmware",
    label="PX4/ArduPilot firmware file",
    spec=FormatSpec("PX4 or ArduPilot firmware file", extensions=(".apj", ".px4")),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_px4,
    read=_read_px4,
)
