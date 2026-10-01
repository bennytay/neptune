"""The dry run's explanation (ADR 0044): what a run would do and why; nothing parsed or kept."""

import hashlib
import json
import os
import shutil
import struct
from collections import Counter
from collections.abc import Callable, Mapping
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
    ShortReadError,
    SourceReader,
)
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.text import TextAdapter
from neptune.identity import canonical_json
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
from neptune.runtime import explain as explain_module
from neptune.runtime.explain import (
    HEAVY_BYTES,
    HEAVY_CHUNKS,
    SCHEMA,
    TRUNCATED,
    Bounds,
    Disposition,
    Explanation,
    PlanEstimate,
    SourceExplanation,
    SourceStatus,
    Verdict,
    explain_transform,
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
    selected = {use.descriptor.id: use.selected for use in explanation.adapters}
    assert set(selected) == {a.descriptor.id for a in builtin_adapters()}
    assert len(selected["tabular"]) == 5
    assert explanation.findings == outcome.findings

    text = explanation.render()
    for needle in (
        "Inventory: 9 files",
        "telemetry_amr.csv",
        "Sessions (inferred",
        "Work:",
        "Left out:",
    ):
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


def test_an_inspect_that_reads_short_is_judged_as_plan_is(tmp_path: Path) -> None:
    """ADR 0033 §3, as for ``plan``: a short read of an intact source is the adapter's failure; of
    a source no longer all there, the source's ``short_read``, and it is quarantined."""
    root = tmp_path / "root"
    root.mkdir()
    victim = root / "notes.txt"
    shutil.copy(FIXTURES / "text" / "notes.txt", victim)
    _, intact = explain(root, tmp_path / "home", AdapterRegistry([ShortInspect()]))
    (item,) = intact.sources
    assert item.status is SourceStatus.PLANNED and item.inspection is not None
    assert item.inspection.failure == {"error": "ShortReadError"}

    shrink = ShortInspect(lambda: victim.write_bytes(victim.read_bytes()[:10]))
    _, cut = explain(root, tmp_path / "home2", AdapterRegistry([shrink]))
    (item,) = cut.sources
    assert item.status is SourceStatus.QUARANTINED
    assert "neptune.discovery.short_read" in item.quarantined


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
    assert calls["probe"] == len(builtin_adapters()) * 8  # every adapter, every distinct head


def test_explain_touches_no_source_and_writes_no_chunk_derivative_or_package(
    root: Path, tmp_path: Path
) -> None:
    """The sources are only read; the workspace is the cache, warmed as ADR 0035 §4 says (its
    ledger and plans), never given a chunk or a derivative."""
    home = tmp_path / "home"
    IngestJob(root, tmp_path / "package", Workspace(home), default_registry(), IN_PROCESS).run()
    (root / "notes" / "new.txt").write_text("a file the workspace has never seen\n")
    before, source = tree(home), tree(root)
    stats = {p: p.lstat().st_mtime_ns for p in root.rglob("*")}
    _, explanation = explain(root, home)
    assert tree(root) == source
    assert {p: p.lstat().st_mtime_ns for p in root.rglob("*")} == stats
    after = tree(home)
    for kept in ("chunks/", "derivatives/"):
        assert {k: v for k, v in after.items() if k.startswith(kept)} == {
            k: v for k, v in before.items() if k.startswith(kept)
        }
    new = by_location(explanation)["notes/new.txt"]
    assert new.plan is not None and new.plan.rule is Rule.SOURCE_NEW
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fleet", "home", "package"]


def test_explain_warms_the_cache_so_the_ingest_after_it_plans_nothing(
    root: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    explain(root, home)
    assert list((home / "chunks").iterdir()) == [] and list((home / "derivatives").iterdir()) == []
    assert any((home / "ledgers").iterdir()) and any((home / "plans").iterdir())
    outcome = IngestJob(
        root, tmp_path / "package", Workspace(home), default_registry(), IN_PROCESS
    ).run()
    assert outcome.state is JobState.COMMITTED and outcome.cache.calls.plan == 0


# --- Determinism -------------------------------------------------------------------------------


def test_the_explanation_is_byte_identical_across_runs_copies_and_workspaces(
    root: Path, tmp_path: Path
) -> None:
    _, first = explain(root, tmp_path / "home")
    copy = mixed_root(tmp_path / "elsewhere")
    _, moved = explain(copy, tmp_path / "other-home")
    assert first.dumps() == moved.dumps() and first.render() == moved.render()
    assert first == moved  # the typed form too
    # Warm workspaces agree with each other: the second dry run reuses the first's plans.
    _, warm = explain(root, tmp_path / "home")
    _, warm_again = explain(root, tmp_path / "home")
    assert warm.dumps() == warm_again.dumps() != first.dumps()
    assert {s.plan.rule for s in warm.sources if s.plan} == {Rule.PLANNED}
    data = json.loads(first.dumps())
    assert str(tmp_path) not in first.dumps().decode() and "job" not in data


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


# --- Bounds ------------------------------------------------------------------------------------


def test_an_explanation_is_bounded_and_says_what_it_cut(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copy(FIXTURES / "hostile" / "many_members.zip", root / "many_members.zip")
    _, whole = explain(root, tmp_path / "whole")
    tight = Bounds(entries=2, locations=1, reasons=1, members=3, summary_bytes=64)
    monkeypatch.setattr(explain_module, "DEFAULT_BOUNDS", tight)
    _, cut = explain(root, tmp_path / "cut")
    data = json.loads(cut.dumps())
    assert data["bounds"] == tight.to_json()
    inventory = data["inventory"]
    assert len(inventory["files"]) == 2 and inventory["files_omitted"] == 8
    assert inventory["bytes"] == whole.inventory.bytes and inventory["sources"] == 9
    assert len(data["sources"]) == 2 and data["sources_omitted"] == len(whole.sources) - 2
    assert data["work"] == json.loads(whole.dumps())["work"]  # totals are over everything
    assert len(data["left_out"]) <= 2 and len(data["grouping"]["proposals"]) <= 2
    tabular = next(u for u in data["adapters"] if u["id"] == "tabular")
    assert len(tabular["selected"]) == 2 and tabular["selected_omitted"] == 3
    for item in data["sources"]:
        assert len(item["locations"]) <= 1
        assert all(len(v["reasons"]) <= 1 for v in item["verdicts"])
        summary = item.get("inspection", {}).get("summary")
        assert summary is None or len(canonical_json.dumps(summary)) <= 64
    cuts = {f.details["list"]: f for f in cut.findings if f.code == TRUNCATED}
    assert {"inventory.files", "sources", "findings", "adapters.tabular.selected"} <= set(cuts)
    assert cuts["inventory.files"].details == {
        "kept": 2,
        "limit": 2,
        "list": "inventory.files",
        "omitted": 8,
    }
    assert all(f.category.value == "limit" for f in cuts.values())
    assert len(cut.findings) == 2 + len(cuts)  # the bound holds, and every cut is listed
    assert "Truncated:" in cut.render()


def test_a_large_inspect_summary_and_container_listing_are_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "mcap" / "robot.mcap", root / "robot.mcap")
    shutil.copy(FIXTURES / "hostile" / "many_members.zip", root / "many_members.zip")
    monkeypatch.setattr(explain_module, "DEFAULT_BOUNDS", Bounds(members=5, summary_bytes=100))
    _, cut = explain(root, tmp_path / "home")
    sources = by_location(cut)
    mcap, archive = sources["robot.mcap"], sources["many_members.zip"]
    assert mcap.inspection is not None and mcap.inspection.summary is None
    assert mcap.inspection.summary_omitted_bytes > 100
    assert archive.probe is not None and archive.probe.container is not None
    listed = len(archive.probe.container.members)
    assert archive.members_omitted == listed - 5
    container = json.loads(canonical_json.dumps(archive.to_json()))["format"]["container"]
    assert len(container["members"]) == 5 and container["members_omitted"] == listed - 5
    lists = {f.details["list"] for f in cut.findings if f.code == TRUNCATED}
    assert lists == {"container.members", "inspection.summary"}


def test_a_session_proposal_s_lists_are_cut_and_copies_still_counted(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(explain_module, "DEFAULT_BOUNDS", Bounds(locations=1))
    _, cut = explain(root, tmp_path / "home")
    data = json.loads(cut.dumps())
    episode = next(
        p for p in data["grouping"]["proposals"]
        if p["directory"].get("path") == "arm/episode_001"
    )  # fmt: skip
    assert len(episode["members"]) == 1 and episode["members_omitted"] == 1
    assert all(p["reasons_omitted"] >= 0 for p in data["grouping"]["proposals"])
    lists = {f.details["list"] for f in cut.findings if f.code == TRUNCATED}
    assert {"grouping.proposal_lists", "source.locations"} <= lists
    assert "notes/notes-copy.txt (+1 copies)" in cut.render()  # the omitted copy still counts


@pytest.mark.parametrize(
    "name", [b"a\n  fake.mcap  [planned]", b"\x1b]0;owned\x07.txt", b"\x7fdel"]
)
def test_hostile_names_render_on_one_line_escaped(tmp_path: Path, name: bytes) -> None:
    root = tmp_path / "root"
    root.mkdir()
    descriptor = os.open(os.fsencode(root) + b"/" + name, os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(descriptor, b"plain text\n")
    os.close(descriptor)
    _, explanation = explain(root, tmp_path / "home")
    text = explanation.render()
    assert not any(ch in text for ch in "\x1b\x07\x7f\r")
    lines = text.splitlines()
    assert not any(line.startswith("  fake.mcap") for line in lines)  # no injected line
    (source,) = [line for line in lines if line.startswith("  ") and line.endswith("utf8")]
    assert "\\x" in source


def stepped_recordings(base: Path, count: int) -> Path:
    """``count`` loose recordings whose names state times 10 s apart: one contested reading per
    rule, each listing every recording's time in its reasons (ADR 0036 §4)."""
    root = base / f"steps-{count}"
    root.mkdir()
    for index in range(count):
        minute, second = divmod(index * 10, 60)
        (root / f"cam_2024-05-01_12-{minute:02d}-{second:02d}.mcap").write_bytes(b"x%d" % index)
    return root


def test_the_explanation_stays_bounded_as_the_tree_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(explain_module, "DEFAULT_BOUNDS", Bounds(entries=4, locations=2))
    sizes = {}
    for count in (10, 50, 200):
        _, explanation = explain(stepped_recordings(tmp_path, count), tmp_path / f"h{count}")
        data = json.loads(explanation.dumps())
        for proposal in data["grouping"]["proposals"]:
            for reason in proposal["reasons"]:
                for key, value in reason["details"].items():
                    if isinstance(value, list):
                        assert len(value) <= 2 and key + "_omitted" in reason["details"]
        sizes[count] = len(explanation.dumps())
    # Only counts grow (more digits), never lists: 20x the files, a few bytes more.
    assert sizes[200] - sizes[10] < 400, sizes


def test_lists_inside_a_proposal_s_reasons_are_cut_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(explain_module, "DEFAULT_BOUNDS", Bounds(locations=2))
    _, explanation = explain(stepped_recordings(tmp_path, 10), tmp_path / "home")
    data = json.loads(explanation.dumps())
    (steps,) = [p for p in data["grouping"]["proposals"] if p["rule"] == "name_time_proximity"]
    (reason,) = steps["reasons"]
    assert len(reason["details"]["times"]) == 2 and reason["details"]["times_omitted"] == 8
    assert len(steps["members"]) == 2 and steps["members_omitted"] == 8
    (cut,) = [
        f for f in explanation.findings
        if f.code == TRUNCATED and f.details["list"] == "grouping.proposal_lists"
    ]  # fmt: skip
    assert steps["contested_omitted"] == 8  # its ten single-recording peers, cut to two
    assert cut.details["omitted"] == 8 * 3 and cut.details["sources"] == 1


def test_hostile_strings_in_an_inspect_summary_cannot_drive_the_terminal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    library = "lib\x1b]0;owned\x07\x9b31m\x7f\u0085end".encode()
    header = b"".join(struct.pack("<I", len(part)) + part for part in (b"ros2", library))
    magic = b"\x89MCAP0\r\n"
    (root / "hostile.mcap").write_bytes(magic + b"\x01" + struct.pack("<Q", len(header)) + header)
    _, explanation = explain(root, tmp_path / "home")
    (item,) = explanation.sources
    assert item.inspection is not None and item.inspection.summary is not None
    assert "\x9b" in json.dumps(item.inspection.summary, ensure_ascii=False)  # kept exact in JSON
    text = explanation.render()
    assert not any(ch in text for ch in "\x1b\x07\x9b\x7f\x85")
    (line,) = [line for line in text.splitlines() if line.startswith("    inspect")]
    assert "\\x9b31m" in line and "\\x7f" in line


def test_bounds_refuse_what_is_not_a_positive_count() -> None:
    with pytest.raises(ValueError, match="entries"):
        Bounds(entries=0)
    with pytest.raises(ValueError, match="summary_bytes"):
        Bounds(summary_bytes=True)
    for wrong in (2.5, None, -1, True):
        with pytest.raises(ValueError, match="inspect_findings"):
            Bounds(inspect_findings=wrong)  # type: ignore[arg-type]
    assert Bounds(inspect_findings=0).inspect_findings == 0
    assert explain_transform(Bounds()).adapter_id == "neptune.explain"


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


class ShortInspect(TextAdapter):
    """The text adapter, whose ``inspect`` reads short (after ``before`` runs, if given)."""

    def __init__(self, before: Callable[[], object] | None = None) -> None:
        super().__init__()
        self.before = before

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        if self.before is not None:
            self.before()
        raise ShortReadError(source.content_id, 0, source.size)
