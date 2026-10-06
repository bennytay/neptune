"""Sphinx configuration for the product documentation site (platform ADR 0012).

``python -m docsite`` passes this directory as Sphinx's configuration directory and an assembled
source tree as its source. Nothing here reads the clock or the network.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sphinx.application import Sphinx

project = "Neptune"
author = "Benjamin Tay"
copyright = "Benjamin Tay"
root_doc = "index"
language = "en"

extensions = ["myst_parser", "sphinx.ext.autodoc"]
source_suffix = {".md": "markdown"}

# MyST: GitHub-style heading anchors, so `page.md#a-heading` resolves, and fails when it does not.
myst_heading_anchors = 6
myst_enable_extensions = ["colon_fence"]

# The API reference: members in source order, annotations in the signature.
autodoc_member_order = "bysource"
autodoc_typehints = "signature"
autodoc_preserve_defaults = True
# Nitpicky mode is on (`-n`): every document cross-reference must resolve. Python annotations name
# types from other packages and the standard library that the site does not document, so Python
# references are exempt, and so is a short type name that two documented modules both define.
nitpick_ignore_regex = [(r"py:.*", r".*")]
suppress_warnings = ["ref.python"]

html_theme = "furo"
html_title = "Neptune"
html_last_updated_fmt: str | None = None
html_copy_source = False
html_show_sourcelink = False
html_show_sphinx = False
# Furo uses the system font stack: no web fonts, analytics or CDN.
html_theme_options: dict[str, Any] = {"top_of_page_buttons": []}

# ``x``s: an inline literal followed straight by a word character is valid Markdown-ish prose but
# not reStructuredText; an escaped space ends the literal without changing the text.
# Literals are paired left to right, so the text between two of them is never taken for one.
_LITERAL = re.compile(r"(``[^`\n]+?``)(\w?)")


def _end_literal(match: re.Match[str]) -> str:
    literal, glued = match.groups()
    return f"{literal}\\ {glued}" if glued else literal


def _docstring(
    app: Sphinx, what: str, name: str, obj: object, options: Any, lines: list[str]
) -> None:
    lines[:] = [_LITERAL.sub(_end_literal, line) for line in lines]


def setup(app: Sphinx) -> dict[str, Any]:
    app.connect("autodoc-process-docstring", _docstring)
    return {"parallel_read_safe": True}
