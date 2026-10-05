"""``python -m neptune_context.mcp``: serve the Neptune tools to an agent over stdio (ADR 0004 §6).

    python -m neptune_context.mcp --url https://neptune.example   # token from $NEPTUNE_TOKEN
    python -m neptune_context.mcp --packets tests/golden/packets  # fixtures: recorded answers only

Claude Code: ``claude mcp add neptune -- python -m neptune_context.mcp --url ...``. The
in-process engine over a local Ledger and Memory arrives with C2; it plugs in as
``build_server(AsyncClient(engine))`` without a change to the tools.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from neptune_context.mcp.server import build_server
from neptune_context.sdk import AsyncClient, SdkError, StubEngine

TOKEN_ENV = "NEPTUNE_TOKEN"


async def serve(client: AsyncClient) -> None:
    server = build_server(client)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neptune_context.mcp", description=__doc__.split("\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--url", help="base URL of a Neptune engine (https, or loopback http)")
    where.add_argument("--packets", type=Path, help="directory of recorded packet JSON (fixtures)")
    args = parser.parse_args(argv)
    try:
        if args.url is not None:
            client = AsyncClient(args.url, token=os.environ.get(TOKEN_ENV) or None)
        else:
            client = AsyncClient(StubEngine.from_directory(args.packets))
    except SdkError as error:
        sys.stderr.write(f"neptune mcp: {error}\n")
        return 2
    anyio.run(serve, client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
