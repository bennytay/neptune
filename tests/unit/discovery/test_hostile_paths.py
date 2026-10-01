"""Hostile names and links in a local tree: names are names, links are never followed, all is recorded."""  # noqa: E501

import os
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

import pytest

from neptune.discovery.policy import (
    DISCOVERY_TRANSFORM,
    SIZE_CHANGED,
    SPECIAL_FILE,
    SYMLINK_NOT_FOLLOWED,
    UNREADABLE,
    path_problem,
    root_forms,
    symlink_details,
)
from neptune.discovery.scan import ScanResult, scan
from neptune.discovery.source import LocalSource, SkipReason, SourceAccessError
from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity, ingest_finding_from_json
from neptune.model.source import LocalPath, SourceLocation

LINKS = {
    "loop": True,
    "ping": True,
    "pong": True,
    "escape_abs": False,
    "escape_rel": False,
    "escape_dir": False,
    "inside": True,
    "inside_abs": True,
    "dangling": True,
    "chain": True,
    "dotdot_inside": True,
}


@pytest.fixture
def tree(tmp_path: Path, hostile: ModuleType) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    hostile.build_tree(root, tmp_path / "outside")
    return root


def by_code(result: ScanResult) -> dict[str, list[IngestFinding]]:
    grouped: dict[str, list[IngestFinding]] = {}
    for finding in result.findings:
        grouped.setdefault(finding.code, []).append(finding)
    return grouped


def path_of(subject: object) -> str:
    assert isinstance(subject, LocalPath)
    return subject.path


def test_traversal_looking_names_are_plain_names(tree: Path, hostile: ModuleType) -> None:
    result = scan(LocalSource(tree), SourceLedger())
    paths = {path_of(o.revision.location) for o in result.observations}
    assert {f"names/{name}" for name in hostile.ODD_NAMES} <= paths
    assert "benign.txt" in paths
    assert "deep/" + "d/" * hostile.DEEP + "leaf.txt" in paths
    assert not any(code.startswith("neptune.discovery.unreadable") for code in by_code(result))


def test_every_symlink_is_a_finding_and_none_is_followed(tree: Path, hostile: ModuleType) -> None:
    ledger = SourceLedger()
    result = scan(LocalSource(tree), ledger)
    links = {path_of(f.subject): f for f in by_code(result)[SYMLINK_NOT_FOLLOWED]}
    assert set(links) == {f"links/{name}" for name in LINKS}
    assert {name: links[f"links/{name}"].details["inside_root"] for name in LINKS} == LINKS
    assert links["links/escape_abs"].details["absolute"] is True
    assert links["links/escape_rel"].details == {
        "absolute": False,
        "inside_root": False,
        "target": "../../outside/canary.txt",
    }
    assert links["links/loop"].details["target"] == "loop"
    for finding in links.values():
        assert finding.severity is Severity.INFO
        assert finding.category is FindingCategory.SKIPPED
        assert finding.transform == DISCOVERY_TRANSFORM.id == result.transform.id
    assert {link.location for link in result.symlinks} == {
        LocalPath(path) for path in links
    }  # the walk result and the findings agree

    canary = content_id(hostile.CANARY)
    assert canary not in {artifact.content_id for artifact in ledger.artifacts()}
    source = LocalSource(tree)
    for path in [*links, "links/escape_dir/canary.txt", "links/inside_abs"]:
        with pytest.raises(SourceAccessError) as info:
            source.open(LocalPath(path))
        assert info.value.reason is SkipReason.SYMLINK


def test_a_fifo_is_an_info_finding_and_never_opened(tree: Path) -> None:
    result = scan(LocalSource(tree), SourceLedger())
    [fifo] = by_code(result)[SPECIAL_FILE]
    assert fifo.subject == LocalPath("fifo")
    assert fifo.severity is Severity.INFO
    assert str(fifo.details["mode"]).startswith("p")


def test_undecodable_symlink_target_is_recorded_as_hex(tmp_path: Path) -> None:
    (tmp_path / "raw").symlink_to(os.fsdecode(b"../weird\xff"))
    result = scan(LocalSource(tmp_path), SourceLedger())
    [finding] = result.findings
    assert finding.details == {
        "absolute": False,
        "inside_root": False,
        "target_hex": "2e2e2f7765697264ff",
    }
    canonical_json.dumps(finding.to_json())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_directory_is_an_error_finding(tmp_path: Path) -> None:
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked/secret").write_bytes(b"x")
    (tmp_path / "open.txt").write_bytes(b"y")
    (tmp_path / "locked").chmod(0)
    try:
        result = scan(LocalSource(tmp_path), SourceLedger())
    finally:
        (tmp_path / "locked").chmod(0o755)
    [finding] = result.findings
    assert (finding.code, finding.severity) == (UNREADABLE, Severity.ERROR)
    assert finding.subject == LocalPath("locked")
    assert [path_of(o.revision.location) for o in result.observations] == ["open.txt"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_an_unreadable_root_is_not_a_finding(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"x")
    tmp_path.chmod(0o111)
    try:
        result = scan(LocalSource(tmp_path), SourceLedger())
    finally:
        tmp_path.chmod(0o755)
    assert result.findings == ()
    assert [(s.raw_path, s.reason) for s in result.skipped] == [(b".", SkipReason.UNREADABLE)]


class GrowsAtOpen(LocalSource):
    """A file that is appended to between the walk's stat and the digest's open."""

    def open(self, location: SourceLocation) -> BinaryIO:
        assert isinstance(location, LocalPath)
        with (Path(self.root) / location.path).open("ab") as stream:
            stream.write(b"more")
        return super().open(location)


def test_size_changed_between_walk_and_digest_is_a_warning(tmp_path: Path) -> None:
    (tmp_path / "log.bin").write_bytes(b"abc")
    ledger = SourceLedger()
    result = scan(GrowsAtOpen(tmp_path), ledger)
    [finding] = result.findings
    assert (finding.code, finding.severity) == (SIZE_CHANGED, Severity.WARNING)
    assert finding.category is FindingCategory.INCONSISTENT
    assert finding.details == {"size_at_walk": 3, "size_digested": 7}
    [observation] = result.observations
    artifact = ledger.artifact(observation.revision.content_id)
    assert artifact is not None and artifact.size == 7  # the digest is authoritative


class SwappedAtOpen(LocalSource):
    """A regular file replaced, between the walk and the digest's open, by something else."""

    def __init__(self, root: Path, swap: str) -> None:
        super().__init__(root)
        self._swap = swap

    def open(self, location: SourceLocation) -> BinaryIO:
        assert isinstance(location, LocalPath)
        path = Path(self.root) / location.path
        if path.is_file() and not path.is_symlink():
            path.unlink()
            if self._swap == "symlink":
                path.symlink_to(Path(self.root).parent / "outside" / "canary.txt")
            else:
                os.mkfifo(path)
        return super().open(location)


@pytest.mark.parametrize(
    ("swap", "code", "severity"),
    [("symlink", SYMLINK_NOT_FOLLOWED, Severity.INFO), ("fifo", SPECIAL_FILE, Severity.INFO)],
)
def test_a_file_swapped_at_open_is_reported_as_what_open_found(
    tmp_path: Path, swap: str, code: str, severity: Severity
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "canary.txt").write_bytes(b"never read")
    (root / "log.bin").write_bytes(b"abc")
    ledger = SourceLedger()
    scan(LocalSource(root), ledger)
    result = scan(SwappedAtOpen(root, swap), ledger)
    [finding] = result.findings
    assert (finding.code, finding.severity) == (code, severity)
    assert finding.subject == LocalPath("log.bin")
    if swap == "fifo":
        assert str(finding.details["mode"]).startswith("p")  # the FIFO's mode, as open found it
    assert result.observations == ()
    assert result.absences == ()  # seen only at open: blind for this scan, the next one decides


def test_scan_findings_are_deterministic_and_verifiable(tree: Path) -> None:
    first = scan(LocalSource(tree), SourceLedger())
    second = scan(LocalSource(tree), SourceLedger())
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert len({f.id for f in first.findings}) == len(first.findings)
    for finding in first.findings:
        line = canonical_json.dumps(finding.to_json())
        assert check_ingest_finding(ingest_finding_from_json(canonical_json.loads(line))) == finding


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("", "empty"),
        ("a\x00b", "nul"),
        ("/etc/passwd", "absolute"),
        ("../x", "parent_reference"),
        ("a/../b", "parent_reference"),
        ("a/..", "parent_reference"),
        ("./a", None),
        ("a//b", None),
        ("..a", None),
        ("a..", None),
        ("...", None),
        ("C:\\..\\x", None),
    ],
)
def test_path_problem(name: str, problem: str | None) -> None:
    assert path_problem(name) == problem


@pytest.mark.parametrize(
    ("link", "target", "absolute", "inside"),
    [
        (b"links/x", b"../real/run.bin", False, True),
        (b"links/x", b"../../x", False, False),
        (b"x", b"..", False, False),
        (b"x", b"../", False, False),
        (b"x", b".", False, True),
        (b"a/b/c", b"../../d", False, True),
        (b"a/b/c", b"../../../d", False, False),
        (b"x", b"/etc/passwd", True, False),
    ],
)
def test_symlink_details_are_lexical(
    tmp_path: Path, link: bytes, target: bytes, absolute: bool, inside: bool
) -> None:
    details = symlink_details(root_forms(tmp_path), link, target)
    assert (details["absolute"], details["inside_root"]) == (absolute, inside)


def test_an_absolute_target_under_the_root_counts_as_inside(tmp_path: Path) -> None:
    forms = root_forms(tmp_path)
    inside = os.fsencode(tmp_path / "real" / "f")
    assert symlink_details(forms, b"x", inside)["inside_root"] is True
    assert symlink_details(forms, b"x", inside + b"/../../../etc")["inside_root"] is False
    sibling = os.fsencode(tmp_path.parent / (tmp_path.name + "2") / "f")
    assert symlink_details(forms, b"x", sibling)["inside_root"] is False


def test_the_root_appears_only_as_a_declared_link_target(tree: Path) -> None:
    """Host paths stay out of findings; a link's own absolute target is the link's content."""
    result = scan(LocalSource(tree), SourceLedger())
    root = os.fsdecode(tree)
    for finding in result.findings:
        without_target = {k: v for k, v in finding.details.items() if k != "target"}
        text = canonical_json.dumps({**finding.to_json(), "details": without_target}).decode()
        assert root not in text
