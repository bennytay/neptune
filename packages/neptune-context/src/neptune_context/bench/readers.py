"""Plain text of a raw file, as any careful reader would get it, and the baselines' Python sandbox.

PDFs come out page by page (pypdf), workbooks sheet by sheet as CSV, everything else as UTF-8.
The sandbox is a separate interpreter with pandas, openpyxl and pypdf, run by ``uv`` outside the
workspace, so the baselines get the usual data tools without adding them to Neptune.
"""

from __future__ import annotations

import subprocess
from functools import cache
from typing import TYPE_CHECKING

from pypdf import PdfReader

if TYPE_CHECKING:
    from pathlib import Path

SANDBOX: tuple[str, ...] = ("uv", "run", "--no-project", "-q", "--with", "pandas")
SANDBOX += ("--with", "openpyxl", "--with", "pypdf", "python")
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

_XLSX = """
import csv, sys, openpyxl
wb = openpyxl.load_workbook(sys.argv[1], read_only=True, data_only=True)
for ws in wb.worksheets:
    print(f"--- sheet {ws.title} ---")
    out = csv.writer(sys.stdout, lineterminator="\\n")
    for row in ws.iter_rows(values_only=True):
        out.writerow(["" if v is None else v for v in row])
"""


def python(code: str, cwd: Path, timeout: float = 120.0, args: tuple[str, ...] = ()) -> str:
    """Run ``code`` in the sandbox interpreter in ``cwd``: stdout, then stderr, then exit code."""
    try:
        done = subprocess.run(
            [*SANDBOX, "-c", code, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:.0f} s"
    out = done.stdout
    if done.stderr.strip():
        out += "\n[stderr]\n" + done.stderr
    if done.returncode:
        out += f"\n[exit {done.returncode}]"
    return out


@cache
def text_of(path: Path) -> str:
    """The file's text; empty for images."""
    suffix = path.suffix.lower()
    if suffix in IMAGES:
        return ""
    if suffix == ".pdf":
        pages = PdfReader(path).pages
        return "\n".join(f"--- page {n} ---\n{p.extract_text()}" for n, p in enumerate(pages, 1))
    if suffix == ".xlsx":
        return python(_XLSX, path.parent, args=(str(path),))
    return path.read_text(encoding="utf-8", errors="replace")
