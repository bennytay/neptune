"""What a reference to another file says about where it points, judged by its text alone.

A geometry file names what it needs: a material library, a texture, a buffer, a sublayer. The
adapter sees one source and no directory, so it never opens, resolves or follows a reference
(ADR 0052 §4). It classifies the text, and a finding names every reference that cannot stay inside
the source root; whether the rest are present, and not reached through a symlink, is a check across
sources.
"""

import re
from typing import Final
from urllib.parse import unquote

RELATIVE: Final = "relative"  # stays at or below the directory of the file that names it
PARENT: Final = "parent"  # climbs out of that directory: inside the root only if the file sits deep
ABSOLUTE: Final = "absolute"  # a root-anchored path, a drive path or a file: URI: outside any root
URI: Final = "uri"  # another scheme (http, https, ftp, s3, package): not a file of the source
EMBEDDED: Final = "embedded"  # a data: URI, whose bytes are in the referring file
UNSAFE: Final = "unsafe"  # control characters or an empty name: never a usable path

_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_DRIVE: Final = re.compile(r"[A-Za-z]:[\\/]")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


def scope_of(target: str, *, percent_encoded: bool = False) -> str:
    """The scope of ``target``. glTF writes a URI, so its percent-encoding is undone first."""
    try:
        target.encode("utf-8")  # a JSON string may hold a lone surrogate
    except UnicodeEncodeError:
        return UNSAFE
    text = unquote(target) if percent_encoded else target
    if not text.strip() or _CONTROL.search(text):
        return UNSAFE
    if _DRIVE.match(text):
        return ABSOLUTE
    scheme = _SCHEME.match(text)
    if scheme is not None:
        name = scheme.group(0)[:-1].lower()
        if name == "data":
            return EMBEDDED
        return ABSOLUTE if name == "file" else URI
    if text[0] in "/\\":
        return ABSOLUTE
    depth = 0
    for part in text.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return PARENT
        else:
            depth += 1
    return RELATIVE
