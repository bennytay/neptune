"""``python -m neptune_context.bench run|summary``.

run      ask a question set through the arms and append one JSON line per answer
summary  per-arm counts of one or more results files, citations checked again; no model
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from neptune_context.bench import agent, questions, run

ROOT = questions.ROOT / "bench"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neptune_context.bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="run a question set through the arms")
    go.add_argument("--set", default="gold")
    go.add_argument("--sites", default="", help="comma-separated; default: the set's sites")
    go.add_argument("--arms", default=",".join(run.ARMS))
    go.add_argument("--only", default="", help="comma-separated question ids")
    go.add_argument("--model", default=run.Settings.model)
    go.add_argument("--effort", default=run.Settings.effort)
    go.add_argument("--judge-model", default=run.Settings.judge_model)
    go.add_argument("--max-tool-calls", type=int, default=run.Settings.max_tool_calls)
    go.add_argument("--workers", type=int, default=run.Settings.workers)
    go.add_argument("--root", type=Path, default=ROOT / "sites")
    go.add_argument("--out", type=Path)
    again = sub.add_parser("rejudge", help="grade recorded answers again against a fixed key")
    again.add_argument("results", type=Path)
    again.add_argument("--set", default="gold")
    again.add_argument("--judge-model", default=run.Settings.judge_model)
    again.add_argument("--root", type=Path, default=ROOT / "sites")
    show = sub.add_parser("summary", help="per-arm counts of results files")
    show.add_argument("results", type=Path, nargs="+")
    show.add_argument("--root", type=Path, default=ROOT / "sites")
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "run":
        sites = tuple(args.sites.split(",")) if args.sites else None
        qs = questions.load(args.set, sites, args.root)
        if args.only:
            wanted = set(args.only.split(","))
            qs = [q for q in qs if q.id in wanted]
        out = args.out or ROOT / "results" / f"{args.set}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        settings = run.Settings(
            model=args.model,
            effort=args.effort,
            judge_model=args.judge_model,
            max_tool_calls=args.max_tool_calls,
            workers=args.workers,
        )
        api = agent.client(os.environ.get("ANTHROPIC_API_KEY"))
        sys.stdout.write(f"{len(qs)} questions x {args.arms} -> {out}\n")
        run.run(api, qs, tuple(args.arms.split(",")), args.root, out, settings)
        records = run.load([out])
    elif args.command == "rejudge":
        records = run.load([args.results])
        sets = {r["site"] for r in records}
        qs = questions.load(args.set, tuple(sorted(sets)), args.root)
        api = agent.client(os.environ.get("ANTHROPIC_API_KEY"))
        done = run.rejudge(api, records, qs, args.judge_model)
        args.results.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        sys.stdout.write(f"graded {done} answers again\n")
    else:
        records = run.load(args.results)
    run.recheck(records, args.root)
    summary = run.summarise(records)
    as_json = getattr(args, "json", False)
    sys.stdout.write((json.dumps(summary, indent=2) if as_json else run.table(summary)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
