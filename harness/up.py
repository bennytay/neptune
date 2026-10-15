"""``python -m harness.up FOLDER`` (``make up SRC=<folder>``): your own folder, through the whole
pipeline, into a folder Claude Code answers from.

The stages are the demo's (compiler ingest, Deploy's map, the Ledger's catalog, Memory's
consolidation, Context's source index), run over one folder of your files with no hand-written
setup:

- **Deploy** maps with every preset and document template Deploy ships. A preset's rule fires
  only on a table that has every column it ``requires``, so a table no preset fits stays as the
  compiler wrote it (a finding in the mapped receipt, never a guess). A preset or template that
  maps nothing is not a problem here, and a map that fails leaves the compiled package flowing on
  alone. ``--deploy FILE`` takes a declaration of your own instead (``harness/acceptance/
  deploy.json`` is one).
- **Memory** consolidates with no declaration unless ``--memory-config FILE`` names one.
- A ``neptune.yaml`` inside the folder is read by the compiler, as ``neptune ingest`` reads it
  (``neptune init-manifest FOLDER`` drafts one). Unless it states ``csv_header``, the compiler
  is told each CSV's first row is its header (``tabular`` ``csv_header=first_row``, in the
  package's transform config like any adapter option; ``--csv-header none`` says not); without
  it no mapping can name a column.

The folder is only read. Everything lands under ``--out`` (default ``up/<folder name>/``,
git-ignored), which may not be inside the folder:

- ``graph.json`` and ``sources.json``: the memory graph and its source index;
- ``up.json`` and ``up.md``: what each stage did and what memory now holds;
- ``claude/``: the folder to start Claude Code in (``.mcp.json`` serving the two files above,
  the ``neptune`` skill), as ``make demo-folder`` writes;
- ``work/``: the run's scratch (the packages, the Ledger's and Memory's files).

Exit 0 when every stage ran and memory was built; 1 when a stage failed; 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Final

from harness import contracts, demo_folder
from harness.corpus import REPO, Case
from harness.run import run_stage
from harness.stages import (
    DEPLOY_FORMAT,
    STAGES,
    Context,
    Json,
    Outcome,
    Stage,
    _shipped_presets,
    deploy_real,
    package_roots,
)
from neptune.manifest import MANIFEST_NAMES, ManifestError
from neptune.manifest.load import parse_bytes

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_ROOT: Final = REPO / "up"
TEMPLATES: Final = REPO / "packages/neptune-deploy/src/neptune_deploy/lifecycle/presets/templates"
# The deploy stage's problems that only say a declared preset or template found nothing to map.
# Over an unknown folder with every preset declared, most map nothing: that is the expected case.
_MAPPED_NOTHING: Final = re.compile(
    r"mapped no record$|wrote no lifecycle record or event-table row$"
    r"|records are \d+, the declaration needs at least \d+$"
)


def default_deploy() -> Json:
    """A deploy declaration naming every preset and document template Deploy ships."""
    shipped, _ = _shipped_presets()
    templates = sorted(p.relative_to(REPO).as_posix() for p in TEMPLATES.glob("*.json"))
    return {
        "deploy_format": DEPLOY_FORMAT,
        "description": "harness.up: every shipped preset and template; each maps what fits.",
        "presets": sorted(shipped),
        "templates": templates,
    }


def adapter_config(source: Path, csv_header: str) -> dict[str, dict[str, str]] | None:
    """``csv_header`` for the tabular adapter, unless the folder's manifest states it (or cannot be
    read, which the compiler reports): its statement wins and nothing is said twice."""
    for name in MANIFEST_NAMES:
        if (source / name).is_file():
            try:
                manifest = parse_bytes((source / name).read_bytes(), name)
            except (ManifestError, OSError):
                return None
            if "csv_header" in manifest.adapter_options().get("tabular", {}):
                return None
            break
    return {"tabular": {"csv_header": csv_header}}


def deploy_lenient(ctx: Context) -> Outcome:
    """The deploy stage, never red: what maps nothing, or fails to map, is a note, and the
    compiled package flows on alone (the ledger registers a mapped one only when it was written).
    """
    outcome = deploy_real(ctx)
    notes = [p for p in outcome.problems if not _MAPPED_NOTHING.search(p)]
    return Outcome({**outcome.output, "notes": notes})


def sources_index(ctx: Context) -> Outcome:
    """Context's source index over the graph Memory wrote: every file and place a claim cites."""
    from neptune_ledger.catalog.registry import PostgresCatalog

    from harness.agent import _source_index
    from harness.consolidate import graph_path
    from harness.stages import LEDGER_TENANT

    graph = graph_path(ctx)
    if not graph.is_file():
        return Outcome({}, ("the memory stage wrote no graph document",))
    if ctx.ledger_uri is None:
        return Outcome({}, ("the ledger stage kept no catalog to ask",))
    raw = json.loads(graph.read_bytes())
    with PostgresCatalog(
        ctx.ledger_uri, LEDGER_TENANT, package_roots=package_roots(ctx)
    ) as catalog:
        index, problems = _source_index(ctx, catalog, raw)
    del index
    return Outcome({"sources": "written"}, tuple(problems))


def stages() -> tuple[Stage, ...]:
    """The demo's stages, with the deploy stage lenient and the context stage building only the
    source index (there are no gold questions to ask of your folder)."""
    by_id = {stage.id: stage for stage in STAGES}
    return (
        by_id["compiler"],
        Stage(**{**by_id["deploy"].__dict__, "real": deploy_lenient}),
        by_id["ledger"],
        by_id["memory"],
        Stage(**{**by_id["context"].__dict__, "real": sources_index, "entry": None}),
    )


def holds(graph: Json) -> dict[str, int]:
    """How many distinct things of each node type memory's claims are about."""
    seen: dict[str, set[str]] = {}
    for claim in graph.get("claims", []):
        subject = claim.get("subject", {})
        seen.setdefault(str(subject.get("node_type")), set()).add(str(subject.get("node_id")))
    return {kind: len(ids) for kind, ids in sorted(seen.items())}


def summary_markdown(source: Path, entries: list[Json], held: dict[str, int], claims: int) -> str:
    lines = [f"# `make up` over {source.name}", "", "| stage | status | note |", "|---|---|---|"]
    for entry in entries:
        notes = entry["problems"] or entry["output"].get("notes") or []
        lines.append(f"| {entry['stage']} | {entry['status']} | {notes[0] if notes else ''} |")
    lines += ["", f"Memory holds {claims} claims about:", ""]
    lines += [f"- {kind}: {count}" for kind, count in held.items()] or ["- nothing"]
    return "\n".join(lines) + "\n"


def _refuse(source: Path, out: Path) -> str | None:
    if source.is_symlink() or not source.is_dir():
        return f"{source} is not a folder"
    if out.is_relative_to(source):
        return f"--out {out} is inside {source}: the folder you ingest is only ever read"
    if source.is_relative_to(out):
        return f"{source} is inside --out {out}, which is rewritten"
    return None


def up(
    source: Path,
    out: Path,
    *,
    deploy: Path | None = None,
    memory_config: Path | None = None,
    csv_header: str = "first_row",
    stream: object = sys.stderr,
) -> int:
    """Run the folder through the stages into ``out``; the exit code (see the module docstring)."""

    def say(text: str) -> None:
        if stream is not None:
            stream.write(text + "\n")  # type: ignore[attr-defined]
            stream.flush()  # type: ignore[attr-defined]

    work = out / "work"
    if work.exists():
        shutil.rmtree(work)  # only this tool's own scratch directory, never a source
    work.mkdir(parents=True)
    if deploy is None:
        deploy = work / "deploy.json"
        deploy.write_text(json.dumps(default_deploy(), indent=2) + "\n", encoding="utf-8")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name) or "folder"
    config = adapter_config(source, csv_header)
    case = Case(name, source, deploy=deploy, memory=memory_config, adapter_config=config)
    ctx = Context(registry=contracts.registry(), work=work, cases=[case])
    entries: list[Json] = []
    healthy = True
    run = stages()
    begun = time.monotonic()
    try:
        for index, stage in enumerate(run, start=1):
            started = time.monotonic()
            say(f"[{index}/{len(run)}] {stage.id}…")
            entry = run_stage(stage, ctx, services_up=True, upstream_ok=healthy)
            entries.append(entry)
            why = f": {entry['problems'][0]}" if entry["problems"] else ""
            say(f"      {entry['status']} {time.monotonic() - started:.1f} s{why}")
            healthy = healthy and entry["status"] == "ok"
    finally:
        ctx.close()
    graph_file, index_file = work / "memory" / "graph.json", work / "context" / "sources.json"
    graph: Json = json.loads(graph_file.read_bytes()) if graph_file.is_file() else {}
    held = holds(graph)
    claims = len(graph.get("claims", []))
    document = {
        "ok": healthy,
        "source": source.name,
        "stages": entries,
        "memory": {
            "claims": claims,
            "holds": held,
            "predicates": dict(
                sorted(Counter(c["predicate"] for c in graph.get("claims", [])).items())
            ),
        },
    }
    (out / "up.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    (out / "up.md").write_text(summary_markdown(source, entries, held, claims), encoding="utf-8")
    if not healthy or not index_file.is_file():
        say(f"up: RED after {time.monotonic() - begun:.0f} s; see {out / 'up.md'}")
        return 1
    shutil.copyfile(graph_file, out / "graph.json")
    shutil.copyfile(index_file, out / "sources.json")
    claude = out / "claude"
    demo_folder.write(claude, demo=out)
    shown = ", ".join(f"{count} {kind}" for kind, count in held.items()) or "nothing"
    say(f"up: green in {time.monotonic() - begun:.0f} s; memory holds {claims} claims ({shown})")
    say(f"ask it: cd {claude} && claude")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.up", description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="the folder of your files (only read)")
    parser.add_argument("--out", type=Path, help="where to write (default: up/<folder name>)")
    parser.add_argument("--deploy", type=Path, help="a deploy declaration instead of every preset")
    parser.add_argument("--memory-config", type=Path, help="Memory's consolidator declaration")
    parser.add_argument(
        "--csv-header",
        choices=("first_row", "none"),
        default="first_row",
        help="whether each CSV's first row is its header, when the folder has no manifest",
    )
    args = parser.parse_args(argv)
    source = args.folder.expanduser().resolve()
    out = (args.out or DEFAULT_ROOT / source.name).expanduser().resolve()
    refused = _refuse(source, out)
    for given in (args.deploy, args.memory_config):
        if given is not None and not given.is_file():
            refused = refused or f"{given} is not a file"
    if refused is not None:
        sys.stderr.write(f"up: {refused}\n")
        return 2
    try:
        return up(
            source,
            out,
            deploy=args.deploy.resolve() if args.deploy else None,
            memory_config=args.memory_config.resolve() if args.memory_config else None,
            csv_header=args.csv_header,
        )
    except demo_folder.Refused as error:
        sys.stderr.write(f"up: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
