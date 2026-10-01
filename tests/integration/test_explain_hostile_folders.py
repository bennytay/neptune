"""Explaining hostile folders through the SDK and the sandbox (ADR 0044): symlink loops,
unreadable and special files, archive bombs, damaged files and names that are not UTF-8, beside
data from several embodiments. Every one is explained; nothing is parsed, extracted or kept."""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Final

import pytest

from neptune.runtime.explain import Disposition, Explanation, SourceStatus, show
from neptune.sdk import AsyncNeptune, Neptune

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
BOMBS: Final = ("bomb.zip", "bomb.tar.gz", "pax_bomb.tar.gz", "nested.zip", "many_members.zip")
RAW_NAME: Final = b"caf\xe9.txt"  # Latin-1, not UTF-8


@pytest.fixture
def hostile(tmp_path: Path) -> Path:
    root = tmp_path / "hostile"
    for directory, fixture in (
        ("arm/episode_001", "mcap/robot.mcap"),
        ("arm", "mcap/truncated.mcap"),
        ("amr/run_002", "tabular/telemetry_amr.csv"),
        ("amr/run_002", "tabular/unclosed_quote.csv"),
        ("humanoid", "tabular/humanoid_joints.parquet"),
        ("auv", "tabular/events_auv.jsonl"),
        *(("archives", f"hostile/{name}") for name in BOMBS),
        ("archives", "hostile/traversal.zip"),
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, root / directory)
    loops = root / "loops"
    loops.mkdir()
    (loops / "a").symlink_to("b")
    (loops / "b").symlink_to("a")
    (loops / "self").symlink_to("self")
    (loops / "up").symlink_to("..")
    (loops / "outside").symlink_to("/etc/passwd")
    secret = root / "secret.txt"
    secret.write_text("never readable\n")
    secret.chmod(0)
    os.mkfifo(root / "pipe")
    descriptor = os.open(os.fsencode(root) + b"/" + RAW_NAME, os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(descriptor, b"caf\xe9 log line\n")
    os.close(descriptor)
    return root


def entries(explanation: Explanation) -> dict[str, Disposition]:
    return {show(entry.location): entry.disposition for entry in explanation.left_out}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_hostile_folder_is_explained_without_parsing_or_extracting_anything(
    hostile: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    result = Neptune(home).dry_run(hostile)
    assert result.planned and result.cache.calls.ingest == 0
    explanation = result.explanation
    assert explanation is not None
    left = entries(explanation)

    for link in ("a", "b", "self", "up", "outside"):
        assert left[f"loops/{link}"] is Disposition.LINK
    assert left["secret.txt"] is Disposition.SKIPPED and left["pipe"] is Disposition.SKIPPED
    for name in (*BOMBS, "traversal.zip"):
        assert left[f"archives/{name}"] is Disposition.UNSUPPORTED, name
        assert "never extracted" in next(
            e.reason for e in explanation.left_out if show(e.location) == f"archives/{name}"
        )
    # The bombs were listed within the probe policy, never inflated, and say where they stopped.
    codes = {finding.code for finding in explanation.findings}
    assert "neptune.probe.container_limit" in codes
    bombs = [s for s in explanation.sources if show(s.locations[0]).startswith("archives/")]
    assert bombs and all(s.probe is not None and s.probe.container for s in bombs)
    assert any(s.probe is not None and s.probe.container and not s.probe.container.complete
               for s in bombs)  # fmt: skip

    statuses = {show(s.locations[0]): s.status for s in explanation.sources}
    for planned in (
        "arm/episode_001/robot.mcap",
        "amr/run_002/telemetry_amr.csv",
        "humanoid/humanoid_joints.parquet",
        "auv/events_auv.jsonl",
    ):
        assert statuses[planned] is SourceStatus.PLANNED, planned
    # Damaged files are explained as whatever planning made of them: planned, or quarantined
    # with the finding that says why. Never a failed job.
    for damaged in ("arm/truncated.mcap", "amr/run_002/unclosed_quote.csv"):
        assert statuses[damaged] in (SourceStatus.PLANNED, SourceStatus.QUARANTINED)
    raw = next(s for s in explanation.sources if s.locations[0].raw == RAW_NAME)
    assert raw.status is SourceStatus.PLANNED and raw.adapter == "text"
    assert "caf\\xe9.txt" in explanation.render()

    for kept in ("chunks", "derivatives"):  # the cache is warmed, never given output
        assert list((home / kept).iterdir()) == [], kept
    again = Neptune(tmp_path / "second-home").dry_run(hostile).explanation
    assert again is not None and again.dumps() == explanation.dumps()


def test_the_async_client_explains_exactly_what_the_sync_one_does(
    hostile: Path, tmp_path: Path
) -> None:
    sync = Neptune(tmp_path / "home").dry_run(hostile).explanation
    asynchronous = asyncio.run(AsyncNeptune(tmp_path / "async-home").dry_run(hostile)).explanation
    assert sync is not None and asynchronous is not None
    assert sync.dumps() == asynchronous.dumps()


def test_an_ingest_after_explaining_a_hostile_folder_is_the_fresh_package(
    hostile: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    Neptune(home).dry_run(hostile)
    later = Neptune(home).ingest(hostile, tmp_path / "later")
    fresh = Neptune(tmp_path / "fresh-home").ingest(hostile, tmp_path / "fresh")
    assert later.committed and later.package == fresh.package
    assert later.explanation is None  # only a dry run explains
