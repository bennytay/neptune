"""Build the site: assemble the sources, run Sphinx in strict mode, check nothing loads remotely.

``build(root, out)`` returns the problems found (empty means a good site in ``out/html``). Strict
means Sphinx's warnings are errors: a broken cross-reference, a page in no table of contents, a
docstring that does not parse or an unknown heading anchor fails the build. Every build starts
from an empty ``out`` with a fresh environment, so a warning cannot hide in a cached page.
"""

from __future__ import annotations

import io
import re
import shutil
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from docsite.assemble import assemble, write

CONF_DIR = Path(__file__).resolve().parent

# A resource the browser would fetch on load from another origin: scripts, styles, fonts, frames,
# media. A plain <a href="https://..."> is navigation, not a load, and is allowed.
_REMOTE_TAG = re.compile(
    r"<(?:script|link|img|iframe|source|video|audio|embed|object)\b[^>]*?"
    r"\b(?:src|href|data)\s*=\s*[\"']?(?:https?:)?//",
    re.IGNORECASE,
)
_REMOTE_CSS = re.compile(r"(?:url\(\s*[\"']?|@import\s+[\"']?)(?:https?:)?//", re.IGNORECASE)


def remote_loads(html_dir: Path) -> list[str]:
    """Files under ``html_dir`` that would load something from the network when opened."""
    problems = []
    for path in sorted(html_dir.rglob("*")):
        if path.suffix not in {".html", ".css", ".js"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        pattern = _REMOTE_TAG if path.suffix == ".html" else _REMOTE_CSS
        if path.suffix == ".js":
            pattern = re.compile(r"(?:fetch|importScripts)\(\s*[\"'](?:https?:)?//")
        if match := pattern.search(text):
            where = path.relative_to(html_dir).as_posix()
            problems.append(f"{where}: loads a remote resource: {match.group(0)[:120]!r}")
    return problems


def sphinx(source: Path, html: Path, doctrees: Path) -> tuple[int, str]:
    """Run Sphinx's HTML builder with warnings as errors; returns its exit code and its output."""
    from sphinx.cmd.build import build_main

    args = ["-b", "html", "-W", "--keep-going", "-E", "-q", "-n", "-j", "auto"]
    args += ["-c", str(CONF_DIR), "-d", str(doctrees), str(source), str(html)]
    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        code = build_main(args)
    return code, captured.getvalue()


def build(root: Path, out: Path) -> list[str]:
    """Build the site for the repository at ``root`` into ``out`` (``src``, ``html``)."""
    if out.exists():
        shutil.rmtree(out)
    tree = assemble(root)
    if tree.problems:
        return tree.problems
    write(tree, out / "src")
    code, output = sphinx(out / "src", out / "html", out / "doctrees")
    shutil.rmtree(out / "doctrees", ignore_errors=True)
    if code != 0:
        lines = [line for line in output.splitlines() if line.strip()]
        return lines or [f"sphinx-build exited with {code}"]
    return remote_loads(out / "html")
