"""Harbor's change log is a real workbook, written with the compiler's own fixture writer.

``tests/fixtures/tabular/make_xlsx_fixtures.py`` is loaded by path (as ``docs.py`` loads the PDF
writer): standard library only, fixed zip times, so the bytes are the same on every host. Cells are
inline strings, so times stay exactly as the site writes them.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[6]


def _writer() -> ModuleType:
    path = ROOT / "tests" / "fixtures" / "tabular" / "make_xlsx_fixtures.py"
    spec = importlib.util.spec_from_file_location("neptune_xlsx_writer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"the XLSX writer is not at {path}; run from a repository checkout")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def workbook(header: list[str], rows: list[list[str]]) -> bytes:
    """One sheet, ``Change Log``: the header on row 1, then one row per entry."""
    w = _writer()
    letters = "ABCDEFGHIJ"
    table = [header, *rows]
    sheet_rows = [
        w.row(number, *(w.i(f"{letters[col]}{number}", text) for col, text in enumerate(cells)))
        for number, cells in enumerate(table, start=1)
    ]
    parts = w.package([("Change Log", w.worksheet(sheet_rows))], styles=False)
    return bytes(w.zipped(parts))
