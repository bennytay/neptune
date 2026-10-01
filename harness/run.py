"""``python -m harness``: check the contracts, flow the corpus through the stages, smoke-query.

Exit 0 only when ``contracts.py check --all`` passes, every stage is ``ok`` and the smoke query
returned a packet. The report (``<run dir>/report.json`` and ``report.md``) is written either way.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from harness import contracts, corpus, report, services
from harness.stages import STAGES, Context, Json, Outcome, Stage, resolve

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO: Final = Path(__file__).resolve().parents[1]
DEFAULT_RUN_DIR: Final = REPO / "harness" / ".run"


def _scrub(text: str, work: Path) -> str:
    """An error message with the scratch path removed, so the report stays path-free."""
    return text.replace(str(work), "<work>").replace(str(REPO), "<repo>")


def run_stage(stage: Stage, ctx: Context, *, services_up: bool, upstream_ok: bool) -> Json:
    """One stage's entry in the report; it never raises."""
    resolution = resolve(stage, ctx.registry)
    entry: Json = {
        "stage": stage.id,
        "package": stage.package,
        "contract": stage.contract,
        "contract_version": resolution.contract_version,
        "mode": resolution.mode,
        "reason": resolution.reason,
        "needs_services": stage.needs_services,
        "problems": [],
        "output": {},
    }
    if not upstream_ok:
        return {**entry, "status": "skipped", "problems": ["an upstream stage did not pass"]}
    driver = stage.real if resolution.mode == "real" and stage.real is not None else stage.stub
    if resolution.mode == "real" and stage.needs_services and not services_up:
        missing = ", ".join(services.unreachable()) or "the compose stack"
        message = f"{stage.id} runs for real and needs {missing}: {services.START_COMMAND}"
        return {**entry, "status": "error", "problems": [message]}
    try:
        outcome: Outcome = driver(ctx)
    except Exception as error:  # a stage error is a finding, not a crash (partial success)
        message = _scrub(f"{type(error).__name__}: {error}", ctx.work)
        return {**entry, "status": "error", "problems": [message]}
    ctx.upstream[stage.id] = outcome.output
    problems = [_scrub(p, ctx.work) for p in outcome.problems]
    return {
        **entry,
        "status": "failed" if problems else "ok",
        "problems": problems,
        "output": outcome.output,
    }


def run(
    run_dir: Path,
    *,
    contracts_root: Path | None = None,
    owner_tests: bool = True,
    corpus_root: Path | None = None,
    stages: Sequence[Stage] = STAGES,
) -> tuple[dict[str, Any], int]:
    """Run everything; returns (report, exit code) and writes the report files."""
    work = run_dir / "work"
    if work.exists():
        shutil.rmtree(work)  # only the harness's own scratch directory, never a source
    work.mkdir(parents=True)
    code, notes, problems, tail = contracts.run_check_all(contracts_root, owner_tests=owner_tests)
    registry = contracts.registry(contracts_root)
    corpus_name, cases = corpus.select(corpus_root)
    ctx = Context(registry=registry, work=work, cases=cases)
    up = not services.unreachable()  # only read when a real stage needs the services
    entries: list[Json] = []
    healthy = True
    for stage in stages:
        entry = run_stage(stage, ctx, services_up=up, upstream_ok=healthy)
        entries.append(entry)
        healthy = healthy and entry["status"] == "ok"
    smoke_source: Json = {}
    for entry in entries:
        if entry["stage"] == "context":
            smoke_source = entry["output"].get("smoke") or {}
    smoke = {
        "ok": bool(smoke_source.get("packet")),
        "query": smoke_source.get("query"),
        "packet_source": smoke_source.get(
            "packet_source", "no packet: the context stage did not run"
        ),
        "packet": smoke_source.get("packet"),
    }
    for entry in entries:
        entry["output"].pop("smoke", None)  # reported once, under "smoke"
    contracts_ok = code == 0
    document: dict[str, Any] = {
        "report_version": report.REPORT_VERSION,
        "ok": contracts_ok and healthy and smoke["ok"],
        "contracts": {
            "command": "scripts/contracts.py check --all" + ("" if owner_tests else " --no-tests"),
            "ok": contracts_ok,
            "exit_code": code,
            "notes": notes,
            "problems": problems,
            **({"output_tail": _scrub(tail, work)} if tail else {}),
        },
        "corpus": {"name": corpus_name, "cases": [case.id for case in cases]},
        "stages": entries,
        "smoke": smoke,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(report.render_json(document), encoding="utf-8")
    (run_dir / "report.md").write_text(report.render_markdown(document), encoding="utf-8")
    return document, 0 if document["ok"] else 1


def summary(document: dict[str, Any]) -> str:
    rows = [f"{s['stage']}: {s['mode']} {s['status']}" for s in document["stages"]]
    verdict = "green" if document["ok"] else "RED"
    return (
        f"harness {verdict} | contracts {'ok' if document['contracts']['ok'] else 'FAILED'} | "
        + " | ".join(rows)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run-dir", type=Path, default=DEFAULT_RUN_DIR, help="scratch + report dir"
    )
    parser.add_argument("--contracts", type=Path, help="a contracts/ dir other than the repo's")
    parser.add_argument("--corpus", type=Path, help="a folder of case folders to ingest")
    parser.add_argument("--no-owner-tests", action="store_true", help="skip owners' contract tests")
    parser.add_argument(
        "--compose", action="store_true", help="start the compose stack, then stop it"
    )
    args = parser.parse_args(argv)
    if args.compose and services.compose("up") != 0:
        sys.stderr.write("harness: docker compose up failed\n")
        services.compose("down")
        return 2
    try:
        document, code = run(
            args.run_dir.resolve(),
            contracts_root=args.contracts,
            owner_tests=not args.no_owner_tests,
            corpus_root=args.corpus,
        )
    finally:
        if args.compose:
            services.compose("down")
    sys.stdout.write(summary(document) + "\n")
    sys.stdout.write(f"report: {args.run_dir / 'report.json'}\n")
    return code
