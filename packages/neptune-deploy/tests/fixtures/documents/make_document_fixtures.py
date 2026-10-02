"""Generate the lifecycle document fixtures: PDFs, and the compiler packages made from them.

Deploy ADR 0003. Run from the repository root::

    uv run python packages/neptune-deploy/tests/fixtures/documents/make_document_fixtures.py

reportlab is not a dependency, so the PDFs are written with the compiler's own small PDF writer
(``tests/fixtures/pdf/make_pdfs.py``, loaded by path: no clock, randomness or third-party library,
so the bytes are the same on every host). This file lays out tagged documents (headings,
paragraphs, lists, tables) over that writer. Then each folder of ``sources/`` is ingested with the
compiler's own command line into ``packages/<folder>``, without ``volatile/`` (wall clock and
host). Deploy's tests read those packages and never run ingestion.

The documents are invented but follow real layouts: an ISO 3691-4 style risk estimate for an AMR
fleet, a cell risk assessment with severity, exposure, avoidance and risk level for an arm, a
commissioning report, an incident report with a timeline, and a maintenance SOP for a legged
robot. ``malformed`` holds a scan with no text layer, a rotated page and a form-revision mismatch.
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]


def _writer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "neptune_pdf_writer", ROOT / "tests" / "fixtures" / "pdf" / "make_pdfs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


W = _writer()

# --- Elements --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Heading:
    text: str
    level: int = 2


@dataclass(frozen=True)
class Para:
    lines: Sequence[str]


@dataclass(frozen=True)
class Item:
    text: str


@dataclass(frozen=True)
class Table:
    rows: Sequence[Sequence[str]]
    widths: Sequence[int]
    header: bool = True


Element = Heading | Para | Item | Table


@dataclass(frozen=True)
class PageSpec:
    elements: Sequence[Element]
    rotate: int = 0
    box: tuple[int, int] = (792, 612)  # landscape, so a seven-column table fits


def _latin1(text: str) -> bytes:
    return text.encode("cp1252")


class _Page:
    """One page's content stream and the structure elements that own its marked content."""

    def __init__(self, pdf: object, spec: PageSpec) -> None:
        self.pdf, self.spec = pdf, spec
        self.stream = b""
        self.mcid = 0
        self.y = float(spec.box[1] - 60)
        self.owners: list[object] = []  # structure element per MCID, for the parent tree
        self.tops: list[object] = []  # top-level structure elements, in order

    def _mark(self, tag: str, body: bytes, owner: object) -> None:
        self.stream += W.marked(tag, self.mcid, body)
        self.owners.append(owner)
        self.mcid += 1


def _elem(
    pdf: object, kind: str, parent: object, kids: list[object], page: object = None
) -> object:
    ref = pdf.reserve()  # type: ignore[attr-defined]
    element: dict[str, object] = {"Type": W.Name("StructElem"), "S": W.Name(kind), "P": parent}
    if page is not None:
        element["Pg"] = page
    element["K"] = kids
    pdf.set(ref, element)  # type: ignore[attr-defined]
    return ref


def tagged_pdf(title: str, pages: Sequence[PageSpec]) -> bytes:
    """A tagged PDF: a Document element of headings, paragraphs, lists and tables."""
    pdf = W.Pdf()
    tree = pdf.reserve()
    fonts = {"F1": pdf.add(W.HELVETICA), "F2": pdf.add(W.HELVETICA_BOLD)}
    root_elem, document = pdf.reserve(), pdf.reserve()
    page_refs = [pdf.reserve() for _ in pages]
    top: list[object] = []
    parent_entries: list[object] = []
    for index, spec in enumerate(pages):
        page = _Page(pdf, spec)
        lst: object = None
        lst_items: list[object] = []
        ref = page_refs[index]
        for element in spec.elements:
            if not isinstance(element, Item) and lst is not None:
                lst = None
            match element:
                case Heading(text, level):
                    size = 18 if level == 1 else 13
                    owner = _elem(pdf, f"H{level}", document, [page.mcid], ref)
                    page._mark(f"H{level}", W.text("F2", size, 54, page.y, _latin1(text)), owner)
                    top.append(owner)
                    page.y -= size + 14
                case Para(lines):
                    owner = _elem(pdf, "P", document, [page.mcid], ref)
                    body = b"".join(
                        W.text("F1", 10, 54, page.y - 13 * n, _latin1(line))
                        for n, line in enumerate(lines)
                    )
                    page._mark("P", body, owner)
                    top.append(owner)
                    page.y -= 13 * len(lines) + 8
                case Item(text):
                    if lst is None:
                        lst = _elem(pdf, "L", document, [], ref)
                        lst_items = []
                        top.append(lst)
                    li = _elem(pdf, "LI", lst, [], ref)
                    body_elem = _elem(pdf, "LBody", li, [page.mcid], ref)
                    pdf.set(li, {**pdf.objects[li.number], "K": [body_elem]})  # type: ignore[attr-defined]
                    lst_items.append(li)
                    pdf.set(lst, {**pdf.objects[lst.number], "K": list(lst_items)})  # type: ignore[attr-defined]
                    page._mark("LBody", W.text("F1", 10, 72, page.y, _latin1(text)), body_elem)
                    page.y -= 16
                case Table(rows, widths, header):
                    table = _elem(pdf, "Table", document, [], ref)
                    row_refs: list[object] = []
                    for r, row in enumerate(rows):
                        tr = _elem(pdf, "TR", table, [], ref)
                        cells: list[object] = []
                        x = 54
                        for c, cell in enumerate(row):
                            head = header and r == 0
                            td = _elem(pdf, "TH" if head else "TD", tr, [], ref)
                            if cell:
                                pdf.set(td, {**pdf.objects[td.number], "K": [page.mcid]})  # type: ignore[attr-defined]
                                font = "F2" if head else "F1"
                                body = W.text(font, 9, x + 3, page.y - 11, _latin1(cell))
                                page._mark("TH" if head else "TD", body, td)
                            cells.append(td)
                            x += widths[c]
                        pdf.set(tr, {**pdf.objects[tr.number], "K": cells})  # type: ignore[attr-defined]
                        row_refs.append(tr)
                        page.y -= 16
                    pdf.set(table, {**pdf.objects[table.number], "K": row_refs})  # type: ignore[attr-defined]
                    top.append(table)
                    page.y -= 8
        content = pdf.add(W.Stream({}, page.stream))
        extra: dict[str, object] = {"Rotate": spec.rotate} if spec.rotate else {}
        pdf.set(
            ref,
            {
                "Type": W.Name("Page"),
                "Parent": tree,
                "MediaBox": [0, 0, *spec.box],
                "Resources": {"Font": fonts},
                "Contents": content,
                "StructParents": index,
                **extra,
            },
        )
        parent_entries += [index, list(page.owners)]
    pdf.set(
        document, {"Type": W.Name("StructElem"), "S": W.Name("Document"), "P": root_elem, "K": top}
    )
    parent_tree = pdf.add({"Nums": parent_entries})
    pdf.set(
        root_elem,
        {
            "Type": W.Name("StructTreeRoot"),
            "K": [document],
            "ParentTree": parent_tree,
            "ParentTreeNextKey": len(pages),
        },
    )
    W.page_tree(pdf, page_refs, tree)
    root = W.catalog(
        pdf, tree, StructTreeRoot=root_elem, MarkInfo={"Marked": True}, Lang=W.Lit(b"en-GB")
    )
    info = pdf.add({"Title": W.Lit(title.encode("cp1252")), "Producer": W.Lit(b"neptune fixtures")})
    return bytes(pdf.build(root, info))


def scanned_pdf() -> bytes:
    """A page that is only an image: no text layer, no structure."""
    pdf = W.Pdf()
    tree = pdf.reserve()
    image = W.gray_image(pdf)
    content = pdf.add(W.Stream({}, b"q 400 0 0 500 100 150 cm /Im1 Do Q\n"))
    page = pdf.add(
        {
            "Type": W.Name("Page"),
            "Parent": tree,
            "MediaBox": [0, 0, 612, 792],
            "Resources": {"XObject": {"Im1": image}},
            "Contents": content,
        }
    )
    W.page_tree(pdf, [page], tree)
    root = W.catalog(pdf, tree)
    info = pdf.add({"Title": W.Lit(b"Scan 0042")})
    return bytes(pdf.build(root, info))


# --- The documents ---------------------------------------------------------------------------

AMR_HAZARDS = [
    ("Hazard", "Severity", "Exposure", "Avoidance", "Risk reduction", "Mitigation"),
    (
        "Collision with a pedestrian in a shared aisle",
        "S3",
        "E2",
        "A2",
        "PLr d",
        "Personnel-detecting scanner; speed limited to 1.2 m/s in aisle zones",
    ),
    (
        "Fork contact with a rack during pallet pick",
        "S2",
        "E2",
        "A1",
        "PLr c",
        "Fork height interlock; rack position check before lift",
    ),
    (
        "Battery thermal event while charging",
        "S3",
        "E1",
        "A2",
        "PLr c",
        "Temperature cut-off; charger bay smoke detection",
    ),
]
AMR_WIDTHS = (190, 56, 56, 62, 80, 294)


def risk_amr(revision: str = "2", rotate: int = 0) -> bytes:
    first = PageSpec(
        [
            Heading("Risk assessment: AMR fleet, Hamburg DC 2", 1),
            Para(["Form: RA-3691-AMR"]),
            Para([f"Revision: {revision}"]),
            Para(["Assessment No: RA-AMR-0417"]),
            Para(["Site: HH-DC2"]),
            Para(["Machines: AMR-07; AMR-08"]),
            Para(["Method: ISO 3691-4 risk estimation (severity, exposure, avoidance)"]),
            Para(["Assessed on: 2026-03-12"]),
            Para(["Reviewer: R. Okafor"]),
        ]
    )
    second = PageSpec(
        [
            Heading("Hazard analysis"),
            Table(AMR_HAZARDS, AMR_WIDTHS),
            Heading("Approval"),
            Para(["Approval decision: Accepted with conditions"]),
            Para(["Approved by: Site safety lead"]),
            Para(["Approved on: 2026-03-14"]),
        ],
        rotate=rotate,
    )
    return tagged_pdf("Risk assessment RA-AMR-0417", [first, second])


def risk_cell() -> bytes:
    fields = [
        ("Document no", "CELL3-RA-009"),
        ("Revision", "B"),
        ("Cell", "Cell 3, Line 2"),
        ("Robots", "R1 6-axis; R2 6-axis"),
        ("Assessment date", "12/03/2026"),
        ("Method", "ISO 10218-2 risk estimation, severity x exposure x avoidance"),
    ]
    hazards = [
        (
            "Task",
            "Hazard",
            "Severity of injury",
            "Exposure",
            "Avoidance",
            "Risk level",
            "Risk reduction measures",
        ),
        (
            "Pallet change",
            "Crushing between the arm and the pallet stack",
            "Serious",
            "Frequent",
            "Possible",
            "High",
            "Light curtain on the pallet gate; safety-rated monitored stop",
        ),
        (
            "Tool change",
            "Gripper release with a part held",
            "Serious",
            "Seldom",
            "Possible",
            "Medium",
            "Part-present check; two-hand enabling for manual tool change",
        ),
        (
            "Teaching",
            "Unexpected motion in manual mode",
            "Serious",
            "Seldom",
            "Likely",
            "Medium",
            "Reduced speed 250 mm/s; enabling switch",
        ),
    ]
    approval = [
        ("Approved by", "Plant safety manager"),
        ("Decision", "Approved"),
        ("Approval date", "14/03/2026"),
    ]
    first = PageSpec(
        [
            Heading("Robot cell risk assessment", 1),
            Table(fields, (150, 540), header=False),
            Heading("Hazards"),
            Table(hazards, (70, 150, 80, 60, 60, 60, 262)),
            Heading("Approval"),
            Table(approval, (150, 540), header=False),
        ]
    )
    return tagged_pdf("Cell 3 risk assessment", [first])


def commissioning() -> bytes:
    first = PageSpec(
        [
            Heading("Commissioning report: Cell 3 palletising", 1),
            Para(["Report no: CR-C3-2026-02"]),
            Para(["Cell: plant2.cell3"]),
            Para(["Robot: R1; R2"]),
            Para(["Configuration baseline: cfg-c3-1.4"]),
            Para(["Commissioned on: 2026-02-20 16:30"]),
            Para(["Calibration records: CAL-C3-001; CAL-C3-002"]),
            Heading("Hardware"),
            Table(
                [
                    ("Item", "Model", "Serial", "Firmware"),
                    ("Manipulator R1", "IRB-6700", "SN-6700-118", "7.8.1"),
                    ("Controller R1", "OmniCore C30", "SN-C30-552", "7.8.1"),
                    ("Gripper", "GX-2", "SN-GX2-0931", "2.3"),
                ],
                (160, 150, 150, 230),
            ),
            Heading("Software"),
            Table(
                [
                    ("Component", "Version"),
                    ("Palletising application", "1.4.0"),
                    ("Safety configuration", "2026.02-a"),
                ],
                (300, 390),
            ),
        ]
    )
    second = PageSpec(
        [
            Heading("Acceptance tests"),
            Table(
                [
                    ("Test", "Result", "Performed"),
                    ("Safety-rated monitored stop", "PASS", "2026-02-20 11:05"),
                    ("Light curtain response", "PASS 142 ms", "2026-02-20 11:40"),
                    ("Palletising cycle, 50 pallets", "PASS", "2026-02-20 14:15"),
                ],
                (300, 190, 200),
            ),
            Heading("Constraints"),
            Item("Payload not above 35 kg"),
            Item("Pallet gate closed during automatic mode"),
            Heading("Sign-off"),
            Para(["Sign-off: Accepted"]),
            Para(["Signed by: Commissioning engineer"]),
            Para(["Signed on: 2026-02-21 09:10"]),
        ]
    )
    return tagged_pdf("Commissioning report CR-C3-2026-02", [first, second])


def incident() -> bytes:
    first = PageSpec(
        [
            Heading("Incident report INC-HH-0092", 1),
            Para(["Incident no: INC-HH-0092"]),
            Para(["Site: HH-DC2"]),
            Para(["Zone: Z3"]),
            Para(["Location: Aisle 14, rack face B"]),
            Para(["Machine: AMR-07"]),
            Para(["Assets: PAL-5521; RACK-14B"]),
            Para(["Occurred at: 2026-04-02 14:07"]),
            Para(["Severity: Minor, no injury"]),
            Para(["Related records: RA-AMR-0417"]),
            Heading("Timeline"),
            Table(
                [
                    ("Time", "Event"),
                    ("2026-04-02 14:05", "AMR-07 starts pallet pick in aisle 14"),
                    ("2026-04-02 14:07", "Fork contacts rack upright; protective stop"),
                    ("2026-04-02 14:09", "Fleet manager flags AMR-07 as blocked"),
                    ("2026-04-02 14:31", "Technician clears the aisle and resets"),
                ],
                (150, 540),
            ),
        ]
    )
    second = PageSpec(
        [
            Heading("Description"),
            Para(
                [
                    "While lifting pallet PAL-5521 the left fork touched the rack upright and the",
                    "mast stopped. The pallet stayed on the forks. No person was in the aisle.",
                ]
            ),
            Heading("Root cause"),
            Para(
                [
                    "The rack face was 40 mm further out than the site map. The pick position",
                    "had not been refreshed after the rack was re-bolted on 2026-03-28.",
                ]
            ),
        ]
    )
    return tagged_pdf("Incident report INC-HH-0092", [first, second])


def sop() -> bytes:
    first = PageSpec(
        [
            Heading("SOP-MP-031 Foot pad replacement", 1),
            Para(["Procedure: SOP-MP-031"]),
            Para(["Revision: C"]),
            Heading("Work record"),
            Para(["Work order: WO-QD-5521"]),
            Para(["Machine: QD-03"]),
            Para(["Performed on: 2026-05-08"]),
            Para(["Diagnosis: Foot pad wear beyond limit on leg FL"]),
            Heading("Procedure"),
            Item("Power the robot down and place it on the service stand"),
            Item("Remove the two retaining screws from the foot pad on leg FL"),
            Item("Fit the new foot pad and torque the screws to 2.5 N.m"),
            Item("Power up and run the stance self-test"),
            Heading("Parts replaced"),
            Table(
                [
                    ("Part", "Removed", "Installed"),
                    ("Foot pad FL", "FP-0183", "FP-0291"),
                    ("Retaining screw set", "SCR-KIT-77", "SCR-KIT-81"),
                ],
                (260, 215, 215),
            ),
            Para(["Performed by: M. Alvarez"]),
        ]
    )
    return tagged_pdf("SOP-MP-031", [first])


# One folder per embodiment; each becomes one compiler package.
SOURCES: dict[str, dict[str, bytes]] = {
    "warehouse_amr": {
        "risk_amr_iso3691_4.pdf": risk_amr(),
        "incident_amr_collision.pdf": incident(),
    },
    "manipulator_cell": {
        "risk_cell_arm.pdf": risk_cell(),
        "commissioning_cell3.pdf": commissioning(),
    },
    "inspection_quadruped": {"sop_foot_pad.pdf": sop()},
    "malformed": {
        "risk_amr_rotated.pdf": risk_amr(rotate=90),
        "risk_amr_revision3.pdf": risk_amr(revision="3"),
        "scan_0042.pdf": scanned_pdf(),
    },
}


def main() -> int:
    command = Path(sys.executable).parent / "neptune"
    for folder, files in SOURCES.items():
        source = HERE / "sources" / folder
        shutil.rmtree(source, ignore_errors=True)
        source.mkdir(parents=True)
        for name, data in files.items():
            (source / name).write_bytes(data)
        out = HERE / "packages" / folder
        shutil.rmtree(out, ignore_errors=True)
        out.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as workspace:
            subprocess.run(
                [str(command), "ingest", str(source), "--out", str(out), "-w", workspace],
                check=True,
            )
        shutil.rmtree(out / "volatile", ignore_errors=True)
        sys.stdout.write(f"wrote {out.relative_to(HERE)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
