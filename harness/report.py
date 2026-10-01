"""The report: one JSON document and its Markdown summary.

The body is a pure function of the repository, the corpus and the adapters' versions: sorted
keys, no wall-clock, no host, no absolute path, so two runs of the same checkout are byte-equal.
"""

from __future__ import annotations

import json
from typing import Any, Final

REPORT_VERSION: Final = 1
MARKER: Final = "<!-- neptune-harness-report -->"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_markdown(report: dict[str, Any], link: str | None = None) -> str:
    """A short comment for a PR or a Linear gate issue."""
    contracts = report["contracts"]
    smoke = report["smoke"]
    lines = [
        MARKER,
        f"### Integration harness: {'green' if report['ok'] else 'RED'}",
        "",
        f"- Contracts: {'ok' if contracts['ok'] else 'FAILED'} (`check --all`, "
        f"exit {contracts['exit_code']})",
        f"- Corpus: {report['corpus']['name']} ({len(report['corpus']['cases'])} cases)",
        "",
        "| Stage | Mode | Status | Contract | Why |",
        "|---|---|---|---|---|",
    ]
    for stage in report["stages"]:
        lines.append(
            f"| {stage['stage']} | {stage['mode']} | {stage['status']} | "
            f"{stage['contract']} {stage['contract_version'] or '-'} | {stage['reason']} |"
        )
    lines += ["", f"Smoke query: {'ok' if smoke['ok'] else 'FAILED'} ({smoke['packet_source']})"]
    problems = list(contracts["problems"])
    for stage in report["stages"]:
        problems += [f"{stage['stage']}: {p}" for p in stage["problems"]]
    if problems:
        lines += ["", "Problems:", *[f"- {problem}" for problem in problems]]
    if link:
        lines += ["", f"Run: {link}"]
    return "\n".join(lines) + "\n"
