"""``python -m neptune_context.mcp``: serve the Neptune tools to an agent over stdio (ADR 0004 §6).

    python -m neptune_context.mcp --url https://neptune.example   # token from $NEPTUNE_TOKEN
    python -m neptune_context.mcp --memory graph.json[.gz]        # the local engine (ADR 0007)
    python -m neptune_context.mcp --packets tests/golden/packets  # fixtures: recorded answers only
    ... --sources sources.json   # with any of them: name each cited source's file (ADR 0016)

Claude Code: ``claude mcp add neptune -- python -m neptune_context.mcp --memory graph.json``, or
the sample ``.mcp.json`` and skill in ``packages/neptune-context/claude/`` (ADR 0009 §6).
``--memory`` answers real queries with ``LocalEngine`` over a Memory graph document (read by
Memory's strict codec into its reference reader), with the graph channel and a lexical channel
over the document's claims (a query's ``text`` finds the claims that state a value, an id or a
phrase as written, ADR 0021), and gives ``neptune_entities`` and
``neptune_plan`` the document's declared identities. ``--planner anthropic`` lets
``neptune_plan`` ask the live model (the ``anthropic`` extra and credentials);
``--planner-recordings FILE`` replays recorded planner responses; without either, a plan says
no model is configured. A Ledger catalog is attached in code, as
``build_server(AsyncClient(LocalEngine(reader, catalog)))``: Context may construct no Ledger
implementation itself, so without one, series windows, frames and ``neptune_hydrate`` say so.
``--sources FILE`` reads a source index (``neptune_context.sources``, written by ``make demo``
from the Ledger's catalog): each answer then lists every cited source's corpus path and place, and
an unavailable ``neptune_hydrate`` says where the source is. Without it answers are as before.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from mcp.server.stdio import stdio_server

from neptune_context.engine import LocalEngine, read_graph_document
from neptune_context.explain.history import IndexedReader
from neptune_context.mcp.server import build_server
from neptune_context.query.model import Budget, Caller
from neptune_context.query.plan import (
    AnthropicClient,
    Defaults,
    ModelClient,
    ModelUnavailable,
    ReplayClient,
    load_recordings,
)
from neptune_context.retrieve.graph import GraphChannel
from neptune_context.retrieve.lexical import LexicalChannel, LexicalCorpus
from neptune_context.sdk import AsyncClient, NoModel, Planner, SdkError, StubEngine, entity_index
from neptune_context.sources import SourceIndex, read_source_index

if TYPE_CHECKING:
    from neptune_memory.schema.codec import GraphDocument
    from neptune_memory.schema.reader import MemoryReader

    from neptune_context.retrieve.channel import RetrievalChannel

TOKEN_ENV = "NEPTUNE_TOKEN"
# What an agent's planned query may spend by default: small, cited answers (ADR 0009 §4).
AGENT_DEFAULTS = Defaults(Caller.AGENT, budget=Budget(items=50, tokens=20_000))

ChannelFactory = Callable[["GraphDocument", "MemoryReader"], Sequence["RetrievalChannel"]]


def text_channel(document: GraphDocument, reader: MemoryReader) -> LexicalChannel:
    """The lexical channel over ``document`` (ADR 0021): every version of every claim indexed by
    its literal value as written (text, numbers, declared values, names) and the declared ids it
    names, read back from ``reader`` at each query's snapshot. Deterministic: no model, no
    embeddings, no network."""
    corpus = LexicalCorpus()
    corpus.add_claims(document.resolution.claims, through=document.head)
    return LexicalChannel(corpus, reader)


def graph_channels(document: GraphDocument, reader: MemoryReader) -> Sequence[RetrievalChannel]:
    """The channels ``--memory`` runs: the graph channel and the lexical channel over the
    document's claims (ADR 0021), as peers. A host with more channels (a catalog's passages, a
    vector index) passes another factory to ``local_client``; the tools do not change."""
    return (GraphChannel(reader), text_channel(document, reader))


def local_client(
    document: GraphDocument,
    *,
    channels: ChannelFactory = graph_channels,
    model: ModelClient | None = None,
    defaults: Defaults = AGENT_DEFAULTS,
) -> AsyncClient:
    """The client ``--memory`` serves: ``LocalEngine`` over ``document`` with ``channels``, and
    a planner over the document's declared identities (Demo v1's in-memory resolver). The
    reader is indexed by claim id, so ``neptune_why`` answers (ADR 0010)."""
    reader = IndexedReader(document)
    planner = Planner(entity_index(document), defaults, model or NoModel())
    return AsyncClient(LocalEngine(reader, channels=channels(document, reader)), planner=planner)


async def serve(
    client: AsyncClient, sources: SourceIndex | None = None, *, links: bool = True
) -> None:
    server = build_server(client, sources=sources, links=links)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _model(args: argparse.Namespace) -> ModelClient | None:
    if args.planner_recordings is not None:
        return ReplayClient(load_recordings(args.planner_recordings))
    if args.planner == "anthropic":
        return AnthropicClient()
    return None


def build_parser() -> argparse.ArgumentParser:
    """The command line (the sample ``.mcp.json`` is checked against it)."""
    parser = argparse.ArgumentParser(
        prog="neptune_context.mcp", description=(__doc__ or "Neptune MCP server").split("\n")[0]
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="base URL of a Neptune engine (https, or loopback http)")
    where.add_argument(
        "--memory", type=Path, help="Memory graph document (JSON, or .json.gz) to answer over"
    )
    where.add_argument("--packets", type=Path, help="directory of recorded packet JSON (fixtures)")
    parser.add_argument(
        "--sources",
        type=Path,
        help="source index (JSON) naming each cited source's corpus path (ADR 0016)",
    )
    planner = parser.add_mutually_exclusive_group()
    planner.add_argument(
        "--planner",
        choices=("none", "anthropic"),
        default="none",
        help="model behind neptune_plan with --memory (default: none)",
    )
    planner.add_argument(
        "--planner-recordings",
        type=Path,
        help="replay recorded planner responses (JSON Lines) instead of a live model",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.memory is None and (args.planner != "none" or args.planner_recordings is not None):
        parser.error("--planner and --planner-recordings need --memory (the planner's names)")
    try:
        sources = None if args.sources is None else read_source_index(args.sources)
        if args.url is not None:
            client = AsyncClient(args.url, token=os.environ.get(TOKEN_ENV) or None)
        elif args.memory is not None:
            client = local_client(read_graph_document(args.memory), model=_model(args))
        else:
            client = AsyncClient(StubEngine.from_directory(args.packets))
    except (SdkError, OSError, ValueError, ModelUnavailable) as error:
        sys.stderr.write(f"neptune mcp: {error}\n")
        return 2
    # ``--memory`` attaches no Ledger catalog, so a resource link could only answer that the
    # source is unavailable: its answers carry none (ADR 0020).
    anyio.run(partial(serve, client, sources, links=args.memory is None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
