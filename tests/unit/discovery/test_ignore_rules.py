"""Ignore rules (ADR 0043): the pattern subset, the walk's ignored entries, the root's
``.neptune-ignore`` as hostile input, and the transform that declares the rules."""

import os
import time
from pathlib import Path

import pytest

from neptune.discovery.ignore import (
    DEFAULT_PATTERNS,
    IGNORE_ADAPTER_ID,
    IGNORED,
    MAX_FILE_BYTES,
    MAX_RULES,
    IgnoreError,
    IgnorePolicy,
    IgnoreRules,
    Origin,
    parse_rule,
)
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource, SkippedEntry, SkipReason, SourceEntry
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, Severity
from neptune.model.source import LocalPath


def write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def rules(*patterns: str) -> IgnoreRules:
    return IgnoreRules(tuple(r for p in patterns if (r := parse_rule(p.encode(), "option"))))


def matches(rule: str, path: str, *, is_dir: bool = False) -> bool:
    return rules(rule).match(tuple(path.encode().split(b"/")), is_dir=is_dir) is not None


# --- pattern syntax --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "path", "is_dir", "expected"),
    [
        ("*.tmp", "a.tmp", False, True),
        ("*.tmp", "deep/er/a.tmp", False, True),  # unanchored: a name at any depth
        ("*.tmp", "a.tmpx", False, False),
        ("*.TMP", "a.tmp", False, False),  # case-sensitive
        ("cache/", "cache", True, True),
        ("cache/", "cache", False, False),  # directories only
        ("/top.log", "top.log", False, True),
        ("/top.log", "sub/top.log", False, False),  # anchored at the root
        ("logs/*.bag", "logs/a.bag", False, True),
        ("logs/*.bag", "x/logs/a.bag", False, False),  # a slash inside anchors it
        ("logs/*.bag", "logs/sub/a.bag", False, False),  # * never crosses a slash
        ("**/scratch", "scratch", True, True),  # ** spans zero names
        ("**/scratch", "a/b/scratch", True, True),
        ("a/**/z", "a/z", False, True),
        ("a/**/z", "a/b/c/z", False, True),
        ("a/**/z", "b/a/z", False, False),
        ("a/**/**/z", "a/q/z", False, True),  # consecutive ** collapse
        ("[ab]?.txt", "b1.txt", False, True),
        ("[ab]?.txt", "c1.txt", False, False),
        ("back\\slash", "back\\slash", False, True),  # a backslash is an ordinary character
    ],
)
def test_pattern_semantics(rule: str, path: str, is_dir: bool, expected: bool) -> None:
    assert matches(rule, path, is_dir=is_dir) is expected


def test_blank_lines_and_comments_are_not_rules_and_trailing_space_is_dropped() -> None:
    assert parse_rule(b"", "file") is None
    assert parse_rule(b"   \r", "file") is None
    assert parse_rule(b"# *.mcap", "file") is None
    rule = parse_rule(b"*.tmp \t\r", "file")
    assert rule is not None and rule.pattern == b"*.tmp" and rule.text == "*.tmp"


@pytest.mark.parametrize(
    ("line", "why"),
    [
        (b"!keep.mcap", "negation"),
        (b"a\x00b", "NUL"),
        (b"../escape", "not a relative path pattern"),
        (b"a/./b", "not a relative path pattern"),
        (b"a//b", "not a relative path pattern"),
        (b"/", "not a relative path pattern"),
        (b"x" * 1025, "longer than"),
    ],
)
def test_refused_patterns(line: bytes, why: str) -> None:
    with pytest.raises(IgnoreError, match=why):
        parse_rule(line, "file")


def test_names_that_are_not_utf8_match_as_bytes() -> None:
    rule = parse_rule(b"caf\xe9*", "option")
    assert rule is not None
    assert IgnoreRules((rule,)).match((b"caf\xe9.log",), is_dir=False) is rule


def test_a_pathological_pattern_is_bounded() -> None:
    """Matching is one pass per component, never a regex over the joined path."""
    pattern = "/".join(["**"] * 40 + ["*a*a*a*a*a*a*a*a*a*a*b"])
    path = "/".join(["a" * 200] * 60)
    start = time.perf_counter()
    assert not matches(pattern, path)
    assert time.perf_counter() - start < 2.0


def test_the_first_matching_rule_names_the_entry() -> None:
    both = rules("*.log", "debug.log")
    rule = both.match((b"debug.log",), is_dir=False)
    assert rule is not None and rule.text == "*.log"


# --- the policy --------------------------------------------------------------------------------


def test_the_policy_checks_its_patterns_when_built() -> None:
    with pytest.raises(IgnoreError, match="pattern 2: negation"):
        IgnorePolicy(patterns=("*.tmp", "!x"))
    with pytest.raises(IgnoreError, match="tuple of text"):
        IgnorePolicy(patterns=["*.tmp"])  # type: ignore[arg-type]
    with pytest.raises(IgnoreError, match="booleans"):
        IgnorePolicy(defaults=1)  # type: ignore[arg-type]


def test_defaults_then_options_then_the_file_in_order(tmp_path: Path) -> None:
    write(tmp_path / ".neptune-ignore", b"# site rules\n*.tmp\n")
    got = IgnorePolicy(patterns=("scratch/",)).rules(LocalSource(tmp_path))
    texts = [(r.origin, r.text) for r in got.rules]
    assert texts == [
        *((Origin.DEFAULT, p) for p in DEFAULT_PATTERNS),
        (Origin.OPTION, "scratch/"),
        (Origin.FILE, "*.tmp"),
    ]
    none = IgnorePolicy(defaults=False, file=False).rules(LocalSource(tmp_path))
    assert none.rules == ()


# --- the root's .neptune-ignore is hostile input -----------------------------------------------


def test_a_missing_ignore_file_is_no_rules(tmp_path: Path) -> None:
    write(tmp_path / "a.txt")
    assert IgnorePolicy(defaults=False).rules(LocalSource(tmp_path)).rules == ()


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda root: write(root / ".neptune-ignore", b"*.tmp\n!*.mcap\n"), "line 2: negation"),
        (lambda root: write(root / ".neptune-ignore", b"../../etc\n"), "line 1:"),
        (lambda root: write(root / ".neptune-ignore", b"a\x00\n"), "NUL"),
        (
            lambda root: write(root / ".neptune-ignore", b"#" * (MAX_FILE_BYTES + 1)),
            "larger than",
        ),
        (
            lambda root: write(
                root / ".neptune-ignore",
                b"".join(b"r%d\n" % n for n in range(MAX_RULES + 1)),
            ),
            "more than",
        ),
        (lambda root: (root / ".neptune-ignore").symlink_to("/etc/passwd"), "symlink"),
        (lambda root: (root / ".neptune-ignore").mkdir(), "not_regular_file"),
        (lambda root: os.mkfifo(root / ".neptune-ignore"), "not_regular_file"),
    ],
    ids=["negation", "escape", "nul", "too-large", "too-many", "symlink", "directory", "fifo"],
)
def test_an_unusable_ignore_file_is_refused_whole(make: object, why: str, tmp_path: Path) -> None:
    make(tmp_path)  # type: ignore[operator]
    with pytest.raises(IgnoreError, match=why):
        IgnorePolicy().rules(LocalSource(tmp_path))


def test_too_many_rules_across_origins_are_refused(tmp_path: Path) -> None:
    write(tmp_path / ".neptune-ignore", b"".join(b"f%d\n" % n for n in range(MAX_RULES - 2)))
    with pytest.raises(IgnoreError, match=f"at most {MAX_RULES}"):
        IgnorePolicy().rules(LocalSource(tmp_path))


def test_a_file_root_has_no_ignore_file(tmp_path: Path) -> None:
    write(tmp_path / "one.txt")
    got = IgnorePolicy(defaults=False).rules(LocalSource(tmp_path / "one.txt"))
    assert got.rules == ()


# --- the walk and the scan ---------------------------------------------------------------------


def test_an_ignored_directory_is_one_entry_and_never_entered(tmp_path: Path) -> None:
    write(tmp_path / ".git/objects/ab/cdef")
    write(tmp_path / "run/.DS_Store")
    write(tmp_path / "run/a.mcap")
    source = LocalSource(tmp_path, ignore=IgnorePolicy().rules(LocalSource(tmp_path)))
    entries = list(source.walk())
    skipped = [e for e in entries if isinstance(e, SkippedEntry)]
    assert [(e.raw_path, e.reason, e.detail) for e in skipped] == [
        (b".git", SkipReason.IGNORED, ".git/"),
        (b"run/.DS_Store", SkipReason.IGNORED, ".DS_Store"),
    ]
    assert [e.location for e in entries if isinstance(e, SourceEntry)] == [LocalPath("run/a.mcap")]


def test_the_scan_records_one_declared_finding_per_ignored_entry(tmp_path: Path) -> None:
    write(tmp_path / ".neptune-ignore", b"*.tmp\n")
    write(tmp_path / "a.tmp")
    write(tmp_path / "b.txt")
    source = LocalSource(tmp_path, ignore=IgnorePolicy(defaults=False).rules(LocalSource(tmp_path)))
    result = scan(source, SourceLedger())
    [finding] = [f for f in result.findings if f.code == IGNORED]
    assert finding.category is FindingCategory.SKIPPED and finding.severity is Severity.INFO
    assert result.ignore is not None and finding.transform == result.ignore.id
    assert result.ignore.adapter_id == IGNORE_ADAPTER_ID
    assert finding.details == {"origin": "file", "pattern": "*.tmp"}
    assert set(result.producers) == {result.transform.id, result.ignore.id}
    # .neptune-ignore is evidence like any other file: walked and hashed.
    walked = {o.revision.location for o in result.observations}
    assert walked == {LocalPath(".neptune-ignore"), LocalPath("b.txt")}


def test_rules_and_their_transform_are_deterministic(tmp_path: Path) -> None:
    write(tmp_path / ".neptune-ignore", b"*.tmp\nscratch/\n")
    one = IgnorePolicy(patterns=("x",)).rules(LocalSource(tmp_path))
    two = IgnorePolicy(patterns=("x",)).rules(LocalSource(tmp_path))
    assert one.transform == two.transform
    other = IgnorePolicy(patterns=("y",)).rules(LocalSource(tmp_path))
    assert other.transform.id != one.transform.id  # the config is every rule in force


def test_a_symlink_a_rule_matches_is_ignored_and_never_followed(tmp_path: Path) -> None:
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    [entry] = list(LocalSource(tmp_path, ignore=rules("loop")).walk())
    assert isinstance(entry, SkippedEntry) and entry.reason is SkipReason.IGNORED
