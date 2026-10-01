"""Software identity: what code, build, firmware, model and packages a file declares (ADR 0040).

The ``software`` adapter reads the files that state software identity and emits one
``SoftwareConfiguration`` per file (ADR 0019 §5), each value ``Known`` with its own citation,
``Unknown`` where the format had a place and the file left it empty, ``NotCovered`` where the
format has none. ``machine`` is ``NotCovered``: none of these formats names a machine. Which
configuration ran in which run is a binding, ``inferred``, and MVL-38's: this adapter only emits
the identity records and the evidence that binding cites.

Formats (one module per family, rules in each module's docstring and in ``_common``):

- git refs (``HEAD``, loose refs, ``packed-refs``), never running git nor reading objects;
- build manifests: ``package.xml``, ``pyproject.toml``, ``Cargo.toml``, ``CMakeLists.txt``,
  ``setup.py`` (syntax only, never executed);
- lockfiles: ``uv.lock``, ``poetry.lock``, ``Cargo.lock``, ``package-lock.json``;
- firmware: ELF build ids, ESP-IDF app descriptors, MCUboot headers, PX4/ArduPilot files;
- checkpoints: safetensors, ONNX and PyTorch headers, never their weights;
- SBOMs and images: SPDX and CycloneDX JSON, OCI image indexes.

Probing reads the head only, never the name, except that a one-line hex file named like a
checksum (``*.sha256``) is not taken for a git ref: the bytes cannot tell those apart. Every
source is one chunk: these files are small, or only their headers are read.
"""

from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
)
from neptune.adapters.software._checkpoints import ONNX, PYTORCH, SAFETENSORS
from neptune.adapters.software._common import ADAPTER_ID, Detected, Format, Reading
from neptune.adapters.software._firmware import ELF, ESP_APP, MCUBOOT, PX4_FIRMWARE
from neptune.adapters.software._git import CHECKSUM_SUFFIXES, GIT_PACKED_REFS, GIT_REF
from neptune.adapters.software._lockfiles import CARGO_LOCK, NPM_LOCK, POETRY_LOCK, UV_LOCK
from neptune.adapters.software._manifests import (
    CARGO_TOML,
    CMAKE,
    PYPROJECT,
    ROS_PACKAGE_XML,
    SETUP_PY,
)
from neptune.adapters.software._sbom import CYCLONEDX, OCI_INDEX, SPDX
from neptune.model.finding import FindingCategory, Severity
from neptune.model.knowledge import AssertionKind

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

# Every format, in the order a tie between two of them would be broken (none overlap).
FORMATS: Final[tuple[Format, ...]] = (
    GIT_REF,
    GIT_PACKED_REFS,
    ROS_PACKAGE_XML,
    PYPROJECT,
    CARGO_TOML,
    CMAKE,
    SETUP_PY,
    CARGO_LOCK,
    POETRY_LOCK,
    UV_LOCK,
    NPM_LOCK,
    ELF,
    ESP_APP,
    MCUBOOT,
    PX4_FIRMWARE,
    SAFETENSORS,
    ONNX,
    PYTORCH,
    SPDX,
    CYCLONEDX,
    OCI_INDEX,
)
_BY_KEY: Final = {spec.key: spec for spec in FORMATS}
UNRECOGNISED: Final = "unrecognised"


def _finding(name: str, description: str) -> Documented:
    return Documented(f"{ADAPTER_ID}.{name}", description)


DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Software identity: git refs, manifests, lockfiles, firmware, checkpoints and SBOMs.",
    formats=tuple(spec.spec for spec in FORMATS),
    record_kinds=("software_configuration",),
    config=(
        ConfigOption(
            "max_document_bytes",
            8 * 1024 * 1024,
            "a text, TOML, JSON or XML file larger than this is reported, not parsed",
        ),
        ConfigOption(
            "max_header_bytes",
            8 * 1024 * 1024,
            "a checkpoint header, ELF header table or note, or zip directory larger than this is"
            " reported, not read",
        ),
        ConfigOption(
            "max_items",
            20000,
            "a file declaring more software items than this is reported and makes no record",
        ),
        ConfigOption(
            "max_script_bytes",
            256 * 1024,
            "a CMake or Python script larger than this is reported, not parsed: its syntax tree"
            " is many times its size",
        ),
    ),
    libraries=(),
    finding_codes=(
        _finding(
            "conflicting_identity",
            "two places in the file give one item different values; the field is Ambiguous"
            " (ambiguous, warning)",
        ),
        _finding(
            "git_symbolic_ref",
            "a ref names another ref, not a commit; details name it for binding (missing, info)",
        ),
        _finding(
            "git_unpeeled_tag",
            "a packed tag may name a tag object; its commit is Unknown (ambiguous, warning)",
        ),
        _finding(
            "invalid_value",
            "a declared value is not a valid member of its kind; it is Unknown"
            " (unrepresentable, warning)",
        ),
        _finding(
            "malformed",
            "the file does not parse as its format; no software is read (corrupt, error)",
        ),
        _finding(
            "malformed_entry",
            "one entry does not parse; it is skipped, the others are read (corrupt, error)",
        ),
        _finding(
            "no_software_declared",
            "the file parses but declares no software item, so no record (missing, info)",
        ),
        _finding(
            "software_identity_missing",
            "an item has no commit, release, build or digest though the format has a place for"
            " one (missing, warning)",
        ),
        _finding(
            "too_large",
            "a document or header is over its size option; it is not read (limit, error)",
        ),
        _finding(
            "too_many_entries",
            "more than max_items entries are malformed or are ELF notes; the rest are not read"
            " or reported (limit, error)",
        ),
        _finding(
            "too_many_items",
            "the file declares more than max_items items; no record is made (limit, error)",
        ),
        _finding(
            "truncated",
            "the bytes end before what the header declares: data, an image or a zip directory"
            " (corrupt, error)",
        ),
        _finding(
            "unevaluated",
            "a value is an expression (a CMake variable, a dynamic or inherited version, Python"
            " code); it is Unknown, never evaluated (unsupported, warning)",
        ),
        _finding(
            "unrecognised",
            "the adapter was chosen for bytes in none of its formats (unsupported, error)",
        ),
        _finding(
            "version_not_semver",
            "a version the format declares SemVer is not; it is kept as declared text"
            " (inconsistent, info)",
        ),
    ),
    locator_steps=(),
    conventions=(
        Documented(
            "assertion",
            "observed where the bytes describe themselves (git refs, firmware and checkpoint"
            " headers); stated where a file declares other software (manifests, lockfiles, SBOMs,"
            " image indexes)",
        ),
        Documented(
            "chunks",
            "one chunk per source; its context names the format the head was recognised as",
        ),
        Documented(
            "citations",
            "a value cites its exact bytes; one decoded from TOML or JSON cites [ByteRange of the"
            " document, JsonPointer], and a part of a string adds a Span of its code points",
        ),
        Documented(
            "digest",
            "only a stated checkpoint or container-image digest; NotApplicable for other"
            " software; a checkpoint's own identity is its content id, the record's source",
        ),
        Documented(
            "items",
            "one SoftwareConfiguration per file, machine NotCovered; an item per software unit"
            " the file declares, in file order",
        ),
        Documented(
            "states",
            "Known where the file gives a value; Unknown where the format has a place and the"
            " file leaves it out or blank; NotCovered where the format has no place",
        ),
        Documented(
            "versions",
            "SemanticVersion where the format makes versions SemVer (package.xml, Cargo);"
            " FirmwareVersion for firmware; DeclaredVersion otherwise; all verbatim",
        ),
    ),
    resources=Resources(max_memory=512 * 1024 * 1024, streaming=False),
    security=(
        "Never runs git, a build tool or Python: setup.py is parsed to a syntax tree only.",
        "Never loads model weights or unpickles: checkpoints are read by their headers.",
        "XML entity declarations are refused, never expanded; JSON with repeated keys is refused.",
        "Documents are bounded by max_document_bytes, headers and directories by"
        " max_header_bytes, items by max_items.",
    ),
)


def detect(head: bytes, size: int) -> tuple[Format, Detected] | None:
    """The format ``head`` starts, by the bytes alone: the most confident, first on a tie."""
    best: tuple[Format, Detected] | None = None
    for spec in FORMATS:
        claim = spec.detect(head, size)
        if claim is not None and (best is None or claim.confidence > best[1].confidence):
            best = spec, claim
    return best


def _head(source: SourceReader) -> bytes:
    return source.read(0, min(source.size, PROBE_HEAD_SIZE))


class SoftwareAdapter:
    """The software-identity adapter: one chunk per source, one record per declaring file."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        found = detect(head, hints.size)
        if found is None:
            return ProbeResult(0.0, ())
        spec, claim = found
        if spec is GIT_REF and hints.name.lower().endswith(CHECKSUM_SUFFIXES):
            why = ProbeReason(
                f"{ADAPTER_ID}.checksum_name", "one line of hex named like a checksum file"
            )
            return ProbeResult(0.0, (*claim.reasons, why))
        return ProbeResult(claim.confidence, claim.reasons, claim.version)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        found = detect(_head(source), source.size)
        summary: dict[str, JsonValue] = {
            "format": found[0].key if found else UNRECOGNISED,
            "size": source.size,
        }
        if found is not None and found[1].version is not None:
            summary["version"] = found[1].version
        return InspectResult(summary)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        found = detect(_head(source), source.size)
        key = found[0].key if found else UNRECOGNISED
        return Plan((make_chunk(source, config, {"format": key}, source.size),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        key = chunk.context["format"]
        spec = _BY_KEY.get(key) if isinstance(key, str) else None
        if spec is None:
            reading = Reading(source, config, "file", AssertionKind.OBSERVED)
            reading.report(
                "unrecognised",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                reading.whole,
                "the bytes are in none of the software adapter's formats; nothing is read",
            )
            return ChunkOutput(findings=tuple(reading.findings))
        reading = Reading(source, config, spec.label, spec.assertion)
        records = reading.configuration(spec.read(reading))
        return ChunkOutput(records=records, findings=tuple(reading.findings))


__all__ = ["DESCRIPTOR", "FORMATS", "SoftwareAdapter", "detect"]
