"""``python -m harness.demo`` (``make demo``): Demo v1 in one command (Platform ADR 0011).

The acceptance corpus runs through the harness with every stage real: compiler ingest, Deploy's
map, the Ledger's catalog, Memory's consolidation (byte-identical to Memory's committed snapshot)
and Context's agent tools answering the gold questions, checked by the claims they cite. Deploy
then renders two evidence packs over the graph just built: the incident reconstruction of
INC-C3-0011 and ARM-3A's configuration traceability report.

Everything lands in the demo directory (default ``demo/``, git-ignored), and nothing in it depends
on the clock or the host, so two runs give the same bytes:

- ``incident-timeline-INC-C3-0011.pdf`` and ``configuration-traceability-ARM-3A.pdf``, with each
  pack's ``pack.json``, ``claims.json`` and spec under ``packs/<name>/``;
- ``graph.json``: Memory's graph document, what ``python -m neptune_context.mcp --memory`` serves;
- ``answers.json`` (every tool call and the cited text it returned) and ``answers.md`` (the same,
  to read);
- ``report.json`` and ``report.md`` (the harness report) and ``demo.md`` (this run in short);
- ``work/``: the run's scratch (the corpus, the packages, the Ledger's and Memory's files).

``--pin`` rewrites ``harness/acceptance/answers.json`` from the answers instead of checking it
(``make demo-pin``); review its diff. Exit 0 only when the harness is green and both packs render.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from harness import run
from harness.corpus import REPO

if TYPE_CHECKING:
    from collections.abc import Sequence

    from harness.stages import Json

DEFAULT_DIR: Final = REPO / "demo"
PACK_TIMEOUT_S: Final = 600
SPEC_SCHEMA: Final = "neptune-deploy.pack-spec/1"
# The incident the demo reconstructs, as the arm-cell incident report identifies it.
INCIDENT: Final = {"namespace": "incident_report.incident", "value": "INC-C3-0011"}
# The machine whose configuration it traces, by the id the hand-over's run sheet declares.
MACHINE: Final = "manifest:ARM-3A"


@dataclass(frozen=True)
class Pack:
    name: str
    template: str
    version: int
    spec: Json | None
    problem: str | None = None


def _known(slot: Any) -> Any:
    return (
        slot.get("value") if isinstance(slot, dict) and slot.get("knowledge") == "known" else None
    )


def _identifiers(record: Json) -> list[Any]:
    listed = record.get("identifiers")
    items = _known(listed) if isinstance(listed, dict) else listed
    out = []
    for item in items if isinstance(items, list) else []:
        out.append(_known(item) if isinstance(item, dict) and "knowledge" in item else item)
    return out


def _span(claims: Sequence[Json], nodes: set[str]) -> tuple[str, int, int] | None:
    """The clock most of ``nodes``' claims are valid on, and their first and last tick on it."""
    stamps = [
        c["valid"]["start"]
        for c in claims
        if c["subject"].get("node_id") in nodes and isinstance(c.get("valid"), dict)
    ]
    if not stamps:
        return None
    clocks = Counter(s["domain_id"] for s in stamps)
    clock = sorted(clocks, key=lambda d: (-clocks[d], d))[0]
    ticks = [s["ticks"] for s in stamps if s["domain_id"] == clock]
    return clock, min(ticks), max(ticks)


def incident_pack(document: Json, mapped: Path, snapshot: str) -> Pack:
    """The reconstruction of ``INCIDENT``: its event node is Memory's node for the incident record
    Deploy mapped from the report (``record:<id>``), and the pack clock is the one its timeline
    entries are stated on, from the first entry to the last."""
    from harness.acceptance.resolve import Package

    name = f"incident-timeline-{INCIDENT['value']}"
    records = [r for r in Package(mapped).kind("incident_record") if INCIDENT in _identifiers(r)]
    if len(records) != 1:
        return Pack(name, "incident-timeline", 2, None, f"{len(records)} incident records name it")
    node = f"record:{records[0]['id']}"
    entries = {
        str(c["subject"]["node_id"])
        for c in document["claims"]
        if str(c["subject"].get("node_id", "")).startswith(f"{node}/timeline/")
    }
    span = _span(document["claims"], entries)
    if span is None:
        return Pack(name, "incident-timeline", 2, None, "Memory holds no timeline entry of it")
    clock, first, last = span
    interval = {
        "end": {"domain_id": clock, "ticks": last + 1},
        "start": {"domain_id": clock, "ticks": first},
    }
    return Pack(
        name,
        "incident-timeline",
        2,
        _spec("incident-timeline", 2, "event", node, interval, snapshot),
    )


def traceability_pack(document: Json, snapshot: str) -> Pack:
    """``MACHINE``'s configuration traceability, on the clock of its latest run (each run is on
    its own clock; the others are listed apart, never compared)."""
    name = f"configuration-traceability-{MACHINE.rsplit(':', 1)[-1]}"
    runs = {
        str(c["subject"]["node_id"])
        for c in document["claims"]
        if c["predicate"] == "recorded_by" and c["object"].get("node_id") == MACHINE
    }
    stamps = [
        c["valid"]["start"]
        for c in document["claims"]
        if c["subject"].get("node_id") in runs and isinstance(c.get("valid"), dict)
    ]
    if not stamps:
        return Pack(name, "configuration-traceability", 2, None, f"no run is recorded by {MACHINE}")
    latest = max(stamps, key=lambda s: (s["ticks"], s["domain_id"]))
    interval = {"end": "open", "start": {"domain_id": latest["domain_id"], "ticks": 0}}
    spec = _spec("configuration-traceability", 2, "machine", MACHINE, interval, snapshot)
    return Pack(name, "configuration-traceability", 2, spec)


def _spec(
    template: str, version: int, node_type: str, node_id: str, interval: Json, snapshot: str
) -> Json:
    return {
        "inference": "exclude",
        "interval": interval,
        "schema": SPEC_SCHEMA,
        "snapshot": snapshot,
        "subject": {"node_id": node_id, "node_type": node_type},
        "template": {"id": template, "version": version},
    }


def render(pack: Pack, graph: Path, out: Path) -> tuple[Json, list[str]]:
    """``python -m neptune_deploy pack`` (Deploy's published interface) for one pack; its PDF is
    copied to ``<out>/<name>.pdf``."""
    row: Json = {"name": pack.name, "template": f"{pack.template}@{pack.version}"}
    if pack.spec is None:
        return row, [f"{pack.name}: {pack.problem}"]
    where = out / "packs" / pack.name
    if where.exists():
        shutil.rmtree(where)  # the demo's own output; Deploy refuses to overwrite other bytes
    where.mkdir(parents=True)
    spec = where / "spec.json"
    spec.write_text(json.dumps(pack.spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    argv = [sys.executable, "-m", "neptune_deploy", "pack", "--spec", str(spec)]
    argv += ["--snapshot", str(graph), "-o", str(where)]
    done = subprocess.run(
        argv, cwd=REPO, capture_output=True, text=True, timeout=PACK_TIMEOUT_S, check=False
    )
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        return row, [f"{pack.name}: neptune_deploy pack exited {done.returncode}: {last}"]
    pdf = (where / "pack.pdf").read_bytes()
    problems = (
        []
        if pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-1024:]
        else [f"{pack.name}: pack.pdf is not a PDF"]
    )
    (out / f"{pack.name}.pdf").write_bytes(pdf)
    built = json.loads((where / "pack.json").read_bytes())
    row.update(
        pack=built.get("id"),
        pdf=f"{pack.name}.pdf",
        pdf_bytes=len(pdf),
        pdf_pages=len(re.findall(rb"/Type\s*/Page(?!s)", pdf)),
        subject=pack.spec["subject"],
    )
    return row, problems


def answers_markdown(transcript: Json) -> str:
    """Every gold question with the calls made and the cited text each returned."""
    lines = [
        "# Demo v1: gold questions answered through Neptune's MCP tools",
        "",
        "Each answer is what the `neptune` MCP server returns to an agent. Every fact ends with",
        "its citations: `[I<n>]` is the item (the Items footer gives the claim id), `[E<k>]` the",
        "source (the Evidence footer gives the exact evidence ref). Supported gold claims and",
        "pinned gaps are checked by those ids, never by the text (Platform ADR 0011).",
        "",
    ]
    for question in transcript.get("questions", []):
        lines += [f"## {question['id']}: {question['question']}", ""]
        lines += [f'Asked as: "{question["asked_as"]}".', ""]
        supported = ", ".join(sorted(question["supported"])) or "none"
        lines += [f"Gold claims supported by cited claims: {supported}.", ""]
        for gap, why in sorted(question["gaps"].items()):
            lines += [f"- Gap {gap}: {why['reason']}"]
        lines += [""]
        for number, call in enumerate(question["calls"], start=1):
            arguments = json.dumps(call["arguments"], sort_keys=True)
            lines += [f"### Call {number}: `{call['tool']}`", "", "```json", arguments, "```", ""]
            lines += ["```text", call["text"].rstrip("\n"), "```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def summary_markdown(document: Json, packs: list[Json], problems: list[str]) -> str:
    stages = {s["stage"]: s for s in document["stages"]}
    memory = stages.get("memory", {}).get("output", {})
    context = stages.get("context", {}).get("output", {})
    answered = context.get("answers", {})
    supported = sum(len(a["supported"]) for a in answered.values())
    gaps = sum(len(a["gaps"]) for a in answered.values())
    lines = [
        f"# Demo v1: {'green' if not problems else 'RED'}",
        "",
        f"- Corpus: {document['corpus']['name']}, tree `{document['corpus'].get('tree')}`",
        "- Stages: "
        + ", ".join(f"{s['stage']} {s['mode']} {s['status']}" for s in document["stages"]),
        f"- Graph: `graph.json`, graph-schema {memory.get('graph_schema')}, "
        f"{memory.get('claims')} claims, generation `{memory.get('generation')}`; "
        f"Memory's committed snapshot: {memory.get('snapshot', {}).get('verdict', 'not compared')}",
        f"- Gold questions asked: {len(answered)}; gold claims supported by cited claims: "
        f"{supported}; pinned gaps: {gaps} (`answers.md`)",
        f"- MCP over stdio: {context.get('stdio', {}).get('same_as_in_process')}",
    ]
    lines += [
        f"- Pack `{p['name']}.pdf`: {p.get('pdf_bytes', 0)} bytes, {p.get('pack')}" for p in packs
    ]
    if problems:
        lines += ["", "Problems:", *[f"- {p}" for p in problems]]
    return "\n".join(lines) + "\n"


def demo(out: Path, *, pin: bool = False, owner_tests: bool = False) -> tuple[int, list[str]]:
    """Run the demo into ``out``; (exit code, problems)."""
    out.mkdir(parents=True, exist_ok=True)
    document, code = run.run(out, owner_tests=owner_tests, pin_answers=pin)
    problems: list[str] = [] if code == 0 else [f"the harness is red: {run.summary(document)}"]
    work = out / "work"
    ctx_graph = work / "memory" / "graph.json"
    packs: list[Json] = []
    if ctx_graph.is_file():
        shutil.copyfile(ctx_graph, out / "graph.json")
        graph = json.loads((out / "graph.json").read_bytes())
        from neptune_deploy.packs import snapshot_id

        case = f"acceptance-{_corpus_version(document)}"
        snapshot = snapshot_id(graph)
        for pack in (
            incident_pack(graph, work / "packages" / f"{case}.deploy", snapshot),
            traceability_pack(graph, snapshot),
        ):
            row, found = render(pack, out / "graph.json", out)
            packs.append(row)
            problems += found
    else:
        problems.append("the memory stage wrote no graph: nothing to render")
    answers = work / "context" / "answers.json"
    if answers.is_file():
        shutil.copyfile(answers, out / "answers.json")
        transcript = json.loads(answers.read_text(encoding="utf-8"))
        (out / "answers.md").write_text(answers_markdown(transcript), encoding="utf-8")
    else:
        problems.append("the context stage wrote no answers")
    (out / "demo.md").write_text(summary_markdown(document, packs, problems), encoding="utf-8")
    return (0 if not problems else 1), problems


def _corpus_version(document: Json) -> str:
    return str(document["corpus"].get("version") or document["corpus"]["name"].split()[-1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.demo", description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_DIR, help="demo directory (default demo/)"
    )
    parser.add_argument(
        "--pin", action="store_true", help="rewrite harness/acceptance/answers.json"
    )
    parser.add_argument(
        "--owner-tests", action="store_true", help="also run every owner's contract tests"
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    code, problems = demo(out, pin=args.pin, owner_tests=args.owner_tests)
    shown = out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out
    sys.stdout.write((out / "demo.md").read_text(encoding="utf-8"))
    sys.stdout.write(f"\nOutputs in {shown}/ (open {shown}/incident-timeline-INC-C3-0011.pdf)\n")
    for problem in problems:
        sys.stderr.write(f"demo: {problem}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
