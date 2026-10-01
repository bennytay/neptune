"""The catalog contract as pytest tests, for any ``CatalogApi`` implementation.

Subclass ``CatalogContract`` in a module pytest collects, name the subclass ``Test…`` and return a
fresh, empty catalog (one tenant) from ``make_catalog``::

    class TestMyCatalog(CatalogContract):
        def make_catalog(self, workdir: Path) -> CatalogApi:
            return MyCatalog(workdir)

Set ``expected_failure`` to an exception type to run the suite as strict expected failures: each
test that calls the API must fail with exactly that exception, and a pass is an error. The
Ledger's ``StubCatalog`` runs this way (ADR 0004 §6). Tests that need no implementation are not in
this class; they live with the API's own unit tests.
"""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import jsonschema
import pytest

from neptune.model.knowledge import Ambiguous, Candidate, Known, NotApplicable, NotCovered, Unknown
from neptune_ledger.api import arrow, codec
from neptune_ledger.api.protocol import CatalogApi
from neptune_ledger.api.types import (
    CATALOG_API_VERSION,
    AsRegisteredBy,
    DeclaredKey,
    EvidenceAnchor,
    History,
    LatestTransform,
    Pinned,
    QueryRow,
    QuerySpec,
    RecordRef,
    Registration,
    ThreadKey,
    TimeWindow,
    TransformInfo,
    WorldTime,
)
from neptune_ledger.contract_tests.examples import (
    EXAMPLES,
    WorkedPackage,
    evidence_anchor,
    machine_threads,
    materialise,
    query_row,
    record_key,
    reparse,
    transform_of,
    with_source_size,
    world_time,
    write,
)

if TYPE_CHECKING:
    from collections.abc import Callable

UNKNOWN_ID = "sha256:" + "0" * 64
NOWHERE = EvidenceAnchor("sha256:" + "f" * 64, ({"kind": "byte_range", "length": 1, "offset": 0},))


def _codes(findings: Any) -> set[str]:
    return {f.code for f in findings}


def _tamper(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"\n")


def _first_table(package: WorkedPackage) -> str:
    """A non-empty record table of the package, by path."""
    for kind, _, _ in package.every_record():
        return f"records/{kind}.jsonl"
    raise AssertionError(f"{package.name} has no records")


def _validate(document: object) -> None:
    """The response validates against the published JSON Schema at its record's pointer."""
    schema = codec.catalog_schema()
    name = type(document).__name__
    validator = jsonschema.Draft202012Validator({**schema, "$ref": f"#/$defs/{name}"})
    errors = sorted(validator.iter_errors(codec.to_json(document)), key=str)
    assert not errors, f"{name}: {errors[0].message}"


class CatalogContract:
    """Golden calls over the four worked examples, error cases and determinism."""

    expected_failure: ClassVar[type[BaseException] | None] = None

    def make_catalog(self, workdir: Path) -> CatalogApi:
        raise NotImplementedError("subclass CatalogContract and return a fresh catalog")

    # --- fixtures ------------------------------------------------------------------------------

    @pytest.fixture
    def catalog(self, request: pytest.FixtureRequest, tmp_path: Path) -> CatalogApi:
        if self.expected_failure is not None:
            request.applymarker(
                pytest.mark.xfail(
                    raises=self.expected_failure,
                    strict=True,
                    reason="the implementation is declared incomplete (expected_failure)",
                )
            )
        workdir = tmp_path / "catalog"
        workdir.mkdir()
        return self.make_catalog(workdir)

    @pytest.fixture
    def packages(self, tmp_path: Path) -> dict[str, WorkedPackage]:
        return {name: materialise(name, tmp_path / "packages" / name) for name in EXAMPLES}

    def register_all(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> dict[str, Registration]:
        done = {name: catalog.register(packages[name].root) for name in EXAMPLES}
        for registration in done.values():
            assert registration.outcome == "registered", registration.findings
        return done

    # --- register ------------------------------------------------------------------------------

    def test_register_each_worked_example(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        seqs: list[int] = []
        times: list[str] = []
        for name in EXAMPLES:
            package = packages[name]
            result = catalog.register(package.root)
            _validate(result)
            assert result.outcome == "registered"
            assert result.package_id == Known(package.package_id)
            assert result.schema_version == Known(1)
            assert result.record_counts == package.record_counts()
            assert result.root_locator == str(package.root.resolve())
            assert result.findings == ()
            assert result.api_version == CATALOG_API_VERSION
            assert isinstance(result.registration_key, Known)
            seqs.append(result.registration_key.value.tx_seq)
            times.append(result.registration_key.value.tx_time)
        assert seqs == sorted(set(seqs)), "tx_seq strictly increases"
        assert times == sorted(times), "tx_time never decreases"

    def test_identical_reregistration_is_a_noop(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage], tmp_path: Path
    ) -> None:
        first = catalog.register(packages["drone"].root)
        again = catalog.register(packages["drone"].root)
        copy = tmp_path / "elsewhere"
        shutil.copytree(packages["drone"].root, copy)
        moved = catalog.register(copy)
        for result in (again, moved):
            assert result.outcome == "already_registered"
            assert result.package_id == first.package_id
            assert result.registration_key == first.registration_key
            assert result.root_locator == first.root_locator, "the stored locator stands"
            assert result.findings == ()
        after = catalog.register(packages["quadruped"].root)
        assert isinstance(first.registration_key, Known)
        assert isinstance(after.registration_key, Known)
        assert after.registration_key.value.tx_seq == first.registration_key.value.tx_seq + 1

    def test_tampered_package_is_refused_and_nothing_is_written(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        package = packages["manipulator"]
        table = _first_table(package)
        _tamper(package.root / table)
        result = catalog.register(package.root)
        _validate(result)
        assert result.outcome == "refused"
        assert result.package_id == Known(package.package_id)
        assert result.registration_key == NotApplicable()
        assert result.record_counts == ()
        assert ("file_digest_mismatch", table) in {(f.code, f.subject) for f in result.findings}
        assert catalog.verify(package.package_id).verdict == "unknown_package"

    def test_unreadable_root_is_refused(self, catalog: CatalogApi, tmp_path: Path) -> None:
        result = catalog.register(tmp_path / "no-such-package")
        _validate(result)
        assert result.outcome == "refused"
        assert isinstance(result.package_id, Unknown)
        assert result.registration_key == NotApplicable()
        assert _codes(result.findings) == {"package_unreadable"}

    # --- verify --------------------------------------------------------------------------------

    def test_verify_intact_packages(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        registered = self.register_all(catalog, packages)
        for name in EXAMPLES:
            report = catalog.verify(packages[name].package_id)
            _validate(report)
            assert report.verdict == "intact"
            assert report.findings == ()
            assert report.files_checked == packages[name].listed_files()
            assert report.registration_key == registered[name].registration_key
            assert report.root_locator == Known(registered[name].root_locator)

    def test_verify_unknown_package(self, catalog: CatalogApi) -> None:
        report = catalog.verify(UNKNOWN_ID)
        _validate(report)
        assert report.verdict == "unknown_package"
        assert [(f.code, f.subject) for f in report.findings] == [("unknown_package", UNKNOWN_ID)]
        assert report.registration_key == NotCovered()
        assert report.root_locator == NotCovered()
        assert report.files_checked == 0

    def test_verify_reports_a_tampered_manifest(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        package = packages["quadruped"]
        catalog.register(package.root)
        _tamper(package.root / "manifest.json")
        report = catalog.verify(package.package_id)
        _validate(report)
        assert report.verdict == "damaged"
        assert ("manifest_digest_mismatch", "manifest.json") in {
            (f.code, f.subject) for f in report.findings
        }

    def test_verify_reports_a_tampered_record_table(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        package = packages["mobile_robot"]
        catalog.register(package.root)
        table = _first_table(package)
        _tamper(package.root / table)
        report = catalog.verify(package.package_id)
        assert report.verdict == "damaged"
        assert ("file_digest_mismatch", table) in {(f.code, f.subject) for f in report.findings}

    # --- resolve -------------------------------------------------------------------------------

    def test_resolve_every_cited_anchor(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        citing: dict[str, set[RecordRef]] = {}
        anchors: dict[str, EvidenceAnchor] = {}
        for name in EXAMPLES:
            package = packages[name]
            for kind, line, record in package.every_record():
                anchor = evidence_anchor(record)
                if anchor is not None:
                    text = codec.dumps(anchor).decode()
                    anchors[text] = anchor
                    citing.setdefault(text, set()).add(package.ref(kind, line, record))
        assert anchors, "the worked examples cite evidence"
        for text, anchor in sorted(anchors.items()):
            result = catalog.resolve(anchor)
            _validate(result)
            assert result.status == "resolved"
            assert result.findings == ()
            assert result.evidence_ref == anchor
            assert result.region.step == anchor.locator[-1]
            expected = sorted(citing[text], key=lambda r: (r.kind, r.record_id, r.package_id))
            assert list(result.cited_by) == expected
            holders = [packages[n] for n in EXAMPLES if packages[n].source(anchor.source)]
            assert [f.package_id for f in result.fetch] == [p.package_id for p in holders]
            for route, holder in zip(result.fetch, holders, strict=True):
                entry = holder.source(anchor.source)
                assert entry is not None
                assert route.storage == entry["storage"]
                assert result.size == Known(entry["size"])
                assert route.locations == holder.locations(anchor.source)
                if route.storage == "materialised":
                    assert route.blob_path == Known(holder.blob(anchor.source))
                else:
                    assert route.blob_path == NotApplicable()

    def test_resolve_unknown_source(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        result = catalog.resolve(NOWHERE)
        _validate(result)
        assert result.status == "unresolvable"
        assert [(f.code, f.subject) for f in result.findings] == [
            ("unresolvable_evidence", NOWHERE.source)
        ]
        assert result.size == NotCovered()
        assert (result.fetch, result.cited_by) == ((), ())

    # --- lineage -------------------------------------------------------------------------------

    def test_lineage_of_every_record(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        transforms = {
            r["id"]: r for name in EXAMPLES for r in packages[name].records("transform_record")
        }
        for name in EXAMPLES:
            package = packages[name]
            for kind, line, record in package.every_record():
                transform = transform_of(record)
                if transform is None:
                    continue
                result = catalog.lineage(record_key(record))
                _validate(result)
                assert result.status == "found"
                assert result.kind == Known(kind)
                assert result.transform_id == Known(transform)
                assert package.ref(kind, line, record) in result.registered_by
                nodes = {n.transform_id: n.transform for n in result.nodes}
                stated = transforms[transform]
                assert nodes[transform] == Known(
                    TransformInfo(
                        stated["adapter_id"],
                        stated["adapter_version"],
                        stated["config_hash"],
                        stated["libraries"],
                    )
                )
                edges = {
                    (e.upstream_id, e.position) for e in result.edges if e.transform_id == transform
                }
                assert edges == {(u, i) for i, u in enumerate(stated["upstream"])}
                assert [n.transform_id for n in result.nodes] == sorted(nodes)

    def test_lineage_of_an_unknown_record(self, catalog: CatalogApi) -> None:
        result = catalog.lineage("rec:" + UNKNOWN_ID)
        _validate(result)
        assert result.status == "unknown_record"
        assert _codes(result.findings) == {"unknown_record"}
        assert (result.registered_by, result.nodes, result.edges) == ((), (), ())

    # --- thread and threads_of -----------------------------------------------------------------

    def test_machine_threads_hold_exactly_their_declared_members(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        expected = machine_threads([packages[n] for n in EXAMPLES])
        assert expected, "the worked examples declare at least one machine"
        records = {
            (packages[n].package_id, record_key(r)): r
            for n in EXAMPLES
            for _, _, r in packages[n].every_record()
        }
        for key, members in expected.items():
            thread = catalog.thread(key, "world", History())
            _validate(thread)
            assert thread.thread_id == key.thread_id
            assert thread.preference == History()
            assert thread.findings == ()
            got: set[tuple[str, str, str]] = set()
            for partition in thread.partitions:
                for entry in partition.entries:
                    assert len(entry.packages) == 1, "history keeps one entry per package"
                    got |= {(entry.packages[0], entry.record_id, role) for role in entry.roles}
                    stated = world_time(records[(entry.packages[0], entry.record_id)])
                    assert codec.to_json(entry)["world"] == stated  # type: ignore[index, call-overload]
            assert got == members
            expected_sets = {
                (records[(p, r)]["kind"], evidence_anchor(records[(p, r)]).source)  # type: ignore[union-attr]
                for p, r, _ in members
            }
            got_sets = {(s.kind, s.source) for s in thread.lineage_sets}
            assert got_sets == expected_sets and len(thread.lineage_sets) == len(expected_sets)
            assert {s.resolution for s in thread.lineage_sets} == {NotApplicable()}

    def test_world_order_partitions_by_clock(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        for key in machine_threads([packages[n] for n in EXAMPLES]):
            thread = catalog.thread(key, "world", History())
            kinds = [p.kind for p in thread.partitions]
            assert set(kinds) <= {"clock", "untimed"}
            assert "untimed" not in kinds[:-1], "the untimed partition comes last"
            for partition in thread.partitions:
                if partition.kind == "untimed":
                    assert partition.clock_key is None
                    assert partition.entries, "a partition is never empty"
                    assert all(not isinstance(e.world, Known) for e in partition.entries)
                    continue
                assert partition.clock_key is not None
                sort_keys = []
                for entry in partition.entries:
                    assert isinstance(entry.world, Known)
                    world: WorldTime = entry.world.value
                    assert world.clock == partition.clock_key
                    closed = world.closed_end
                    end = (0, closed) if closed is not None else (1, 0)
                    sort_keys.append(
                        (
                            world.start.ticks,
                            end,
                            entry.registration_key.tx_seq,
                            entry.record_id.encode(),
                            entry.packages[0].encode(),
                        )
                    )
                assert sort_keys == sorted(sort_keys)

    def test_current_view_is_within_history(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        for key in machine_threads([packages[n] for n in EXAMPLES]):
            history = catalog.thread(key, "world", History())
            current = catalog.thread(key, "world", LatestTransform())
            _validate(current)
            every = {e.record_id for p in history.partitions for e in p.entries}
            assert {e.record_id for p in current.partitions for e in p.entries} <= every
            for lineage_set in current.lineage_sets:
                assert lineage_set.resolution.state.value in ("known", "ambiguous", "not_covered")

    # --- current-view resolution over synthetic lineage siblings (ADR 0003 §4.4) ---------------

    @pytest.fixture
    def siblings(self, tmp_path: Path) -> dict[str, WorkedPackage]:
        """The drone at ulog 1.0.0 (its own config), 1.0.0 with another config, and 2.0.0."""
        root = tmp_path / "siblings"
        return {
            "v1a": materialise("drone", root / "v1a"),
            "v1b": write("v1b", root / "v1b", reparse("drone", "1.0.0", {"variant": "b"})),
            "v2": write("v2", root / "v2", reparse("drone", "2.0.0", {})),
            "other": materialise("quadruped", root / "other"),
        }

    def _current(
        self, catalog: CatalogApi, sibling: WorkedPackage, preference: Any
    ) -> tuple[Any, set[tuple[str, str]]]:
        """The current view of the drone's machine thread, and its entries as (package, record)."""
        (key,) = machine_threads([sibling])
        thread = catalog.thread(key, "world", preference)
        _validate(thread)
        assert thread.findings == ()
        entries = {
            (p, e.record_id) for part in thread.partitions for e in part.entries for p in e.packages
        }
        return thread, entries

    def _assert_resolved(
        self, catalog: CatalogApi, sibling: WorkedPackage, thread: Any, expected: Any
    ) -> None:
        """The view lists exactly history's lineage sets, each resolved to ``expected``."""
        (key,) = machine_threads([sibling])
        history = catalog.thread(key, "world", History())
        sets = {(s.kind, s.source) for s in history.lineage_sets}
        assert sets, "the drone's machine thread has lineage sets"
        assert len(history.lineage_sets) == len(sets)
        got = {(s.kind, s.source): s.resolution for s in thread.lineage_sets}
        assert len(thread.lineage_sets) == len(got) == len(sets)
        assert set(got) == sets
        for lineage_set, resolution in sorted(got.items()):
            assert resolution == expected, lineage_set

    @staticmethod
    def _members(package: WorkedPackage) -> set[tuple[str, str]]:
        (members,) = machine_threads([package]).values()
        return {(p, r) for p, r, _ in members}

    @staticmethod
    def _transform(package: WorkedPackage) -> str:
        (row,) = package.records("transform_record")
        return row["id"]  # type: ignore[no-any-return]

    def test_latest_transform_resolves_to_the_dominant_version(
        self, catalog: CatalogApi, siblings: dict[str, WorkedPackage]
    ) -> None:
        for name in ("v1a", "v1b", "v2"):
            assert catalog.register(siblings[name].root).outcome == "registered"
        thread, entries = self._current(catalog, siblings["v2"], LatestTransform())
        v2 = self._transform(siblings["v2"])
        everything = sorted(self._transform(siblings[n]) for n in ("v1a", "v1b", "v2"))
        self._assert_resolved(catalog, siblings["v2"], thread, Known(v2))  # {v1a, v1b, v2}
        for lineage_set in thread.lineage_sets:
            assert list(lineage_set.transforms) == everything
        assert entries == self._members(siblings["v2"])

    def test_latest_transform_is_ambiguous_between_equal_versions(
        self, catalog: CatalogApi, siblings: dict[str, WorkedPackage]
    ) -> None:
        for name in ("v1a", "v1b"):
            catalog.register(siblings[name].root)
        thread, entries = self._current(catalog, siblings["v1a"], LatestTransform())
        tied = sorted(self._transform(siblings[n]) for n in ("v1a", "v1b"))
        expected = Ambiguous(tuple(Candidate(t) for t in tied))
        self._assert_resolved(catalog, siblings["v1a"], thread, expected)
        assert entries == set(), "an Ambiguous lineage set selects no records"

    def test_pinned_selects_exactly_one_transform(
        self, catalog: CatalogApi, siblings: dict[str, WorkedPackage]
    ) -> None:
        for name in ("v1a", "v1b", "v2"):
            catalog.register(siblings[name].root)
        v1b = self._transform(siblings["v1b"])
        thread, entries = self._current(catalog, siblings["v1b"], Pinned(v1b))
        self._assert_resolved(catalog, siblings["v1b"], thread, Known(v1b))
        assert entries == self._members(siblings["v1b"])
        absent = "rec:sha256:" + "9" * 64
        thread, entries = self._current(catalog, siblings["v1b"], Pinned(absent))
        self._assert_resolved(catalog, siblings["v1b"], thread, NotCovered())
        assert entries == set(), "pinned never falls back"

    def test_as_registered_by_follows_one_package(
        self, catalog: CatalogApi, siblings: dict[str, WorkedPackage]
    ) -> None:
        for name in ("v1a", "v1b", "v2", "other"):
            catalog.register(siblings[name].root)
        v1a = siblings["v1a"]
        thread, entries = self._current(catalog, v1a, AsRegisteredBy(v1a.package_id))
        self._assert_resolved(catalog, v1a, thread, Known(self._transform(v1a)))
        assert entries == self._members(v1a)
        other = siblings["other"].package_id
        thread, entries = self._current(catalog, v1a, AsRegisteredBy(other))
        self._assert_resolved(catalog, v1a, thread, NotCovered())
        assert entries == set()

    def test_conflicting_id_is_refused_and_nothing_is_written(
        self, catalog: CatalogApi, tmp_path: Path
    ) -> None:
        original = materialise("drone", tmp_path / "drone")
        (source,) = original.records("source_artifact")
        liar = write("liar", tmp_path / "liar", with_source_size("drone", source["size"] + 1))
        assert catalog.register(original.root).outcome == "registered"
        result = catalog.register(liar.root)
        _validate(result)
        assert result.outcome == "refused"
        assert result.registration_key == NotApplicable()
        assert ("conflicting_id", source["content_id"]) in {
            (f.code, f.subject) for f in result.findings
        }
        assert catalog.verify(liar.package_id).verdict == "unknown_package"

    def test_as_of_beyond_the_catalog_is_refused(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        first = catalog.register(packages["drone"].root)
        assert isinstance(first.registration_key, Known)
        beyond = first.registration_key.value.tx_seq + 1000
        key = next(iter(machine_threads([packages["drone"]])))
        thread = catalog.thread(key, "world", History(), as_of=beyond)
        _validate(thread)
        assert _codes(thread.findings) == {"as_of_out_of_range"}
        assert thread.partitions == ()
        table = catalog.query(QuerySpec(kinds=("run",), as_of=beyond))
        assert table.num_rows == 0
        assert _codes(arrow.query_meta(table).findings) == {"as_of_out_of_range"}
        report = catalog.verify(packages["drone"].package_id, as_of=beyond)
        assert _codes(report.findings) == {"as_of_out_of_range"}

    def test_thread_without_a_preference_is_rejected(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        key = next(iter(machine_threads([packages[n] for n in EXAMPLES])))
        thread = catalog.thread(key, "world", None)  # type: ignore[arg-type]
        _validate(thread)
        assert _codes(thread.findings) == {"preference_required"}
        assert thread.preference is None
        assert (thread.partitions, thread.lineage_sets) == ((), ())

    def test_unknown_thread_is_empty_not_an_error(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        key = ThreadKey("machine", DeclaredKey("serial", "no-such-robot"))
        thread = catalog.thread(key, "world", History())
        assert thread.partitions == ()
        assert thread.findings == ()
        assert isinstance(thread.as_of, Known)

    def test_threads_of_reports_every_machine_membership(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        for key, members in machine_threads([packages[n] for n in EXAMPLES]).items():
            for package_id, record_id, role in members:
                result = catalog.threads_of(record_id)
                _validate(result)
                assert result.status == "found"
                found = {
                    (m.thread_id, m.package_id, r) for m in result.memberships for r in m.roles
                }
                assert (key.thread_id, package_id, role) in found

    def test_as_of_replays_an_earlier_catalog_point(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        first = catalog.register(packages["drone"].root)
        catalog.register(packages["mobile_robot"].root)
        assert isinstance(first.registration_key, Known)
        point = first.registration_key.value
        key = next(iter(machine_threads([packages["drone"]])))
        thread = catalog.thread(key, "world", History(), as_of=point.tx_seq)
        assert thread.as_of == Known(point)
        table = catalog.query(QuerySpec(kinds=("run",), as_of=point.tx_seq))
        assert arrow.query_meta(table).as_of == Known(point)
        assert {r.package_id for r in arrow.query_rows(table)} == {packages["drone"].package_id}

    # --- query ---------------------------------------------------------------------------------

    def _expected_rows(
        self, packages: dict[str, WorkedPackage], registered: dict[str, Registration], kind: str
    ) -> list[QueryRow]:
        rows = []
        for name in EXAMPLES:
            package, key = packages[name], registered[name].registration_key
            assert isinstance(key, Known)
            for line, record in enumerate(package.records(kind), start=1):
                rows.append(query_row(package, kind, line, record, key.value.tx_seq))
        return sorted(rows, key=lambda r: (r.kind, r.record_id.encode(), r.package_id.encode()))

    def test_query_by_kind_returns_catalog_rows(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        registered = self.register_all(catalog, packages)
        for kind in ("run", "stream"):
            table = catalog.query(QuerySpec(kinds=(kind,)))
            assert table.schema.remove_metadata().equals(arrow.QUERY_RESULT_SCHEMA)
            meta = arrow.query_meta(table)
            _validate(meta)
            assert meta.findings == ()
            assert list(arrow.query_rows(table)) == self._expected_rows(packages, registered, kind)

    def test_query_time_window_stays_on_one_clock(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        registered = self.register_all(catalog, packages)
        rows = [r for r in self._expected_rows(packages, registered, "run") if r.world_clock]
        assert rows, "the worked examples have timed runs"
        target = rows[0]
        assert target.world_clock is not None and target.world_first is not None
        window = TimeWindow(target.world_clock, target.world_first, target.world_first)
        hits = arrow.query_rows(catalog.query(QuerySpec(kinds=("run",), window=window)))
        assert target in hits
        assert all(r.world_clock == target.world_clock for r in hits)
        elsewhere = TimeWindow("rec:" + UNKNOWN_ID, -(2**62), 2**62)
        assert arrow.query_rows(catalog.query(QuerySpec(kinds=("run",), window=elsewhere))) == ()

    def test_query_rejects_an_inverted_window(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        window = TimeWindow("rec:" + UNKNOWN_ID, 10, 9)
        table = catalog.query(QuerySpec(kinds=("run",), window=window))
        assert table.num_rows == 0
        assert _codes(arrow.query_meta(table).findings) == {"invalid_request"}

    # --- determinism ---------------------------------------------------------------------------

    def test_same_call_twice_gives_identical_bytes(
        self, catalog: CatalogApi, packages: dict[str, WorkedPackage]
    ) -> None:
        self.register_all(catalog, packages)
        drone = packages["drone"]
        record = next(r for _, _, r in drone.every_record() if evidence_anchor(r) is not None)
        anchor = evidence_anchor(record)
        assert anchor is not None
        key = next(iter(machine_threads([drone])))
        calls: list[Callable[[], object]] = [
            lambda: catalog.verify(drone.package_id),
            lambda: catalog.resolve(anchor),
            lambda: catalog.lineage(record_key(record)),
            lambda: catalog.threads_of(record_key(record)),
            lambda: catalog.thread(key, "world", History()),
            lambda: catalog.thread(key, "transaction", LatestTransform()),
        ]
        for make in calls:
            assert codec.dumps(make()) == codec.dumps(make())
        spec = QuerySpec(kinds=("run", "stream"))
        assert arrow.ipc_bytes(catalog.query(spec)) == arrow.ipc_bytes(catalog.query(spec))
