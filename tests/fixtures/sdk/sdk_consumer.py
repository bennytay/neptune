"""A robotics codebase integrating Neptune through the SDK: MVL-12's acceptance example.

``ingest_run`` is the whole integration a codebase needs: a few lines, no CLI, no subprocess.
The rest shows what a codebase reaches for next: a dry run before a long ingest, the async
client with live events and an adapter option, and branching on an error's stable code. The
SDK's tests run this file and check it with ``mypy --strict``.

Usage: ``python sdk_consumer.py RUN_FOLDER OUTPUT WORKSPACE``; prints one JSON line.
"""

import asyncio
import json
import sys
from pathlib import Path

from neptune.sdk import AsyncNeptune, IngestResult, JobEvent, JobOptions, Neptune, NeptuneError


def ingest_run(run_folder: Path, package: Path, workspace: Path) -> str:
    """Ingest one run folder into a package; the receipt's id."""
    result = Neptune(workspace).ingest(run_folder, package)
    if not result.committed or result.receipt is None:
        raise RuntimeError(f"the ingest ended {result.state}")
    return result.receipt


def preview(run_folder: Path, workspace: Path) -> dict[str, int]:
    """How many chunks each adapter would parse, without parsing any."""
    plan = Neptune(workspace).dry_run(run_folder)
    chunks: dict[str, int] = {}
    for source in plan.cache.sources:
        chunks[source.adapter] = chunks.get(source.adapter, 0) + len(source.chunks)
    return chunks


async def ingest_live(run_folder: Path, package: Path, workspace: Path) -> tuple[IngestResult, int]:
    """The async client, with the text adapter told to make a block of every line, counting
    chunks as they are committed."""
    committed = 0

    def on_event(event: JobEvent) -> None:
        nonlocal committed
        if event.kind == "chunk_committed":
            committed += 1

    options = JobOptions(config={"text": {"block_rule": "line"}})
    client = AsyncNeptune(workspace, options=options)
    result = await client.ingest(run_folder, package, on_event=on_event)
    return result, committed


def main(argv: list[str]) -> int:
    run_folder, output, workspace = (Path(arg) for arg in argv)
    try:
        chunks = preview(run_folder, workspace)
        receipt = ingest_run(run_folder, output / "package", workspace)
        by_line, committed = asyncio.run(ingest_live(run_folder, output / "by-line", workspace))
        findings = sorted(finding.code for finding in by_line.read_receipt().findings)
    except NeptuneError as error:
        sys.stderr.write(f"{error.code}: {error}\n")
        return 2
    summary = {
        "by_line": by_line.receipt,
        "chunks": chunks,
        "committed": committed,
        "findings": findings,
        "receipt": receipt,
    }
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
