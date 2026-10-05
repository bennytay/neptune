"""The evidence-pack compiler (ADR 0013): a ``PackSpec`` over a frozen Memory snapshot gives an
``EvidencePack`` of cited claims, rendered as canonical JSON and as a deterministic PDF.

Deploy reads Memory only through the published graph-schema contract (a graph document) and points
at the Ledger only through catalog-api request documents; it imports neither package.
"""

from neptune_deploy.packs.compile import (
    COMPILER_VERSION,
    PACK_SCHEMA,
    EvidencePack,
    builtin_registry,
    compile_pack,
    pack_id,
)
from neptune_deploy.packs.errors import PackError
from neptune_deploy.packs.render import render_claims, render_json, render_pdf
from neptune_deploy.packs.snapshot import Snapshot, load_snapshot, read_snapshot, snapshot_id
from neptune_deploy.packs.spec import SPEC_SCHEMA, PackSpec, load_spec, read_spec
from neptune_deploy.packs.templates import (
    TEMPLATE_SCHEMA,
    Template,
    TemplateRegistry,
    load_template,
    read_template,
)

__all__ = [
    "COMPILER_VERSION",
    "PACK_SCHEMA",
    "SPEC_SCHEMA",
    "TEMPLATE_SCHEMA",
    "EvidencePack",
    "PackError",
    "PackSpec",
    "Snapshot",
    "Template",
    "TemplateRegistry",
    "builtin_registry",
    "compile_pack",
    "load_snapshot",
    "load_spec",
    "load_template",
    "pack_id",
    "read_snapshot",
    "read_spec",
    "read_template",
    "render_claims",
    "render_json",
    "render_pdf",
    "snapshot_id",
]
