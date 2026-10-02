"""Snapshots from the record connectors flow through compiler ingest and the existing Deploy mapper.

There is no second mapping path (ADR 0008 §2): the connector hands the compiler bytes (a Jira issue
as the JSON the ``jira_json`` preset reads, a ServiceNow change as the CSV ``servicenow_csv`` reads,
a work order as the columns ``cmms_generic`` reads, a ticket's PDF as a separate source). The
tabular and PDF adapters read them, and the mapper declared in MVL-113 makes the lifecycle records.
Ingestion runs as a subprocess (a member never imports it: root ``test_merge_freshness``).
"""

import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from deploy_records_fake import FakeServer, JiraBackend, RestBackend, ServiceNowBackend
from deploy_records_fake_graph import LinearBackend
from deploy_records_support import jira, linear, rest, servicenow
from neptune.store.package import IngestPackage, read_package
from neptune_deploy.lifecycle import preset
from neptune_deploy.lifecycle.run import map_records
from neptune_deploy.sources.records import RecordSource

pytestmark = pytest.mark.integration


def write_snapshots(source: RecordSource, root: Path, folder: str) -> list[str]:
    """What a connector-aware ingest hands the compiler: each item's bytes, by its listed name."""
    written = []
    for entry in source.listing().entries:
        target = root / folder / entry.id.replace("/", "__")
        target = target.with_name(target.name + "__" + entry.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open(entry.location) as stream:
            target.write_bytes(stream.read())
        written.append(target.name)
    options = source.ingest_options()  # what the snapshots need declared: a CSV's header row
    if options:
        lines = ["neptune: 1", "adapters:"]
        for adapter, declared in options.items():
            inner = ", ".join(f"{name}: {value}" for name, value in declared.items())
            lines.append(f"  {adapter}: {{options: {{{inner}}}}}")
        (root / "neptune.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def ingest(folder: Path, out: Path, workspace: Path) -> IngestPackage:
    command = [
        str(Path(sys.executable).parent / "neptune"),
        "ingest",
        str(folder),
        "--out",
        str(out),
        "-w",
        str(workspace),
        "--isolation",
        "in_process",
        "--job",
        "records",
    ]
    subprocess.run(command, check=True, capture_output=True)
    return read_package(out)


def kinds(records: list[Any]) -> Counter[str]:
    return Counter(type(record).kind for record in records)


def test_jira_issues_become_incident_records_through_the_existing_preset(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        names = write_snapshots(source, sources, "jira")
        assert any(n.endswith("incident-report.pdf") for n in names)  # the ticket's PDF is a source
    base = ingest(sources, tmp_path / "package", tmp_path / "ws")
    records = map_records(base, [preset("jira_json")])
    counts = kinds(records)
    assert counts["incident_record"] == 3  # OPS-1, OPS-3, OPS-4; the Task is a finding
    assert counts["ingest_finding"] >= 1


def test_servicenow_changes_become_change_records_through_the_existing_preset(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    with servicenow(FakeServer(ServiceNowBackend()), tmp_path) as source:
        write_snapshots(source, sources, "servicenow")
    base = ingest(sources, tmp_path / "package", tmp_path / "ws")
    counts = kinds(map_records(base, [preset("servicenow_csv")]))
    assert counts["change_record"] == 3


def test_cmms_work_orders_become_maintenance_events_through_the_existing_preset(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    with rest(FakeServer(RestBackend()), tmp_path) as source:
        write_snapshots(source, sources, "cmms")
    base = ingest(sources, tmp_path / "package", tmp_path / "ws")
    counts = kinds(map_records(base, [preset("cmms_generic")]))
    assert counts["maintenance_event"] == 3


def test_linear_issues_become_intervention_records_through_the_existing_preset(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    with linear(FakeServer(LinearBackend()), tmp_path) as source:
        write_snapshots(source, sources, "linear")
    base = ingest(sources, tmp_path / "package", tmp_path / "ws")
    counts = kinds(map_records(base, [preset("linear_csv")]))
    assert counts["intervention"] == 3  # the trashed issue is not listed
