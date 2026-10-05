#!/usr/bin/env python3
"""Re-record the planner's golden responses against the live Anthropic API (ADR 0005 §7).

NOT run in CI: it needs network access and ``ANTHROPIC_API_KEY`` (or an ``ant auth login``
profile), spends real tokens (one request per ``model`` case, about 90) and its output is
not byte-reproducible. Run it from the repository root when the prompt template, the query schema
or the model changes, and commit the result with an explanation::

    uv run --all-packages --extra anthropic python packages/neptune-context/scripts/record_planner_golden.py

Cases whose ``source`` is ``model`` get a fresh live response and are recorded ``live``; cases
whose ``source`` is ``scripted`` keep their fixed bad response (they test the planner's checks,
which a good model would not trigger). The script rewrites ``recordings.jsonl`` and prints the
pass rate; a live pass rate below the synthetic one is the number to read.
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "tests"))

from planner_golden_context import GOLDEN  # noqa: E402

from neptune_context.eval import planner_golden as pg  # noqa: E402
from neptune_context.query.plan import (  # noqa: E402
    AnthropicClient,
    ModelUnavailable,
    RecordingClient,
    build_request,
    dump_recordings,
    load_recordings,
    plan,
)


def main() -> int:
    index, profiles = pg.load_world(GOLDEN)
    kept = load_recordings(GOLDEN / "recordings.jsonl")
    try:
        client = RecordingClient(AnthropicClient())
    except ModelUnavailable as error:
        print(f"cannot record: {error}", file=sys.stderr)
        return 2
    cases = pg.load_cases(GOLDEN)
    stale = {
        build_request(c.question, c.as_of, profiles[c.profile], index.find(c.question, as_of=None)).sha256
        for c in cases
        if c.source == "model"
    }
    recordings = {sha: r for sha, r in kept.items() if sha not in stale}
    for case in cases:
        if case.source != "model":
            continue
        snapshot = None if case.as_of == "head" else int(case.as_of)
        mentions = index.find(case.question, as_of=snapshot)
        request = build_request(case.question, case.as_of, profiles[case.profile], mentions)
        try:
            response = client.complete(request)
        except ModelUnavailable as error:
            print(f"{case.id}: {error}", file=sys.stderr)
            return 1
        print(f"{case.id}: recorded ({response.stop})")
    for recording in client.recordings:
        recordings[recording.request_sha256] = recording
    (GOLDEN / "recordings.jsonl").write_text(dump_recordings(recordings.values()), encoding="utf-8")
    print(pg.run(GOLDEN).summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
