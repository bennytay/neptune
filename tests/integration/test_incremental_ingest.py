"""MVL-9 acceptance: re-ingesting unchanged sources does no transform work; a parser or config
change invalidates only what it affects (ADR 0031).

Every adapter here is wrapped in a counter, so "no transform work" is measured as calls to
``plan`` and ``ingest``, independently of the job's own report, which must agree. The corpus holds
two text files (the reference adapter), a tally file (a series), and a binary blob nobody reads.
"""

import importlib.util
import shutil
import socket
import struct
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.check import check_chunk_output
from neptune.adapters.contract import (
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ContractError,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    SourceReader,
)
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.text import TextAdapter
from neptune.discovery.reader import LocalReader
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.jsonvalue import JsonValue
from neptune.model.series import SEQ
from neptune.runtime import (
    CacheReport,
    IngestJob,
    Isolation,
    JobError,
    JobEvent,
    JobOptions,
    JobOutcome,
    JobState,
    cache_report_from_json,
    collect,
    lineage,
)
from neptune.runtime import job as job_module
from neptune.runtime.lineage import Failure, Step
from neptune.store.package import read_cache_report, read_envelope, read_package
from neptune.store.workspace import Workspace

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "adapters" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _load("tally_adapter")
BRITTLE: Final = _load("brittle_adapter")
FRAMELOG: Final = _load("framelog_adapter")


class Counted:
    """An adapter, counting its calls; with ``version``, the same code released as another one."""

    def __init__(self, inner: Any, version: str | None = None) -> None:
        self.inner = inner
        self.descriptor: AdapterDescriptor = (
            inner.descriptor if version is None else replace(inner.descriptor, version=version)
        )
        self.calls: Counter[str] = Counter()

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        self.calls["probe"] += 1
        result: ProbeResult = self.inner.probe(head, hints)
        return result

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        self.calls["inspect"] += 1
        result: InspectResult = self.inner.inspect(source, config)
        return result

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        self.calls["plan"] += 1
        result: Plan = self.inner.plan(source, config)
        return result

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        self.calls["ingest"] += 1
        result: ChunkOutput = self.inner.ingest(source, chunk, config)
        return result


class Adapters:
    """A registry of counted adapters, by id."""

    def __init__(self, *adapters: Counted) -> None:
        self.by_id = {adapter.descriptor.id: adapter for adapter in adapters}
        self.registry = AdapterRegistry(adapters)

    def calls(self, method: str) -> dict[str, int]:
        return {name: adapter.calls[method] for name, adapter in sorted(self.by_id.items())}


def adapters(text_version: str | None = None, *, brittle: bool = False) -> Adapters:
    counted = [
        Counted(TextAdapter(chunk_bytes=64), text_version),
        Counted(TALLY.TallyAdapter(rows_per_chunk=4)),
    ]
    if brittle:
        counted.append(Counted(BRITTLE.BrittleAdapter()))
    return Adapters(*counted)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("a local ingest reached for the network")

    for target, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(target, name, refuse)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "survey"
    root.mkdir()
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "text" / "operator_log", root / "operator_log")
    rows = b"".join(f"{t * 10} {t}\n".encode() for t in range(20))
    (root / "lift.tally").write_bytes(b"TALLY1\n" + rows)
    (root / "faults.brittle").write_bytes(BRITTLE.brittle("first", "second"))
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    return root


class Runner:
    """Runs jobs over one workspace, each into a fresh destination."""

    def __init__(self, home: Path, out: Path) -> None:
        self.home, self.out, self.count = home, out, 0

    def __call__(
        self,
        root: Path,
        chosen: Adapters,
        config: Mapping[str, Mapping[str, JsonValue]] | None = None,
        *,
        watch: Callable[[JobEvent], None] = lambda _: None,
        cancel: threading.Event | None = None,
    ) -> tuple[JobOutcome, list[JobEvent]]:
        self.count += 1
        seen: list[JobEvent] = []

        def on_event(event: JobEvent) -> None:
            seen.append(event)
            watch(event)

        job = IngestJob(
            root,
            self.out / f"package-{self.count}",
            Workspace(self.home),
            chosen.registry,
            # These trusted test adapters count their own calls in process, which a forked
            # sandbox child could not report back; the cache logic is isolation-independent.
            JobOptions(config=config or {}, isolation=Isolation.IN_PROCESS),
            on_event=on_event,
            cancel=cancel,
        )
        return job.run(), seen


@pytest.fixture
def run(tmp_path: Path) -> Runner:
    return Runner(tmp_path / "home", tmp_path / "out")


def by_source(report: CacheReport, root: Path) -> dict[str, Any]:
    """Each source's plan and chunk outcomes, keyed by the file name that holds it."""
    names = {content_id(p.read_bytes()): p.name for p in root.iterdir() if p.is_file()}
    return {names[s.source]: s for s in report.sources}


def rules(entry: Any) -> set[str]:
    return {str(chunk.rule) for chunk in entry.chunks}


def check_report(outcome: JobOutcome) -> CacheReport:
    """The report in the package is the outcome's, reads back as itself, and names the receipt."""
    assert outcome.state is JobState.COMMITTED
    stored = read_cache_report(outcome.destination)
    report = cache_report_from_json(stored)
    assert report == outcome.cache
    assert report.receipt == read_envelope(outcome.destination).receipt
    assert report.receipt == read_package(outcome.destination).manifest.receipt
    return report


# --- Unchanged sources ---------------------------------------------------------------------------


def test_reingesting_unchanged_sources_calls_no_adapter_and_builds_the_same_package(
    corpus: Path, run: Runner
) -> None:
    first_adapters, again_adapters = adapters(), adapters()
    first, _ = run(corpus, first_adapters)
    first_report = check_report(first)
    assert first_report.totals()["chunks"]["hit"] == 0
    assert {str(s.plan.rule) for s in first_report.sources} == {"source_new"}
    assert sum(first_adapters.calls("ingest").values()) == first_report.calls.ingest > 0

    again, seen = run(corpus, again_adapters)
    report = check_report(again)
    assert again_adapters.calls("plan") == {"tally": 0, "text": 0}
    assert again_adapters.calls("ingest") == {"tally": 0, "text": 0}
    assert (report.calls.plan, report.calls.ingest) == (0, 0)
    totals = report.totals()
    assert totals["plans"] == {"hit": len(report.sources), "miss": 0}
    assert totals["chunks"]["miss"] == 0 and totals["chunks"]["hit"] > 0
    assert totals["derivatives"]["miss"] == 0  # verdicts and the tally's series file, reused
    recipes = {d.recipe for d in report.derivatives}
    assert recipes == {"neptune.runtime.admission/1", "neptune.store.series/1"}
    assert {"chunk_parsed", "chunk_committed", "derivative_built"}.isdisjoint(e.kind for e in seen)
    assert read_package(again.destination).id == read_package(first.destination).id == again.package


def test_the_cache_report_is_deterministic(corpus: Path, tmp_path: Path) -> None:
    """Same sources, adapters and workspace state: the same report, byte for byte."""
    reports = []
    for name in ("one", "two"):
        runner = Runner(tmp_path / name / "home", tmp_path / name / "out")
        first, _ = runner(corpus, adapters())
        again, _ = runner(corpus, adapters())
        reports.append(
            [read_cache_report(o.destination) for o in (first, again)],
        )
    assert canonical_json.dumps(reports[0]) == canonical_json.dumps(reports[1])
    assert reports[0][0] != reports[0][1]  # the rerun hit where the first run missed
    for stored in reports[0]:
        assert not {"time", "started", "finished", "host", "duration"} & set(stored)


def test_the_cache_is_shared_by_every_root_holding_the_same_bytes(
    corpus: Path, run: Runner, tmp_path: Path
) -> None:
    run(corpus, adapters())
    elsewhere = tmp_path / "copy"
    shutil.copytree(corpus, elsewhere)
    (elsewhere / "notes.txt").rename(elsewhere / "renamed.txt")  # names are not identity
    counted = adapters()
    outcome, _ = run(elsewhere, counted)
    assert counted.calls("plan") == {"tally": 0, "text": 0}
    assert counted.calls("ingest") == {"tally": 0, "text": 0}
    assert check_report(outcome).totals()["chunks"]["miss"] == 0


# --- Changes invalidate only what they affect -------------------------------------------------


def test_a_config_change_recomputes_only_that_adapters_chunks(corpus: Path, run: Runner) -> None:
    run(corpus, adapters())
    counted = adapters()
    outcome, _ = run(corpus, counted, {"text": {"block_rule": "line"}})
    report = by_source(check_report(outcome), corpus)
    for name in ("notes.txt", "operator_log"):
        assert report[name].plan.rule == "transform_changed"
        assert report[name].plan.changed == ("config",)
        assert rules(report[name]) == {"transform_changed"}
    assert report["lift.tally"].plan.rule == "planned"
    assert rules(report["lift.tally"]) == {"committed"}
    text = [entry for entry in report.values() if entry.adapter == "text"]
    assert len(text) == 3  # notes.txt, operator_log, and faults.brittle read as text
    assert counted.calls("ingest") == {"tally": 0, "text": sum(len(e.chunks) for e in text)}
    assert counted.calls("plan") == {"tally": 0, "text": 3}
    derivatives = {d.recipe: d for d in outcome.cache.derivatives if d.cache == "hit"}
    assert "neptune.store.series/1" in derivatives  # the tally's series file was not merged again


def test_a_new_adapter_version_recomputes_only_its_own_chunks(corpus: Path, run: Runner) -> None:
    run(corpus, adapters())
    counted = adapters(text_version="0.2.0")
    outcome, _ = run(corpus, counted)
    report = by_source(check_report(outcome), corpus)
    assert report["notes.txt"].plan.rule == "transform_changed"
    assert report["notes.txt"].plan.changed == ("adapter_version",)
    assert report["notes.txt"].adapter_version == "0.2.0"
    assert rules(report["lift.tally"]) == {"committed"}
    assert counted.calls("ingest")["tally"] == 0
    # the old lineage is still kept beside the new one: going back costs nothing
    back = adapters()
    outcome, _ = run(corpus, back)
    assert back.calls("ingest") == {"tally": 0, "text": 0}
    assert check_report(outcome).totals()["chunks"]["miss"] == 0


def test_a_changed_file_recomputes_only_itself(corpus: Path, run: Runner) -> None:
    run(corpus, adapters())
    before = content_id((corpus / "notes.txt").read_bytes())
    (corpus / "notes.txt").write_bytes(b"Rewritten notes.\n\nA second paragraph.\n")
    (corpus / "added.txt").write_bytes(b"A file that was not there before.\n")
    counted = adapters()
    outcome, _ = run(corpus, counted)
    report = by_source(check_report(outcome), corpus)
    assert report["notes.txt"].plan.rule == "source_changed"
    assert report["notes.txt"].plan.previous == before
    assert report["added.txt"].plan.rule == "source_new"
    assert rules(report["operator_log"]) == {"committed"}
    assert rules(report["lift.tally"]) == {"committed"}
    new_chunks = len(report["notes.txt"].chunks) + len(report["added.txt"].chunks)
    assert counted.calls("ingest") == {"tally": 0, "text": new_chunks}


def test_a_new_adapter_that_claims_a_source_is_adapter_changed(corpus: Path, run: Runner) -> None:
    run(corpus, adapters())  # no brittle adapter: the text adapter reads the .brittle file
    counted = adapters(brittle=True)
    outcome, _ = run(corpus, counted)
    report = by_source(check_report(outcome), corpus)
    assert report["faults.brittle"].adapter == "brittle"
    assert report["faults.brittle"].plan.rule == "adapter_changed"
    assert counted.calls("ingest") == {
        "brittle": len(report["faults.brittle"].chunks),
        "tally": 0,
        "text": 0,
    }


def a_new_law(monkeypatch: pytest.MonkeyPatch, law: str) -> None:
    """Release a new runtime version whose per-chunk laws refuse some chunk of the corpus.

    ``check_chunk_output``: a document block may not come third or later in its document (some
    text chunks break it). ``chunk_series``: a series may not hold ``seq`` 5 (one tally chunk).
    """
    monkeypatch.setattr(lineage, "RUNTIME_VERSION", "99.0.0")  # any version but this one
    if law == "check_chunk_output":

        def stricter(
            descriptor: AdapterDescriptor,
            source: SourceReader,
            config: AdapterConfig,
            chunk: Chunk,
            output: ChunkOutput,
        ) -> None:
            check_chunk_output(descriptor, source, config, chunk, output)
            if any(getattr(record, "order", 0) >= 2 for record in output.records):
                raise ContractError("a block comes third")

        monkeypatch.setattr(job_module, "check_chunk_output", stricter)
    else:
        series = job_module._chunk_series_failure

        def stricter_series(output: ChunkOutput) -> Failure | None:
            for batch in output.series:
                for column in batch.columns:
                    if column.name == SEQ and 5 in column.values:
                        facts: dict[str, JsonValue] = {"law": "seq_five", "stream": batch.stream}
                        return Failure(Step.CHUNK_SERIES, "ContractError", facts)
            return series(output)

        monkeypatch.setattr(job_module, "_chunk_series_failure", stricter_series)


@pytest.mark.parametrize("law", ["check_chunk_output", "chunk_series"])
def test_a_new_per_chunk_law_judges_kept_chunks_as_a_fresh_workspace_would(
    corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, law: str
) -> None:
    """Kept chunks are judged again by a new runtime's laws, without the adapter: one the new
    laws refuse quarantines its source exactly as in a fresh workspace, so the package is the
    same whatever the cache held (ADR 0031 §2)."""
    warm = Runner(tmp_path / "warm" / "home", tmp_path / "warm" / "out")
    before, _ = warm(corpus, adapters())  # every chunk committed under this version's laws
    assert not [f for f in before.findings if f.code == lineage.CHUNK_FAILED]

    a_new_law(monkeypatch, law)
    counted = adapters()
    judged, seen = warm(corpus, counted)
    fresh, _ = Runner(tmp_path / "fresh" / "home", tmp_path / "fresh" / "out")(corpus, adapters())
    failed = [f for f in judged.findings if f.code == lineage.CHUNK_FAILED]
    assert failed and {f.details["step"] for f in failed} == {law}
    assert {f.details["attempts"] for f in failed} == {1}
    skipped = {e.details["chunk"] for e in seen if e.kind == "chunk_skipped"}
    assert skipped and not skipped & {f.details["chunk"] for f in failed}  # refused, not reused
    assert judged.findings == fresh.findings
    assert judged.package == fresh.package == read_package(judged.destination).id
    assert counted.calls("ingest") == {"tally": 0, "text": 0}  # judged as kept: no adapter call
    report = check_report(judged)
    assert report.totals()["chunks"]["miss"] == 0
    kept_chunks = sum(len(s.chunks) for s in report.sources)
    verdicts = [d for d in report.derivatives if d.recipe == "neptune.runtime.chunk-laws/1"]
    assert len(verdicts) == kept_chunks and all(d.cache == "miss" for d in verdicts)

    opened: list[object] = []

    class Opening(LocalReader):  # every reader the job opens
        def __init__(self, *args: Any) -> None:
            opened.append(args[1])
            super().__init__(*args)

    monkeypatch.setattr(job_module, "LocalReader", Opening)
    again, _ = warm(corpus, adapters())  # judged once per runtime version: the verdicts are kept
    verdicts = [d for d in again.cache.derivatives if d.recipe == "neptune.runtime.chunk-laws/1"]
    assert len(verdicts) == kept_chunks and all(d.cache == "hit" for d in verdicts)
    assert (again.findings, again.package) == (fresh.findings, fresh.package)
    assert len(opened) == len(list(corpus.iterdir()))  # each probed once; none opened to judge


def test_a_chunk_admitted_under_unknown_laws_is_judged_again(corpus: Path, run: Runner) -> None:
    """A chunk with no record of the laws that admitted it (format 1, or a damaged record) is
    judged by the current ones, once, rather than trusted."""
    first, _ = run(corpus, adapters())
    workspace = Workspace(run.home)
    chunks = [chunk.chunk for source in first.cache.sources for chunk in source.chunks]
    (workspace.chunk_path(chunks[0]) / "admitted.json").unlink()
    (workspace.chunk_path(chunks[1]) / "admitted.json").write_bytes(b"{damaged")
    counted = adapters()
    outcome, _ = run(corpus, counted)
    judged = [d for d in outcome.cache.derivatives if d.recipe == "neptune.runtime.chunk-laws/1"]
    assert len(judged) == 2 and all(d.cache == "miss" for d in judged)
    assert counted.calls("ingest") == {"tally": 0, "text": 0}
    assert outcome.package == first.package


def test_an_interrupted_job_is_resumed_from_the_cache(corpus: Path, run: Runner) -> None:
    cancel = threading.Event()

    committed: list[str] = []

    def stop_after_three(event: JobEvent) -> None:
        if event.kind == "chunk_committed" and event.details["new"]:
            committed.append(str(event.details["chunk"]))
            if len(committed) == 3:
                cancel.set()

    cancelled, _ = run(corpus, adapters(), watch=stop_after_three, cancel=cancel)
    assert cancelled.state is JobState.CANCELLED and cancelled.package is None
    assert cancelled.cache.calls.ingest == 3

    counted = adapters()
    outcome, _ = run(corpus, counted)
    report = check_report(outcome)
    assert {str(s.plan.rule) for s in report.sources} == {"planned"}  # every plan was saved
    chunk_rules = Counter(str(c.rule) for s in report.sources for c in s.chunks)
    assert chunk_rules["committed"] == 3
    assert set(chunk_rules) == {"committed", "not_committed"}
    assert sum(counted.calls("ingest").values()) == chunk_rules["not_committed"]
    assert sum(counted.calls("plan").values()) == 0


def test_collection_drops_superseded_lineages_and_keeps_the_current_one(
    corpus: Path, run: Runner
) -> None:
    run(corpus, adapters())
    line = {"text": {"block_rule": "line"}}
    run(corpus, adapters(), line)
    workspace = Workspace(run.home)
    plans_before = len(list(workspace.plans()))
    collected = collect(workspace, adapters().registry, JobOptions(config=line))
    assert collected.plans == plans_before - len(list(workspace.plans())) > 0
    assert collected.chunks > 0 and collected.derivatives > 0

    current = adapters()
    outcome, _ = run(corpus, current, line)
    assert current.calls("ingest") == {"tally": 0, "text": 0}  # what was kept is reused
    previous = adapters()
    outcome, _ = run(corpus, previous)  # the collected paragraph lineage is recomputed
    report = by_source(check_report(outcome), corpus)
    assert report["notes.txt"].plan.rule == "transform_changed"
    assert previous.calls("ingest")["text"] > 0 and previous.calls("ingest")["tally"] == 0


def test_a_job_and_a_collection_never_overlap(corpus: Path, run: Runner) -> None:
    run(corpus, adapters())
    workspace = Workspace(run.home)
    with workspace.in_use(), pytest.raises(JobError, match="in use"):
        collect(workspace, adapters().registry)
    (ledger,) = (run.home / "ledgers").rglob("ledger.jsonl")
    ledger.write_bytes(b"{damaged")
    with pytest.raises(JobError, match="cannot be collected"):
        collect(workspace, adapters().registry)


def test_a_damaged_old_plan_never_stops_a_job_that_plans_again(corpus: Path, run: Runner) -> None:
    """Only the explanation of a miss reads a source's other plans; damage there costs nothing."""
    first, _ = run(corpus, adapters())
    old = by_source(first.cache, corpus)["notes.txt"].transform.removeprefix("rec:sha256:")
    for plan in (run.home / "plans").rglob(f"{old}.json"):
        plan.write_bytes(b"{damaged")
    outcome, _ = run(corpus, adapters(), {"text": {"block_rule": "line"}})
    report = by_source(check_report(outcome), corpus)
    assert report["notes.txt"].plan.rule == "source_new"  # nothing readable is known of it
    assert rules(report["lift.tally"]) == {"committed"}


# --- Scale -------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.usefixtures("no_network")
def test_reingesting_a_multi_gb_recording_does_no_transform_work(tmp_path: Path) -> None:
    """The issue's "100 GB-scale" acceptance, scaled to a 4 GiB sparse recording and 1 MiB of
    text: the second ingest calls ``plan`` and ``ingest`` zero times and merges nothing. Its only
    pass over the bytes is the fingerprint hash, which is how it knows they are unchanged."""
    root = tmp_path / "run"
    root.mkdir()
    recording = root / "camera.framelog"
    frame, frames = 4 * 1024 * 1024, 1024  # 4 GiB of payload: offsets past 2**32
    with recording.open("wb") as stream:  # sparse: headers are written, payloads are holes
        stream.write(FRAMELOG.MAGIC)
        for index in range(frames):
            stream.seek(len(FRAMELOG.MAGIC) + index * (12 + frame))
            stream.write(struct.pack("<QI", 1_000_000 * index, frame))
        stream.truncate(len(FRAMELOG.MAGIC) + frames * (12 + frame))
    assert recording.stat().st_size > 4 * 1024**3
    paragraphs = (f"Inspection note {n}: the arm moved as commanded.\n\n" for n in range(2_000))
    (root / "notes.txt").write_text("".join(paragraphs))

    def chosen() -> Adapters:
        return Adapters(
            Counted(TextAdapter(chunk_bytes=16 * 1024)),
            Counted(FRAMELOG.FrameLogAdapter(frames_per_chunk=64)),
        )

    runner = Runner(tmp_path / "home", tmp_path / "out")
    first_adapters = chosen()
    first, _ = runner(root, first_adapters)
    assert first.state is JobState.COMMITTED
    assert first_adapters.calls("ingest")["framelog"] == frames // 64 + 1
    assert first_adapters.calls("ingest")["text"] > 1  # the reference adapter, chunked

    again_adapters = chosen()
    again, _ = runner(root, again_adapters)
    report = check_report(again)
    assert again_adapters.calls("plan") == {"framelog": 0, "text": 0}
    assert again_adapters.calls("ingest") == {"framelog": 0, "text": 0}
    assert report.totals()["chunks"] == {
        "hit": sum(len(s.chunks) for s in report.sources),
        "miss": 0,
    }
    assert report.totals()["derivatives"]["miss"] == 0  # no verdict recomputed, no series merged
    assert again.package == first.package
    durations = dict(read_envelope(again.destination).durations)
    first_durations = dict(read_envelope(first.destination).durations)
    transform = ("plan", "parse", "normalize")
    assert sum(durations[p] for p in transform) < sum(first_durations[p] for p in transform)
