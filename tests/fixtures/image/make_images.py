"""Write the image fixtures, byte for byte, from the standard library alone.

    uv run python tests/fixtures/image/make_images.py             # the fixtures
    uv run --no-project --with pillow python tests/fixtures/image/make_images.py --oracle

The second command reads every valid fixture with Pillow, the reference Python image reader, and
writes ``oracle.json``: what an independent reader says each file's size, mode, EXIF, GPS, ICC
profile and XMP are. Pillow is never a dependency of Neptune; ``test_image_fixtures.py`` checks
the committed files are what ``build()`` writes and the adapter's output against the oracle.

Every file is written here, never by an encoder: a JPEG of flat grey blocks whose entropy-coded
data is two bits per block, a PNG through ``zlib``, TIFF and DNG through a small TIFF writer,
minimal VP8 and VP8L bitstreams. The robots vary on purpose (a pipe crawler, a warehouse AMR, an
ROV, a field rover, a humanoid, a quadruped, a manipulator's wrist camera).
"""

import json
import struct
import sys
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

HERE: Final = Path(__file__).parent

# --- TIFF / EXIF ------------------------------------------

BYTE, ASCII, SHORT, LONG, RATIONAL = 1, 2, 3, 4, 5
UNDEFINED, SSHORT, SLONG, SRATIONAL, FLOAT, DOUBLE = 7, 8, 9, 10, 11, 12
_SIZE: Final = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8}
_FORMAT: Final = {1: "B", 3: "H", 4: "I", 7: "B", 8: "h", 9: "i", 11: "f", 12: "d", 16: "Q"}


@dataclass
class Tag:
    """One entry. ``value``: bytes (ASCII, UNDEFINED), numbers, (n, d) pairs, or a reference.

    A reference is ``("ifd", name)``, ``("ifds", [names])`` or ``("blob", name)``: the offset of
    another IFD or of a data blob, resolved when the file is laid out.
    """

    tag: int
    type: int
    value: Any


@dataclass
class Directory:
    entries: list[Tag]
    next: str | None = None


def ascii_value(text: str) -> bytes:
    return text.encode("latin-1") + b"\x00"


@dataclass
class TiffLayout:
    directories: dict[str, Directory]
    first: str
    little: bool = True
    big: bool = False
    blobs: dict[str, bytes] = field(default_factory=dict)

    def _count(self, tag: Tag) -> int:
        value = tag.value
        if isinstance(value, tuple) and value[0] == "ifds":
            return len(value[1])
        if isinstance(value, tuple) and value[0] in ("ifd", "blob"):
            return 1
        return len(value)

    def _payload(self, tag: Tag, offsets: dict[str, int]) -> bytes:
        order = "<" if self.little else ">"
        value = tag.value
        if isinstance(value, tuple) and value[0] in ("ifd", "blob", "ifds"):
            names = value[1] if value[0] == "ifds" else [value[1]]
            fmt = "Q" if tag.type == 16 else "I"
            return struct.pack(f"{order}{len(names)}{fmt}", *(offsets[name] for name in names))
        if isinstance(value, bytes):
            return value
        if tag.type in (RATIONAL, SRATIONAL):
            flat = [number for pair in value for number in pair]
            return struct.pack(f"{order}{len(flat)}{'I' if tag.type == RATIONAL else 'i'}", *flat)
        return struct.pack(f"{order}{len(value)}{_FORMAT[tag.type]}", *value)

    def build(self) -> bytes:
        order = "<" if self.little else ">"
        count_size, entry_size, tail, inline = (8, 20, 8, 8) if self.big else (2, 12, 4, 4)
        header_size = 16 if self.big else 8
        sizes: dict[str, int] = {}
        for name, directory in self.directories.items():
            spill = sum(
                (n + 1) & ~1
                for n in (self._count(t) * _SIZE[t.type] for t in directory.entries)
                if n > inline
            )
            sizes[name] = count_size + entry_size * len(directory.entries) + tail + spill
        offsets: dict[str, int] = {}
        position = header_size
        for name in self.directories:
            offsets[name] = position
            position += sizes[name]
        for name, blob in self.blobs.items():
            offsets[name] = position
            position += (len(blob) + 1) & ~1
        out = bytearray()
        if self.big:
            out += b"II" if self.little else b"MM"
            out += struct.pack(order + "HHHQ", 43, 8, 0, offsets[self.first])
        else:
            out += (b"II" if self.little else b"MM") + struct.pack(
                order + "HI", 42, offsets[self.first]
            )
        for name, directory in self.directories.items():
            start = offsets[name]
            values_at = start + count_size + entry_size * len(directory.entries) + tail
            entries, values = bytearray(), bytearray()
            entries += struct.pack(order + ("Q" if self.big else "H"), len(directory.entries))
            for tag in directory.entries:
                payload = self._payload(tag, offsets)
                count = self._count(tag)
                head = struct.pack(order + ("HHQ" if self.big else "HHI"), tag.tag, tag.type, count)
                if len(payload) <= inline:
                    field_bytes = payload.ljust(inline, b"\x00")
                else:
                    pointer = values_at + len(values)
                    field_bytes = struct.pack(order + ("Q" if self.big else "I"), pointer)
                    values += payload + b"\x00" * (len(payload) & 1)
                entries += head + field_bytes
            following = offsets[directory.next] if directory.next else 0
            entries += struct.pack(order + ("Q" if self.big else "I"), following)
            assert len(out) == start, (name, len(out), start)
            out += entries + values
        for name, blob in self.blobs.items():
            assert len(out) == offsets[name]
            out += blob + b"\x00" * (len(blob) & 1)
        return bytes(out)


def exif_block(
    ifd0: list[Tag],
    exif: list[Tag] | None = None,
    gps: list[Tag] | None = None,
    interop: list[Tag] | None = None,
    ifd1: list[Tag] | None = None,
    little: bool = True,
) -> bytes:
    """An EXIF TIFF stream: IFD0 with Exif and GPS pointers, IFD1 after it."""
    directories: dict[str, Directory] = {}
    entries = list(ifd0)
    if exif is not None:
        entries.append(Tag(34665, LONG, ("ifd", "exif")))
    if gps is not None:
        entries.append(Tag(34853, LONG, ("ifd", "gps")))
    directories["ifd0"] = Directory(sorted(entries, key=lambda t: t.tag), "ifd1" if ifd1 else None)
    if exif is not None:
        exif_entries = list(exif)
        if interop is not None:
            exif_entries.append(Tag(40965, LONG, ("ifd", "interop")))
        directories["exif"] = Directory(sorted(exif_entries, key=lambda t: t.tag))
    if interop is not None:
        directories["interop"] = Directory(interop)
    if gps is not None:
        directories["gps"] = Directory(sorted(gps, key=lambda t: t.tag))
    if ifd1 is not None:
        directories["ifd1"] = Directory(ifd1)
    return TiffLayout(directories, "ifd0", little=little).build()


# --- ICC and XMP ------------------------------------------


def icc_profile() -> bytes:
    """A small v2 display profile: header, a tag table of four tags, their data."""
    desc = b"desc" + b"\x00" * 4 + struct.pack(">I", 13) + b"Neptune grey\x00" + b"\x00" * 79
    wtpt = b"XYZ " + b"\x00" * 4 + struct.pack(">3i", 63190, 65536, 54061)
    cprt = b"text" + b"\x00" * 4 + b"No copyright, test data\x00"
    gtrc = b"curv" + b"\x00" * 4 + struct.pack(">IH", 1, 0x0233)
    tags = [(b"desc", desc), (b"wtpt", wtpt), (b"cprt", cprt), (b"kTRC", gtrc)]
    table_size = 4 + 12 * len(tags)
    offset = 128 + table_size
    table, data = bytearray(struct.pack(">I", len(tags))), bytearray()
    for signature, body in tags:
        table += signature + struct.pack(">II", offset + len(data), len(body))
        data += body + b"\x00" * (-len(body) % 4)
    size = 128 + table_size + len(data)
    header = bytearray(128)
    header[0:4] = struct.pack(">I", size)
    header[4:8] = b"lcms"
    header[8:12] = bytes([2, 0x10, 0, 0])
    header[12:16], header[16:20], header[20:24] = b"mntr", b"GRAY", b"XYZ "
    header[24:36] = struct.pack(">6H", 2026, 9, 14, 10, 21, 7)
    header[36:40] = b"acsp"
    header[40:44] = b"\x00\x00\x00\x00"  # no primary platform
    header[64:68] = struct.pack(">I", 0)
    header[68:80] = struct.pack(">3i", 63190, 65536, 54061)
    header[80:84] = b"nept"
    return bytes(header) + bytes(table) + bytes(data)


XMP: Final = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
    xmlns:Camera="http://pix4d.com/camera/1.0/"
    xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
    tiff:Make="Ridgeback Robotics"
    Camera:ModelType="perspective">
   <dc:title>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">Pipe 7, joint 12: weld inspection</rdf:li>
    </rdf:Alt>
   </dc:title>
   <dc:subject>
    <rdf:Bag>
     <rdf:li>weld</rdf:li>
     <rdf:li>pipe-7</rdf:li>
    </rdf:Bag>
   </dc:subject>
   <Camera:PerspectiveFocalLength>4.35</Camera:PerspectiveFocalLength>
   <Camera:PrincipalPoint>3.12,2.34</Camera:PrincipalPoint>
   <Camera:PerspectiveDistortion>
    <rdf:Seq>
     <rdf:li>-0.1210</rdf:li>
     <rdf:li>0.0340</rdf:li>
     <rdf:li>0.0000</rdf:li>
    </rdf:Seq>
   </Camera:PerspectiveDistortion>
   <Iptc4xmpCore:CreatorContactInfo rdf:parseType="Resource">
    <Iptc4xmpCore:CiAdrCity>Gladstone</Iptc4xmpCore:CiAdrCity>
   </Iptc4xmpCore:CreatorContactInfo>
   <dc:description></dc:description>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>""".encode()

XMP_BOMB: Final = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>&lol3;</dc:title>
</rdf:Description></rdf:RDF></x:xmpmeta>"""

# --- JPEG ------------------------------------------


def segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg(width: int, height: int, components: int, segments: Sequence[bytes]) -> bytes:
    """A baseline JPEG of flat grey: every block's DC difference 0 and an immediate EOB.

    One quantisation table, one DC and one AC Huffman table of a single one-bit code each, and
    4:4:4 sampling, so the entropy-coded data is two zero bits per block.
    """
    dqt = segment(0xDB, b"\x00" + bytes([1] * 64))
    ids = [(index + 1, 0x11, 0) for index in range(components)]
    sof = segment(
        0xC0,
        struct.pack(">BHHB", 8, height, width, components) + b"".join(bytes(c) for c in ids),
    )
    one_code = bytes([1] + [0] * 15)
    dht = segment(0xC4, b"\x00" + one_code + b"\x00" + b"\x10" + one_code + b"\x00")
    scan = bytes([components]) + b"".join(bytes([index + 1, 0x00]) for index in range(components))
    sos = segment(0xDA, scan + b"\x00\x3f\x00")
    blocks = ((width + 7) // 8) * ((height + 7) // 8) * components
    bits = blocks * 2
    data = bytes(bits // 8) + (bytes([0xFF >> (bits % 8)]) if bits % 8 else b"")
    jfif = segment(0xE0, b"JFIF\x00" + struct.pack(">BBBHHBB", 1, 2, 1, 72, 72, 0, 0))
    return b"\xff\xd8" + jfif + b"".join(segments) + dqt + sof + dht + sos + data + b"\xff\xd9"


def icc_segments(profile: bytes, parts: int) -> list[bytes]:
    step = -(-len(profile) // parts)
    pieces = [profile[i : i + step] for i in range(0, len(profile), step)]
    return [
        segment(0xE2, b"ICC_PROFILE\x00" + bytes([index + 1, len(pieces)]) + piece)
        for index, piece in enumerate(pieces)
    ]


# --- PNG ------------------------------------------


def chunk(kind: bytes, data: bytes, crc: int | None = None) -> bytes:
    value = zlib.crc32(kind + data) if crc is None else crc
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", value)


def png(
    width: int,
    height: int,
    before: Sequence[bytes] = (),
    after: Sequence[bytes] = (),
    idat: bytes | None = None,
    ihdr: bytes | None = None,
) -> bytes:
    """An 8-bit RGB PNG of a horizontal gradient, ``before`` and ``after`` its image data."""
    if idat is None:
        rows = b"".join(
            b"\x00"
            + b"".join(bytes([x * 255 // max(1, width - 1), y * 8 % 256, 96]) for x in range(width))
            for y in range(height)
        )
        idat = zlib.compress(rows, 9)
    header = ihdr if ihdr is not None else struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + b"".join(before)
        + chunk(b"IDAT", idat)
        + b"".join(after)
        + chunk(b"IEND", b"")
    )


# --- WebP ------------------------------------------

# The smallest lossless and lossy 1 x 1 bitstreams, as libwebp writes them.
VP8L_1X1: Final = bytes.fromhex("2f0000001007101111888 8fe0700".replace(" ", ""))
VP8_1X1: Final = bytes.fromhex("3001009d012a0100010002003425a4000370000000")


def riff_chunk(fourcc: bytes, data: bytes) -> bytes:
    return fourcc + struct.pack("<I", len(data)) + data + b"\x00" * (len(data) & 1)


def webp(chunks: Sequence[bytes], declared: int | None = None) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body) if declared is None else declared) + body


# --- The fixtures ------------------------------------------

CRAWLER_IFD0: Final = [
    Tag(271, ASCII, ascii_value("Ridgeback Robotics")),
    Tag(272, ASCII, ascii_value("PipeCrawler C2 camera")),
    Tag(274, SHORT, [6]),
    Tag(282, RATIONAL, [(72, 1)]),
    Tag(283, RATIONAL, [(72, 1)]),
    Tag(296, SHORT, [2]),
    Tag(305, ASCII, ascii_value("crawler-fw 4.2.1")),
    Tag(306, ASCII, ascii_value("2026:09:14 10:25:00")),
]
CRAWLER_EXIF: Final = [
    Tag(33434, RATIONAL, [(1, 250)]),
    Tag(36864, UNDEFINED, b"0232"),
    Tag(36867, ASCII, ascii_value("2026:09:14 10:21:07")),
    Tag(36881, ASCII, ascii_value("+10:00")),
    Tag(37386, RATIONAL, [(435, 100)]),
    Tag(37500, UNDEFINED, bytes(range(256)) + b"RIDGEBACK-MAKERNOTE" * 4),
    Tag(37521, ASCII, ascii_value("042")),
    Tag(41486, RATIONAL, [(2835, 10)]),
    Tag(41487, RATIONAL, [(2835, 10)]),
    Tag(41488, SHORT, [3]),
    Tag(42033, ASCII, ascii_value("RC2-00417")),
    Tag(42036, ASCII, ascii_value("4.35 mm f/2.8")),
]
CRAWLER_GPS: Final = [
    Tag(0, BYTE, [2, 3, 0, 0]),
    Tag(1, ASCII, ascii_value("S")),
    Tag(2, RATIONAL, [(23, 1), (50, 1), (2604, 100)]),
    Tag(3, ASCII, ascii_value("E")),
    Tag(4, RATIONAL, [(151, 1), (15, 1), (3312, 100)]),
    Tag(5, BYTE, [0]),
    Tag(6, RATIONAL, [(1250, 100)]),
    Tag(18, ASCII, ascii_value("WGS-84")),
]
CRAWLER_INTEROP: Final = [Tag(1, ASCII, ascii_value("R98"))]
CRAWLER_IFD1: Final = [Tag(259, SHORT, [6]), Tag(282, RATIONAL, [(72, 1)])]


def crawler_exif(little: bool = True) -> bytes:
    return exif_block(
        CRAWLER_IFD0, CRAWLER_EXIF, CRAWLER_GPS, CRAWLER_INTEROP, CRAWLER_IFD1, little=little
    )


def crawler_jpeg() -> bytes:
    return jpeg(
        64,
        48,
        3,
        [
            segment(0xE1, b"Exif\x00\x00" + crawler_exif()),
            segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP),
            *icc_segments(icc_profile(), 2),
            segment(0xFE, b"pipe 7, joint 12"),
        ],
    )


def amr_png() -> bytes:
    exif = exif_block(
        [
            Tag(271, ASCII, ascii_value("Northbay Automation")),
            Tag(272, ASCII, ascii_value("AMR-400 dock camera")),
        ],
        [
            Tag(36867, ASCII, ascii_value("2026:08:30 06:15:42")),
            Tag(42033, ASCII, ascii_value("NB4-1182")),
        ],
        little=False,
    )
    before = [
        chunk(b"sRGB", b"\x00"),
        chunk(b"gAMA", struct.pack(">I", 45455)),
        chunk(b"cHRM", struct.pack(">8I", 31270, 32900, 64000, 33000, 30000, 60000, 15000, 6000)),
        chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1)),
        chunk(b"iCCP", b"Neptune grey\x00\x00" + zlib.compress(icc_profile(), 9)),
        chunk(b"eXIf", exif),
    ]
    after = [
        chunk(b"tIME", struct.pack(">HBBBBB", 2026, 8, 30, 6, 16, 1)),
        chunk(b"tEXt", b"Software\x00dock-capture 1.4"),
        chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(b"Dock 3, charging contacts visible", 9)),
        chunk(b"iTXt", b"XML:com.adobe.xmp\x00\x00\x00\x00\x00" + XMP),
    ]
    return png(32, 24, before, after)


def strips(width: int, height: int, channels: int, sample: int = 1) -> bytes:
    return bytes((index * 7) % 251 for index in range(width * height * channels * sample))


def rov_tiff() -> bytes:
    page1, page2 = strips(16, 12, 3), strips(8, 6, 1)
    xmp_tag = Tag(700, BYTE, list(XMP))
    icc_tag = Tag(34675, UNDEFINED, icc_profile())
    directories = {
        "ifd0": Directory(
            [
                Tag(256, SHORT, [16]),
                Tag(257, SHORT, [12]),
                Tag(258, SHORT, [8, 8, 8]),
                Tag(259, SHORT, [1]),
                Tag(262, SHORT, [2]),
                Tag(271, ASCII, ascii_value("Abyssal Systems")),
                Tag(272, ASCII, ascii_value("ROV-9 survey camera")),
                Tag(273, LONG, ("blob", "page1")),
                Tag(274, SHORT, [1]),
                Tag(277, SHORT, [3]),
                Tag(278, SHORT, [12]),
                Tag(279, LONG, [len(page1)]),
                xmp_tag,
                Tag(34665, LONG, ("ifd", "exif")),
                icc_tag,
                Tag(34853, LONG, ("ifd", "gps")),
            ],
            "ifd1",
        ),
        "exif": Directory(
            [
                Tag(36867, ASCII, ascii_value("2026:07:02 14:03:55")),
                Tag(36881, ASCII, ascii_value("-03:00")),
            ]
        ),
        "gps": Directory(
            [
                Tag(1, ASCII, ascii_value("S")),
                Tag(2, RATIONAL, [(22, 1), (54, 1), (1200, 100)]),
                Tag(3, ASCII, ascii_value("W")),
                Tag(4, RATIONAL, [(43, 1), (10, 1), (3000, 100)]),
                Tag(5, BYTE, [1]),
                Tag(6, RATIONAL, [(355, 10)]),
            ]
        ),
        "ifd1": Directory(
            [
                Tag(256, SHORT, [8]),
                Tag(257, SHORT, [6]),
                Tag(258, SHORT, [8]),
                Tag(259, SHORT, [1]),
                Tag(262, SHORT, [1]),
                Tag(273, LONG, ("blob", "page2")),
                Tag(277, SHORT, [1]),
                Tag(278, SHORT, [6]),
                Tag(279, LONG, [len(page2)]),
            ]
        ),
    }
    return TiffLayout(directories, "ifd0", blobs={"page1": page1, "page2": page2}).build()


def rover_dng() -> bytes:
    preview, raw = strips(16, 12, 3), strips(32, 24, 1, 2)
    matrix = [(n, 10000) for n in (6722, -635, -963, -4287, 12460, 2028, -908, 2162, 5668)]
    directories = {
        "ifd0": Directory(
            [
                Tag(254, LONG, [1]),
                Tag(256, SHORT, [16]),
                Tag(257, SHORT, [12]),
                Tag(258, SHORT, [8, 8, 8]),
                Tag(259, SHORT, [1]),
                Tag(262, SHORT, [2]),
                Tag(271, ASCII, ascii_value("Fieldline")),
                Tag(272, ASCII, ascii_value("AgriRover R7 mast camera")),
                Tag(273, LONG, ("blob", "preview")),
                Tag(274, SHORT, [1]),
                Tag(277, SHORT, [3]),
                Tag(278, SHORT, [12]),
                Tag(279, LONG, [len(preview)]),
                Tag(330, LONG, ("ifds", ["raw"])),
                Tag(34665, LONG, ("ifd", "exif")),
                Tag(50706, BYTE, [1, 6, 0, 0]),
                Tag(50708, ASCII, ascii_value("Fieldline AgriRover R7")),
                Tag(50721, SRATIONAL, matrix),
                Tag(50735, ASCII, ascii_value("AGR-7731")),
            ]
        ),
        "exif": Directory([Tag(36867, ASCII, ascii_value("2026:05:19 07:44:10"))]),
        "raw": Directory(
            [
                Tag(254, LONG, [0]),
                Tag(256, SHORT, [32]),
                Tag(257, SHORT, [24]),
                Tag(258, SHORT, [16]),
                Tag(259, SHORT, [1]),
                Tag(262, SHORT, [32803]),
                Tag(273, LONG, ("blob", "pixels")),
                Tag(277, SHORT, [1]),
                Tag(278, SHORT, [24]),
                Tag(279, LONG, [len(raw)]),
                Tag(33421, SHORT, [2, 2]),
                Tag(33422, BYTE, [0, 1, 1, 2]),
            ]
        ),
    }
    return TiffLayout(
        directories, "ifd0", little=False, blobs={"preview": preview, "pixels": raw}
    ).build()


def survey_bigtiff() -> bytes:
    tile = strips(8, 8, 1)
    directories = {
        "ifd0": Directory(
            [
                Tag(256, SHORT, [8]),
                Tag(257, SHORT, [8]),
                Tag(258, SHORT, [8]),
                Tag(259, SHORT, [1]),
                Tag(262, SHORT, [1]),
                Tag(271, ASCII, ascii_value("Terrafold")),
                Tag(273, 16, ("blob", "tile")),
                Tag(277, SHORT, [1]),
                Tag(278, SHORT, [8]),
                Tag(279, 16, [len(tile)]),
                Tag(305, ASCII, ascii_value("mosaic-tiler 2.0")),
            ]
        )
    }
    return TiffLayout(directories, "ifd0", big=True, blobs={"tile": tile}).build()


def headcam_webp() -> bytes:
    flags = 0x20 | 0x08 | 0x04  # ICC, EXIF, XMP
    vp8x = struct.pack("<B3x", flags) + (0).to_bytes(3, "little") + (0).to_bytes(3, "little")
    exif = exif_block(
        [
            Tag(271, ASCII, ascii_value("Kinesis Humanoids")),
            Tag(272, ASCII, ascii_value("K2 head camera")),
            Tag(274, SHORT, [1]),
        ],
        [Tag(36867, ASCII, ascii_value("2026:09:01 16:40:00"))],
    )
    return webp(
        [
            riff_chunk(b"VP8X", vp8x),
            riff_chunk(b"ICCP", icc_profile()),
            riff_chunk(b"VP8L", VP8L_1X1),
            riff_chunk(b"EXIF", b"Exif\x00\x00" + exif),
            riff_chunk(b"XMP ", XMP),
        ]
    )


def quadruped_webp() -> bytes:
    return webp([riff_chunk(b"VP8 ", VP8_1X1)])


def bmp_v5(width: int, height: int, profile: bytes) -> bytes:
    row = (24 * width + 31) // 32 * 4
    pixels = bytes((i * 13) % 256 for i in range(row * abs(height)))
    header = struct.pack(
        "<IiiHHIIiiIIIIIII9i3I4I",
        124, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0,
        0, 0, 0, 0, 0x4D424544, *([0] * 9), 0, 0, 0,
        4, 124 + len(pixels), len(profile), 0,
    )  # fmt: skip
    offset = 14 + len(header)
    size = offset + len(pixels) + len(profile)
    return b"BM" + struct.pack("<IHHI", size, 0, 0, offset) + header + pixels + profile


def bmp_info(width: int, height: int) -> bytes:
    row = (24 * width + 31) // 32 * 4
    pixels = bytes((i * 29) % 256 for i in range(row * abs(height)))
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 0, 0, 0, 0)
    offset = 14 + len(header)
    return b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset) + header + pixels


def pgm16() -> bytes:
    header = b"P5\n# wrist depth, millimetres as declared by the driver\n16 12\n65535\n"
    return header + b"".join(struct.pack(">H", 400 + i) for i in range(16 * 12))


def ppm() -> bytes:
    return b"P6\n8 6\n255\n" + bytes((i * 5) % 256 for i in range(8 * 6 * 3))


def pbm_plain() -> bytes:
    return b"P1\n# gripper mask\n8 4\n" + b"".join(b"0 1 1 0 0 1 1 0\n" for _ in range(4))


def pam() -> bytes:
    header = b"P7\nWIDTH 4\nHEIGHT 3\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n"
    return header + bytes(range(4 * 3 * 4))


# --- Damaged and hostile files ------------------------------------------


def exif_loop_jpeg() -> bytes:
    """EXIF whose IFD1 points back at IFD0, whose Exif pointer leaves the block, and whose values
    lie: a count past the block, Orientation 9, a blank capture time, Make not ASCII."""
    block = bytearray(
        exif_block(
            [
                Tag(271, ASCII, b"Ridgeb\xe4ck\x00"),
                Tag(274, SHORT, [9]),
                Tag(305, ASCII, ascii_value("x" * 40)),
            ],
            [Tag(36867, ASCII, ascii_value("    :  :     :  :  "))],
            [Tag(1, ASCII, ascii_value("N"))],
            ifd1=[Tag(282, RATIONAL, [(72, 1)])],
        )
    )
    ifd0 = struct.unpack("<I", block[4:8])[0]
    count = struct.unpack("<H", block[ifd0 : ifd0 + 2])[0]
    # Entry 2 (Software, 305): a count no stream can hold.
    entry = ifd0 + 2 + 2 * 12
    block[entry + 4 : entry + 8] = struct.pack("<I", 0x10000000)
    # IFD1's next-IFD offset: back to IFD0.
    ifd1 = struct.unpack("<I", block[ifd0 + 2 + count * 12 : ifd0 + 6 + count * 12])[0]
    ifd1_count = struct.unpack("<H", block[ifd1 : ifd1 + 2])[0]
    next_at = ifd1 + 2 + ifd1_count * 12
    block[next_at : next_at + 4] = struct.pack("<I", ifd0)
    # The GPS pointer (34853), entry 4 after Make, Orientation, Software, Exif: past the end.
    for index in range(count):
        at = ifd0 + 2 + index * 12
        if struct.unpack("<H", block[at : at + 2])[0] == 34853:
            block[at + 8 : at + 12] = struct.pack("<I", 0xFFFF00)
    return jpeg(16, 16, 1, [segment(0xE1, b"Exif\x00\x00" + bytes(block))])


def bad_gps_jpeg() -> bytes:
    exif = exif_block(
        [Tag(271, ASCII, ascii_value("Ridgeback Robotics"))],
        [
            Tag(36867, ASCII, ascii_value("2026:02:30 25:00:00")),
            Tag(37521, ASCII, ascii_value("12a")),
        ],
        [
            Tag(1, ASCII, ascii_value("Q")),
            Tag(2, RATIONAL, [(23, 0), (50, 1), (26, 1)]),
            Tag(3, ASCII, ascii_value("E")),
            Tag(4, RATIONAL, [(151, 1), (15, 1), (33, 1)]),
        ],
    )
    return jpeg(16, 16, 1, [segment(0xE1, b"Exif\x00\x00" + exif)])


def xmp_bomb_jpeg() -> bytes:
    return jpeg(8, 8, 1, [segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP_BOMB)])


def bomb_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 100_000, 100_000, 8, 6, 0, 0, 0)
    return png(1, 1, idat=zlib.compress(b"\x00" * 4096, 9), ihdr=ihdr)


def zlib_bomb_png() -> bytes:
    bomb = zlib.compress(b"\x00" * (32 * 1024 * 1024), 9)
    return png(4, 4, after=[chunk(b"zTXt", b"Comment\x00\x00" + bomb)])


def bad_crc_png() -> bytes:
    return png(4, 4, after=[chunk(b"tEXt", b"Author\x00Northbay", crc=0x12345678)])


def wrong_first_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + chunk(b"gAMA", struct.pack(">I", 45455)) + chunk(b"IEND", b"")


def truncated_tiff() -> bytes:
    data = bytearray(rov_tiff())
    ifd0 = struct.unpack("<I", data[4:8])[0]
    data[ifd0 : ifd0 + 2] = struct.pack("<H", 30)  # declares more entries than are there
    return bytes(data[: ifd0 + 2 + 3 * 12 + 5])


def subifd_cycle_tiff() -> bytes:
    raw = strips(8, 8, 1)
    directories = {
        "ifd0": Directory(
            [
                Tag(256, SHORT, [8]),
                Tag(257, SHORT, [8]),
                Tag(273, LONG, [10_000_000]),
                Tag(279, LONG, [64]),
                Tag(330, LONG, ("ifds", ["sub", "ifd0"])),
            ]
        ),
        "sub": Directory(
            [
                Tag(256, SHORT, [8]),
                Tag(257, SHORT, [8]),
                Tag(273, LONG, ("blob", "raw")),
                Tag(279, LONG, [len(raw)]),
                Tag(330, LONG, ("ifds", ["ifd0"])),
            ]
        ),
    }
    return TiffLayout(directories, "ifd0", blobs={"raw": raw}).build()


def build() -> dict[str, bytes]:
    """Every fixture, by file name."""
    crawler, amr = crawler_jpeg(), amr_png()
    files: dict[str, Callable[[], bytes] | bytes] = {
        "crawler_inspection.jpg": crawler,
        "crawler_inspection": crawler,
        "amr_dock.png": amr,
        "rov_survey.tif": rov_tiff,
        "rover_raw.dng": rover_dng,
        "survey_tile.tif": survey_bigtiff,
        "humanoid_headcam.webp": headcam_webp,
        "quadruped_lossy.webp": quadruped_webp,
        "floor_map.bmp": lambda: bmp_v5(8, -6, icc_profile()),
        "legacy_cam.bmp": lambda: bmp_info(4, 3),
        "wrist_depth.pgm": pgm16,
        "thermal.ppm": ppm,
        "gripper_mask.pbm": pbm_plain,
        "gripper.pam": pam,
        "truncated.jpg": crawler[: len(crawler) - 20],
        "truncated.png": amr[: amr.index(b"IDAT") + 40],
        "truncated.webp": headcam_webp()[:-30],
        "truncated.bmp": bmp_info(4, 3)[:-10],
        "truncated.tif": truncated_tiff,
        "bad_crc.png": bad_crc_png,
        "bomb.png": bomb_png,
        "zlib_bomb.png": zlib_bomb_png,
        "exif_loop.jpg": exif_loop_jpeg,
        "bad_gps.jpg": bad_gps_jpeg,
        "xmp_bomb.jpg": xmp_bomb_jpeg,
        "wrong_first.png": wrong_first_png,
        "subifd_cycle.tif": subifd_cycle_tiff,
        "empty.png": b"",
        "not_a_bitmap.bmp": b"BM is the bay manager's initials, not a bitmap.\n",
    }
    return {name: made if isinstance(made, bytes) else made() for name, made in files.items()}


VALID: Final = (
    "crawler_inspection.jpg",
    "amr_dock.png",
    "rov_survey.tif",
    "rover_raw.dng",
    "survey_tile.tif",
    "humanoid_headcam.webp",
    "quadruped_lossy.webp",
    "floor_map.bmp",
    "legacy_cam.bmp",
    "wrist_depth.pgm",
    "thermal.ppm",
    "gripper_mask.pbm",
    "gripper.pam",
)

# Pillow reads Netpbm P1-P6 and no P7; the PAM header is checked against the specification by hand.
NOT_IN_PILLOW: Final = ("gripper.pam",)

# --- The oracle ------------------------------------------


def _plain(value: Any) -> Any:
    """A Pillow value as JSON: rationals as [numerator, denominator], bytes as a list."""
    if hasattr(value, "numerator") and hasattr(value, "denominator") and not isinstance(value, int):
        return [int(value.numerator), int(value.denominator)]
    if isinstance(value, bytes):
        return list(value)
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def oracle() -> dict[str, Any]:
    """What Pillow reads from every valid fixture: size, mode, frames, EXIF, GPS, ICC, XMP."""
    from PIL import Image  # type: ignore[import-not-found,unused-ignore]

    found: dict[str, Any] = {}
    for name in VALID:
        if name in NOT_IN_PILLOW:
            continue
        with Image.open(HERE / name) as image:
            frames = []
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                frames.append(list(image.size))
            image.seek(0)
            image.load()  # chunks after the pixels (PNG text, XMP) are read by decoding
            exif = image.getexif()
            xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp") or b""
            entry = {
                "format": image.format,
                "frames": frames,
                "mode": image.mode,
                "ifd0": _plain({k: v for k, v in exif.items() if k not in (34665, 34853)}),
                "exif": _plain(dict(exif.get_ifd(0x8769))),
                "gps": _plain(dict(exif.get_ifd(0x8825))),
                "icc_bytes": len(image.info.get("icc_profile") or b""),
                "xmp_bytes": len(xmp if isinstance(xmp, bytes) else xmp.encode("utf-8")),
                "decoded_size": list(image.size),
            }
        found[name] = entry
    return found


def main(arguments: list[str]) -> None:
    if "--oracle" in arguments:
        text = json.dumps(oracle(), indent=1, sort_keys=True) + "\n"
        (HERE / "oracle.json").write_text(text)
        return
    for name, data in build().items():
        (HERE / name).write_bytes(data)


if __name__ == "__main__":
    main(sys.argv[1:])
