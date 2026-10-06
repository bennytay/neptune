"""Golden why and diff packets (ADR 0010): the local engine's answers over the explain fixtures.

Four persona questions, across a manipulator's wrist camera, an autonomous truck, a legged robot
and a mobile robot, each answered by ``LocalEngine`` at transaction 4 with the fixtures' Ledger.
They are part of the ``query-packet`` contract's goldens (``contracts/query-packet/goldens.py``),
so a consumer sees a packet with trails validated against the schema. Run this file to
regenerate ``tests/golden/trails/``; the tests fail when the files drift from the engine.
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import explain_fixtures_context as X
from neptune_context.engine import LocalEngine
from neptune_context.explain import IndexedReader
from neptune_context.packets.codec import canonical_bytes
from neptune_context.query import Budget, CivilTime, Instant, Query, Subject, Why, to_json
from neptune_context.query.model import Diff
from neptune_context.sdk import Client

HERE: Final = Path(__file__).resolve().parent
TRAILS: Final = HERE / "golden" / "trails"
UTC_NS: Final = CivilTime("utc", "unix", Fraction(1, 10**9))


def questions() -> dict[str, Query]:
    drift = X.find(X.WCAM, "drift")
    yard = X.find(X.TRUCK, "located_at", X.YARD)
    leg = Subject("machine", "asset-tag:LEG-9")
    amr = Subject("machine", "asset-tag:AMR-9")
    return {
        "trail-w1-auditor-why-wrist-camera-drift": Query(
            include_inferred=False, budget=Budget(items=10), as_of=4, explain=(Why(drift.id),)
        ),
        "trail-w2-safety-lead-why-the-truck-is-in-the-yard": Query(
            include_inferred=True, budget=Budget(items=10), as_of=4, explain=(Why(yard.id),)
        ),
        "trail-d1-fleet-engineer-legged-firmware-change": Query(
            include_inferred=False,
            budget=Budget(items=10),
            as_of=4,
            explain=(Diff(leg, Instant(UTC_NS, X.APR_1), Instant(UTC_NS, X.JUN_1)),),
        ),
        "trail-d2-fleet-engineer-what-memory-learned-about-the-amr": Query(
            include_inferred=False,
            budget=Budget(items=10),
            as_of=4,
            subjects=frozenset({amr}),
            explain=(Diff(amr, 1, 2),),
        ),
    }


def pretty(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def files() -> dict[Path, bytes]:
    client = Client(LocalEngine(IndexedReader(X.document()), X.Catalog()))
    out: dict[Path, bytes] = {}
    for name, query in questions().items():
        packet = client.query(query)
        out[TRAILS / f"query.{name}.json"] = pretty(to_json(query))
        out[TRAILS / f"packet.{name}.json"] = pretty(json.loads(canonical_bytes(packet)))
    return out


if __name__ == "__main__":
    TRAILS.mkdir(parents=True, exist_ok=True)
    for path, data in files().items():
        path.write_bytes(data)
        sys.stdout.write(f"{path.relative_to(HERE)}\n")
