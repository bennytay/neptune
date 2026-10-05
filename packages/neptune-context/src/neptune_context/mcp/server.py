"""The MCP server (ADR 0004 §6): four read-only tools and an evidence resource.

Tools: ``neptune_query``, ``neptune_why``, ``neptune_diff`` and ``neptune_hydrate``. Each answer is
the packet rendered as cited text (``render.citations.render_text``: ``[E1]`` keys, an ``Evidence:``
footer, every inferred item marked ``INFERRED``) followed by one resource link per evidence ref;
reading a link (``neptune://evidence/<token>``) hydrates it through the Ledger. The server holds no
retrieval logic and names no channel: it speaks to an ``AsyncClient``, so whichever engine sits
behind the SDK (stub, remote, in-process) is what answers, and channels added later appear only in
the query schema the tools advertise.

``include_inferred`` is a required parameter of every packet tool, so an agent must choose it.
Failures are tool errors (``isError``) carrying the SDK's structured error as JSON, never a crash
and never a credential.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
from typing import TYPE_CHECKING, Any, Final, cast

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from neptune_ledger.api import dumps as ledger_dumps

from neptune.identity.canonical_json import dumps
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune_context import __version__
from neptune_context.packets.schema import packet_schema
from neptune_context.query.decode import from_json
from neptune_context.query.findings import Refused
from neptune_context.query.schema import query_schema
from neptune_context.render.citations import render_text
from neptune_context.sdk.client import DEFAULT_EXPLAIN_ITEMS
from neptune_context.sdk.errors import ErrorCode, SdkError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonValue
    from neptune_context.packets.model import ContextPacket
    from neptune_context.query.model import Query
    from neptune_context.sdk.client import AsyncClient

SERVER_NAME: Final = "neptune"
TOOL_QUERY: Final = "neptune_query"
TOOL_WHY: Final = "neptune_why"
TOOL_DIFF: Final = "neptune_diff"
TOOL_HYDRATE: Final = "neptune_hydrate"
TOOLS: Final = (TOOL_QUERY, TOOL_WHY, TOOL_DIFF, TOOL_HYDRATE)
EVIDENCE_SCHEME: Final = "neptune://evidence/"
MAX_LINKS: Final = 100  # resource links per answer; the footer lists every ref regardless
MAX_URI_CHARS: Final = 8192

INSTRUCTIONS: Final = """\
Neptune is a deployment memory for robots: typed, cited claims about machines, sites, runs, \
configurations and documents, each with the source bytes it came from. Every tool is read-only.
- Answers cite evidence as [E1], [E2] ... ending each line; the Evidence: footer maps a key to the \
exact source. Quote those keys when you state a fact, and never state one the answer does not hold.
- You must choose include_inferred. false: evidence only (observed or stated). true: model- or \
rule-inferred items are included and marked INFERRED; present them as inferences, never as facts.
- A line under "Not answered" is a gap, not a "no": say what is missing instead of guessing.
- Times are on a named clock and never converted for you; "as of transaction N" is the snapshot \
the answer was assembled at.
- Start with neptune_query. Use neptune_why on a claim id from an answer to see its evidence and \
what superseded it, neptune_diff for what changed about one subject, and neptune_hydrate (or read \
a resource link) for the source behind a [E] key."""

_INFERRED_PARAM: Final = {
    "type": "boolean",
    "description": (
        "Required. false: only observed or stated evidence. true: also inferred items, each marked "
        "INFERRED in the answer. You must choose."
    ),
}
_AS_OF: Final = {
    "description": 'Ledger transaction the answer is as known at: an integer, or "head" (latest).',
    "oneOf": [{"const": "head"}, {"type": "integer", "minimum": 0, "maximum": 2**63 - 1}],
}
_EXAMPLE_QUERY: Final = {
    "include_inferred": False,
    "query": {
        "budget": {"items": 20},
        "subjects": [{"kind": "run"}],
        "text": {
            "text": "thruster stall during descent",
            "fields": ["finding", "document"],
            "channels": ["lexical"],
        },
    },
}


# --- Schemas, derived from the query contract so they cannot drift from it ---------------------


def _query_defs() -> dict[str, Any]:
    """The query schema's definitions, relaxed for an agent: ``include_inferred`` is a tool
    parameter, and members with one fixed default (version, ``head``, empty lists, depth 0) may be
    left out. Everything else is the contract's own definition, verbatim."""
    defs: dict[str, Any] = copy.deepcopy(cast("dict[str, Any]", query_schema()["$defs"]))
    query = defs["Query"]
    del query["properties"]["include_inferred"]
    query["required"] = ["budget"]
    defs["Subject"]["required"] = ["kind"]
    return {**defs, **_evidence_defs()}


def _refs(node: Any) -> set[str]:
    """Names of the ``#/$defs`` entries a schema fragment points at."""
    if isinstance(node, dict):
        found = {v.rsplit("/", 1)[-1] for k, v in node.items() if k == "$ref"}
        return found.union(*(_refs(v) for k, v in node.items() if k != "$ref"))
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node))
    return set()


def _renamed(node: Any, names: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {
            k: f"#/$defs/{names[v.rsplit('/', 1)[-1]]}" if k == "$ref" else _renamed(v, names)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_renamed(v, names) for v in node]
    return node


def _evidence_defs() -> dict[str, Any]:
    """The compiler's ``EvidenceRef`` schema (as the packet contract exports it) and everything it
    points at, renamed ``Ev...`` so none collides with a query definition of the same name."""
    source = cast("dict[str, Any]", packet_schema()["$defs"])
    wanted, todo = set(), ["EvidenceRef"]
    while todo:
        name = todo.pop()
        if name not in wanted:
            wanted.add(name)
            todo.extend(_refs(source[name]))
    names = {n: n if n == "EvidenceRef" else f"Ev{n}" for n in wanted}
    return {names[n]: _renamed(source[n], names) for n in sorted(wanted)}


def _object(properties: dict[str, Any], required: list[str], defs: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(required),
    }
    if defs:
        schema["$defs"] = _query_defs()
    return schema


def input_schemas() -> dict[str, dict[str, Any]]:
    """The input schema of each tool (JSON Schema draft 2020-12)."""
    return {
        TOOL_QUERY: _object(
            {
                "include_inferred": _INFERRED_PARAM,
                "query": {"$ref": "#/$defs/Query"},
            },
            ["include_inferred", "query"],
            defs=True,
        ),
        TOOL_WHY: _object(
            {
                "claim_id": {"$ref": "#/$defs/ClaimId"},
                "include_inferred": _INFERRED_PARAM,
                "as_of": _AS_OF,
                "max_items": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["claim_id", "include_inferred"],
            defs=True,
        ),
        TOOL_DIFF: _object(
            {
                "subject": {"$ref": "#/$defs/Subject"},
                "before": {"$ref": "#/$defs/DiffPoint"},
                "after": {"$ref": "#/$defs/DiffPoint"},
                "include_inferred": _INFERRED_PARAM,
                "as_of": _AS_OF,
                "max_items": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["subject", "before", "after", "include_inferred"],
            defs=True,
        ),
        TOOL_HYDRATE: _object(
            {
                "evidence": {"$ref": "#/$defs/EvidenceRef"},
                "as_of": {
                    "description": "Ledger transaction to resolve at; omit for the latest.",
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2**63 - 1,
                },
            },
            ["evidence"],
            defs=True,
        ),
    }


def _tools() -> list[types.Tool]:
    schemas = input_schemas()
    read_only = types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    example = json.dumps(_EXAMPLE_QUERY, separators=(",", ":"))
    described = {
        TOOL_QUERY: (
            "Ask Neptune what memory holds: a typed query over subjects, time (as_of snapshot, "
            "during on a named clock), space, graph hops and free text. Returns a cited answer. "
            f"Example arguments: {example}"
        ),
        TOOL_WHY: (
            "Why does memory hold one claim? Its evidence, the transform that produced it, what "
            "superseded it and any resolver findings. Takes a claim id (claim:sha256:...) from an "
            "earlier answer."
        ),
        TOOL_DIFF: (
            "What changed about one subject between two points: two Ledger transactions (what was "
            "known then) or two instants on a named clock (what held then). Never one of each. "
            "A diff across two clocks needs a named clock mapping: use neptune_query with "
            "explain and clock_bridges."
        ),
        TOOL_HYDRATE: (
            "Resolve one cited source (an evidence ref copied from an Evidence: footer line) "
            "through the Ledger: whether it resolves, its size, and the records that cite it."
        ),
    }
    return [
        types.Tool(
            name=name,
            title=name.replace("_", " "),
            description=described[name],
            inputSchema=schemas[name],
            annotations=read_only,
        )
        for name in TOOLS
    ]


# --- Evidence resources ------------------------------------------------------------------------


def evidence_uri(ref: EvidenceRef) -> str:
    """The resource URI of ``ref``: its canonical JSON, base64url without padding."""
    token = base64.urlsafe_b64encode(dumps(ref.to_json())).rstrip(b"=").decode("ascii")
    return EVIDENCE_SCHEME + token


def parse_evidence_uri(uri: str) -> EvidenceRef:
    """The evidence ref a URI names; raises ``SdkError(INVALID_ARGUMENT)`` for anything else."""
    bad = SdkError(ErrorCode.INVALID_ARGUMENT, "not a neptune evidence URI")
    if len(uri) > MAX_URI_CHARS or not uri.startswith(EVIDENCE_SCHEME):
        raise bad
    token = uri[len(EVIDENCE_SCHEME) :]
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        ref = evidence_ref_from_json(json.loads(raw.decode("utf-8")))
    except (binascii.Error, ValueError, TypeError, RecursionError):
        raise bad from None
    if evidence_uri(ref) != uri:  # one URI per ref: refuse padded, re-ordered or aliased forms
        raise bad
    return ref


# --- Arguments ---------------------------------------------------------------------------------


def _bad(message: str) -> SdkError:
    return SdkError(ErrorCode.INVALID_ARGUMENT, message)


def _only(arguments: Mapping[str, Any], required: set[str], optional: set[str]) -> None:
    missing = required - set(arguments)
    extra = set(arguments) - required - optional
    if missing:
        raise _bad(f"missing argument(s): {', '.join(sorted(missing))}")
    if extra:
        raise _bad(f"unknown argument(s): {', '.join(sorted(extra))}")


def _flag(arguments: Mapping[str, Any]) -> bool:
    value = arguments["include_inferred"]
    if not isinstance(value, bool):
        raise _bad("include_inferred must be true or false; you must choose")
    return value


def _count(arguments: Mapping[str, Any]) -> int:
    value = arguments.get("max_items", DEFAULT_EXPLAIN_ITEMS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        raise _bad("max_items is an integer from 1 to 10000")
    return value


def _snapshot(arguments: Mapping[str, Any]) -> JsonValue:
    value = arguments.get("as_of", "head")
    if value != "head" and (
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63
    ):
        raise _bad('as_of is a transaction number or "head"')
    return value  # type: ignore[no-any-return]


def _decoded(document: dict[str, Any]) -> Query:
    """A query document, with the members that have one fixed default filled in, read strictly."""
    document.setdefault("query_version", 1)
    document.setdefault("as_of", "head")
    for member in ("clock_bridges", "explain", "frame_bridges", "regions", "subjects"):
        document.setdefault(member, [])
    subjects = [*document["subjects"]] if isinstance(document["subjects"], list) else []
    subjects += (
        [
            item["subject"]
            for item in document["explain"]
            if isinstance(item, dict) and isinstance(item.get("subject"), dict)
        ]
        if isinstance(document["explain"], list)
        else []
    )
    for subject in subjects:
        if isinstance(subject, dict):
            subject.setdefault("same_as_depth", 0)
    query = from_json(document)
    if isinstance(query, Refused):
        raise SdkError(
            ErrorCode.QUERY_REFUSED,
            f"{len(query.findings)} finding(s); first: {query.findings[0].code} at "
            f"{query.findings[0].at}: {query.findings[0].message}",
            findings=query.findings,
        )
    return query


def query_from_arguments(tool: str, arguments: Mapping[str, Any]) -> Query:
    """The ``Query`` a packet tool call asks, or ``SdkError`` saying what is wrong with the call."""
    if tool == TOOL_QUERY:
        _only(arguments, {"include_inferred", "query"}, set())
        document = arguments["query"]
        if not isinstance(document, dict):
            raise _bad("query must be an object")
        document = copy.deepcopy(document)
        flag = _flag(arguments)
        if document.setdefault("include_inferred", flag) != flag:
            raise _bad("query.include_inferred disagrees with the include_inferred argument")
        return _decoded(document)
    if tool == TOOL_WHY:
        _only(arguments, {"claim_id", "include_inferred"}, {"as_of", "max_items"})
    else:
        _only(arguments, {"subject", "before", "after", "include_inferred"}, {"as_of", "max_items"})
    base: dict[str, Any] = {
        "include_inferred": _flag(arguments),
        "as_of": _snapshot(arguments),
        "budget": {"items": _count(arguments)},
    }
    if tool == TOOL_WHY:
        base["explain"] = [{"kind": "why", "claim_id": arguments["claim_id"]}]
    else:
        subject = arguments["subject"]
        base["subjects"] = [subject]
        base["explain"] = [
            {
                "kind": "diff",
                "subject": subject,
                "before": arguments["before"],
                "after": arguments["after"],
            }
        ]
    return _decoded(copy.deepcopy(base))


# --- Results -----------------------------------------------------------------------------------


def _failure(error: SdkError) -> types.CallToolResult:
    text = json.dumps({"error": error.to_json()}, sort_keys=True, indent=2)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


def packet_content(packet: ContextPacket) -> list[types.ContentBlock]:
    """A packet as the tool's content: the cited text, then a resource link per evidence ref."""
    refs = packet.evidence_refs()
    blocks: list[types.ContentBlock] = [types.TextContent(type="text", text=render_text(packet))]
    for number, ref in enumerate(refs[:MAX_LINKS], start=1):
        blocks.append(
            types.ResourceLink(
                type="resource_link",
                uri=evidence_uri(ref),  # type: ignore[arg-type]
                name=f"E{number}",
                description=f"Source behind [E{number}]; reading it resolves it via the Ledger.",
                mimeType="application/json",
            )
        )
    if len(refs) > MAX_LINKS:
        blocks.append(
            types.TextContent(
                type="text",
                text=f"Resource links cover [E1]..[E{MAX_LINKS}] of {len(refs)}; "
                "use neptune_hydrate for the rest.",
            )
        )
    return blocks


def build_server(client: AsyncClient, *, name: str = SERVER_NAME) -> Server[Any]:
    """An MCP server whose every answer comes from ``client``."""
    server: Server[Any] = Server(name, version=__version__, instructions=INSTRUCTIONS)
    tools = _tools()

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if tool in (TOOL_QUERY, TOOL_WHY, TOOL_DIFF):
                packet = await client.query(query_from_arguments(tool, arguments))
                return types.CallToolResult(content=packet_content(packet), isError=False)
            if tool == TOOL_HYDRATE:
                _only(arguments, {"evidence"}, {"as_of"})
                try:
                    ref = evidence_ref_from_json(arguments["evidence"])
                except (ValueError, TypeError) as error:
                    raise _bad(f"evidence is not an evidence ref: {error}") from None
                as_of = arguments.get("as_of")
                resolution = await client.hydrate(ref, as_of=as_of)
                text = ledger_dumps(resolution).decode("utf-8")
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=text)], isError=False
                )
            raise _bad(f"unknown tool {tool!r}; the tools are {', '.join(TOOLS)}")
        except SdkError as error:
            return _failure(error)
        except Exception as error:
            return _failure(SdkError(ErrorCode.ENGINE_ERROR, f"{type(error).__name__}: {error}"))

    @server.list_resources()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_resources() -> list[types.Resource]:
        return []  # evidence is addressed by link from an answer, never enumerated

    @server.list_resource_templates()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_templates() -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                uriTemplate=EVIDENCE_SCHEME + "{ref}",
                name="evidence",
                description="One cited source, resolved through the Ledger. Take the URI from a "
                "resource link in an answer.",
                mimeType="application/json",
            )
        ]

    @server.read_resource()  # type: ignore[no-untyped-call,untyped-decorator]
    async def read_resource(uri: Any) -> list[ReadResourceContents]:
        resolution = await client.hydrate(parse_evidence_uri(str(uri)))
        return [
            ReadResourceContents(
                content=ledger_dumps(resolution).decode("utf-8"), mime_type="application/json"
            )
        ]

    return server
