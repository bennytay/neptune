# ruff: noqa: RUF001, E501
"""The controlled documents and the shift photo of Plant 7 (spec §9).

The PDFs are tagged (one heading, then paragraphs) and written with the compiler's own small
PDF writer, ``tests/fixtures/pdf/make_pdfs.py``, loaded by path: no clock, randomness or
third-party library, so the bytes are the same on every host. The whiteboard photo is a
grey-scale PNG drawn pixel by pixel; its extraction is a recorded model output, never a call.
"""

import hashlib
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[6]


def _writer() -> ModuleType:
    path = ROOT / "tests" / "fixtures" / "pdf" / "make_pdfs.py"
    spec = importlib.util.spec_from_file_location("neptune_pdf_writer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"the PDF writer is not at {path}; run from a repository checkout")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def tagged_pdf(title: str, heading: str, paragraphs: list[str]) -> bytes:
    """One page: an H1, then one P per paragraph (each a block for the compiler)."""
    w = _writer()
    pdf = w.Pdf()
    tree = pdf.reserve()
    fonts = {"F1": pdf.add(w.HELVETICA), "F2": pdf.add(w.HELVETICA_BOLD)}
    root_elem, document, page_ref = pdf.reserve(), pdf.reserve(), pdf.reserve()

    def element(kind: str, mcid: int) -> object:
        ref = pdf.reserve()
        pdf.set(
            ref,
            {
                "Type": w.Name("StructElem"),
                "S": w.Name(kind),
                "P": document,
                "Pg": page_ref,
                "K": [mcid],
            },
        )
        return ref

    y = 732.0
    stream = w.marked("H1", 0, w.text("F2", 18, 54, y, heading.encode("cp1252")))
    owners = [element("H1", 0)]
    y -= 36
    for mcid, paragraph in enumerate(paragraphs, start=1):
        body = w.text("F1", 11, 54, y, paragraph.encode("cp1252"))
        stream += w.marked("P", mcid, body)
        owners.append(element("P", mcid))
        y -= 26
    content = pdf.add(w.Stream({}, stream))
    pdf.set(
        page_ref,
        {
            "Type": w.Name("Page"),
            "Parent": tree,
            "MediaBox": [0, 0, 612, 792],
            "Resources": {"Font": fonts},
            "Contents": content,
            "StructParents": 0,
        },
    )
    pdf.set(
        document,
        {"Type": w.Name("StructElem"), "S": w.Name("Document"), "P": root_elem, "K": owners},
    )
    parent_tree = pdf.add({"Nums": [0, list(owners)]})
    pdf.set(
        root_elem,
        {
            "Type": w.Name("StructTreeRoot"),
            "K": [document],
            "ParentTree": parent_tree,
            "ParentTreeNextKey": 1,
        },
    )
    w.page_tree(pdf, [page_ref], tree)
    root = w.catalog(
        pdf, tree, StructTreeRoot=root_elem, MarkInfo={"Marked": True}, Lang=w.Lit(b"en-GB")
    )
    info = pdf.add(
        {"Title": w.Lit(title.encode("cp1252")), "Producer": w.Lit(b"neptune site corpus")}
    )
    return bytes(pdf.build(root, info))


def sop_cv04() -> bytes:
    return tagged_pdf(
        "SOP-CV-04 rev B",
        "SOP-CV-04 Conveyor CV-2 belt service, revision B",
        [
            "Belt tension shall be 45–55 N measured at the mid-span test point.",
            "Clearing a jam at a photo-eye such as PE3 requires the conveyor-lockout qualification.",
            "Apply lockout before any hand enters the belt path and verify zero energy.",
        ],
    )


def sop_c3_02() -> bytes:
    return tagged_pdf(
        "SOP-C3-02",
        "SOP-C3-02 Arm C3 restart",
        [
            "After a starve or a protective stop the arm restarts only with the arm-reset-c3 qualification.",
            "Check the pick position is clear, then acknowledge the reset at the pendant.",
        ],
    )


def psr_12() -> bytes:
    return tagged_pdf(
        "PSR-12 excerpt",
        "PSR-12 Plant safety regulation (excerpt)",
        [
            "4.2 Jams on powered conveyors are cleared only under lockout by a person qualified "
            "for conveyor lockout.",
            "Qualification code: conveyor-lockout.",
        ],
    )


# --- The whiteboard photo and its recorded extraction ----------------------------------------

WIDTH, HEIGHT = 640, 360
REGION = (120, 40, 380, 90)
PROMPT = "List each handwritten note on the whiteboard photo with its bounding box."
MODEL = "claude-sonnet-4-5"
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode()).hexdigest()


def whiteboard_png() -> bytes:
    """A white board with a dark frame and one marker scrawl inside the extracted region."""
    rows = []
    x0, y0, x1, y1 = REGION
    for y in range(HEIGHT):
        row = bytearray([235] * WIDTH)
        if y < 6 or y >= HEIGHT - 6:
            row[:] = bytes([60] * WIDTH)
        row[:6] = bytes([60] * 6)
        row[-6:] = bytes([60] * 6)
        if y0 <= y < y1 and (y - y0) % 12 < 3:
            for x in range(x0, x1):
                if (x * 7 + y) % 11 < 8:
                    row[x] = 25
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def whiteboard_extraction() -> str:
    x0, y0, x1, y1 = REGION
    record = {
        "image": "whiteboard_2026-10-20.png",
        "model": MODEL,
        "prompt": PROMPT,
        "prompt_sha256": PROMPT_SHA256,
        "regions": [
            {"id": 1, "box": [x0, y0, x1, y1], "text": "CV-2: PE3 dirty — clean every shift"}
        ],
    }
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def files() -> dict[str, bytes]:
    return {
        "static/docs/SOP-CV-04_rev-B.pdf": sop_cv04(),
        "static/docs/SOP-C3-02.pdf": sop_c3_02(),
        "static/docs/PSR-12_excerpt.pdf": psr_12(),
        "static/photos/whiteboard_2026-10-20.png": whiteboard_png(),
        "static/photos/whiteboard_2026-10-20.extraction.json": whiteboard_extraction().encode(),
    }
