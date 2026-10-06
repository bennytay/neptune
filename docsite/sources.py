"""What the site is made of: one section per layer, the API modules, the repository URL.

A section takes every Markdown file under its ``root`` (recursively), so a new document appears
without touching this file. ``lead`` only orders the section's first entries; the rest follow in
path order. Files under an ``adr/`` directory are the section's decisions (the index first), files
under ``reviews/`` its gate reviews.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

REPOSITORY_URL: Final = "https://github.com/bennytay/neptune"
# Links to repository files that are not pages point at this ref. A URL is text: the build never
# fetches it.
REPOSITORY_REF: Final = "main"


@dataclass(frozen=True)
class Section:
    """A layer's documents: ``layers/<slug>`` lists them.

    ``key`` are (title, repository path) pairs the page links first, for the decisions a reader
    needs before the rest (a query language, a packet format), which would otherwise sit in the
    ADR list only.
    """

    slug: str
    title: str
    summary: str
    root: str
    lead: tuple[str, ...] = ()
    key: tuple[tuple[str, str], ...] = ()


SECTIONS: Final = (
    Section(
        "compiler",
        "Compiler",
        "Turns raw robotics evidence (MCAP, ROS bags, flight logs, URDF, calibration, configs, "
        "PDFs, images, site records) into a canonical, provenance-preserving ingest package. It "
        "answers *what exactly exists in this evidence*, never what it means.",
        "docs",
        (
            "architecture.md",
            "ingestion-pipeline.md",
            "canonical-data-model.md",
            "provenance-and-identity.md",
            "adapter-contract.md",
            "cli.md",
            "sdk.md",
            "manifest.md",
            "security.md",
        ),
    ),
    Section(
        "ledger",
        "Ledger",
        "Layer 1: registers compiler packages and serves the catalog API (register, verify, "
        "resolve, threads, lineage, query) that every later layer reads.",
        "packages/neptune-ledger/docs",
        ("guarantees.md", "catalog-api.md", "catalog-walkthrough.md", "contracts.md"),
    ),
    Section(
        "memory",
        "Memory",
        "Layer 2: consolidates registered packages into a bi-temporal claim graph in which every "
        "claim cites its evidence and says whether it is observed, stated or inferred.",
        "packages/neptune-memory/docs",
        ("guarantees.md", "graph-schema.md", "contracts.md"),
    ),
    Section(
        "context",
        "Context",
        "Layer 3: turns a question into a query over the catalog and the claim graph and returns "
        "a context packet with provenance on every item, through an SDK and an MCP server.",
        "packages/neptune-context/docs",
        ("sdk.md", "contracts.md"),
        (
            ("The query language", "packages/neptune-context/docs/adr/0002-query-language.md"),
            (
                "The context packet format",
                "packages/neptune-context/docs/adr/0003-the-context-packet.md",
            ),
            (
                "The SDK and the MCP server",
                "packages/neptune-context/docs/adr/0004-sdk-and-mcp-server.md",
            ),
        ),
    ),
    Section(
        "deploy",
        "Deploy",
        "A compiler plugin for deployment lifecycle evidence (commissioning, interventions, "
        "maintenance, incidents, changes) and the evidence-pack compiler over a frozen Memory "
        "snapshot.",
        "packages/neptune-deploy/docs",
        ("samples/README.md", "contracts.md"),
        (
            (
                "Evidence packs",
                "packages/neptune-deploy/docs/adr/"
                "0013-evidence-packs-compile-cited-claims-from-a-frozen-memory-snapshot.md",
            ),
        ),
    ),
    Section(
        "platform",
        "Platform",
        "The cross-layer contracts registry, the integration harness, the acceptance corpus and "
        "this site.",
        "packages/neptune-platform/docs",
        ("harness.md", "acceptance-corpus.md", "contracts.md"),
    ),
)

# Repository Markdown outside the sections that the site also renders.
EXTRA_DOCUMENTS: Final = ("contracts/compatibility.md",)


@dataclass(frozen=True)
class ApiModule:
    """A public Python surface: the page ``api/<page>`` documents ``modules``.

    A facade module (one that re-exports its surface in ``__all__``) is documented through its
    ``__all__``, imported names included; any other module through the public names it defines.
    """

    page: str
    title: str
    summary: str
    modules: tuple[str, ...]
    facade: bool = True


_MEMORY_SCHEMA = ("nodes", "interval", "clocks", "clock_map", "claim", "predicates", "supersede")
_MEMORY_READ = ("reader", "reference", "traverse", "codec", "export")

API_MODULES: Final = (
    ApiModule(
        "neptune.sdk",
        "Compiler SDK",
        "Ingest from code, sync or async, without the CLI.",
        ("neptune.sdk",),
    ),
    ApiModule(
        "neptune_ledger.api",
        "Ledger catalog API",
        "The typed catalog protocol, its request and response records and their JSON form.",
        ("neptune_ledger.api",),
    ),
    ApiModule(
        "neptune_memory.schema",
        "Memory graph schema",
        "The claim model, its time model, the predicate vocabulary and the read protocol.",
        (
            "neptune_memory.schema",
            *(f"neptune_memory.schema.{name}" for name in (*_MEMORY_SCHEMA, *_MEMORY_READ)),
        ),
        facade=False,
    ),
    ApiModule(
        "neptune_context.sdk",
        "Context SDK",
        "Query, why, diff and hydrate over context packets, sync or async.",
        ("neptune_context.sdk",),
    ),
    ApiModule(
        "neptune_context.mcp",
        "Context MCP tools",
        "The read-only MCP server that exposes query and packets to agents.",
        ("neptune_context.mcp",),
    ),
    ApiModule(
        "neptune_deploy.packs",
        "Deploy evidence packs",
        "Compile a pack spec over a frozen Memory snapshot into cited claims, JSON and PDF.",
        ("neptune_deploy.packs",),
    ),
)

# The README section the quickstart page embeds: the first level-2 heading containing this word.
QUICKSTART_HEADING: Final = "quickstart"
