from dataclasses import dataclass
from itertools import combinations, pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import AssertionKind, Known, Unknown, from_json, from_text, to_json
from neptune.model.versions import (
    MAX_TEXT_LENGTH,
    BuildId,
    ContainerImageDigest,
    DeclaredVersion,
    FirmwareVersion,
    GitCommit,
    HashAlgorithm,
    ModelCheckpointHash,
    SemanticVersion,
    VersionPrimitive,
    version_from_json,
    version_to_json,
)

SHA1 = "3f786850e387550fdab836ed7e6dc881de23001b"
SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@dataclass(frozen=True)
class Cite:
    """Stand-in for ``Provenance``: any evidence-layer ``Grounding``."""

    where: str
    assertion_kind: AssertionKind = AssertionKind.OBSERVED

    def to_json(self) -> JsonObject:
        return {"where": self.where}


def cite(data: JsonObject) -> Cite:
    where = data["where"]
    assert isinstance(where, str)
    return Cite(where)


# One value of every kind, all spelled from the same few characters where the kind allows it.
SAME_TEXT: list[VersionPrimitive] = [
    GitCommit("1234"),
    SemanticVersion("1.2.3"),
    DeclaredVersion("1.2.3"),
    BuildId("1.2.3"),
    FirmwareVersion("1.2.3"),
    DeclaredVersion("1234"),
    BuildId("1234"),
    FirmwareVersion("1234"),
]
EVERY_KIND: list[VersionPrimitive] = [
    GitCommit(SHA1),
    SemanticVersion("1.14.0-rc.1+px4.7"),
    DeclaredVersion("v1.14.0"),
    BuildId("build-4812"),
    FirmwareVersion("0x010E"),
    ModelCheckpointHash(HashAlgorithm.SHA256, SHA256),
    ContainerImageDigest(f"sha256:{SHA256}"),
]


# --- Acceptance: two version kinds never compare equal or sort together ----------------------


@pytest.mark.parametrize(("a", "b"), list(combinations(SAME_TEXT + EVERY_KIND, 2)))
def test_different_values_or_kinds_are_never_equal(
    a: VersionPrimitive, b: VersionPrimitive
) -> None:
    assert a != b
    assert len({a, b}) == 2
    assert canonical_json.dumps(a.to_json()) != canonical_json.dumps(b.to_json())


@pytest.mark.parametrize(
    ("a", "b"),
    [(a, b) for a, b in combinations(EVERY_KIND, 2)]
    + [(SemanticVersion("1.2.3"), DeclaredVersion("1.2.3"))],
)
def test_kinds_never_sort_together(a: VersionPrimitive, b: VersionPrimitive) -> None:
    for left, right in ((a, b), (b, a)):
        with pytest.raises(TypeError):
            _ = left < right  # type: ignore[operator]
        with pytest.raises(TypeError):
            _ = left >= right  # type: ignore[operator]
    with pytest.raises(TypeError):
        sorted([a, b])  # type: ignore[type-var]


@pytest.mark.parametrize(
    "values",
    [
        [DeclaredVersion("10"), DeclaredVersion("9")],
        [BuildId("10"), BuildId("9")],
        [FirmwareVersion("10"), FirmwareVersion("9")],
        [GitCommit("abcd"), GitCommit("abce")],
        [
            ModelCheckpointHash(HashAlgorithm.MD5, "0" * 32),
            ModelCheckpointHash(HashAlgorithm.MD5, "1" * 32),
        ],
        [ContainerImageDigest("sha256:" + "0" * 64), ContainerImageDigest("sha256:" + "1" * 64)],
    ],
)
def test_only_semver_has_an_order(values: list[VersionPrimitive]) -> None:
    with pytest.raises(TypeError):
        sorted(values)  # type: ignore[type-var]


# --- Acceptance: stored exactly as declared; semver keeps prerelease and build ----------------


@pytest.mark.parametrize(
    ("text", "core", "prerelease", "build"),
    [
        ("1.2.3", (1, 2, 3), (), ()),
        ("0.0.0", (0, 0, 0), (), ()),
        ("1.0.0-alpha", (1, 0, 0), ("alpha",), ()),
        ("1.0.0-alpha.1", (1, 0, 0), ("alpha", "1"), ()),
        ("1.0.0-0.3.7", (1, 0, 0), ("0", "3", "7"), ()),
        ("1.0.0-x.7.z.92", (1, 0, 0), ("x", "7", "z", "92"), ()),
        ("1.0.0-x-y-z.--", (1, 0, 0), ("x-y-z", "--"), ()),
        ("1.0.0+20130313144700", (1, 0, 0), (), ("20130313144700",)),
        ("1.0.0-beta+exp.sha.5114f85", (1, 0, 0), ("beta",), ("exp", "sha", "5114f85")),
        ("1.0.0+21AF26D3----117B344092BD", (1, 0, 0), (), ("21AF26D3----117B344092BD",)),
        ("1.0.0+001", (1, 0, 0), (), ("001",)),
        ("1.0.0-0alpha", (1, 0, 0), ("0alpha",), ()),
        ("99999999999999999999.0.0", (99999999999999999999, 0, 0), (), ()),
    ],
)
def test_semver_keeps_every_part(
    text: str, core: tuple[int, int, int], prerelease: tuple[str, ...], build: tuple[str, ...]
) -> None:
    version = SemanticVersion(text)
    assert version.value == text
    assert (version.major, version.minor, version.patch) == core
    assert version.prerelease == prerelease
    assert version.build == build
    assert version_from_json(version.to_json()) == version


def test_build_metadata_distinguishes_records_but_not_precedence() -> None:
    a, b = SemanticVersion("1.0.0+a"), SemanticVersion("1.0.0+b")
    assert a != b
    assert not a < b and not b < a
    assert a <= b and b <= a


def test_semver_precedence_follows_the_spec_example() -> None:
    # SemVer 2.0.0 §11, in order.
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.9.0",
        "1.10.0",
        "1.11.0",
        "2.0.0",
        "2.1.0",
        "2.1.1",
    ]
    versions = [SemanticVersion(text) for text in ordered]
    assert sorted(reversed(versions)) == versions
    for lower, higher in pairwise(versions):
        assert lower < higher and higher > lower and lower <= higher and higher >= lower


@pytest.mark.parametrize(
    "text",
    [
        "v1.2.3",
        "1.2",
        "1",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-",
        "1.2.3+",
        "1.2.3-alpha..1",
        "1.2.3+a..b",
        "1.2.3-alpha_1",
        " 1.2.3",
        "1.2.3 ",
        "1.2.3\n",
        "\u0661.\u0662.\u0663",  # Arabic-Indic digits: \d would accept these
        "1.2.3-\u0661",
        "-1.2.3",
    ],
)
def test_semver_rejects_anything_not_semver(text: str) -> None:
    with pytest.raises(ValueError, match="SemVer"):
        SemanticVersion(text)


@pytest.mark.parametrize("kind", [DeclaredVersion, BuildId, FirmwareVersion])
@pytest.mark.parametrize("text", ["v1.2.3", " 1.2 ", "humble", "2024.03", "ß-β", "0x010E"])
def test_free_form_text_is_verbatim(
    kind: type[DeclaredVersion | BuildId | FirmwareVersion], text: str
) -> None:
    version = kind(text)
    assert version.value == text
    assert version_from_json(version.to_json()) == version


@pytest.mark.parametrize(
    ("sha", "abbreviated"),
    [
        (SHA1, False),
        (SHA1.upper(), False),
        (SHA256, False),
        ("5114f85", True),
        ("abcd", True),
        (SHA256[:41], True),
    ],
)
def test_git_commit_keeps_case_and_flags_abbreviations(sha: str, abbreviated: bool) -> None:
    commit = GitCommit(sha)
    assert commit.sha == sha
    assert commit.abbreviated is abbreviated


def test_git_commit_case_is_not_folded() -> None:
    assert GitCommit(SHA1) != GitCommit(SHA1.upper())


@pytest.mark.parametrize("sha", ["abc", "g1234567", SHA256 + "0", f" {SHA1}", "v1.2.3", "12 34"])
def test_git_commit_rejects_non_object_names(sha: str) -> None:
    with pytest.raises(ValueError, match="git object name"):
        GitCommit(sha)


@pytest.mark.parametrize("algorithm", list(HashAlgorithm))
def test_checkpoint_hash_needs_the_full_digest_of_its_algorithm(algorithm: HashAlgorithm) -> None:
    digest = "a" * algorithm.hex_length
    checkpoint = ModelCheckpointHash(algorithm, digest)
    assert version_from_json(checkpoint.to_json()) == checkpoint
    assert ModelCheckpointHash(algorithm, digest.upper()).digest == digest.upper()
    for wrong in (digest[:-1], digest + "a", "g" * algorithm.hex_length):
        with pytest.raises(ValueError, match="digest"):
            ModelCheckpointHash(algorithm, wrong)


def test_a_sha1_checkpoint_hash_is_not_a_git_commit() -> None:
    checkpoint: object = ModelCheckpointHash(HashAlgorithm.SHA1, SHA1)
    assert checkpoint != GitCommit(SHA1)


def test_checkpoint_hash_algorithm_must_be_named() -> None:
    with pytest.raises(TypeError, match="HashAlgorithm"):
        ModelCheckpointHash("sha256", SHA256)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "digest",
    [
        SHA256,
        f"sha256:{SHA256.upper()}",
        f"sha256:{SHA256[:-1]}",
        f"sha512:{SHA256}",
        f"md5:{'0' * 32}",
        f"SHA256:{SHA256}",
        f"ubuntu@sha256:{SHA256}",
        "ubuntu:22.04",
    ],
)
def test_container_digest_follows_oci(digest: str) -> None:
    with pytest.raises(ValueError, match="OCI"):
        ContainerImageDigest(digest)


def test_container_digest_accepts_sha512() -> None:
    digest = ContainerImageDigest("sha512:" + "0" * 128)
    assert version_from_json(digest.to_json()) == digest


# --- Malformed input and boundaries -----------------------------------------------------------


@pytest.mark.parametrize("kind", [DeclaredVersion, BuildId, FirmwareVersion, SemanticVersion])
def test_text_is_bounded_and_non_empty(
    kind: type[DeclaredVersion | BuildId | FirmwareVersion | SemanticVersion],
) -> None:
    ok = "1.0.0-" + "a" * (MAX_TEXT_LENGTH - 6)
    assert kind(ok).value == ok
    for bad in ("", ok + "a", "\ud800"):
        with pytest.raises(ValueError):
            kind(bad)


@pytest.mark.parametrize(
    "kind", [DeclaredVersion, BuildId, FirmwareVersion, SemanticVersion, GitCommit]
)
def test_non_text_is_a_type_error(kind: type[VersionPrimitive]) -> None:
    with pytest.raises(TypeError):
        kind(123)  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize(
    "data",
    [
        "1.2.3",
        [],
        {},
        {"kind": "version", "value": "1"},
        {"kind": "semver"},
        {"kind": "semver", "value": "1.2.3", "confidence": 1},
        {"kind": "semver", "value": 123},
        {"kind": "git_commit", "value": SHA1},
        {"kind": "model_checkpoint_hash", "algorithm": "crc32", "digest": "00000000"},
        {"kind": "model_checkpoint_hash", "digest": SHA256},
        {"kind": 1, "value": "1"},
    ],
)
def test_json_parsing_is_strict(data: JsonObject) -> None:
    with pytest.raises(ValueError):
        version_from_json(data)


# --- JSON and determinism ---------------------------------------------------------------------


def test_json_shapes() -> None:
    assert [canonical_json.dumps(v.to_json()) for v in EVERY_KIND] == [
        f'{{"kind":"git_commit","sha":"{SHA1}"}}'.encode(),
        b'{"kind":"semver","value":"1.14.0-rc.1+px4.7"}',
        b'{"kind":"declared_version","value":"v1.14.0"}',
        b'{"kind":"build_id","value":"build-4812"}',
        b'{"kind":"firmware_version","value":"0x010E"}',
        f'{{"algorithm":"sha256","digest":"{SHA256}","kind":"model_checkpoint_hash"}}'.encode(),
        f'{{"digest":"sha256:{SHA256}","kind":"container_image_digest"}}'.encode(),
    ]


@pytest.mark.parametrize("version", EVERY_KIND)
def test_round_trip_inside_knowledge(version: VersionPrimitive) -> None:
    state = Known(version, Cite("package.xml#/package/version"))
    data = to_json(state, version_to_json)
    assert from_json(data, version_from_json, cite) == state
    assert canonical_json.dumps(
        to_json(from_json(data, version_from_json, cite), version_to_json)
    ) == (canonical_json.dumps(data))


def test_adapter_reads_a_declared_field_with_from_text() -> None:
    assert from_text("", SemanticVersion) == Unknown()
    assert from_text("1.2.3", SemanticVersion) == Known(SemanticVersion("1.2.3"))
    with pytest.raises(ValueError):
        from_text("v1.2.3", SemanticVersion)  # the adapter's finding, not a guess
    assert from_text("v1.2.3", DeclaredVersion) == Known(DeclaredVersion("v1.2.3"))


_ID = st.from_regex(r"[0-9a-zA-Z-]{1,8}", fullmatch=True)
_NUM = st.integers(min_value=0, max_value=10**6).map(str)
_SEMVERS = st.builds(
    lambda core, pre, build: (
        ".".join(core)
        + (f"-{'.'.join(pre)}" if pre else "")
        + (f"+{'.'.join(build)}" if build else "")
    ),
    st.tuples(_NUM, _NUM, _NUM),
    st.lists(_NUM | _ID.filter(lambda s: not s.isdigit()), max_size=4),
    st.lists(_ID, max_size=3),
)


@given(_SEMVERS)
def test_semver_round_trips_byte_for_byte(text: str) -> None:
    version = SemanticVersion(text)
    reparsed = version_from_json(version.to_json())
    assert reparsed == version
    assert canonical_json.dumps(reparsed.to_json()) == canonical_json.dumps(version.to_json())
    assert version.value == text


@given(st.lists(_SEMVERS, min_size=2, max_size=6))
def test_semver_precedence_is_a_consistent_total_preorder(texts: list[str]) -> None:
    versions = [SemanticVersion(text) for text in texts]
    once = sorted(versions)
    # Ties (differing only in build metadata) keep input order, so compare precedence, not records.
    keys = [v.precedence_key() for v in once]
    assert keys == sorted(keys)
    assert [v.precedence_key() for v in sorted(reversed(once))] == keys
    for lower, higher in pairwise(once):
        assert lower <= higher and not higher < lower
