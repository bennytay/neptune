"""A Formant intervention over an Open-RMF task: both are kept, nothing is linked (ADR 0010 §6).

The fixtures are one warehouse shift seen by two systems. Formant states that a remote operator
intervened on ``dev-amr-07`` from 13:42:30 to 13:51:00; Open-RMF's log states that task
``delivery.dispatch-12`` ran from 13:40:00 to 13:55:00 on ``tinyRobot/AMR-07``. The two overlap in
real time, and a person would join them. The connectors do not: different systems name things
differently, their clocks are not known to be one clock, and "this intervention happened during
that task" is an interpretation for ``derived/``, never evidence.
"""

import datetime
from pathlib import Path
from typing import Any

import pytest

from deploy_formant_fake import FakeFormant, serve
from neptune.model.knowledge import Known
from neptune.model.time import DomainMismatchError
from neptune_deploy.sources.fleet_ops import formant_source, open_rmf_source

RMF = Path(__file__).parent / "fixtures" / "fleet_ops" / "rmf"
LINKS = {"identity_link", "clock_mapping", "run_assembly", "frame_binding", "snapshot_binding"}


class Online:
    def require_network(self, purpose: str) -> None:
        pass


def both() -> tuple[Any, Any]:
    with serve(FakeFormant.standard()) as endpoint:
        fm = formant_source(
            "formant://org-acme",
            network=Online(),
            options={"endpoint": endpoint, "instance": "@acme"},
            credentials={"formant_access_token": "test-token-123"},
        )
        fm.catalog()
    rmf = open_rmf_source(
        RMF,
        options={
            "site": "warehouse-1",
            "files": {"tasks": {"file": "tasks.json"}, "map": {"file": "nav_graph.json"}},
        },
    )
    rmf.catalog()
    return fm, rmf


def iso_ms(text: str) -> int:
    return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def test_the_fixture_really_overlaps_in_real_time() -> None:
    """The oracle: standard-library arithmetic, here and nowhere in the connectors."""
    fm, rmf = both()
    intervention = next(
        r for r in fm.catalog().of("intervention") if r.identifiers[0].value.value == "ir-0001"
    )
    task = next(
        r for r in rmf.catalog().of("run") if r.logical_id.value.value == "delivery.dispatch-12"
    )
    start, end = iso_ms("2026-03-01T13:42:30.000Z"), iso_ms("2026-03-01T13:51:00.000Z")
    # The intervention's own ticks are milliseconds since 1970 only because the text carries a Z
    # and the format declares it; the task's ticks are the integers the log states.
    assert intervention.start.value.ticks == start and intervention.end.value.ticks == end
    assert task.first.value.ticks <= start and end <= task.last.value.ticks


def test_both_are_kept_each_under_its_own_system_and_nothing_is_merged() -> None:
    fm, rmf = both()
    intervention = next(
        r for r in fm.catalog().of("intervention") if r.identifiers[0].value.value == "ir-0001"
    )
    task = next(
        r for r in rmf.catalog().of("run") if r.logical_id.value.value == "delivery.dispatch-12"
    )
    assert intervention.id != task.id
    # The names look alike and are not matched: two namespaces, two ids, no link between them.
    assert intervention.machines[0].value.namespace == "formant.device"
    assert task.machine.value.namespace == "rmf.robot"
    assert intervention.machines[0].value != task.machine.value
    assert intervention.related == ()
    # Each cites only its own system's document and was made by its own transform.
    fm_docs = {d.content_id for d in fm.catalog().documents}
    rmf_docs = {d.content_id for d in rmf.catalog().documents}
    assert not fm_docs & rmf_docs
    assert intervention.provenance.evidence.source in fm_docs
    assert task.provenance.evidence.source in rmf_docs
    assert intervention.provenance.transform == fm.transform.id != rmf.transform.id
    assert task.provenance.transform == rmf.transform.id


def test_no_record_of_either_source_cites_the_other_or_links_anything() -> None:
    fm, rmf = both()
    fm_docs = {d.content_id for d in fm.catalog().documents}
    rmf_docs = {d.content_id for d in rmf.catalog().documents}
    for source, own, other in ((fm, fm_docs, rmf_docs), (rmf, rmf_docs, fm_docs)):
        for record in source.catalog().records:
            assert record.provenance.evidence.source in own
            assert record.kind not in LINKS
        for finding in source.findings():
            cited = getattr(finding.subject, "source", None)
            assert cited not in other
    # No finding anywhere says anything about an overlap, a match or a relation.
    text = " ".join(f.message + str(f.details) for s in (fm, rmf) for f in s.findings())
    assert "overlap" not in text and "matched" not in text


def test_their_times_are_on_different_clocks_that_nothing_here_compares() -> None:
    fm, rmf = both()
    intervention = next(
        r for r in fm.catalog().of("intervention") if r.identifiers[0].value.value == "ir-0001"
    )
    task = next(
        r for r in rmf.catalog().of("run") if r.logical_id.value.value == "delivery.dispatch-12"
    )
    assert isinstance(intervention.start, Known) and isinstance(task.first, Known)
    a, b = intervention.start.value, task.first.value
    assert a.domain_id != b.domain_id
    with pytest.raises(DomainMismatchError):  # the model refuses to order two clocks' instants
        _ = a < b
    domains = {d.id: d for source in (fm, rmf) for d in source.catalog().of("timestamp_domain")}
    # Formant's carries the instant its text states; Open-RMF's meaning is not stated by its log.
    assert domains[a.domain_id].epoch.value == "unix"
    assert not isinstance(domains[b.domain_id].epoch, Known)


def test_the_sources_state_evidence_only_so_nothing_inferred_is_built() -> None:
    fm, rmf = both()
    kinds = {r.kind for s in (fm, rmf) for r in s.catalog().records}
    assert kinds == {
        "structured_table",
        "structured_record",
        "timestamp_domain",
        "intervention",
        "run",
        "frame_graph",
        "frame",
        "spatial_artifact",
    }
    for source in (fm, rmf):
        for record in source.catalog().records:
            assert record.provenance.assertion_kind.value == "stated"
