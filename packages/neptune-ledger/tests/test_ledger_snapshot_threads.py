"""Configuration snapshots open their anchored configuration thread (Ledger ADR 0017).

A ``configuration_snapshot`` is the ``subject`` of the anchored ``configuration`` thread its own
record-level evidence keys (ADR 0003 §1.2), so its thread id is ADR 0003 §1.3's rule over that
evidence: the id Memory's ADR 0022 §1 computes for a pinned snapshot today. The demo corpus is
frozen in ``fixtures/acceptance_corpus_2_1_0/``: the ``configuration_snapshot`` and
``snapshot_binding`` lines the compiler wrote for Platform's acceptance corpus 2.1.0 (PR #150 at
3e547000), copied so Ledger tests never run ingestion. The compiler's manifest goldens
(``tests/golden/manifest/``) are read live, as the catalog walkthrough reads its goldens.
"""

import hashlib
import json
import random
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

from conftest import new_database
from neptune.identity import canonical_json
from neptune_ledger.api.types import EvidenceAnchor, History, LatestTransform, ThreadKey
from neptune_ledger.catalog.migrate import apply_migrations, migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.cli import main
from neptune_ledger.contract_tests.examples import WorkedPackage, at_schema_2, materialise, write
from neptune_ledger.threads.membership import ThreadRows, thread_rows
from test_ledger_registration import dump, fresh

Record = dict[str, Any]
Conn = psycopg.Connection[tuple[object, ...]]
HERE: Final = Path(__file__).resolve().parent
CORPUS: Final = HERE / "fixtures" / "acceptance_corpus_2_1_0"
GOLDEN: Final = HERE.parents[2] / "tests" / "golden" / "manifest"
EMBODIMENTS: Final = ("aerial_survey", "amr_fleet", "manipulator_cell")
SNAPSHOT: Final = "records/configuration_snapshot.jsonl"
T1: Final = "rec:sha256:" + "1" * 64
T2: Final = "rec:sha256:" + "2" * 64


def lines(path: Path) -> list[Record]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def corpus(kind: str) -> list[Record]:
    return lines(CORPUS / f"{kind}.jsonl")


def golden(name: str) -> list[Record]:
    return lines(GOLDEN / name / SNAPSHOT)


def rows(records: list[Record]) -> ThreadRows:
    table: dict[str, list[bytes]] = {}
    for r in records:
        table.setdefault(r["kind"], []).append(canonical_json.dumps(r))
    return thread_rows({kind: tuple(found) for kind, found in table.items()})


def memory_thread_id(snapshot: Mapping[str, Any]) -> str:
    """Memory ADR 0022 §1 (``threads.anchored_node`` in Memory PR #154), restated: the anchored
    configuration thread of the snapshot's record-level evidence, by ADR 0003 §1.3. Written out
    here, independent of the code under test; Ledger tests never import Memory."""
    key = {"key": snapshot["provenance"]["evidence"], "kind": "configuration"}
    return "sha256:" + hashlib.sha256(canonical_json.dumps(key)).hexdigest()


def snapshot(source: str, *, digest: str = "a", assertion: str = "observed") -> Record:
    """A minimal snapshot: membership reads only its id, kind and provenance."""
    return {
        "digest": "sha256:" + digest * 64,
        "id": "rec:sha256:" + hashlib.sha256(source.encode() + assertion.encode()).hexdigest(),
        "kind": "configuration_snapshot",
        "provenance": {
            "assertion_kind": assertion,
            "evidence": {
                "locator": [
                    {"index": 0, "kind": "config:document"},
                    {"kind": "json_pointer", "pointer": ""},
                ],
                "source": source,
            },
            "transform": T1,
        },
    }


def subjects(found: ThreadRows) -> dict[str, list[str]]:
    """Each member record's thread ids, for ``subject`` entries."""
    out: dict[str, list[str]] = {}
    for m in found.members:
        assert m.roles == ("subject",)
        out.setdefault(m.record_id, []).append(m.thread_id)
    return out


# --- the row (ADR 0017 §1) ---------------------------------------------------------------------


def test_a_snapshot_opens_the_configuration_thread_its_own_evidence_keys() -> None:
    record = snapshot("sha256:" + "c" * 64)
    found = rows([record])
    evidence = record["provenance"]["evidence"]
    key = ThreadKey("configuration", EvidenceAnchor(evidence["source"], tuple(evidence["locator"])))
    ((thread_id, kind, key_json),) = [(t.thread_id, t.kind, t.key) for t in found.threads]
    assert (thread_id, kind) == (key.thread_id, "configuration")
    assert json.loads(key_json) == {"key": evidence, "kind": "configuration"}
    (member,) = found.members
    assert (member.record_id, member.kind, member.roles) == (
        record["id"],
        "configuration_snapshot",
        ("subject",),
    )
    assert (member.transform_id, member.source_content_id) == (T1, evidence["source"])
    assert (member.world_clock, member.world_first, member.world_last) == (None, None, None)
    assert json.loads(member.world) == {"knowledge": "not_applicable"}
    assert found.unresolved == ()
    assert thread_id == memory_thread_id(record)


def test_equal_values_on_other_evidence_are_two_threads() -> None:
    """Two parameter files that declare equal values (one digest) are two configurations: the
    digest is a reader's comparison, never an identity join (ADR 0017 §2)."""
    one, two = snapshot("sha256:" + "c" * 64), snapshot("sha256:" + "d" * 64)
    assert one["digest"] == two["digest"]
    found = subjects(rows([one, two]))
    assert len({t for ids in found.values() for t in ids}) == 2


def test_two_transforms_over_one_document_are_siblings_in_one_thread() -> None:
    """A re-ingest under another config adapter version cites the same evidence the same way:
    one thread, two entries, one lineage set (ADR 0003 §4.1)."""
    first = snapshot("sha256:" + "c" * 64)
    second = {**first, "id": "rec:sha256:" + "9" * 64}
    second["provenance"] = {**first["provenance"], "transform": T2}
    found = rows([first, second])
    (thread,) = found.threads
    assert {(m.record_id, m.transform_id) for m in found.members} == {
        (first["id"], T1),
        (second["id"], T2),
    }
    assert {m.thread_id for m in found.members} == {thread.thread_id}


def test_inferred_or_anchorless_snapshots_open_nothing_and_never_raise() -> None:
    """ADR 0010 §2: only stated or observed records with a record-level anchor join a thread.
    The catalog refuses such a line at registration (below); membership itself never raises."""
    inferred = snapshot("sha256:" + "c" * 64, assertion="inferred")
    no_locator = snapshot("sha256:" + "d" * 64)
    no_locator["provenance"]["evidence"]["locator"] = []
    no_evidence = snapshot("sha256:" + "e" * 64)
    del no_evidence["provenance"]["evidence"]
    no_provenance = {"id": "rec:sha256:" + "f" * 64, "kind": "configuration_snapshot"}
    external = snapshot("sha256:" + "a" * 64)
    external["provenance"]["evidence"]["source"] = {"uri": "s3://bucket/params.yaml"}
    malformed = [inferred, no_locator, no_evidence, no_provenance, external]
    assert rows(malformed) == ThreadRows((), (), ())
    good = snapshot("sha256:" + "b" * 64)
    assert list(subjects(rows([*malformed, good]))) == [good["id"]]


@pytest.mark.parametrize(
    "kind", ["configuration_value", "snapshot_binding", "run_declaration", "task_brief"]
)
def test_kinds_adr_0017_leaves_unthreaded_join_no_thread(kind: str) -> None:
    """ADR 0017 §3: a value, a binding, a run declaration and a task brief open nothing, even
    beside the snapshot they name."""
    parent = snapshot("sha256:" + "c" * 64)
    other = {**snapshot("sha256:" + "d" * 64), "kind": kind, "snapshot": parent["id"]}
    assert list(subjects(rows([parent, other]))) == [parent["id"]]


def test_membership_is_byte_identical_whatever_the_line_order() -> None:
    records = [*corpus("configuration_snapshot"), *corpus("snapshot_binding")]
    expected = rows(records)
    for seed in range(5):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        assert rows(shuffled) == expected
    assert repr(rows(records)).encode() == repr(expected).encode()


# --- the demo corpus and the compiler's goldens ----------------------------------------------


def test_every_corpus_snapshot_has_exactly_the_thread_memory_computes() -> None:
    """Acceptance corpus 2.1.0: 12 snapshots, each the subject of one configuration thread whose
    id is byte-identical to Memory's ADR 0022 node, so Memory can drop its workaround."""
    snapshots = corpus("configuration_snapshot")
    assert len(snapshots) == 12
    found = subjects(rows(snapshots))
    assert found == {s["id"]: [memory_thread_id(s)] for s in snapshots}
    assert len({memory_thread_id(s) for s in snapshots}) == 12


def test_all_ten_configuration_snapshot_pins_resolve_to_one_thread() -> None:
    """The 10 run-sheet pins to a parameter document (Memory ADR 0022, Context) each name a
    snapshot the package holds, and each such snapshot has exactly one thread."""
    bindings = corpus("snapshot_binding")
    pins = [b for b in bindings if b["snapshot_kind"] == "configuration_snapshot"]
    assert len(pins) == 10
    held = {s["id"]: s for s in corpus("configuration_snapshot")}
    found = subjects(rows([*held.values(), *bindings]))
    for pin in pins:
        assert found[pin["snapshot"]] == [memory_thread_id(held[pin["snapshot"]])]
    assert set(found) == set(held)  # bindings themselves open nothing


@pytest.mark.parametrize("name", EMBODIMENTS)
def test_the_compilers_manifest_golden_snapshots_have_their_thread(name: str) -> None:
    snapshots = golden(name)
    assert snapshots
    assert subjects(rows(snapshots)) == {s["id"]: [memory_thread_id(s)] for s in snapshots}


# --- the catalog -------------------------------------------------------------------------------


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


def schema_2(tmp_path: Path, name: str = "drone-v2", version: str = "2.0.0") -> WorkedPackage:
    """The drone at package schema 2, with a controller parameter file's snapshot."""
    return write(name, tmp_path / name, at_schema_2("drone", version, {}))


def test_threads_of_a_registered_snapshot_names_its_configuration_thread(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    package = schema_2(tmp_path)
    assert catalog.register(package.root).outcome == "registered"
    (record,) = package.records("configuration_snapshot")
    found = catalog.threads_of(record["id"])
    assert (found.status, found.unresolved, found.findings) == ("found", (), ())
    (membership,) = found.memberships
    assert membership.thread_id == memory_thread_id(record)
    assert (membership.key.kind, membership.package_id, membership.roles) == (
        "configuration",
        package.package_id,
        ("subject",),
    )
    thread = catalog.thread(membership.key, "world", History())
    assert [(p.kind, [(e.record_id, e.kind) for e in p.entries]) for p in thread.partitions] == [
        ("untimed", [(record["id"], "configuration_snapshot")])
    ]
    assert [(s.kind, s.source) for s in thread.lineage_sets] == [
        ("configuration_snapshot", record["provenance"]["evidence"]["source"])
    ]


def test_one_snapshot_in_two_packages_is_one_thread_with_both_registrations(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    first, second = schema_2(tmp_path), schema_2(tmp_path, "drone-v2b", "2.1.0")
    for package in (first, second):
        assert catalog.register(package.root).outcome == "registered"
    (record,) = first.records("configuration_snapshot")
    found = catalog.threads_of(record["id"])
    assert sorted(m.package_id for m in found.memberships) == sorted(
        (first.package_id, second.package_id)
    )
    (key,) = {m.key for m in found.memberships}
    current = catalog.thread(key, "world", LatestTransform())
    ((entry,),) = [p.entries for p in current.partitions]
    assert entry.record_id == record["id"] and set(entry.packages) == {
        first.package_id,
        second.package_id,
    }


def test_a_snapshot_line_without_evidence_is_record_invalid_and_writes_nothing(
    catalog: PostgresCatalog, pg: Conn, pg_uri: str, tmp_path: Path
) -> None:
    files = dict(at_schema_2("drone", "2.0.0", {}))
    (line,) = files[SNAPSHOT].splitlines()
    body = canonical_json.loads(line)
    assert isinstance(body, dict)
    body["provenance"] = {k: v for k, v in body["provenance"].items() if k != "evidence"}
    data = canonical_json.dumps(body) + b"\n"
    files[SNAPSHOT] = data
    manifest = canonical_json.loads(files["manifest.json"])
    assert isinstance(manifest, dict)
    manifest["files"] = [
        {
            "path": SNAPSHOT,
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        if f["path"] == SNAPSHOT
        else f
        for f in manifest["files"]
    ]
    files["manifest.json"] = canonical_json.dumps(manifest)
    package = write("drone-bad", tmp_path / "drone-bad", files)
    result = catalog.register(package.root)
    assert result.outcome == "refused"
    assert [f.code for f in result.findings] == ["record_invalid"]
    with psycopg.connect(pg_uri) as conn:
        assert not any(dump(conn, "tenant_acme")[t] for t in ("package", "thread", "thread_member"))


# --- migration 0012 and rebuild (ADR 0017 §4) -------------------------------------------------


def test_migration_0012_refuses_a_catalog_holding_unthreaded_snapshots(
    pg: Conn, pg_uri: str, tmp_path: Path
) -> None:
    """A snapshot registered under the old membership has no thread rows, and the thread tables
    are append-only: the migration refuses and the catalog is rebuilt. The old catalog has every
    other migration (13's ``stream_ids`` column too, which today's registration fills)."""
    shipped = migrations()
    (last,) = [m for m in shipped if m.name == "configuration_snapshot_threads"]
    assert last.version == 12
    apply_migrations(pg, "acme", shipped=[m for m in shipped if m.version != 12])
    with PostgresCatalog(pg_uri, "acme", package_roots=None) as old:
        assert old.register(schema_2(tmp_path).root).outcome == "registered"
    with pytest.raises(psycopg.errors.RaiseException, match="rebuild this catalog"):
        apply_migrations(pg, "acme")


def test_migration_0012_applies_to_a_catalog_without_snapshots(
    pg: Conn, pg_uri: str, tmp_path: Path
) -> None:
    apply_migrations(pg, "acme", shipped=[m for m in migrations() if m.version != 12])
    with PostgresCatalog(pg_uri, "acme", package_roots=None) as old:
        assert old.register(materialise("drone", tmp_path / "drone").root).outcome == "registered"
    assert apply_migrations(pg, "acme") == [12]


@pytest.mark.integration
def test_a_rebuild_from_packages_writes_the_snapshot_threads(
    pg_server: str, pg_uri: str, tmp_path: Path
) -> None:
    """MVL-94's rebuild replays registration, so a catalog rebuilt from its packages holds the
    snapshot threads, byte for byte as a direct registration writes them."""
    packages = [materialise("drone", tmp_path / "packages" / "drone")]
    packages.append(schema_2(tmp_path / "packages"))
    roots = ("--package-root", str(tmp_path / "packages"))
    manifest = tmp_path / "manifest.json"
    db = ("--dsn", pg_uri, "--tenant", "acme")
    assert main([*db, "migrate"]) == 0
    for package in packages:
        assert main([*db, "--manifest", str(manifest), "register", str(package.root), *roots]) == 0
    with psycopg.connect(pg_uri) as conn:
        before = dump(conn, "tenant_acme")
    (record,) = packages[1].records("configuration_snapshot")
    assert any(record["id"] in row for row in before["thread_member"])

    fresh_uri = new_database(pg_server)
    rebuilt = ("--dsn", fresh_uri, "--tenant", "acme")
    assert main([*rebuilt, "rebuild", "--from", str(manifest), *roots]) == 0
    with psycopg.connect(fresh_uri) as conn:
        assert dump(conn, "tenant_acme") == before
    with PostgresCatalog(fresh_uri, "acme", package_roots=None) as catalog:
        (membership,) = catalog.threads_of(record["id"]).memberships
        assert membership.thread_id == memory_thread_id(record)
