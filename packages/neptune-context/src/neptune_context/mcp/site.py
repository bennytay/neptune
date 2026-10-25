"""The site tools (site model v3, ``docs/site-model/spec.md`` §8): ``neptune_site_<name>``.

One read-only MCP tool per function of ``neptune_memory.site.query``. Each takes a JSON object that
mirrors the Python arguments and answers ``neptune_memory.site.render.render(answer)``: compact
cited lines and a ``Sources:`` footer. ``python -m neptune_context.mcp --site <site_dir>`` serves
them.

The model is folded from ``<site_dir>/.neptune/log`` once; each tool call then applies the entries
appended since (``log.Tail``), so a live site (``python -m neptune_memory.site live``) shows up in
the next answer without a restart. A log started again (a fresh ``build``) is folded from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mcp import types
from mcp.server.lowlevel import Server
from neptune_memory.site import insights, log, query, views
from neptune_memory.site.render import render

from neptune_context import __version__

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune_memory.site.query import Answer
    from neptune_memory.site.views import SiteModel

PREFIX: Final = "neptune_site_"
SITE_SERVER_NAME: Final = "neptune-site"
SITE_INSTRUCTIONS: Final = (
    "Neptune's live site model: every robot, machine, person (by role), task, zone, document and "
    "rule of one site, and what feeds what. Answer site questions with the neptune_site_* tools; "
    "every line ends with [S<n>] keys into the Sources footer, and says observed, stated or "
    "inferred, and stale, unknown, ambiguous or pending when it is. Times are the site clock's, "
    "never converted."
)

_STR = {"type": "string"}
_TIME = {"type": "string", "description": "site-clock time YYYY-MM-DDTHH:MM:SS"}
_AT = {**_TIME, "description": "site-clock time YYYY-MM-DDTHH:MM:SS; default: the latest report"}
_STRINGS = {"type": "array", "items": {"type": "string"}}


@dataclass(frozen=True)
class SiteTool:
    """One tool: the query function, its argument schemas and which arguments it needs."""

    function: Callable[..., Answer]
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }


SITE_TOOLS: Final[dict[str, SiteTool]] = {
    PREFIX + "resolve": SiteTool(
        query.resolve,
        "Find entities by name or id (MR-1142, cv-2, okafor): the entity refs to pass to the other "
        "site tools, ids the sources state are one (same_as), and pending identity proposals.",
        {"name": _STR},
        ("name",),
    ),
    PREFIX + "state_at": SiteTool(
        query.state_at,
        "Everything known about one entity at a time: mode, health, battery, task, shift, notes "
        "..., each known, stale, unknown or ambiguous, cited.",
        {"entity": _STR, "at": _AT},
        ("entity",),
    ),
    PREFIX + "find": SiteTool(
        query.find,
        "Who or what has predicate = obj at a time (obj is an entity ref such as "
        "qualification:arm-reset-c3, or a value as text), plus what is known about obj.",
        {"predicate": _STR, "obj": _STR, "at": _AT},
        ("predicate", "obj"),
    ),
    PREFIX + "timeline": SiteTool(
        query.timeline,
        "One entity's history between two times: each value and the interval it held, cited. "
        "Optionally only some predicates.",
        {"entity": _STR, "start": _TIME, "end": _TIME, "predicates": _STRINGS},
        ("entity", "start", "end"),
    ),
    PREFIX + "why": SiteTool(
        query.why,
        "One fact (fact:...) with its citations, the fact it replaced, the one that replaced it, "
        "and any fact it beats or ties with.",
        {"fact_id": _STR},
        ("fact_id",),
    ),
    PREFIX + "diff": SiteTool(
        query.diff,
        "Every fact that began after start and up to end (work orders, changes, setpoints, "
        "firmware ...), optionally only about some entities.",
        {"start": _TIME, "end": _TIME, "entities": _STRINGS},
        ("start", "end"),
    ),
    PREFIX + "gaps": SiteTool(
        query.gaps,
        "What the model does not know at a time: stale, unknown and ambiguous values, and "
        "inferred proposals nobody has confirmed.",
        {"at": _AT},
        (),
    ),
    PREFIX + "impact": SiteTool(
        query.impact,
        "What is downstream of an entity at a time (what it feeds, what sits there, their "
        "tasks), with the state of each: use it for 'what is blocked'.",
        {"entity": _STR, "at": _AT, "depth": {"type": "integer", "minimum": 1, "maximum": 6}},
        ("entity",),
    ),
    PREFIX + "search_evidence": SiteTool(
        query.search_evidence,
        "Keyword search over the cited source text (log lines, CSV cells, document blocks, "
        "photo notes): every fact whose evidence holds all the words.",
        {"text": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ("text",),
    ),
    PREFIX + "sql": SiteTool(
        query.sql,
        "Read-only SQLite over tables facts(fact_id, subject, predicate, object, object_is_ref, "
        "unit, valid_from, valid_to, kind, source, model, pending, citations), intervals(fact_id, "
        'subject, predicate, object, start, "end", kind), seen(source, t) and entities(ref, type, '
        "id, names). Select fact_id to get each row's citations.",
        {"query": _STR},
        ("query",),
    ),
    PREFIX + "changes": SiteTool(
        query.changes,
        "What changed on the live site since a log seq: every fact appended after since_seq, in "
        "log order, cited. Each answer's header gives its log seq; pass it back next time.",
        {"since_seq": {"type": "integer", "minimum": 0}},
        (),
    ),
    PREFIX + "insights": SiteTool(
        insights.insights,
        "Call this first for 'what is going on' or 'what went wrong': Neptune's inferred findings "
        "at a time, from deterministic rules: starvation chains (a fault, then what waits "
        "downstream), qualification gaps (no free on-shift holder), stale sources, changes "
        "before faults (correlation only) and work orders checked against rule clauses. Each is "
        "inferred, cited by its supporting facts. sql also has tables downtime(entity, state, "
        'start, "end", seconds, cause, work_order, fact_id), utilisation(robot, mode, seconds) '
        'and fault_clear_time(entity, fault, start, "end", seconds, fact_id).',
        {"at": _AT},
        (),
    ),
    PREFIX + "shift_report": SiteTool(
        insights.shift_report,
        "Call this first for a shift summary or handover: a compact cited report of one shift "
        "(shift_id such as shift:A, its latest window) or of start..end: who was on, faults, "
        "stops and work orders, chains, gaps and checks, stale sources, changes, what is unknown.",
        {"shift_id": _STR, "start": _TIME, "end": _TIME},
        (),
    ),
}


class SiteTools:
    """The site tools over one site directory, applying new log entries before each call."""

    def __init__(self, site_dir: Path) -> None:
        self.site_dir = Path(site_dir)
        self._tail = log.Tail(self.site_dir)
        self._model = views.SiteModel.empty()

    def model(self) -> SiteModel:
        new = self._tail.new()
        if new is None:  # the log was started again (a fresh build): fold it from scratch
            self._tail = log.Tail(self.site_dir)
            self._model = views.SiteModel.empty()
            new = self._tail.new() or []
        return views.fold(new, self._model)

    def tools(self) -> list[types.Tool]:
        read_only = types.ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
        return [
            types.Tool(
                name=name,
                title=name.replace("_", " "),
                description=tool.description,
                inputSchema=tool.schema(),
                annotations=read_only,
            )
            for name, tool in SITE_TOOLS.items()
        ]

    def call(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        tool = SITE_TOOLS.get(name)
        arguments = arguments or {}
        if tool is None:
            return _error(f"unknown tool {name!r}; the site tools are {', '.join(SITE_TOOLS)}")
        missing = [a for a in tool.required if a not in arguments]
        unknown = [a for a in arguments if a not in tool.properties]
        if missing or unknown:
            return _error(
                f"{name} needs {list(tool.required)}; missing {missing}, unknown {unknown}"
            )
        try:
            answer = tool.function(self.model(), **arguments)
        except Exception as error:
            return _error(f"{type(error).__name__}: {error}")
        text = render(answer)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], isError=True
    )


def build_site_server(site: SiteTools) -> Server[Any]:
    """An MCP server offering the site tools alone."""
    server: Server[Any] = Server(
        SITE_SERVER_NAME, version=__version__, instructions=SITE_INSTRUCTIONS
    )
    tools = site.tools()

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return site.call(tool, arguments)

    return server
