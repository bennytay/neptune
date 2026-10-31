"""Do an answer's citations check out? Each ``[Sn] <file> <where> "<quote>"`` line is checked
against the site's files without a model: the file exists, the quote is in it and, when a line
is named, within a line of it. Photos cannot be checked and are counted apart."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from neptune_context.bench import readers

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY = re.compile(r"^\s*\[S\d+\]\s+", re.MULTILINE)
_LINE = re.compile(r"\bline\s+(\d+)")
_COLUMN = re.compile(r'(.*?\bcol\s+"[^"]*")\s+"')


def _norm(text: str) -> str:
    return " ".join(text.replace('\\"', '"').split())


def sources(answer: str) -> list[tuple[str, str, str]]:
    """``(file, where, quote)`` per source line, from the last ``Sources:`` section on."""
    tail = answer[answer.rfind("Sources") :] if "Sources" in answer else answer
    starts = [m.end() for m in _ENTRY.finditer(tail)]
    ends = [*[m.start() for m in _ENTRY.finditer(tail)][1:], len(tail)]
    out = []
    for start, end in zip(starts, ends, strict=True):
        entry = tail[start:end].strip()
        file, _, rest = entry.partition(" ")
        column = _COLUMN.match(rest)
        if column:  # row N col "Name" "value": the column name is quoted too
            where, rest = column.group(1), rest[column.end(1) :]
        else:
            where = ""
        first, last = rest.find('"'), rest.rfind('"')
        quote = rest[first + 1 : last] if 0 <= first < last else ""
        if not column:
            where = rest[:first].strip() if first >= 0 else rest.strip()
        out.append((file.strip("`"), where, quote))
    return out


def check(answer: str, site: Path) -> dict[str, Any]:
    details = []
    for file, where, quote in sources(answer):
        path = site / file
        if path.suffix.lower() in readers.IMAGES and path.is_file():
            status = "unverifiable"
        elif not path.is_file() or not path.resolve().is_relative_to(site.resolve()):
            status = "no such file"
        elif not _norm(quote):
            status = "no quote"
        else:
            text = readers.text_of(path)
            line = _LINE.search(where)
            if path.suffix.lower() not in (".pdf", ".xlsx") and line:
                n, lines = int(line.group(1)), text.splitlines()
                text = "\n".join(lines[max(n - 2, 0) : n + 1])
            status = "verified" if _norm(quote) in _norm(text) else "quote not found"
        details.append({"file": file, "where": where, "quote": quote[:300], "status": status})
    return {
        "listed": len(details),
        "verified": sum(d["status"] == "verified" for d in details),
        "unverifiable": sum(d["status"] == "unverifiable" for d in details),
        "details": details,
    }
