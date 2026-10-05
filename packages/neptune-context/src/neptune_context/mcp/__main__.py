"""``python -m neptune_context.mcp``: serve the Neptune tools to an agent over stdio (ADR 0004 §6).

    python -m neptune_context.mcp --url https://neptune.example   # token from $NEPTUNE_TOKEN
    python -m neptune_context.mcp --memory graph.json             # the local engine (ADR 0007)
    python -m neptune_context.mcp --packets tests/golden/packets  # fixtures: recorded answers only

Claude Code: ``claude mcp add neptune -- python -m neptune_context.mcp --memory graph.json``, or
the sample ``.mcp.json`` and skill in ``packages/neptune-context/claude/`` (ADR 0009 §6).
``--memory`` answers real queries with ``LocalEngine`` over a Memory graph document (read by
Memory's strict codec into its reference reader), and gives ``neptune_entities`` and
``neptune_plan`` the document's declared identities. ``--planner anthropic`` lets
``neptune_plan`` ask the live model (the ``anthropic`` extra and credentials);
``--planner-recordings FILE`` replays recorded planner responses; without either, a plan says
no model is configured. A Ledger catalog is attached in code, as
``build_server(AsyncClient(LocalEngine(reader, catalog)))``: Context may construct no Ledger
implementation itself, so without one, series windows, frames and ``neptune_hydrate`` say so.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from mcp.server.stdio import stdio_server
from neptune_memory.schema.reference import ReferenceReader

from neptune_context.engine import LocalEngine, read_graph_document
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
from neptune_context.sdk import AsyncClient, NoModel, Planner, SdkError, StubEngine, entity_index

if TYPE_CHECKING:
    from neptune_memory.schema.codec import GraphDocument
    from neptune_memory.schema.reader import MemoryReader

    from neptune_context.retrieve.channel import RetrievalChannel

TOKEN_ENV = "NEPTUNE_TOKEN"
# What an agent's planned query may spend by default: small, cited answers (ADR 0009 §4).
AGENT_DEFAULTS = Defaults(Caller.AGENT, budget=Budget(items=50, tokens=20_000))

ChannelFactory = Callable[["GraphDocument", "MemoryReader"], Sequence["RetrievalChannel"]]


def graph_channels(document: GraphDocument, reader: MemoryReader) -> Sequence[RetrievalChannel]:
    """The channels ``--memory`` runs: the graph channel. A channel that indexes the document
    itself (MVL-142's lexical channel needs the document export, ADR 0008) joins by passing
    another factory to ``local_client``; the tools do not change."""
    del document
    return (GraphChannel(reader),)


def local_client(
    document: GraphDocument,
    *,
    channels: ChannelFactory = graph_channels,
    model: ModelClient | None = None,
    defaults: Defaults = AGENT_DEFAULTS,
) -> AsyncClient:
    """The client ``--memory`` serves: ``LocalEngine`` over ``document`` with ``channels``, and
    a planner over the document's declared identities (Demo v1's in-memory resolver)."""
    reader = ReferenceReader(document)
    planner = Planner(entity_index(document), defaults, model or NoModel())
    return AsyncClient(LocalEngine(reader, channels=channels(document, reader)), planner=planner)


async def serve(client: AsyncClient) -> None:
    server = build_server(client)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _model(args: argparse.Namespace) -> ModelClient | None:
    if args.planner_recordings is not None:
        return ReplayClient(load_recordings(args.planner_recordings))
    if args.planner == "anthropic":
        return AnthropicClient()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neptune_context.mcp", description=(__doc__ or "Neptune MCP server").split("\n")[0]
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="base URL of a Neptune engine (https, or loopback http)")
    where.add_argument("--memory", type=Path, help="Memory graph document (JSON) to answer over")
    where.add_argument("--packets", type=Path, help="directory of recorded packet JSON (fixtures)")
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
    args = parser.parse_args(argv)
    try:
        if args.url is not None:
            client = AsyncClient(args.url, token=os.environ.get(TOKEN_ENV) or None)
        elif args.memory is not None:
            client = local_client(read_graph_document(args.memory), model=_model(args))
        else:
            client = AsyncClient(StubEngine.from_directory(args.packets))
    except (SdkError, OSError, ValueError, ModelUnavailable) as error:
        sys.stderr.write(f"neptune mcp: {error}\n")
        return 2
    anyio.run(serve, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
