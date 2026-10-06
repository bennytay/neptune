"""The context stage, real (Platform ADR 0011): the gold questions asked through Context's agent
tools over the graph the memory stage built, and checked by structure, never by text.

The questions are asked of the server ``python -m neptune_context.mcp --memory`` runs
(``build_server`` over ``neptune_context``'s ``LocalEngine`` and the graph, with no Ledger catalog,
as the sample ``.mcp.json`` runs it), called through an in-memory MCP session: the same tools,
arguments and text an agent such as Claude Code gets. The ledger stage's catalog is attached only to
open each question's first cited source (``neptune_hydrate``).

A case's answer declaration (``Case.answers``; the acceptance corpus's is
``harness/acceptance/answers.json``) lists, per gold question, the tool calls an agent makes and
what the answers must hold. Each answer is read back with Context's own parser
(``render.agent.parse_answer``): every cited statement names its items and evidence refs. A
A statement's citations, in the terms of Platform ADR 0007 §6, are the records its claims are about
(a subject or object that is a record; never the other records a claim was built from) and, per
evidence ref, the corpus path of its source with its row, page or JSON pointer; a single cell is
cited as its row and column, never as the whole row. A gold
claim is **cited** when a statement's citations support one of its evidence items
(``harness.acceptance.resolve.supports``). That rule cannot tell a statement that carries the gold
fact from one that shares its row or record while stating something else, so the declaration pins
each gold claim in one of three classes, each with a reason (ADR 0011 §4): **supported** (the claim
ids citing it, and a statement among them carries the fact), **co_cited** (the claim ids citing
it, none of which carries it) or a **gap** (nothing cites it; ``in_graph`` says whether any claim
of the graph would). The stage fails when an answer is an error or holds a statement without
evidence, when the ids citing a gold claim differ from its pin, when a pin has no reason, when an
answer cites what a question says must never be cited, or when a cited source does not hydrate.
Re-pinning never moves a claim between classes: whatever changed is left without a reason, which
fails until a person classifies it.

One more check proves the path Claude Code takes: ``python -m neptune_context.mcp --memory
<graph>`` is spawned over stdio, lists its tools and answers the first question's first query with
the same text the in-process server gives (both without a catalog, as the sample ``.mcp.json``
runs it).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from harness.consolidate import graph_path
from harness.corpus import REPO
from harness.stages import LEDGER_TENANT, Context, Json, Outcome, package_roots

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from neptune_context.sdk import AsyncClient

ANSWERS_FORMAT: Final = 1
# The tools whose answers are cited text (the others answer lists, plans or one resolution).
PACKET_TOOLS: Final = ("neptune_query", "neptune_why", "neptune_diff", "neptune_compare_runs")
# An argument written "$support:<gold claim id>" is the first claim id (sorted) that supports that
# gold claim in the question's earlier answers: what an agent copies from an Items footer.
SUPPORT_REF: Final = "$support:"
# An argument written "$named:<kind>:<name>" is the id an earlier ``neptune_entities`` answer of
# the question lists for that kind and name (``<kind> "<id>" named "<name>"``; the first id, sorted,
# when one name has several, as SKILL.md allows for copies of one run): how the skill finds a run
# or an incident by name. Calls never hard-code a content-addressed id.
NAMED_REF: Final = "$named:"
CALL_TIMEOUT_S: Final = 120
SMOKE_SOURCE: Final = "context LocalEngine over the memory stage's graph, through neptune_query"


@dataclass(frozen=True)
class Called:
    """One tool call and what came back: the error flag, the cited text, the resource links."""

    tool: str
    arguments: Json
    is_error: bool
    text: str
    links: int


# --- Calling the tools -----------------------------------------------------------------------


def local_client(document: Any, catalog: Any | None = None) -> AsyncClient:
    """What ``python -m neptune_context.mcp --memory`` serves (``local_client``), with a Ledger
    catalog attached when given: the graph channel, claim histories for ``neptune_why``, the
    declared identities for ``neptune_entities``, and no planner model."""
    from neptune_context.engine import LocalEngine
    from neptune_context.explain.history import IndexedReader
    from neptune_context.mcp.__main__ import AGENT_DEFAULTS
    from neptune_context.mcp.__main__ import local_client as served
    from neptune_context.retrieve.graph import GraphChannel
    from neptune_context.sdk import AsyncClient, NoModel, Planner, entity_index

    if catalog is None:
        return served(document)
    reader = IndexedReader(document)
    engine = LocalEngine(reader, catalog, channels=(GraphChannel(reader, catalog),))
    planner = Planner(entity_index(document), AGENT_DEFAULTS, NoModel())
    return AsyncClient(engine, planner=planner)


def _called(tool: str, arguments: Json, result: Any) -> Called:
    from mcp import types

    texts = [b.text for b in result.content if isinstance(b, types.TextContent)]
    links = [b for b in result.content if isinstance(b, types.ResourceLink)]
    # The first block is the answer; a second says how many links were left out.
    return Called(tool, arguments, bool(result.isError), texts[0] if texts else "", len(links))


async def _session(server: Any, calls: Sequence[Json]) -> list[Called]:
    from mcp.shared.memory import create_connected_server_and_client_session

    timeout = timedelta(seconds=CALL_TIMEOUT_S)
    async with create_connected_server_and_client_session(
        server, read_timeout_seconds=timeout
    ) as session:
        return [
            _called(c["tool"], c["arguments"], await session.call_tool(c["tool"], c["arguments"]))
            for c in calls
        ]


def call_tools(client: AsyncClient, calls: Sequence[Json]) -> list[Called]:
    """``calls`` through the MCP server's own tools, in one in-memory session."""
    from neptune_context.mcp import build_server

    return asyncio.run(_session(build_server(client), calls))


def stdio_round_trip(graph: Path, call: Json) -> tuple[list[str], Called]:
    """Spawn ``python -m neptune_context.mcp --memory <graph>`` over stdio (as Claude Code runs
    it), list its tools and make ``call``."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "neptune_context.mcp", "--memory", str(graph)],
        cwd=str(REPO),
    )

    async def run() -> tuple[list[str], Called]:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_S)) as s,
        ):
            await s.initialize()
            names = [tool.name for tool in (await s.list_tools()).tools]
            result = await s.call_tool(call["tool"], call["arguments"])
            return names, _called(call["tool"], call["arguments"], result)

    return asyncio.run(run())


# --- Reading answers as citations --------------------------------------------------------------


@dataclass(frozen=True)
class Statement:
    """One cited statement: the claims it names and its citations (ADR 0007 §6 terms)."""

    claims: tuple[str, ...]
    citations: tuple[Json, ...]
    items: int
    evidence: int


def _about(claim: Json) -> list[str]:
    """The record ids a claim's subject and object are: a ``record`` object, or a node whose id is
    a record (``record:<id>``, or a part of one, ``record:<id>/timeline/0``)."""
    out = []
    for end in (claim.get("subject", {}), claim.get("object", {})):
        if end.get("kind") == "record" and isinstance(end.get("record_id"), str):
            out.append(end["record_id"])
        node = end.get("node_id")
        if isinstance(node, str) and node.startswith("record:"):
            out.append(node.removeprefix("record:").split("/", 1)[0])
    return out


class Scorer:
    """Turns answers into citations and scores them against the gold evidence (ADR 0007 §6)."""

    def __init__(self, roots: Sequence[Path], resolved: Json, graph: Json) -> None:
        from harness.acceptance.resolve import Package

        self.paths: dict[str, str] = {}
        for root in roots:
            for revision in Package(root).kind("source_revision"):
                path = revision["location"].get("path")
                if isinstance(path, str):
                    self.paths[str(revision["content_id"])] = path
        self.resolved = resolved
        self.claims: dict[str, Json] = {str(c["id"]): c for c in graph.get("claims", [])}
        # Evidence refs that locate exactly one value a compiler record holds: a configuration
        # value (located by its JSON pointer) or a calibration parameter (by its name). A claim
        # citing those bytes cites that value, as a cell cites its row and column.
        self.values: dict[tuple[str, str], Json] = {}
        for root in roots:
            package = Package(root)
            for record in package.kind("configuration_value"):
                pointer = "".join(
                    "/" + str(part).replace("~", "~0").replace("/", "~1")
                    for part in record.get("path", [])
                )
                self._value(record.get("value"), {"pointer": pointer})
            for record in package.kind("calibration"):
                for item in record.get("parameters", []):
                    self._value(item.get("value"), {"parameter": item.get("name")})

    def _value(self, slot: Any, locator: Json) -> None:
        evidence = slot.get("provenance", {}).get("evidence") if isinstance(slot, dict) else None
        if isinstance(evidence, dict) and evidence.get("locator"):
            key = (str(evidence.get("source")), json.dumps(evidence["locator"], sort_keys=True))
            self.values.setdefault(key, locator)

    def ref_citations(self, ref: Json) -> list[Json]:
        """An evidence ref as path-and-locator citations: its row, page or JSON pointer, or the
        whole source when it has no locator."""
        path = self.paths.get(str(ref.get("source")))
        if path is None:
            return []
        out: list[Json] = []
        for part in ref.get("locator", []):
            kind = part.get("kind")
            if kind == "row" and isinstance(part.get("row"), int):
                out.append({"locator": {"row": part["row"]}, "path": path})
            elif kind == "row_cell" and isinstance(part.get("row"), int):
                # one cell is never the whole row: it meets only an item naming its column
                column = part.get("column_name")
                out.append({"locator": {"column": column, "row": part["row"]}, "path": path})
            elif kind == "page" and isinstance(part.get("index"), int):
                out.append({"locator": {"page": part["index"] + 1}, "path": path})
            elif kind == "json_pointer" and isinstance(part.get("pointer"), str):
                out.append({"locator": {"pointer": part["pointer"]}, "path": path})
        if not ref.get("locator"):
            out.append({"locator": {}, "path": path})
        key = (str(ref.get("source")), json.dumps(ref.get("locator", []), sort_keys=True))
        if key in self.values:
            out.append({"locator": self.values[key], "path": path})
        return out

    def claim_citations(self, claim_id: str) -> list[Json]:
        """The records a claim is about (its subject or object, when that is a record) and its
        own evidence refs. The other records it was built from (``provenance.records``) are not
        citations: a claim built from a work order states one of its cells, not the work order."""
        claim = self.claims.get(claim_id, {})
        out: list[Json] = [{"record": r} for r in _about(claim)]
        for ref in claim.get("provenance", {}).get("evidence", []):
            out += self.ref_citations(ref)
        return out

    def statements(self, text: str) -> list[Statement]:
        """Every cited statement of an answer: its claims, and what it cites (each evidence ref
        it gives, and the records its claims rest on)."""
        from neptune_context.render.agent import parse_answer

        out = []
        for statement in parse_answer(text).statements:
            claims = tuple(k.claim_id for k in statement.items if k.claim_id is not None)
            citations: list[Json] = []
            for ref in statement.evidence:
                citations += self.ref_citations(ref.to_json())  # type: ignore[arg-type]
            for claim in claims:
                citations += [c for c in self.claim_citations(claim) if "record" in c]
            out.append(
                Statement(claims, tuple(citations), len(statement.items), len(statement.evidence))
            )
        return out

    def supporting(self, evidence: Sequence[str], statements: Sequence[Statement]) -> list[str]:
        """The claim ids of the statements whose citations support any of ``evidence``."""
        from harness.acceptance.resolve import supports

        items = [self.resolved[key] for key in evidence if key in self.resolved]
        return sorted(
            {
                claim
                for s in statements
                if any(supports(item, c) for item in items for c in s.citations)
                for claim in s.claims
            }
        )

    def in_graph(self, evidence: Sequence[str]) -> bool:
        """Whether any claim of the graph would support ``evidence``, cited or not."""
        from harness.acceptance.resolve import supports

        items = [self.resolved[key] for key in evidence if key in self.resolved]
        return any(
            supports(item, c)
            for claim in self.claims
            for c in self.claim_citations(claim)
            for item in items
        )


# --- Asking the gold questions ----------------------------------------------------------------


def _named(ref: str, earlier: Sequence[str]) -> str:
    kind, _, name = ref.removeprefix(NAMED_REF).partition(":")
    pattern = re.compile(rf'{re.escape(kind)} "([^"]+)" named "{re.escape(name)}"')
    found = sorted({str(m) for text in earlier for m in pattern.findall(text)})
    if not found:
        raise LookupError(f"{ref}: no earlier neptune_entities answer names it")
    return found[0]


def _resolve_refs(value: Any, supported: dict[str, list[str]], earlier: Sequence[str] = ()) -> Any:
    """``value`` with every ``$support:<gold claim>`` string replaced by a claim id, and every
    ``$named:<kind>:<name>`` by the id an earlier answer lists for it."""
    if isinstance(value, dict):
        return {k: _resolve_refs(v, supported, earlier) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, supported, earlier) for v in value]
    if isinstance(value, str) and value.startswith(NAMED_REF):
        return _named(value, earlier)
    if isinstance(value, str) and value.startswith(SUPPORT_REF):
        found = supported.get(value.removeprefix(SUPPORT_REF), [])
        if not found:
            raise LookupError(f"{value}: no earlier answer supports it")
        return found[0]
    return value


@dataclass
class Asked:
    """One gold question asked: the calls made, and what its answers support."""

    question: Json
    calls: list[Called]
    statements: list[Statement]
    # each gold claim's id -> the claim ids whose statements cite its evidence (ADR 0007 §6)
    cited: dict[str, list[str]]
    problems: list[str]


def ask(client: AsyncClient, question: Json, declared: Json, scorer: Scorer) -> Asked:
    """Make the declared calls for one gold question, in order, and score the answers."""
    asked = Asked(question, [], [], {}, [])
    gold_claims = question.get("claims", [])

    def score() -> None:
        for claim in gold_claims:
            asked.cited[claim["id"]] = scorer.supporting(claim["evidence"], asked.statements)

    for number, call in enumerate(declared.get("calls", []), start=1):
        try:
            earlier = [c.text for c in asked.calls if c.tool == "neptune_entities"]
            arguments = _resolve_refs(
                copy.deepcopy(call.get("arguments", {})), asked.cited, earlier
            )
        except LookupError as error:
            asked.problems.append(f"call {number} ({call.get('tool')}): {error}")
            continue
        (result,) = call_tools(client, [{"tool": call.get("tool"), "arguments": arguments}])
        asked.calls.append(result)
        if result.is_error:
            asked.problems.append(f"call {number} ({result.tool}) is an error: {_first(result)}")
            continue
        if result.tool in PACKET_TOOLS:
            asked.statements += scorer.statements(result.text)
            score()
    score()
    return asked


def _first(result: Called) -> str:
    try:
        return str(json.loads(result.text)["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return result.text.splitlines()[0] if result.text else "no text"


# The classes a gold claim is pinned in (ADR 0011 §4). ``supported``: a cited statement carries the
# gold fact. ``co_cited``: statements cite the gold claim's evidence (ADR 0007 §6) but none carries
# the fact: they share a row, page or record with it while stating something else. ``gaps``: no
# statement cites its evidence.
CITED_CLASSES: Final = ("supported", "co_cited")
CLASSES: Final = (*CITED_CLASSES, "gaps")


def _entry(declared: Json, cid: str) -> tuple[str | None, Json]:
    """The class ``cid`` is pinned in and its entry; (None, {}) when it is pinned in none."""
    found = [(c, declared.get(c, {})[cid]) for c in CLASSES if cid in declared.get(c, {})]
    if len(found) != 1:
        return None, {"classes": [c for c, _ in found]}
    klass, entry = found[0]
    return klass, entry if isinstance(entry, dict) else {}


def check(asked: Asked, declared: Json, scorer: Scorer, must_not: Sequence[str]) -> list[str]:
    """What the pins say against what the answers cite (see the module docstring)."""
    qid = asked.question["id"]
    problems = [f"{qid}: {p}" for p in asked.problems]
    uncited = sum(1 for s in asked.statements if not s.evidence or not s.items)
    if uncited:
        problems.append(f"{qid}: {uncited} statement(s) cite no item or no evidence")
    if not asked.statements:
        problems.append(f"{qid}: no answer holds a cited statement")
    for key in must_not:
        if scorer.supporting([key], asked.statements):
            problems.append(f"{qid}: an answer cites {key}, which it must never cite")
    for claim in asked.question.get("claims", []):
        cid, found = claim["id"], asked.cited.get(claim["id"], [])
        klass, entry = _entry(declared, cid)
        if klass is None:
            where = " and ".join(entry["classes"]) or "no class"
            problems.append(f"{cid}: pinned in {where}; a gold claim is pinned in exactly one")
            continue
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{cid}: a {klass} pin needs a reason (re-pinned: review it)")
        if klass == "gaps":
            if found:
                problems.append(f"{cid}: pinned as a gap, but {len(found)} cited claim(s) cite it")
            elif entry.get("in_graph") != (held := scorer.in_graph(claim["evidence"])):
                problems.append(f"{cid}: the gap's in_graph is {entry.get('in_graph')}, not {held}")
        elif not found:
            problems.append(f"{cid}: pinned as {klass}, but no cited claim cites it any more")
        elif sorted(entry.get("claims", [])) != found:
            problems.append(
                f"{cid}: cited by {len(found)} claim(s), not the {len(entry.get('claims', []))}"
                f" pinned as {klass} (re-pin with `make demo-pin` and review the diff)"
            )
    known = {c["id"] for c in asked.question.get("claims", [])}
    named = {cid for c in CLASSES for cid in declared.get(c, {})}
    problems += [f"{qid}: pins name unknown gold claim {c}" for c in sorted(named - known)]
    return problems


def pinned(asked: Asked, declared: Json, scorer: Scorer) -> Json:
    """``declared`` with its pins rewritten from what the answers cite. A pin never moves between
    classes by itself: a claim whose cited ids are unchanged keeps its class and reason; one whose
    ids changed keeps its class with an empty reason; one newly cited is pinned ``co_cited`` and
    one no longer cited a gap, both with an empty reason. The check refuses an empty reason, so
    every change needs a person to (re)classify it."""
    out: Json = {k: v for k, v in declared.items() if k not in CLASSES}
    classes: dict[str, Json] = {c: {} for c in CLASSES}
    for claim in asked.question.get("claims", []):
        cid, found = claim["id"], asked.cited.get(claim["id"], [])
        klass, entry = _entry(declared, cid)
        reason = str(entry.get("reason", "")) if klass is not None else ""
        if not found:
            keep = reason if klass == "gaps" else ""
            classes["gaps"][cid] = {"in_graph": scorer.in_graph(claim["evidence"]), "reason": keep}
            continue
        if klass not in CITED_CLASSES:
            klass, reason = "co_cited", ""
        elif sorted(entry.get("claims", [])) != found:
            reason = ""
        classes[klass][cid] = {"claims": found, "reason": reason}
    return {**out, **classes}


# --- The stage -------------------------------------------------------------------------------


def _hydrated(client: AsyncClient, asked: Asked) -> str:
    """Whether the first source the question's first cited answer gives resolves through the
    Ledger (``neptune_hydrate``, as an agent opens an [E1] link)."""
    from neptune_context.render.agent import parse_answer

    for call in asked.calls:
        if call.tool in PACKET_TOOLS and not call.is_error:
            evidence = parse_answer(call.text).evidence
            if not evidence:
                continue
            arguments = {"evidence": evidence[0].to_json()}
            (result,) = call_tools(client, [{"tool": "neptune_hydrate", "arguments": arguments}])
            if result.is_error:
                return f"error ({_first(result)})"
            return str(json.loads(result.text).get("status"))
    return "nothing cited"


def _declaration(ctx: Context) -> tuple[Path | None, Path | None]:
    """The one case with an answer declaration: its declaration and its gold answers."""
    for case in ctx.cases:
        if case.answers is not None:
            return case.answers, case.gold
    return None, None


def _smoke(client: AsyncClient, call: Json | None, question: str | None) -> Json:
    """The smoke query: ``call`` (the first question's first query) asked through the SDK, so the
    report has the packet itself. Without one, the graph's first declared identity, one hop; and
    when the graph declares none (a corpus of corrupt files), every run, which the engine answers
    with a packet that says what it could not cover, never with nothing."""
    from neptune_context.mcp import query_from_arguments

    async def entities() -> Any:
        return await client.entities(None, include_inferred=False)

    if call is None:
        found = asyncio.run(entities())
        query: Json = {"budget": {"items": 20}, "subjects": [{"kind": "run"}]}
        question = "which runs does memory hold?"
        if found:
            first = found[0]
            query = {
                "budget": {"items": 20},
                "graph": {"direction": "both", "hops": 1, "predicates": "any"},
                "subjects": [{"declared_id": first.declared_id, "kind": first.kind}],
            }
            question = f"what does memory hold about {first.kind} {first.declared_id}?"
        call = {"arguments": {"include_inferred": False, "query": query}, "tool": "neptune_query"}

    async def ask() -> Any:
        return await client.query(query_from_arguments(call["tool"], call["arguments"]))

    packet = asyncio.run(ask())
    claims = [i for i in packet.items if getattr(i, "claim", None) is not None]
    return {
        "packet": {
            "as_of": packet.as_of,
            "claims": len(claims),
            "id": packet.id,
            "items": len(packet.items),
            "kind": "context_packet",
            "packet_version": packet.to_json()["packet_version"],
        },
        "packet_source": SMOKE_SOURCE,
        "query": question,
    }


def answers_path(ctx: Context) -> Path:
    """Where the context stage writes every answer in full (the demo's ``answers.json``)."""
    return ctx.work / "context" / "answers.json"


def run_answers(
    ctx: Context,
    client: AsyncClient,
    document: Json,
    *,
    pin: bool = False,
    hydrator: AsyncClient | None = None,
) -> tuple[Json, list[str], Json | None]:
    """Ask every declared gold question through ``client``, and open each question's first
    cited source through ``hydrator`` (default ``client``); (summary for the report, problems,
    the transcript).
    With ``pin``, the declaration file is rewritten from the answers first."""
    from harness.acceptance import resolve as gold_resolve

    declaration_path, gold_path = _declaration(ctx)
    if declaration_path is None or gold_path is None:
        return {}, [], None
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if declaration.get("answers_format") != ANSWERS_FORMAT:
        problems.append(f"answers_format is {declaration.get('answers_format')!r}, not 1")
    if declaration.get("corpus_version") != gold.get("corpus_version"):
        problems.append(
            f"the answers are pinned for corpus {declaration.get('corpus_version')},"
            f" the gold answers are {gold.get('corpus_version')}"
        )
    case = next(c for c in ctx.cases if c.answers is not None)
    roots = [ctx.package_root(case.id), ctx.deploy_root(case.id)]
    resolved = gold_resolve.resolve(ctx.package_root(case.id), gold)
    scorer = Scorer([r for r in roots if r.is_dir()], resolved, document)
    by_id = {q["id"]: q for q in declaration.get("questions", [])}
    unknown = sorted(set(by_id) - {q["id"] for q in gold["questions"]})
    problems += [f"answers name unknown question {qid}" for qid in unknown]
    summary: Json = {}
    transcript: list[Json] = []
    rewritten: list[Json] = []
    for question in gold["questions"]:
        qid = question["id"]
        declared = by_id.get(qid)
        if declared is None or not declared.get("calls"):
            problems.append(f"{qid}: not asked (no calls declared)")
            continue
        asked = ask(client, question, declared, scorer)
        if pin:
            declared = pinned(asked, declared, scorer)
            rewritten.append(declared)
        problems += check(asked, declared, scorer, question.get("must_not_cite", []))
        hydrated = _hydrated(hydrator or client, asked)
        if hydrated != "resolved":
            problems.append(f"{qid}: the first cited source did not hydrate ({hydrated})")
        summary[qid] = {
            "asked_as": declared.get("asked_as"),
            "calls": [c.tool for c in asked.calls],
            "cited_claims": len({c for s in asked.statements for c in s.claims}),
            "co_cited": sorted(declared.get("co_cited", {})),
            "gaps": sorted(declared.get("gaps", {})),
            "hydrated": hydrated,
            "statements": len(asked.statements),
            "supported": sorted(declared.get("supported", {})),
        }
        transcript.append(
            {
                "asked_as": declared.get("asked_as"),
                "calls": [
                    {
                        "arguments": c.arguments,
                        "is_error": c.is_error,
                        "text": c.text,
                        "tool": c.tool,
                    }
                    for c in asked.calls
                ],
                "co_cited": declared.get("co_cited", {}),
                "gaps": declared.get("gaps", {}),
                "id": qid,
                "question": question["question"],
                "supported": declared.get("supported", {}),
            }
        )
    generation = document.get("generation")
    pinned_graph = declaration.get("graph", {}).get("generation")
    if pin:
        declaration = {
            **declaration,
            "graph": {"generation": generation, "graph_schema": document.get("graph_schema")},
            "questions": rewritten + [by_id[q] for q in unknown],
        }
        text = json.dumps(declaration, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        declaration_path.write_text(text, encoding="utf-8")
    elif pinned_graph != generation:
        problems.append(
            f"the answers are pinned for graph {pinned_graph}, the memory stage built {generation}"
            " (re-pin with `make demo-pin` and review the diff)"
        )
    return summary, problems, {"graph": generation, "questions": transcript}


def context_real(ctx: Context) -> Outcome:
    """Answer over the memory stage's graph through Context's tools; check the pinned answers
    and the stdio server (see the module docstring)."""
    from neptune_context.engine import read_graph_document
    from neptune_ledger.catalog.registry import PostgresCatalog

    graph = graph_path(ctx)
    if not graph.is_file():
        return Outcome({}, ("the memory stage wrote no graph document",))
    document = read_graph_document(graph)
    raw = json.loads(graph.read_bytes())
    problems: list[str] = []
    output: Json = {}
    if ctx.ledger_uri is None:
        return Outcome({}, ("the ledger stage kept no catalog for evidence hydration",))
    with PostgresCatalog(
        ctx.ledger_uri, LEDGER_TENANT, package_roots=package_roots(ctx)
    ) as catalog:
        # The questions are asked as Claude Code asks them, of the server `.mcp.json` runs (no
        # catalog: a catalog adds a hydrated evidence item per source, which changes what a
        # budget holds); the first cited source of each is then opened through the catalog.
        client = local_client(document, catalog)
        summary, found, transcript = run_answers(
            ctx, local_client(document), raw, pin=ctx.pin_answers, hydrator=client
        )
        # The smoke query and the stdio probe are the first question's own calls, as asked
        # (their names resolved): its first query, and its last cited answer.
        asked = (transcript or {}).get("questions", [])[:1]
        calls = [
            {"arguments": c["arguments"], "tool": c["tool"]}
            for q in asked
            for c in q["calls"]
            if not c["is_error"]
        ]
        first_call = next((c for c in calls if c["tool"] == "neptune_query"), None)
        packet_calls = [c for c in calls if c["tool"] in PACKET_TOOLS]
        question = asked[0]["question"] if asked else None
        output["smoke"] = _smoke(client, first_call, question)
    problems += found
    if summary:
        output["answers"] = summary
    if transcript is not None:
        answers_path(ctx).parent.mkdir(parents=True, exist_ok=True)
        answers_path(ctx).write_text(
            json.dumps(transcript, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if output["smoke"].get("packet") is None:
        problems.append("the smoke query returned no packet")
    probe: Json = (
        packet_calls[-1]
        if packet_calls
        else {"arguments": {"include_inferred": False}, "tool": "neptune_entities"}
    )
    tools, over_stdio = stdio_round_trip(graph, probe)
    (in_process,) = call_tools(local_client(document), [probe])
    output["stdio"] = {
        "call": probe["tool"],
        "same_as_in_process": over_stdio == in_process,
        "tools": tools,
    }
    if over_stdio != in_process or over_stdio.is_error:
        problems.append("the stdio MCP server did not answer as the in-process server does")
    return Outcome(output, tuple(problems))
