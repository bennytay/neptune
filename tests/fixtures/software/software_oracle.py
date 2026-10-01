"""An independent reader of the software adapter's citations, for its tests.

Every ``Known`` value the adapter emits must be found again where it cites: the cited bytes hold
its text, or the decoded document holds it at the cited pointer (and span). This module resolves
a citation with the standard library alone, never with the adapter's code.
"""

import json
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any, Final

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.software import SoftwareAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Ambiguous, Known, KnownAbsent, Unknown
from neptune.model.machine import SoftwareConfiguration, SoftwareItem
from neptune.model.provenance import ByteRange, EvidenceRef, JsonPointer, Provenance, Span
from neptune.model.versions import ContainerImageDigest, GitCommit, ModelCheckpointHash

HERE: Final = Path(__file__).parent
FIELDS: Final = ("name", "device", "commit", "release", "build", "digest")


def fixture(relative: str) -> bytes:
    return (HERE / relative).read_bytes()


def fixtures() -> list[str]:
    """Every fixture file, relative to this directory, in order."""
    return sorted(
        path.relative_to(HERE).as_posix()
        for path in HERE.rglob("*")
        if path.is_file() and path.suffix not in (".py", ".md") and "__pycache__" not in path.parts
    )


def run(data: bytes, **config: Any) -> SourceOutput:
    return ingest_source(SoftwareAdapter(), BytesReader(data), config)


def items(output: SourceOutput) -> list[SoftwareItem]:
    records = [record for record in output.records() if isinstance(record, SoftwareConfiguration)]
    return [item for record in records for item in record.software]


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code.removeprefix("software.") for finding in output.findings())


def text_of(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, GitCommit):
        return value.sha
    if isinstance(value, ModelCheckpointHash | ContainerImageDigest):
        return value.digest
    text = getattr(value, "value", None)
    assert isinstance(text, str), value
    return text


def known(state: object) -> str | None:
    """A ``Known`` state's value as text, else ``None``."""
    return text_of(state.value) if isinstance(state, Known) else None


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return tomllib.loads(text)


def _follow(document: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        document = document[int(key)] if isinstance(document, list) else document[key]
    return document


def resolve(data: bytes, evidence: EvidenceRef) -> Any:
    """What ``evidence`` cites in ``data``: bytes, a decoded value, or a part of a string."""
    first, *rest = evidence.locator
    assert isinstance(first, ByteRange)
    assert first.offset + first.length <= len(data), evidence
    cited: Any = data[first.offset : first.offset + first.length]
    for step in rest:
        if isinstance(step, JsonPointer):
            cited = _follow(_decode(cited), step.pointer)
        else:
            assert isinstance(step, Span) and isinstance(cited, str), step
            cited = cited[step.start : step.end]
    return cited


def _varint(raw: bytes) -> int:
    value = 0
    for shift, byte in enumerate(raw):
        value |= (byte & 0x7F) << (7 * shift)
    return value


def _holds(cited: Any, text: str, evidence: EvidenceRef) -> bool:
    if isinstance(cited, dict):  # an npm entry named by its key: the pointer's last token
        last = evidence.locator[-1]
        assert isinstance(last, JsonPointer)
        key = last.pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
        return key.rsplit("node_modules/", 1)[-1] == text
    if isinstance(cited, str):
        return cited == text or urllib.parse.unquote(cited) == text
    assert isinstance(cited, bytes)
    if text.encode() in cited or cited.hex() == text:
        return True
    if len(cited) == 8 and "+" in text:  # an MCUboot ih_ver, rendered major.minor.revision+build
        major, minor = cited[0], cited[1]
        revision = int.from_bytes(cited[2:4], "little")
        build = int.from_bytes(cited[4:8], "little")
        return text == f"{major}.{minor}.{revision}+{build}"
    return text.lstrip("-").isdigit() and _varint(cited) % (1 << 64) == int(text) % (1 << 64)


def check_citations(data: bytes, output: SourceOutput) -> int:
    """Every value resolves where it cites; returns how many values were checked."""
    checked = 0
    for record in output.records():
        resolve(data, record.provenance.evidence)
        for item in record.software:
            for name in FIELDS:
                state = getattr(item, name)
                candidates = state.candidates if isinstance(state, Ambiguous) else (state,)
                for candidate in candidates:
                    provenance = getattr(candidate, "provenance", None)
                    if not isinstance(provenance, Provenance):
                        continue
                    cited = resolve(data, provenance.evidence)
                    if isinstance(candidate, Unknown | KnownAbsent):
                        continue
                    value = text_of(candidate.value)
                    assert _holds(cited, value, provenance.evidence), (name, value, cited)
                    checked += 1
    for finding in output.findings():
        assert isinstance(finding.subject, EvidenceRef)
        resolve(data, finding.subject)
    return checked
