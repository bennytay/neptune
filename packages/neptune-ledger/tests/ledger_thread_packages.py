"""Packages for the thread tests: ADR 0003 §6's worked examples, built from the compiler's own.

Each package is a subset of one of the compiler's worked examples (a legged robot's URDF, a
manipulator's hand-eye calibration, a mobile robot's bag), edited as JSON, re-identified with the
compiler's id rules and written with its package writer. ``rebase`` replaces content and transform
ids and recomputes every evidence record's tier-2 id (root ADR 0003), rewriting each reference to
it, so a package is what an adapter at that version, over those bytes, would have written.
"""

import hashlib
from collections.abc import Callable, Mapping
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import revision_id
from neptune.model.kinds import RECORD_KINDS
from neptune.model.source import location_from_json
from neptune.store.package import package_files
from neptune_ledger.contract_tests.examples import examples_dir

Record = dict[str, Any]
# Kinds whose ids do not come from (kind, evidence, transform).
LEDGER_KINDS: Final = frozenset(
    {"source_artifact", "source_revision", "source_absence", "transform_record", "ingest_finding"}
)


def records(name: str) -> list[Record]:
    """Every record of one worked example, as JSON, kinds in name order."""
    root = examples_dir() / name / "records"
    out: list[Record] = []
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_bytes().splitlines():
            value = canonical_json.loads(line)
            assert isinstance(value, dict)
            out.append(value)
    return out


def subset(name: str, adapter_id: str) -> list[Record]:
    """One adapter's output in a worked example: its transform, its records (findings left out)
    and the sources they cite, with their revisions."""
    every = records(name)
    (transform,) = [
        r for r in every if r["kind"] == "transform_record" and r["adapter_id"] == adapter_id
    ]
    made = [
        r
        for r in every
        if r["kind"] not in LEDGER_KINDS and r["provenance"]["transform"] == transform["id"]
    ]
    sources = {r["provenance"]["evidence"]["source"] for r in made}
    ledger = [
        r
        for r in every
        if (r["kind"] == "source_artifact" and r["content_id"] in sources)
        or (r["kind"] == "source_revision" and r["content_id"] in sources)
    ]
    return [transform, *ledger, *made]


def replace_strings(value: Any, strings: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return strings.get(value, value)
    if isinstance(value, list):
        return [replace_strings(v, strings) for v in value]
    if isinstance(value, dict):
        return {k: replace_strings(v, strings) for k, v in value.items()}
    return value


def _read(record: Record) -> Any:
    _, read = RECORD_KINDS[record["kind"]]
    return read(record)


def rebase(rows: list[Record], strings: Mapping[str, str] | None = None) -> list[Record]:
    """Replace ``strings`` (content and transform ids) everywhere, then give every evidence
    record the tier-2 id its evidence and transform give, and rewrite references to match."""
    stepped = [replace_strings(r, strings or {}) for r in rows]
    transforms = {r["id"]: _read(r) for r in stepped if r["kind"] == "transform_record"}
    ids: dict[str, str] = {}
    for row in stepped:
        if row["kind"] in LEDGER_KINDS:
            continue
        model = _read(row)
        evidence = model.provenance.evidence
        locator = canonical_json.dumps(evidence.locator_json()).decode()
        assert "rec:" not in locator, "a locator holding a tier-2 id cannot be rebased"
        transform = transforms[model.provenance.transform]
        ids[row["id"]] = evidence_record_id(row["kind"], evidence, transform)
    return [replace_strings(r, ids) for r in stepped]


def new_transform(old: Record, version: str, config: Mapping[str, Any] | None = None) -> Record:
    """``old``'s adapter at another version or config, as a transform record's JSON."""
    made = transform_record(
        adapter_id=old["adapter_id"],
        adapter_version=version,
        config=config if config is not None else old["config"],
        libraries=old["libraries"],
        upstream=old["upstream"],
    )
    value = canonical_json.loads(canonical_json.dumps(made.to_json()))
    assert isinstance(value, dict)
    return value


def artifact(data: bytes, chunk_size: int = 8388608) -> Record:
    """The source artifact of ``data``."""
    return {
        "chunk_size": chunk_size,
        "chunks": [
            "sha256:" + hashlib.sha256(data[at : at + chunk_size]).hexdigest()
            for at in range(0, max(len(data), 1), chunk_size)
        ],
        "content_id": "sha256:" + hashlib.sha256(data).hexdigest(),
        "kind": "source_artifact",
        "schema_version": 1,
        "size": len(data),
    }


def revision(location: Record, content_id: str, supersedes: tuple[str, ...] = ()) -> Record:
    """A source revision of ``content_id`` at ``location`` superseding ``supersedes``."""
    return {
        "content_id": content_id,
        "id": revision_id(location_from_json(location), content_id, supersedes),  # type: ignore[arg-type]
        "kind": "source_revision",
        "location": location,
        "schema_version": 1,
        "supersedes": list(supersedes),
    }


def resourced(
    rows: list[Record], data: bytes, location: Record | None = None, supersede: bool = False
) -> list[Record]:
    """The same adapter output over other bytes: a new artifact and revision (at ``location``,
    or the old one; superseding the old revision when ``supersede``), every record re-identified."""
    (old_artifact,) = [r for r in rows if r["kind"] == "source_artifact"]
    (old_revision,) = [r for r in rows if r["kind"] == "source_revision"]
    new_artifact = artifact(data, old_artifact["chunk_size"])
    content = new_artifact["content_id"]
    new_revision = revision(
        location or old_revision["location"],
        content,
        (old_revision["id"],) if supersede else (),
    )
    kept = [r for r in rows if r["kind"] not in ("source_artifact", "source_revision")]
    return [new_artifact, new_revision, *rebase(kept, {old_artifact["content_id"]: content})]


def reparsed(rows: list[Record], version: str) -> list[Record]:
    """The same bytes through the same adapter at ``version``: lineage siblings."""
    (old,) = [r for r in rows if r["kind"] == "transform_record"]
    new = new_transform(old, version)
    kept = [r for r in rows if r["kind"] != "transform_record"]
    return rebase([new, *kept], {old["id"]: new["id"]})


def edit(rows: list[Record], kind: str, change: Callable[[Record], Record]) -> list[Record]:
    """``change`` applied to every record of ``kind``; ids are not recomputed (call ``rebase``)."""
    return [change(r) if r["kind"] == kind else r for r in rows]


def files(rows: list[Record]) -> dict[str, bytes]:
    """The package files of these records, written by the compiler's package writer."""
    return package_files([_read(r) for r in rows])


def known(value: Any, provenance: Record | None = None) -> Record:
    out: Record = {"knowledge": "known", "value": value}
    if provenance is not None:
        out["provenance"] = provenance
    return out


def stated(record: Record) -> Record:
    """The record's own evidence and transform, asserted ``stated`` (a manifest's declaration)."""
    provenance = record["provenance"]
    return {**provenance, "assertion_kind": "stated"}


def timestamp(domain: str, ticks: int) -> Record:
    return {"domain_id": domain, "ticks": ticks}


def logical(namespace: str, value: str) -> Record:
    return {"namespace": namespace, "value": value}
