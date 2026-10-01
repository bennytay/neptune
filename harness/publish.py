"""Post a harness report to the Linear gate issues (a PR comment is a workflow step, not this).

``python -m harness.publish --report <report.json> [--link URL] [--only-failed]``

The comment goes to the current gate issue (``contracts/packages.toml``) of every package that owns
a stage, and of ``neptune-platform``, which owns the harness. It reuses ``post_comment`` from
``scripts/contracts.py`` (the Linear GraphQL API over stdlib ``urllib``). Without
``LINEAR_API_KEY`` it posts nothing and exits 0. ``--only-failed`` posts only a red report, which
is what the nightly schedule uses so a green night does not comment on every gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness import contracts, report

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

OWNER = "neptune-platform"


def gate_issues(
    document: Mapping[str, Any], packages: Mapping[str, Mapping[str, str]]
) -> list[str]:
    """The gate issues to comment on: stage owners' and the harness owner's, sorted, once each."""
    names = {stage["package"] for stage in document["stages"]} | {OWNER}
    issues = {
        packages[name]["gate_issue"] for name in names if "gate_issue" in packages.get(name, {})
    }
    return sorted(issues)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.publish", description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--link", help="URL of the run, appended to the comment")
    parser.add_argument("--only-failed", action="store_true", help="post only when the run is red")
    parser.add_argument("--dry-run", action="store_true", help="print the targets, post nothing")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    document = json.loads(args.report.read_text(encoding="utf-8"))
    key = env.get("LINEAR_API_KEY")
    if args.only_failed and document["ok"]:
        sys.stdout.write("harness.publish: green run, nothing to post\n")
        return 0
    if not key and not args.dry_run:
        sys.stdout.write("harness.publish: LINEAR_API_KEY is not set, nothing posted\n")
        return 0
    issues = gate_issues(document, contracts.registry().packages())
    body = report.render_markdown(document, args.link)
    failed = 0
    tool = contracts.load_tool()
    for issue in issues:
        if args.dry_run or not key:
            sys.stdout.write(f"harness.publish: would post to {issue}\n")
            continue
        try:
            tool.post_comment(issue, body, key)
            sys.stdout.write(f"harness.publish: posted to {issue}\n")
        except (tool.ContractError, OSError) as error:
            sys.stderr.write(f"harness.publish: {issue}: {error}\n")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
