"""Golden documents for the catalog-api contract (``contracts/catalog-api/v*/golden/``).

``scripts/contracts.py bump catalog-api <version>`` runs this file and stores its output. Each
golden is a request or response record built with the API's own types, valued from the compiler's
four worked examples by the rules the contract tests check (``examples.py``). Transaction keys
are fixed illustrative ticks, since a real ``tx_time`` is the host's clock at registration.

Prints one JSON object: golden file name -> {"target": JSON pointer into the schema, "value"}.
"""

import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from neptune.model.knowledge import Known, NotApplicable, NotCovered
from neptune_ledger.api import codec
from neptune_ledger.api.types import (
    CatalogFinding,
    ClockMerge,
    EvidenceAnchor,
    History,
    LineageEdge,
    LineageGraph,
    LineageNode,
    LineageSet,
    MappingPath,
    Partition,
    QueryCursor,
    QueryMeta,
    QueryRow,
    QuerySpec,
    Region,
    RegisterRequest,
    Registration,
    Resolution,
    ResolveRequest,
    SourceLocation,
    Thread,
    ThreadEntry,
    ThreadKey,
    ThreadRequest,
    TimeWindow,
    TransactionKey,
    TransformInfo,
    VerifyReport,
)
from neptune_ledger.contract_tests.examples import (
    EXAMPLES,
    WorkedPackage,
    evidence_anchor,
    machine_keys,
    materialise,
    query_row,
    record_key,
    transform_of,
    world_value,
)

LEDGER_VERSION = "0.0.1"
CORE_STEPS = {
    "byte_range",
    "frame",
    "image_region",
    "json_pointer",
    "object",
    "page",
    "page_region",
    "record_range",
    "row",
    "row_cell",
    "span",
    "video_frame",
}


def tx(seq: int) -> TransactionKey:
    return TransactionKey(seq, f"2026-10-02T00:00:{seq:02d}.000000Z")


def registration(package: WorkedPackage, seq: int) -> Registration:
    return Registration(
        outcome="registered",
        package_id=Known(package.package_id),
        registration_key=Known(tx(seq)),
        root_locator=f"/srv/neptune/packages/{package.name}",
        ledger_version=LEDGER_VERSION,
        schema_version=Known(package.schema_version),
        record_counts=package.record_counts(),
        findings=(),
    )


def resolution(package: WorkedPackage, seq: int) -> Resolution:
    anchor = next(a for _, _, r in package.every_record() if (a := evidence_anchor(r)))
    cited = [
        package.ref(kind, line, r)
        for kind, line, r in package.every_record()
        if evidence_anchor(r) == anchor
    ]
    source = package.source(anchor.source)
    assert source is not None
    materialised = source["storage"] == "materialised"
    step = anchor.locator[-1]
    return Resolution(
        evidence_ref=anchor,
        status="resolved",
        size=Known(source["size"]),
        region=Region(step["kind"] if step["kind"] in CORE_STEPS else "adapter", step),
        fetch=(
            SourceLocation(
                package.package_id,
                source["storage"],
                Known(package.blob(anchor.source)) if materialised else NotApplicable(),
                package.locations(anchor.source),
            ),
        ),
        cited_by=tuple(sorted(cited, key=lambda r: (r.kind, r.record_id, r.package_id))),
        as_of=Known(tx(seq)),
        findings=(),
    )


def lineage(package: WorkedPackage, seq: int) -> LineageGraph:
    kind, line, record = next(
        (k, n, r) for k, n, r in package.every_record() if transform_of(r) is not None
    )
    transform = transform_of(record)
    assert transform is not None
    stated = {r["id"]: r for r in package.records("transform_record")}
    nodes, edges, todo = {}, [], [transform]
    while todo:
        current = todo.pop()
        if current in nodes:
            continue
        row = stated.get(current)
        if row is None:
            nodes[current] = LineageNode(current, NotCovered())
            continue
        info = TransformInfo(
            row["adapter_id"], row["adapter_version"], row["config_hash"], row["libraries"]
        )
        nodes[current] = LineageNode(current, Known(info))
        for position, upstream in enumerate(row["upstream"]):
            edges.append(LineageEdge(current, upstream, position))
            todo.append(upstream)
    return LineageGraph(
        record_id=record_key(record),
        status="found",
        kind=Known(kind),
        transform_id=Known(transform),
        registered_by=(package.ref(kind, line, record),),
        nodes=tuple(nodes[t] for t in sorted(nodes)),
        edges=tuple(sorted(edges, key=lambda e: (e.transform_id, e.position))),
        siblings=(),
        as_of=Known(tx(seq)),
        findings=(),
    )


def machine_thread(package: WorkedPackage, seq: int) -> tuple[ThreadRequest, Thread]:
    """The package's first machine thread in world order, history view (ADR 0003 §3, §4.2)."""
    members: dict[ThreadKey, list[tuple[str, int, Any, str]]] = defaultdict(list)
    for kind, line, record in package.every_record():
        for role, declared in machine_keys(record):
            members[ThreadKey("machine", declared)].append((kind, line, record, role))
    key = next(iter(members))
    clocks: dict[str, list[ThreadEntry]] = defaultdict(list)
    untimed: list[ThreadEntry] = []
    sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for kind, _, record, role in members[key]:
        anchor = evidence_anchor(record)
        transform = transform_of(record)
        assert anchor is not None and transform is not None
        world = world_value(record)
        entry = ThreadEntry(
            record_id=record_key(record),
            kind=kind,
            roles=(role,),  # type: ignore[arg-type]
            packages=(package.package_id,),
            registration_key=tx(seq),
            transform_id=transform,
            lineage_source=anchor.source,
            world=world,
        )
        sets[(kind, anchor.source)].add(transform)
        if isinstance(world, Known):
            clocks[world.value.clock].append(entry)
        else:
            untimed.append(entry)

    def native(entry: ThreadEntry) -> tuple[Any, ...]:
        assert isinstance(entry.world, Known)
        end = entry.world.value.closed_end
        start = entry.world.value.start.ticks
        return (start, (0, end) if end is not None else (1, 0), entry.record_id)

    partitions = [
        Partition("clock", tuple(sorted(entries, key=native)), clock)
        for clock, entries in sorted(clocks.items())
    ]
    if untimed:
        partitions.append(Partition("untimed", tuple(sorted(untimed, key=lambda e: e.record_id))))
    request = ThreadRequest(key, "world", History())
    thread = Thread(
        thread_id=key.thread_id,
        key=key,
        order="world",
        as_of=Known(tx(seq)),
        partitions=tuple(partitions),
        lineage_sets=tuple(
            LineageSet(kind, source, tuple(sorted(ts)), NotApplicable())
            for (kind, source), ts in sorted(sets.items())
        ),
        revisions=(),
        unresolved=(),
        links=(),
        findings=(),
        preference=History(),
    )
    return request, thread


def query_rows(package: WorkedPackage, seq: int) -> list[QueryRow]:
    return [
        query_row(package, "run", line, record, seq)
        for line, record in enumerate(package.records("run"), start=1)
    ]


def goldens() -> dict[str, dict[str, Any]]:
    documents: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as tmp:
        packages = [materialise(name, Path(tmp) / name) for name in EXAMPLES]
        last = len(packages)
        for seq, package in enumerate(packages, start=1):
            name = package.name
            documents[f"{name}.register_request.json"] = RegisterRequest(
                f"/srv/neptune/packages/{name}"
            )
            documents[f"{name}.registration.json"] = registration(package, seq)
            documents[f"{name}.verify_report.json"] = VerifyReport(
                package.package_id,
                "intact",
                Known(tx(seq)),
                Known(f"/srv/neptune/packages/{name}"),
                package.listed_files(),
                Known(tx(last)),
                (),
            )
            found = resolution(package, last)
            documents[f"{name}.resolve_request.json"] = ResolveRequest(found.evidence_ref)
            documents[f"{name}.resolution.json"] = found
            documents[f"{name}.lineage.json"] = lineage(package, last)
            for index, row in enumerate(query_rows(package, seq)):
                documents[f"{name}.query_row.{index}.json"] = row
        drone = packages[0]
        request, thread = machine_thread(drone, 1)
        documents["drone.thread_request.json"] = request
        documents["drone.thread.json"] = thread
        clock = next(p.clock_key for p in thread.partitions if p.clock_key is not None)
        assert clock is not None
        # The shape of a merge request; the worked examples hold no ClockMapping, so the
        # mapping id is a placeholder.
        mapping = "rec:sha256:" + "a" * 64
        documents["drone.thread_request.merge_example.json"] = ThreadRequest(
            request.key, "world", History(), merge=ClockMerge(clock, (mapping,))
        )
        documents["query_spec.window.json"] = QuerySpec(
            kinds=("run", "stream"), window=TimeWindow(clock, 0, 2**40), as_of=last
        )
        run = query_rows(drone, 1)[0]
        documents["query_spec.page.json"] = QuerySpec(
            kinds=("run",),
            as_of=last,
            limit=1000,
            after=QueryCursor(run.kind, run.record_id, run.package_id),
        )
        documents["query_meta.json"] = QueryMeta(Known(tx(last)), ())
        # Error cases: a tampered package refused; an unknown package; unresolvable evidence.
        documents["error.registration_refused.json"] = Registration(
            outcome="refused",
            package_id=Known(packages[1].package_id),
            registration_key=NotApplicable(),
            root_locator="/srv/neptune/packages/manipulator-tampered",
            ledger_version=LEDGER_VERSION,
            schema_version=Known(packages[1].schema_version),
            record_counts=(),
            findings=(
                CatalogFinding(
                    "file_digest_mismatch",
                    "records/frame.jsonl",
                    "the file's sha256 differs from the manifest's",
                ),
            ),
        )
        # A package holding a symlink is refused, and the link is never followed (ADR 0006 §1).
        documents["error.registration_refused_unsafe_entry.json"] = Registration(
            outcome="refused",
            package_id=Known(packages[2].package_id),
            registration_key=NotApplicable(),
            root_locator="/srv/neptune/packages/mobile_robot-linked",
            ledger_version=LEDGER_VERSION,
            schema_version=Known(packages[2].schema_version),
            record_counts=(),
            findings=(
                CatalogFinding(
                    "unsafe_entry",
                    "records/run.jsonl",
                    "a symlink; a package holds regular files and directories only",
                ),
            ),
        )
        # A registered package whose stored root is gone: nothing compared (ADR 0006 §2).
        documents["error.verify_unreachable.json"] = VerifyReport(
            drone.package_id,
            "unreachable",
            Known(tx(1)),
            Known("/srv/neptune/packages/drone"),
            0,
            Known(tx(last)),
            (
                CatalogFinding(
                    "package_unreadable",
                    "/srv/neptune/packages/drone",
                    "the stored root locator does not exist",
                ),
            ),
        )
        unknown = "sha256:" + "0" * 64
        documents["error.verify_unknown_package.json"] = VerifyReport(
            unknown,
            "unknown_package",
            NotCovered(),
            NotCovered(),
            0,
            Known(tx(last)),
            (CatalogFinding("unknown_package", unknown, "no package with this id is registered"),),
        )
        # A thread finding naming the mapping paths it tried (ADR 0003 §3.4); ids are placeholders.
        documents["error.finding_mapping_out_of_range.json"] = CatalogFinding(
            "mapping_out_of_range",
            thread.partitions[0].entries[0].record_id,
            "no usable path covers the entry's interval",
            paths_tried=(MappingPath((mapping,)), MappingPath(("rec:sha256:" + "b" * 64, mapping))),
        )
        nowhere = EvidenceAnchor(
            "sha256:" + "f" * 64, ({"kind": "byte_range", "length": 1, "offset": 0},)
        )
        documents["error.resolution_unresolvable.json"] = Resolution(
            evidence_ref=nowhere,
            status="unresolvable",
            size=NotCovered(),
            region=Region("byte_range", nowhere.locator[0]),
            fetch=(),
            cited_by=(),
            as_of=Known(tx(last)),
            findings=(
                CatalogFinding(
                    "unresolvable_evidence", nowhere.source, "no registered package holds it"
                ),
            ),
        )
    return {
        name: {"target": f"#/$defs/{type(doc).__name__}", "value": codec.to_json(doc)}
        for name, doc in sorted(documents.items())
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(goldens(), sort_keys=True, ensure_ascii=False))
