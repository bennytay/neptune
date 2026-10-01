"""The dry run's explanation (ADR 0044): what a run would do and why; nothing parsed or kept."""

import hashlib
import json
import os
import shutil
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.builtin import builtin_adapters, default_registry
from neptune.adapters.contract import (
    Adapter,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    Resources,
    SourceReader,
)
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.text import TextAdapter
from neptune.model.ids import RecordId
from neptune.runtime import (
    IngestJob,
    Isolation,
    JobEvent,
    JobOptions,
    JobOutcome,
    JobState,
    Limits,
    Phase,
    Rule,
)
from neptune.runtime.explain import (
    HEAVY_BYTES,
    HEAVY_CHUNKS,
    SCHEMA,
    Disposition,
    Explanation,
    PlanEstimate,
    SourceExplanation,
    SourceStatus,
    Verdict,
    heavy_reasons,
    show,
)
from neptune.store.workspace import Workspace

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
IN_PROCESS: Final = JobOptions(isolation=Isolation.IN_PROCESS)


def mixed_root(base: Path) -> Path:
    """Several embodiments in one tree: an arm episode (MCAP + joint states), a mobile base run, an
    AUV's events, a humanoid's joints, a quadruped inspection, an operator log, a copy, an unknown
    blob and a link."""
    root = base / "fleet"
    for directory, fixture in (
        ("arm/episode_001", "mcap/robot.mcap"),
        ("arm/episode_001", "tabular/joint_states_arm.json"),
        ("amr/run_002", "tabular/telemetry_amr.csv"),
        ("auv", "tabular/events_auv.jsonl"),
        ("humanoid", "tabular/humanoid_joints.parquet"),
        ("quadruped", "tabular/inspection_quadruped.tsv"),
        ("notes", "text/notes.txt"),
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, root / directory)
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes" / "notes-copy.txt")
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    (root / "latest").symlink_to("arm/episode_001")
    return root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return mixed_root(tmp_path)


def explain(
    root: Path,
    home: Path,
    registry: AdapterRegistry | None = None,
    options: JobOptions = IN_PROCESS,
) -> tuple[JobOutcome, Explanation]:
    registry = registry if registry is not None else default_registry()
    outcome = IngestJob(root, None, Workspace(home), registry, options).dry_run()
    assert outcome.state is JobState.PLANNED and outcome.explanation is not None
    return outcome, outcome.explanation


def by_location(explanation: Explanation) -> dict[str, SourceExplanation]:
    return {show(loc): item for item in explanation.sources for loc in item.locations}


def tree(path: Path) -> dict[str, str]:
    """Every entry below ``path``: a file's digest, a link's target, or a directory marker."""
    found = {}
    for entry in sorted(path.rglob("*")):
        name = str(entry.relative_to(path))
        if entry.is_symlink():
            found[name] = "link:" + str(entry.readlink())
        elif entry.is_file():
            found[name] = hashlib.sha256(entry.read_bytes()).hexdigest()
        else:
            found[name] = "dir"
    return found


# --- What it says ------------------------------------------------------------------------------


def test_an_explanation_covers_inventory_formats_adapters_grouping_work_and_left_out(
    root: Path, tmp_path: Path
) -> None:
    outcome, explanation = explain(root, tmp_path / "home")
    assert explanation.to_json()["schema"] == SCHEMA
    inventory = explanation.inventory
    assert len(inventory.files) == 9 and inventory.sources == 8  # the copy is one source
    assert [link.location.to_json() for link in inventory.links] == [
        {"kind": "local", "path": "latest"}
    ]
    sources = by_location(explanation)
    statuses = {name: item.status for name, item in sources.items()}
    assert statuses["blob.bin"] is SourceStatus.UNSUPPORTED
    assert {s for name, s in statuses.items() if name != "blob.bin"} == {SourceStatus.PLANNED}
    adapters = {name: item.adapter for name, item in sources.items()}
    assert adapters["arm/episode_001/robot.mcap"] == "mcap"
    assert adapters["humanoid/humanoid_joints.parquet"] == "tabular"
    assert adapters["notes/notes.txt"] == adapters["notes/notes-copy.txt"] == "text"

    csv = sources["amr/run_002/telemetry_amr.csv"]
    verdicts = {v.adapter: v for v in csv.verdicts}
    assert verdicts["tabular"].verdict is Verdict.SELECTED
    assert verdicts["text"].verdict is Verdict.OUTRANKED and "below tabular" in verdicts["text"].why
    assert verdicts["mcap"].verdict is Verdict.DECLINED and "mcap.no_magic" in verdicts["mcap"].why
    assert verdicts["tabular"].reasons  # the adapter's own reasons travel with its verdict
    assert csv.inspection is not None and csv.inspection.summary is not None
    assert csv.inspection.summary["layout"] == "csv"
    mcap = sources["arm/episode_001/robot.mcap"]
    assert mcap.probe is not None and mcap.probe.sniff.signatures[0].name == "MCAP"
    assert mcap.inspection is not None and mcap.inspection.summary is not None
    statistics = mcap.inspection.summary["statistics"]
    assert isinstance(statistics, Mapping) and statistics["message_count"] == 18

    for item in explanation.sources:
        if item.status is SourceStatus.PLANNED:
            assert item.plan is not None and item.plan.rule is Rule.SOURCE_NEW
            assert item.plan.committed == 0 and item.plan.bytes_to_read == item.plan.cost
            assert item.heavy == ()
    work = explanation.work
    assert work.sources == 7 and work.sources_to_parse == 7 and work.committed == 0
    assert work.chunks == sum(s.plan.chunks for s in explanation.sources if s.plan)
    assert work.ingest_calls == work.chunks
    assert work.calls == {
        "inspect": 7,
        "plan": 7,
        "probe": outcome.cache.calls.probe,
    }

    rules = Counter(p.rule for p in explanation.grouping.proposals)
    assert rules["session_directory"] == 2  # arm/episode_001 and amr/run_002
    episode = next(p for p in explanation.grouping.proposals if p.rule == "session_directory")
    assert episode.reasons and episode.assertion_kind == "inferred"

    left = {(show(e.location), e.disposition) for e in explanation.left_out}
    assert left == {("blob.bin", Disposition.UNSUPPORTED), ("latest", Disposition.LINK)}
    selected = {d.id: ids for d, ids in explanation.adapters}
    assert set(selected) == {"mcap", "tabular", "text"} and len(selected["tabular"]) == 5
    assert explanation.findings == outcome.findings

    text = explanation.render()
    for needle in ("Inventory: 9 files", "telemetry_amr.csv", "Sessions:", "Work:", "Left out:"):
        assert needle in text


def test_ties_and_failed_probes_are_explained(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    registry = AdapterRegistry([*builtin_adapters(), Twin(), Broken()])
    _, explanation = explain(root, tmp_path / "home", registry)
    (item,) = explanation.sources
    assert item.status is SourceStatus.AMBIGUOUS and item.adapter is None and item.plan is None
    verdicts = {v.adapter: v for v in item.verdicts}
    assert verdicts["text"].verdict is verdicts["twin"].verdict is Verdict.TIED
    assert verdicts["broken"].verdict is Verdict.FAILED
    assert verdicts["broken"].confidence is None
    assert verdicts["broken"].failure == {"error": "RuntimeError"}
    assert {f.code for f in explanation.ambiguities} == {"neptune.probe.ambiguous"}
    (left,) = explanation.left_out
    assert left.disposition is Disposition.AMBIGUOUS and "text, twin" in left.reason
    assert explanation.work.chunks == 0 and explanation.to_json()["ambiguities"]


def test_an_inspect_that_fails_is_shown_and_the_source_is_still_planned(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    registry = AdapterRegistry([BadInspect()])
    _, explanation = explain(root, tmp_path / "home", registry)
    (item,) = explanation.sources
    assert item.status is SourceStatus.PLANNED and item.plan is not None
    assert item.inspection is not None and item.inspection.summary is None
    assert item.inspection.failure == {"error": "ValueError"}
    assert "inspect failed" in explanation.render()


def test_a_dry_run_after_an_ingest_explains_that_nothing_is_left(
    root: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    IngestJob(root, tmp_path / "package", Workspace(home), default_registry(), IN_PROCESS).run()
    _, explanation = explain(root, home)
    for item in explanation.sources:
        if item.plan is not None:
            assert item.plan.rule is Rule.PLANNED and item.plan.to_parse == 0
            assert item.plan.bytes_to_read == 0
    assert explanation.work.ingest_calls == 0 and explanation.work.sources_to_parse == 0
    assert explanation.work.calls["plan"] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_source_unreadable_when_probed_is_explained_as_unreadable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    victim = root / "notes.txt"

    def revoke(event: JobEvent) -> None:  # hashed, then unreadable before inspect opens it
        if event.kind == "phase_finished" and event.phase is Phase.FINGERPRINT:
            victim.chmod(0)

    job = IngestJob(
        root, None, Workspace(tmp_path / "home"), default_registry(), IN_PROCESS, on_event=revoke
    )
    try:
        explanation = job.dry_run().explanation
    finally:
        victim.chmod(0o644)
    assert explanation is not None
    (item,) = explanation.sources
    assert item.status is SourceStatus.UNREADABLE and item.probe is None
    assert item.quarantined == ("neptune.runtime.source_unreadable",)
    (left,) = explanation.left_out
    assert left.disposition is Disposition.UNREADABLE
    assert left.reason == "neptune.runtime.source_unreadable"


# --- What it does not do -----------------------------------------------------------------------


def test_explain_never_calls_ingest(root: Path, tmp_path: Path) -> None:
    calls: Counter[str] = Counter()
    registry = AdapterRegistry([Counting(adapter, calls) for adapter in builtin_adapters()])
    outcome, explanation = explain(root, tmp_path / "home", registry)
    assert calls["ingest"] == 0 and outcome.cache.calls.ingest == 0
    assert calls["inspect"] == calls["plan"] == explanation.work.sources == 7
    assert calls["probe"] == 3 * 8  # every adapter, every distinct source's head


def test_explain_keeps_nothing_in_the_workspace_and_touches_no_source(
    root: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    IngestJob(root, tmp_path / "package", Workspace(home), default_registry(), IN_PROCESS).run()
    (root / "notes" / "new.txt").write_text("a file the workspace has never seen\n")
    before, source = tree(home), tree(root)
    stats = {p: p.lstat().st_mtime_ns for p in root.rglob("*")}
    _, explanation = explain(root, home)
    assert tree(home) == before and tree(root) == source
    assert {p: p.lstat().st_mtime_ns for p in root.rglob("*")} == stats
    new = by_location(explanation)["notes/new.txt"]
    assert new.plan is not None and new.plan.rule is Rule.SOURCE_NEW


def test_explain_with_a_fresh_workspace_saves_no_ledger_or_plan(root: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    explain(root, home)
    for kept in ("ledgers", "plans", "chunks", "derivatives"):
        assert list((home / kept).iterdir()) == [], kept


# --- Determinism -------------------------------------------------------------------------------


def test_the_explanation_is_byte_identical_across_runs_copies_and_workspaces(
    root: Path, tmp_path: Path
) -> None:
    _, first = explain(root, tmp_path / "home")
    _, again = explain(root, tmp_path / "home")  # the first kept nothing that changes the second
    copy = mixed_root(tmp_path / "elsewhere")
    _, moved = explain(copy, tmp_path / "other-home")
    assert first.dumps() == again.dumps() == moved.dumps()
    assert first.render() == again.render() == moved.render()
    data = json.loads(first.dumps())
    assert str(tmp_path) not in first.dumps().decode() and "job" not in data
    assert first == again  # the typed form too


def test_explain_never_changes_a_later_package(root: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    explain(root, home)
    explain(root, home, options=JobOptions(isolation=Isolation.IN_PROCESS, attempts=3))
    later = IngestJob(root, tmp_path / "later", Workspace(home), default_registry(), IN_PROCESS)
    fresh = IngestJob(
        root, tmp_path / "fresh", Workspace(tmp_path / "fresh-home"), default_registry(), IN_PROCESS
    )
    assert later.run().package == fresh.run().package


def test_a_cancelled_dry_run_has_no_explanation(root: Path, tmp_path: Path) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    job = IngestJob(root, None, Workspace(tmp_path / "home"), default_registry(), IN_PROCESS,
                    cancel=cancel)  # fmt: skip
    outcome = job.dry_run()
    assert outcome.state is JobState.CANCELLED and outcome.explanation is None


def test_an_empty_root_is_explained(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _, explanation = explain(root, tmp_path / "home")
    assert explanation.sources == () and explanation.left_out == ()
    assert explanation.work.chunks == 0 and explanation.inventory.bytes == 0
    assert explanation.grouping.proposals == ()
    assert explanation.render().startswith("Inventory: 0 files")


# --- Heavy transforms: boundaries --------------------------------------------------------------


def estimate(chunks: int, cost: int, committed: int = 0, left: int | None = None) -> PlanEstimate:
    return PlanEstimate(
        RecordId("rec:sha256:" + "0" * 64),
        Rule.SOURCE_NEW,
        chunks,
        committed,
        cost,
        cost if left is None else left,
    )


TEXT: Final = TextAdapter().descriptor


def test_heavy_bytes_and_chunks_start_exactly_at_their_thresholds() -> None:
    assert heavy_reasons(1, estimate(1, HEAVY_BYTES - 1), TEXT, Limits()) == ()
    (large,) = heavy_reasons(1, estimate(1, HEAVY_BYTES), TEXT, Limits())
    assert large.code == "large_input"
    assert heavy_reasons(1, estimate(HEAVY_CHUNKS - 1, 10), TEXT, Limits()) == ()
    (many,) = heavy_reasons(1, estimate(HEAVY_CHUNKS, 10), TEXT, Limits())
    assert many.code == "many_chunks"
    # Nothing left to parse is never heavy, however big the source.
    assert heavy_reasons(1, estimate(HEAVY_CHUNKS, 0, HEAVY_CHUNKS, 0), TEXT, Limits()) == ()


def test_memory_reasons_follow_the_descriptor_and_the_sandbox() -> None:
    hungry = replace(TEXT, resources=Resources(max_memory=1024, streaming=False))
    assert heavy_reasons(1024, estimate(1, 1), hungry, Limits()) == ()
    (grows,) = heavy_reasons(1025, estimate(1, 1), hungry, Limits())
    assert grows.code == "memory_grows"
    huge = replace(TEXT, resources=Resources(max_memory=Limits().memory_bytes + 1, streaming=True))
    (limit,) = heavy_reasons(1, estimate(1, 1), huge, Limits())
    assert limit.code == "memory_limit"
    assert heavy_reasons(1, estimate(1, 1), huge, None) == ()  # in process: no sandbox limit


# --- Test adapters -----------------------------------------------------------------------------


@dataclass
class Counting:
    """An adapter that counts every call made to it, then delegates."""

    inner: Adapter
    calls: Counter[str] = field(default_factory=Counter)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self.inner.descriptor

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        self.calls["probe"] += 1
        return self.inner.probe(head, hints)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        self.calls["inspect"] += 1
        return self.inner.inspect(source, config)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        self.calls["plan"] += 1
        return self.inner.plan(source, config)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        self.calls["ingest"] += 1
        return self.inner.ingest(source, chunk, config)


def _renamed(adapter_id: str) -> AdapterDescriptor:
    return replace(TEXT, id=adapter_id, finding_codes=(), locator_steps=(), formats=TEXT.formats)


class Twin(TextAdapter):
    """Claims exactly what the text adapter claims, under another id: a tie."""

    descriptor = _renamed("twin")


class Broken(TextAdapter):
    """A probe that raises."""

    descriptor = _renamed("broken")

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        raise RuntimeError("no")


class BadInspect(TextAdapter):
    """The text adapter, whose ``inspect`` raises."""

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        raise ValueError("no summary")
