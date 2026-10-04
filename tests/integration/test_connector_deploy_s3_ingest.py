"""Neptune Deploy's S3 connector through ``neptune ingest s3://…``, end to end (ADR 0067, MVL-45).

The connector is the installed workspace member's (``deploy_s3``, Deploy ADR 0006); the store is
Deploy's own in-process fake (``packages/neptune-deploy/tests/deploy_object_store_fake.py``), an
S3 server on a loopback port inside this process, so nothing leaves the host. The jobs are real
and sandboxed. Skipped until the member's connector is installed in this environment.

This is MVL-45's acceptance: a re-sync of the bucket fetches, hashes and parses nothing
unchanged; a re-uploaded object is hashed once under its new version, then recognised; a deleted
object is an absence; the packages are deterministic.
"""

import importlib.util
import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.cli import exit_codes, run

pytestmark = pytest.mark.integration

DEPLOY_FAKE: Final = (
    Path(__file__).parents[2]
    / "packages"
    / "neptune-deploy"
    / "tests"
    / "deploy_object_store_fake.py"
)
URI: Final = "s3://fleet-logs/arm-cell/"
CSV: Final = b"t_ns,joint_1_rad\n0,0.10\n1000000,0.12\n2000000,0.15\n"
NOTE: Final = b"# Cell 3\n\nThe gripper was recalibrated before episode 7.\n"


def _fake_module() -> ModuleType:
    pytest.importorskip("neptune_deploy.sources.object_store")
    if not DEPLOY_FAKE.is_file():
        pytest.skip("Deploy's fake store is not in this checkout")
    spec = importlib.util.spec_from_file_location("deploy_object_store_fake_root", DEPLOY_FAKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str]]:
    """Deploy's fake S3 bucket, versioned, serving on loopback; the store and its endpoint."""
    store = _fake_module().FakeStore()
    store.put("arm-cell/episode-7/joints.csv", CSV)
    store.put("arm-cell/episode-7/notes.md", NOTE)
    store.put("arm-cell/episode-7/camera.bin", os.urandom(9 * 1024 * 1024))  # two 8 MiB chunks
    monkeypatch.setenv("NEPTUNE_S3_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("NEPTUNE_S3_SECRET_ACCESS_KEY", "never-printed")
    with store.serve() as endpoint:
        yield store, endpoint


def ingest(
    bucket: tuple[Any, str], workspace: Path, out: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """``neptune ingest`` of the bucket prefix; its result, events, and object reads served."""
    store, endpoint = bucket
    store.requests.clear()
    options = json.dumps({"endpoint": endpoint, "store": "site-a"})
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["ingest", URI, "--connector", "deploy_s3", "--source-options", options]
    argv += ["--out", str(out), "-w", str(workspace), "--allow-network", "--json"]
    code = run(argv, stdout=stdout, stderr=stderr)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert code == exit_codes.OK, (lines[-1], stderr.getvalue())
    events = [line["event"] for line in lines if line["type"] == "event"]
    return lines[-1], events, len(store.object_requests())


def kinds(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event["details"] for event in events if event["kind"] == kind]


def package_bytes(package: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(package)): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.relative_to(package).parts[0] != "volatile"
    }


def test_deploy_s3_syncs_a_bucket_incrementally_through_neptune_ingest(
    bucket: tuple[Any, str], tmp_path: Path
) -> None:
    store, _ = bucket
    workspace = tmp_path / "ws"
    first, events, reads = ingest(bucket, workspace, tmp_path / "pkg-1")
    assert first["state"] == "committed" and reads > 0
    assert len(kinds(events, "source_hashed")) == 3
    lines = (tmp_path / "pkg-1" / "records" / "source_revision.jsonl").read_bytes().splitlines()
    locations = [json.loads(line)["location"] for line in lines]
    assert {loc["connector_id"] for loc in locations} == {"deploy_s3"}
    assert all(loc["revision_token"].startswith("version:") for loc in locations)

    again, events, reads = ingest(bucket, workspace, tmp_path / "pkg-2")
    assert reads == 0 and kinds(events, "source_hashed") == []
    assert len(kinds(events, "source_recognised")) == 3
    report = json.loads((tmp_path / "pkg-2" / "volatile" / "cache-report.json").read_bytes())
    assert report["calls"] == {"ingest": 0, "plan": 0, "probe": 0}
    assert again["package"] == first["package"]
    assert package_bytes(tmp_path / "pkg-1") == package_bytes(tmp_path / "pkg-2")

    store.put("arm-cell/episode-7/notes.md", NOTE)  # re-uploaded unchanged: a new version id
    _, events, _ = ingest(bucket, workspace, tmp_path / "pkg-3")
    (hashed,) = kinds(events, "source_hashed")
    assert not hashed["new_revision"] and hashed["location"]["object_id"].endswith("notes.md")
    _, events, reads = ingest(bucket, workspace, tmp_path / "pkg-4")
    assert reads == 0 and kinds(events, "source_hashed") == []

    store.delete("arm-cell/episode-7/joints.csv")
    _, events, reads = ingest(bucket, workspace, tmp_path / "pkg-5")
    (absent,) = kinds(events, "source_absent")
    assert absent["location"]["object_id"].endswith("joints.csv") and reads == 0
