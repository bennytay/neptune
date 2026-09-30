"""Software, firmware and model identity as the evidence declares it (ADR 0014).

Each kind of version is its own type. A git commit, a semantic version, a free-form version
string, a build id, a firmware version, a model checkpoint hash and a container image digest never
compare equal to one another, and ordering across kinds raises ``TypeError``. Values are stored
verbatim. Validation only decides whether the declared text is a well-formed member of its kind;
nothing is trimmed, case-folded, prefixed or completed.

A value becomes a given kind because the source says so (a ``git_sha`` field, a SemVer-governed
manifest, an OCI digest field), never because its text looks like one. The ``Knowledge`` wrapper
and its provenance record which statement that was.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from neptune.model.ids import check_text
from neptune.model.jsonvalue import JsonObject, JsonValue

# Bound on hostile or absurd text. Real version strings are far shorter.
MAX_TEXT_LENGTH: Final = 256


def _check_version_text(what: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a str, got {type(value).__name__}")
    check_text(what, value)
    if len(value) > MAX_TEXT_LENGTH:
        raise ValueError(f"{what} is longer than {MAX_TEXT_LENGTH} characters")
    return value


# --- Free-form text kinds ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Verbatim:
    """A declared string with no structure Neptune may rely on, so no order: is "10" after "9"?"""

    kind: ClassVar[str]
    value: str

    def __post_init__(self) -> None:
        _check_version_text(self.kind, self.value)

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class DeclaredVersion(_Verbatim):
    """A version string under no scheme the source names: ``v1.2.3``, ``2024.03``, ``humble``."""

    kind: ClassVar[str] = "declared_version"


@dataclass(frozen=True)
class BuildId(_Verbatim):
    """A build or CI identifier: ``build-4812``, ``20240301.3``."""

    kind: ClassVar[str] = "build_id"


@dataclass(frozen=True)
class FirmwareVersion(_Verbatim):
    """A device firmware version as the device or its log reports it: ``v1.14.0``, ``0x010E``.

    Which device it belongs to is the enclosing record's business (MVL-1, MVL-27).
    """

    kind: ClassVar[str] = "firmware_version"


# --- Semantic versions -------------------------------------------------------------------------

_NUMBER = r"0|[1-9][0-9]*"
_PRERELEASE_ID = rf"(?:{_NUMBER}|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
_BUILD_ID = r"[0-9a-zA-Z-]+"
# SemVer 2.0.0 §2, §9, §10, with ASCII digits only (Python's \d also matches other scripts).
_SEMVER = re.compile(
    rf"(?P<major>{_NUMBER})\.(?P<minor>{_NUMBER})\.(?P<patch>{_NUMBER})"
    rf"(?:-(?P<prerelease>{_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*))?"
    rf"(?:\+(?P<build>{_BUILD_ID}(?:\.{_BUILD_ID})*))?"
)

_PrecedenceKey: TypeAlias = tuple[int, int, int, tuple[int, tuple[tuple[int, int | str], ...]]]


@dataclass(frozen=True)
class SemanticVersion:
    """A SemVer 2.0.0 version, kept as its full declared text.

    ``prerelease`` and ``build`` are views of ``value``, so neither can be dropped. ``==`` is record
    equality (the same text). ``<`` and friends are SemVer precedence (§11), which ignores build
    metadata: ``1.0.0+a`` and ``1.0.0+b`` are unequal records, and neither precedes the other.
    A leading ``v`` is not SemVer; ``v1.2.3`` is a ``DeclaredVersion``.
    """

    kind: ClassVar[str] = "semver"
    value: str
    major: int = field(init=False, compare=False, repr=False)
    minor: int = field(init=False, compare=False, repr=False)
    patch: int = field(init=False, compare=False, repr=False)
    prerelease: tuple[str, ...] = field(init=False, compare=False, repr=False)
    build: tuple[str, ...] = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        _check_version_text(self.kind, self.value)
        match = _SEMVER.fullmatch(self.value)
        if match is None:
            raise ValueError(f"not a SemVer 2.0.0 version: {self.value!r}")
        prerelease, build = match["prerelease"], match["build"]
        object.__setattr__(self, "major", int(match["major"]))
        object.__setattr__(self, "minor", int(match["minor"]))
        object.__setattr__(self, "patch", int(match["patch"]))
        object.__setattr__(self, "prerelease", tuple(prerelease.split(".")) if prerelease else ())
        object.__setattr__(self, "build", tuple(build.split(".")) if build else ())

    def precedence_key(self) -> _PrecedenceKey:
        """SemVer §11: a release follows its prereleases; numeric identifiers precede others."""
        if not self.prerelease:
            return (self.major, self.minor, self.patch, (1, ()))
        identifiers = tuple(
            (0, int(part)) if part.isdigit() else (1, part) for part in self.prerelease
        )
        return (self.major, self.minor, self.patch, (0, identifiers))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.precedence_key() < other.precedence_key()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.precedence_key() <= other.precedence_key()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.precedence_key() > other.precedence_key()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.precedence_key() >= other.precedence_key()

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "value": self.value}


# --- Hashes and digests ------------------------------------------------------------------------

_HEX = re.compile(r"[0-9a-fA-F]+")
# Git's minimum abbreviation; full object names are 40 (SHA-1) or 64 (SHA-256) hex digits.
GIT_MIN_ABBREVIATION: Final = 4
GIT_FULL_LENGTHS: Final = (40, 64)


@dataclass(frozen=True)
class GitCommit:
    """A git commit object name, full or abbreviated, in the case the source wrote it.

    An abbreviation is still a commit reference, but not a unique one: ``abbreviated`` says so.
    Resolving it against a repository is a derived step (MVL-27).
    """

    kind: ClassVar[str] = "git_commit"
    sha: str

    def __post_init__(self) -> None:
        _check_version_text(self.kind, self.sha)
        if not _HEX.fullmatch(self.sha) or not (
            GIT_MIN_ABBREVIATION <= len(self.sha) <= max(GIT_FULL_LENGTHS)
        ):
            raise ValueError(
                f"not a git object name ({GIT_MIN_ABBREVIATION}-64 hex digits): {self.sha!r}"
            )

    @property
    def abbreviated(self) -> bool:
        return len(self.sha) not in GIT_FULL_LENGTHS

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "sha": self.sha}


class HashAlgorithm(StrEnum):
    """Checkpoint digest algorithms. Weak ones are kept: they are declared."""

    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SHA384 = "sha384"
    SHA512 = "sha512"

    @property
    def hex_length(self) -> int:
        return _HEX_LENGTHS[self]


_HEX_LENGTHS: Final[Mapping[HashAlgorithm, int]] = {
    HashAlgorithm.MD5: 32,
    HashAlgorithm.SHA1: 40,
    HashAlgorithm.SHA256: 64,
    HashAlgorithm.SHA384: 96,
    HashAlgorithm.SHA512: 128,
}


@dataclass(frozen=True)
class ModelCheckpointHash:
    """A digest the source states for a model checkpoint, full length, in the case it was written.

    It is a claim, not Neptune's own content id: whether the bytes Neptune holds match it is a
    validation check (``validate/``), never assumed.
    """

    kind: ClassVar[str] = "model_checkpoint_hash"
    algorithm: HashAlgorithm
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.algorithm, HashAlgorithm):
            raise TypeError(f"algorithm must be a HashAlgorithm, got {self.algorithm!r}")
        _check_version_text(self.kind, self.digest)
        length = self.algorithm.hex_length
        if len(self.digest) != length or not _HEX.fullmatch(self.digest):
            raise ValueError(
                f"not a {self.algorithm} digest ({length} hex digits): {self.digest!r}"
            )

    def to_json(self) -> JsonObject:
        return {"algorithm": str(self.algorithm), "digest": self.digest, "kind": self.kind}


# OCI image-spec "Digests": sha256 and sha512 are the registered algorithms, lowercase hex only.
_OCI_DIGEST = re.compile(r"sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}")


@dataclass(frozen=True)
class ContainerImageDigest:
    """An OCI content digest, ``sha256:<64 hex>`` or ``sha512:<128 hex>`` (``image@sha256:…``).

    Only the digest identifies an image. A tag (``:latest``) is mutable and is not a version here.
    """

    kind: ClassVar[str] = "container_image_digest"
    digest: str

    def __post_init__(self) -> None:
        _check_version_text(self.kind, self.digest)
        if not _OCI_DIGEST.fullmatch(self.digest):
            raise ValueError(f"not an OCI sha256/sha512 digest: {self.digest!r}")

    def to_json(self) -> JsonObject:
        return {"digest": self.digest, "kind": self.kind}


VersionPrimitive: TypeAlias = (
    GitCommit
    | SemanticVersion
    | DeclaredVersion
    | BuildId
    | FirmwareVersion
    | ModelCheckpointHash
    | ContainerImageDigest
)


# --- JSON --------------------------------------------------------------------------------------


def version_to_json(version: VersionPrimitive) -> JsonObject:
    """Encoder for ``Knowledge`` fields: ``to_json(knowledge, version_to_json)``."""
    return version.to_json()


def _str(obj: Mapping[str, JsonValue], key: str) -> str:
    value = obj[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _hash(obj: Mapping[str, JsonValue]) -> ModelCheckpointHash:
    return ModelCheckpointHash(HashAlgorithm(_str(obj, "algorithm")), _str(obj, "digest"))


_DECODERS: Final[
    Mapping[str, tuple[frozenset[str], Callable[[Mapping[str, JsonValue]], VersionPrimitive]]]
] = {
    GitCommit.kind: (frozenset({"kind", "sha"}), lambda o: GitCommit(_str(o, "sha"))),
    SemanticVersion.kind: (
        frozenset({"kind", "value"}),
        lambda o: SemanticVersion(_str(o, "value")),
    ),
    DeclaredVersion.kind: (
        frozenset({"kind", "value"}),
        lambda o: DeclaredVersion(_str(o, "value")),
    ),
    BuildId.kind: (frozenset({"kind", "value"}), lambda o: BuildId(_str(o, "value"))),
    FirmwareVersion.kind: (
        frozenset({"kind", "value"}),
        lambda o: FirmwareVersion(_str(o, "value")),
    ),
    ModelCheckpointHash.kind: (frozenset({"algorithm", "digest", "kind"}), _hash),
    ContainerImageDigest.kind: (
        frozenset({"digest", "kind"}),
        lambda o: ContainerImageDigest(_str(o, "digest")),
    ),
}


def version_from_json(data: JsonValue) -> VersionPrimitive:
    """Parse strictly: an unknown kind, a missing or unexpected key, or an invalid value raises."""
    if not isinstance(data, Mapping):
        raise ValueError(f"version must be a JSON object, got {type(data).__name__}")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in _DECODERS:
        raise ValueError(f"unknown version kind: {kind!r}")
    keys, decode = _DECODERS[kind]
    if data.keys() != keys:
        missing, extra = keys - data.keys(), data.keys() - keys
        raise ValueError(f"bad {kind}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return decode(data)
