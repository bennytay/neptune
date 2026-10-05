"""MCP server exposing query and packets to agents as read-only tools (ADR 0004).

``build_server(client)`` returns a low-level ``mcp`` server whose tools (``neptune_query``,
``neptune_why``, ``neptune_diff``, ``neptune_hydrate``) answer through an SDK ``AsyncClient``;
``python -m neptune_context.mcp`` serves it over stdio. Named for the protocol; the third-party
``mcp`` distribution is imported by absolute name and nothing here shadows it.
"""

from neptune_context.mcp.server import (
    TOOLS,
    build_server,
    evidence_uri,
    input_schemas,
    parse_evidence_uri,
    query_from_arguments,
)

__all__ = [
    "TOOLS",
    "build_server",
    "evidence_uri",
    "input_schemas",
    "parse_evidence_uri",
    "query_from_arguments",
]
