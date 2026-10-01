"""The catalog scale harness at a small scale (L1 gate; Ledger ADR 0005 §5).

The gate's 10⁵-package measurement is recorded in docs/reviews/l1-stress-test.md. This test runs
the same harness on 1000 packages so it cannot rot: the generated catalog is exactly the one the
harness describes, its keys and indexes are the migrations' own, and every measured query is
served by the index the review names, never by a sequential scan of a record partition.
"""

from typing import Any

import pytest

from ledger_catalog_scale import EMBODIMENTS, FIXED_KINDS, Scale, run

pytestmark = pytest.mark.slow

# The gate's budgets (ADR 0005 §5). At this scale they hold with a wide margin; the point here is
# the plans, the budget is checked at 10⁵ packages by the script.
THREAD_P95_MS = 50
WINDOW_P95_MS = 200


def _expected_records(scale: Scale) -> int:
    fixed = sum(n for _, n in FIXED_KINDS)
    total = 0
    for seq in range(1, scale.packages + 1):
        src = seq - 1 if seq % scale.sibling_every == 0 else seq
        machine = 0 if src % scale.heavy_every == 0 else 1 + src % (scale.machines - 1)
        streams = EMBODIMENTS[(machine + 1) % 7][2]
        if src % scale.long_every == scale.long_offset:
            streams = scale.long_streams
        total += fixed + streams
    return total


@pytest.fixture(scope="module")
def report(pg_server: str) -> dict[str, Any]:
    return run(pg_server, Scale(packages=1000, samples=40))


def test_the_generated_catalog_is_the_documented_one(report: dict[str, Any]) -> None:
    scale = Scale(packages=1000)
    counts = report["counts"]
    assert counts["packages"] == 1000
    assert counts["records"] == _expected_records(scale)
    assert counts["transforms"] == 2 * len({adapter for _, adapter, _, _ in EMBODIMENTS})
    assert report["schema_matches_migrations"] is True


WORLD_INDEX = "record_stream_world_clock_world_first_world_last_registrati_idx"


@pytest.mark.parametrize(
    ("measure", "index"),
    [
        ("thread_declared_typical", "record_logical_id_by_value"),
        ("thread_declared_workhorse", "record_logical_id_by_value"),
        ("thread_declared_sensor", "record_logical_id_by_value"),
        ("thread_anchored", "record_stream_source_content_id_kind_md5_idx"),
        ("lineage_set", "record_stream_source_content_id_kind_md5_idx"),
        ("window_typical", WORLD_INDEX),
        ("window_long_recording", WORLD_INDEX),
        ("query_page", "record_stream_pkey"),
        ("package_lookup", "package_tenant_id_package_id_tx_seq_key"),
    ],
)
def test_every_measured_query_uses_its_index(
    report: dict[str, Any], measure: str, index: str
) -> None:
    scans = report[measure]["plan"]["scans"]
    assert any(index in scan for scan in scans), scans
    assert not [s for s in scans if s.startswith("Seq Scan record")], scans


def test_a_lookup_without_the_tenant_key_scans(report: dict[str, Any]) -> None:
    """Why ADR 0005 §1 makes every keyed lookup name tenant_id: the keys lead with it."""
    assert report["query_page_without_tenant_key"]["plan"]["scans"] == ["Seq Scan record_stream"]
    assert report["package_lookup_without_tenant_key"]["plan"]["scans"] == ["Seq Scan package"]


def test_the_budgets_hold_at_this_scale(report: dict[str, Any]) -> None:
    for measure in ("thread_declared_typical", "thread_declared_sensor", "thread_anchored"):
        assert report[measure]["p95_ms"] < THREAD_P95_MS, measure
    for measure in ("window_typical", "window_long_recording"):
        assert report[measure]["p95_ms"] < WINDOW_P95_MS, measure
