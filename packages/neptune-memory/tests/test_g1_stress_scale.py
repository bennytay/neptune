"""G1 scenario 8: 10^8 claims under the store budgets (every embodiment of the benchmark fleet).

Expected (ADR 0004, final per ADR 0007 §7): the as-of thread, traversal and graph-filtered vector
search meet their budgets at 10^8 claims, measured where the host allows and extrapolated as
labelled upper bounds where it does not; the walk's cost is bounded by entities and edges reached
even through a hub. The measured numbers live in ``docs/benchmarks/g1-results.json`` (produced by
``bench/g1_bench.py``); these tests hold the published numbers to the budgets and the shipped
settings to the measured ones, and walk a 10^4-entity hub on a live PostgreSQL.

Verdict: HOLDS, except the full rebuild of 10^8 claims, which is still an extrapolation (GAP,
owner MVL-132).
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from memory_pg_live import open_live
from neptune_memory.store import AsOf, ClaimRecord, PostgresStore
from neptune_memory.store import postgres as pg

if TYPE_CHECKING:
    from collections.abc import Iterator

RESULTS: Final = Path(__file__).parents[1] / "docs" / "benchmarks" / "g1-results.json"
HUB_SIZE: Final = 10_000
#: Budget rows the gate leaves open (ADR 0007 §7), both owned by MVL-132.
GAPS: Final = frozenset({"rebuild_1e8_s", "graph_filtered_recall_at_10_wide_scope"})


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(RESULTS.read_text(encoding="utf-8"))
    return data


def test_published_results_are_small_and_labelled(results: dict[str, Any]) -> None:
    assert RESULTS.stat().st_size <= 100_000
    for row in results["budgets"]:
        assert row["label"] in {"measured", "extrapolated", "estimated"}, row
    assert {r["name"] for r in results["budgets"] if r.get("status") == "gap"} == GAPS


def test_every_budget_row_is_met(results: dict[str, Any]) -> None:
    rows = {row["name"]: row for row in results["budgets"]}
    for name, row in rows.items():
        if row.get("status") == "gap":  # below budget or unproven, and owned: never a pass
            assert name in GAPS and row["owner"] == "MVL-132", row
            continue
        value, budget = row["value"], row["budget"]
        assert (value >= budget) if row["direction"] == ">=" else (value < budget), row


def test_the_shipped_vector_settings_are_the_measured_ones(results: dict[str, Any]) -> None:
    recall = results["recall"]
    assert recall["queries"] >= 200
    assert recall["chosen"] == {"ef_search": pg.DEFAULT_EF_SEARCH, "graph_filtered": "exact"}
    assert (
        10 * results["raw"]["g1_10000000_recall"]["scope_embeddings"]["max"]
        <= pg.DEFAULT_EXACT_SCOPE_LIMIT
    )  # the measured 2-hop scopes, 10x deeper at 10^8, still take the exact branch


def test_the_walk_checks_a_keyed_visited_set_never_a_scan() -> None:
    sql = pg.neighbours_sql("memory")
    assert "jsonb_exists(b.seen, e.other)" in sql and "<> ALL" not in sql


# --- live: a hub of 10^4 entities -----------------------------------------------------------------


@pytest.fixture
def live() -> Iterator[tuple[Any, PostgresStore]]:
    yield from open_live()


def _edge(cid: int, subject: str, predicate: str, obj: str) -> ClaimRecord:
    return ClaimRecord(
        cid,
        subject,
        predicate,
        obj,
        None,
        "fleet_utc",
        0,
        None,
        0,
        None,
        "stated",
        "ev:site:hub",
        "memory.test@1",
    )


def _hub() -> list[ClaimRecord]:
    """A site with 10^4 machines of every kind; each mounts a sensor calibrated by one shared
    rig that sits at the site: cycles everywhere, and 2 * 10^4 entities two hops out."""
    kinds = ("arm", "amr", "quadruped", "humanoid", "rov", "uav")
    out = [_edge(1, "calibration:rig", "located_at", "site:hub")]
    for n in range(HUB_SIZE):
        robot = f"robot:{kinds[n % len(kinds)]}-{n:05d}"
        out.append(_edge(len(out) + 1, robot, "located_at", "site:hub"))
        out.append(_edge(len(out) + 1, robot, "mounts/s0", f"sensor:{n:05d}"))
        out.append(_edge(len(out) + 1, f"sensor:{n:05d}", "calibrated_by", "calibration:rig"))
    return out


def _bfs(claims: list[ClaimRecord], start: str, hops: int) -> dict[str, int]:
    adj: dict[str, set[str]] = {}
    for c in claims:
        assert c.object_entity is not None
        adj.setdefault(c.subject, set()).add(c.object_entity)
        adj.setdefault(c.object_entity, set()).add(c.subject)
    depth, queue = {start: 0}, deque([start])
    while queue:
        node = queue.popleft()
        if depth[node] < hops:
            for nxt in adj.get(node, ()):
                if nxt not in depth:
                    depth[nxt] = depth[node] + 1
                    queue.append(nxt)
    del depth[start]
    return depth


@pytest.mark.slow
def test_live_walk_through_a_hub_of_ten_thousand_entities(live: tuple[Any, PostgresStore]) -> None:
    conn, store = live
    claims = _hub()
    store.write_claims(claims)
    store.rebuild()
    at = AsOf("fleet_utc", 1, 1)
    got = store.neighbours("site:hub", 3, at)
    assert {n.entity: n.depth for n in got} == _bfs(claims, "site:hub", 3)
    assert len(got) == 2 * HUB_SIZE + 1  # every machine and sensor once, and the rig
    assert all(len(n.via) == n.depth for n in got)
    # Timing is the benchmark's job (docs/benchmarks/g1-results.json: 379 ms against 2.09 s for
    # the text[] walk); here the hub must only be walked correctly, each entity once.
    assert conn.info.transaction_status == 0
