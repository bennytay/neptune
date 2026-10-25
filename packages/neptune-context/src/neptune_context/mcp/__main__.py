"""``python -m neptune_context.mcp --site <site_dir>``: serve the site tools over stdio.

The tools answer over the site model folded from ``<site_dir>``'s fact log (``mcp.site``, site
model v3). Claude Code: ``claude mcp add neptune-site -- <the "MCP:" line make site-demo prints>``.
"""

from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from neptune_context.mcp.site import SiteTools, build_site_server


async def serve(site: SiteTools) -> None:
    server = build_site_server(site)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neptune_context.mcp", description=(__doc__ or "Neptune MCP server").split("\n")[0]
    )
    parser.add_argument(
        "--site",
        type=Path,
        required=True,
        help="site directory whose fact log the neptune_site_* tools answer over (site model v3)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    anyio.run(partial(serve, SiteTools(args.site)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
