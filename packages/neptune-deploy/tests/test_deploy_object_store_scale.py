"""A 10^5-key prefix listing stays within its time, request and memory budget (ADR 0006 §8)."""

import time
import tracemalloc
from pathlib import Path

import pytest

from deploy_object_store_fake import FakeStore
from neptune.identity.revisions import SourceLedger
from neptune.store.workspace import Workspace
from neptune_deploy.sources.object_store import s3_source

KEYS = 100_000
# Measured at about 6 s on a laptop (Linux, Python 3.14), server included. The budget leaves
# room for a slow CI runner and still fails on anything quadratic, which would take minutes.
BUDGET_SECONDS = 60.0
BUDGET_BYTES = 256 * 1024 * 1024  # peak Python allocations while listing and discovering


@pytest.mark.slow
def test_a_hundred_thousand_key_listing_is_within_budget(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.bulk({f"fleet/amr-{i % 50:02d}/{i:06d}.mcap".encode(): b"" for i in range(KEYS)})
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    with fake.serve() as endpoint:
        source = s3_source(
            f"s3://{fake.bucket}/fleet/",
            network=workspace,
            options={"endpoint": endpoint, "store": "site-a"},
            credentials={"s3_access_key_id": "AKID", "s3_secret_access_key": "secret"},
        )
        tracemalloc.start()
        started = time.perf_counter()
        listing = source.listing()
        discovery = source.discover(SourceLedger())
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert listing.complete and len(listing.entries) == KEYS
    assert len(discovery.new) == KEYS
    assert len(fake.requests) == KEYS // 1000  # one request per 1,000-key page, no more
    assert source.findings() == ()
    assert elapsed < BUDGET_SECONDS, f"{elapsed:.1f} s"
    assert peak < BUDGET_BYTES, f"{peak / 2**20:.0f} MiB"
