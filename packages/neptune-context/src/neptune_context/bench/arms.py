"""The three arms: what Claude is given besides the question. Only the memory differs.

Every arm shares ``FRAME`` (the same rules for unknown, stale and pending, and the same
``Sources:`` format) and the runner's model, effort and tool-call budget.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from neptune_context.bench import readers, sites
from neptune_context.mcp.site import PREFIX, SITE_INSTRUCTIONS, SiteTools

if TYPE_CHECKING:
    from pathlib import Path

FRAME = """You answer questions about one industrial robot site from its evidence: exports, \
documents, photos and recorded robot and machine feeds. Times are the site clock's; never \
convert them.

Rules:
- Answer only from the evidence. Name the things you mean (robots, machines, people, tasks).
- If the evidence does not establish a value, say it is unknown; never fill it in.
- If the latest report of a value is old for a live feed, say it is stale and when it was last \
reported; do not give it as the current value.
- If two names may be one thing but no source confirms it, say it is likely but unconfirmed.

End with a "Sources:" section: one line per source your answer rests on, exactly in the form
[S1] <file path relative to the site folder> <where: line N, row N col "Name", page N, or a JSON \
path> "<text quoted exactly from that place>"
"""

Content = str | list[dict[str, Any]]
CACHE = {"type": "ephemeral", "ttl": "1h"}
MAX_CHARS = 40_000


class Arm(Protocol):
    name: str
    notes: dict[str, Any]

    def system(self) -> list[dict[str, Any]]: ...
    def tools(self) -> list[dict[str, Any]]: ...
    def execute(self, tool: str, arguments: dict[str, Any]) -> Content: ...
    def prompt(self, question: str) -> Content: ...


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters cut; narrow the request]"


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# --- files: the folder and the usual tools ---------------------------------------------------


@dataclass
class Files:
    """Claude with the raw folder: list, read, grep, a Python sandbox and image viewing."""

    raw: Path
    name: str = "files"
    notes: dict[str, Any] = field(default_factory=dict)

    def system(self) -> list[dict[str, Any]]:
        text = FRAME + (
            "\nThe site's files are in a folder you reach with the tools. The python tool runs "
            "with that folder as its working directory and has pandas, openpyxl and pypdf."
        )
        return [{"type": "text", "text": text, "cache_control": CACHE}]

    def tools(self) -> list[dict[str, Any]]:
        path = {"type": "string", "description": "file path relative to the site folder"}
        return [
            {
                "name": "list_files",
                "description": "Every file in the site folder with its size in bytes.",
                "input_schema": _schema({}, []),
            },
            {
                "name": "read_file",
                "description": "Numbered lines of a file. PDFs read page by page and workbooks "
                "sheet by sheet as CSV. At most 500 lines per call.",
                "input_schema": _schema(
                    {
                        "path": path,
                        "start_line": {"type": "integer", "minimum": 1},
                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    ["path"],
                ),
            },
            {
                "name": "grep",
                "description": "Lines matching a Python regular expression, as path:line: text, "
                "in one file or every file under a folder prefix (default: all).",
                "input_schema": _schema(
                    {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                        "ignore_case": {"type": "boolean"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    ["pattern"],
                ),
            },
            {
                "name": "python",
                "description": "Run Python 3 code in the site folder; returns stdout and stderr.",
                "input_schema": _schema({"code": {"type": "string"}}, ["code"]),
            },
            {
                "name": "view_image",
                "description": "Look at an image file (PNG, JPEG).",
                "input_schema": _schema({"path": path}, ["path"]),
            },
        ]

    def prompt(self, question: str) -> Content:
        return question

    def _path(self, rel: str) -> Path:
        path = (self.raw / rel).resolve()
        if not path.is_relative_to(self.raw.resolve()) or not path.is_file():
            raise FileNotFoundError(f"no file {rel!r} in the site folder")
        return path

    def execute(self, tool: str, arguments: dict[str, Any]) -> Content:
        if tool == "list_files":
            return "\n".join(f"{p}  {(self.raw / p).stat().st_size}" for p in sites.files(self.raw))
        if tool == "read_file":
            lines = readers.text_of(self._path(arguments["path"])).splitlines()
            start = int(arguments.get("start_line", 1))
            count = int(arguments.get("max_lines", 200))
            shown = lines[start - 1 : start - 1 + count]
            body = "\n".join(f"{start + i}: {line}" for i, line in enumerate(shown))
            return _clip(f"{body}\n[lines {start}-{start + len(shown) - 1} of {len(lines)}]")
        if tool == "grep":
            return self._grep(arguments)
        if tool == "python":
            return _clip(readers.python(arguments["code"], self.raw))
        if tool == "view_image":
            path = self._path(arguments["path"])
            media = readers.IMAGES.get(path.suffix.lower())
            if media is None:
                return f"{arguments['path']} is not an image"
            data = base64.standard_b64encode(path.read_bytes()).decode()
            return [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
            ]
        raise ValueError(f"unknown tool {tool!r}")

    def _grep(self, arguments: dict[str, Any]) -> str:
        flags = re.IGNORECASE if arguments.get("ignore_case") else 0
        pattern = re.compile(arguments["pattern"], flags)
        prefix = str(arguments.get("path", "")).strip("/")
        limit = int(arguments.get("max_results", 50))
        hits: list[str] = []
        for rel in sites.files(self.raw):
            if prefix and not str(rel).startswith(prefix):
                continue
            for n, line in enumerate(readers.text_of(self.raw / rel).splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{n}: {line[:400]}")
                    if len(hits) >= limit:
                        return _clip("\n".join(hits) + f"\n[stopped at {limit} matches]")
        return _clip("\n".join(hits) or "no matches")


# --- context: every file in the prompt ---------------------------------------------------------


@dataclass
class Context:
    """Claude with the whole folder in its prompt, line-numbered. When the folder is bigger than
    ``budget`` tokens, the oldest feed lines are dropped first and ``notes`` says how many."""

    raw: Path
    chars_per_token: float
    budget: int = 900_000
    name: str = "context"
    notes: dict[str, Any] = field(default_factory=dict)
    _blob: str = ""

    def _feed_lines(self) -> list[tuple[str, str, int, str]]:
        out = []
        for rel in sites.files(self.raw):
            if rel.parts[0] != "feed":
                continue
            for n, line in enumerate(readers.text_of(self.raw / rel).splitlines(), 1):
                try:
                    t = str(json.loads(line).get("t", ""))
                except (ValueError, AttributeError):
                    t = ""
                out.append((t, str(rel), n, line))
        return out

    def blob(self) -> str:
        if self._blob:
            return self._blob
        parts = []
        for rel in sites.files(self.raw):
            if rel.parts[0] == "feed" or (self.raw / rel).suffix.lower() in readers.IMAGES:
                continue
            lines = readers.text_of(self.raw / rel).splitlines()
            parts.append(
                f"=== FILE {rel} ===\n" + "\n".join(f"{n}: {x}" for n, x in enumerate(lines, 1))
            )
        static = "\n\n".join(parts)
        room = int(self.budget * self.chars_per_token) - len(static) - len(FRAME)
        feed = self._feed_lines()
        kept: list[tuple[str, str, int, str]] = []
        for item in sorted(feed, key=lambda x: (x[0], x[1], x[2]), reverse=True):
            cost = len(item[3]) + len(str(item[2])) + 3
            if cost > room:
                break
            room -= cost
            kept.append(item)
        by_file: dict[str, list[tuple[int, str]]] = {}
        for _, name, n, text in kept:
            by_file.setdefault(name, []).append((n, text))
        totals: dict[str, int] = {}
        for _, name, _, _ in feed:
            totals[name] = totals.get(name, 0) + 1
        feeds = []
        for name in sorted(totals):
            rows = sorted(by_file.get(name, []))
            head = f"=== FILE {name} ({totals[name] - len(rows)} oldest lines left out) ==="
            feeds.append(head + "\n" + "\n".join(f"{n}: {x}" for n, x in rows))
        self.notes = {
            "feed_lines": len(feed),
            "feed_lines_kept": len(kept),
            "feed_lines_dropped": len(feed) - len(kept),
        }
        self._blob = static + "\n\n" + "\n\n".join(feeds)
        return self._blob

    def system(self) -> list[dict[str, Any]]:
        intro = (
            FRAME + "\nThe site's files follow, each line numbered. Photos come with the question."
        )
        return [
            {"type": "text", "text": intro},
            {"type": "text", "text": self.blob(), "cache_control": CACHE},
        ]

    def tools(self) -> list[dict[str, Any]]:
        return []

    def execute(self, tool: str, arguments: dict[str, Any]) -> Content:
        raise ValueError("the context arm has no tools")

    def prompt(self, question: str) -> Content:
        blocks: list[dict[str, Any]] = []
        for rel in sites.files(self.raw):
            media = readers.IMAGES.get(rel.suffix.lower())
            if media:
                data = base64.standard_b64encode((self.raw / rel).read_bytes()).decode()
                blocks.append({"type": "text", "text": f"Photo {rel}:"})
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data},
                    }
                )
        return [*blocks, {"type": "text", "text": question}]


# --- neptune: the site tools -------------------------------------------------------------------


@dataclass
class Neptune:
    """Claude with the read-only ``neptune_site_*`` tools over the built site."""

    built: Path
    name: str = "neptune"
    notes: dict[str, Any] = field(default_factory=dict)
    _site: SiteTools | None = None

    @property
    def site(self) -> SiteTools:
        if self._site is None:
            self._site = SiteTools(self.built)
        return self._site

    def system(self) -> list[dict[str, Any]]:
        text = FRAME + (
            "\nThe site is reached through the neptune_site_* tools, Neptune's model of it. Their "
            "answers end with a Sources footer; copy the sources you rely on into your own.\n\n"
            "The server's instructions (as an MCP client receives them):\n" + SITE_INSTRUCTIONS
        )
        return [{"type": "text", "text": text, "cache_control": CACHE}]

    def tools(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in self.site.tools()
            if t.name != PREFIX + "propose_action"
        ]

    def execute(self, tool: str, arguments: dict[str, Any]) -> Content:
        result = self.site.call(tool, arguments)
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return _clip(("ERROR: " if result.isError else "") + text)

    def prompt(self, question: str) -> Content:
        return question
