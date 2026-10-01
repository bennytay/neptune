"""Discovery's own findings: what a walk saw and, by policy, did not read (ADR 0029 §1).

Discovery is a producer like an adapter. Its transform is ``neptune.discovery`` at one version with
an empty config, so every finding it emits names what found it, and a policy change is a new
version and a new lineage. The policy itself is ``neptune.discovery.source``: symlinks are never
followed, special files are never opened, and nothing a walk could not examine is dropped silently.

Each finding's subject is the location it is about. Finding details hold nothing host-specific
except what the source itself declares: a symlink's target is the link's content, recorded exactly
as stored (ADR 0010 §2), with a lexical classification of where it points.
"""

import os
from pathlib import Path
from typing import Final

from neptune.discovery.source import SkippedEntry, SkipReason, SymlinkEntry
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonObject
from neptune.model.provenance import TransformRecord
from neptune.model.source import LocalPath, RawLocalPath, local_location

DISCOVERY_ADAPTER_ID: Final = "neptune.discovery"
DISCOVERY_VERSION: Final = "1.0.0"
DISCOVERY_TRANSFORM: Final[TransformRecord] = transform_record(
    adapter_id=DISCOVERY_ADAPTER_ID, adapter_version=DISCOVERY_VERSION, config={}
)

# Finding codes. Every code is ``<producer>.<name>`` and is documented in ADR 0029.
SYMLINK_NOT_FOLLOWED: Final = "neptune.discovery.symlink_not_followed"
SPECIAL_FILE: Final = "neptune.discovery.special_file"
VANISHED: Final = "neptune.discovery.vanished"
UNREADABLE: Final = "neptune.discovery.unreadable"
SIZE_CHANGED: Final = "neptune.discovery.size_changed"
TRUNCATED: Final = "neptune.discovery.truncated"
GROWN: Final = "neptune.discovery.grown"
CHUNK_CHANGED: Final = "neptune.discovery.chunk_changed"
SHORT_READ: Final = "neptune.discovery.short_read"

# The root's own skip has no location to name and is not a finding: a root that cannot be read is
# a job-level condition the caller sees in ``ScanResult.skipped``.
ROOT_ENTRY: Final = b"."


def symlink_finding(root_forms: frozenset[bytes], entry: SymlinkEntry) -> IngestFinding:
    details = symlink_details(root_forms, entry.location.raw, entry.target)
    where = "inside" if details["inside_root"] else "outside"
    return ingest_finding(
        code=SYMLINK_NOT_FOLLOWED,
        category=FindingCategory.SKIPPED,
        severity=Severity.INFO,
        subject=entry.location,
        transform=DISCOVERY_TRANSFORM,
        message=f"symlink recorded and not followed; its target points {where} the root",
        details=details,
    )


def skipped_finding(entry: SkippedEntry) -> IngestFinding:
    subject = local_location(entry.raw_path)
    match entry.reason:
        case SkipReason.NOT_REGULAR_FILE:
            code, severity = SPECIAL_FILE, Severity.INFO
            message = "not a regular file; never opened"
            details: JsonObject = {"mode": entry.detail}
        case SkipReason.MISSING:
            code, severity = VANISHED, Severity.WARNING
            message = "vanished during the scan; not read, and nothing is asserted about it"
            details = {"detail": entry.detail}
        case SkipReason.SYMLINK:
            code, severity = SYMLINK_NOT_FOLLOWED, Severity.INFO
            message = "a symlink at or above this path was refused at open; not followed"
            details = {"detail": entry.detail}
        case SkipReason.UNREADABLE:
            code, severity = UNREADABLE, Severity.ERROR
            message = "could not be read; nothing at or below it was seen"
            details = {"detail": entry.detail}
    return ingest_finding(
        code=code,
        category=FindingCategory.SKIPPED,
        severity=severity,
        subject=subject,
        transform=DISCOVERY_TRANSFORM,
        message=message,
        details=details,
    )


def size_changed_finding(
    location: LocalPath | RawLocalPath, size_at_walk: int, size_digested: int
) -> IngestFinding:
    """The file held a different number of bytes when hashed than when the walk saw it."""
    return ingest_finding(
        code=SIZE_CHANGED,
        category=FindingCategory.INCONSISTENT,
        severity=Severity.WARNING,
        subject=location,
        transform=DISCOVERY_TRANSFORM,
        message="size changed between the walk and the digest; the file was being written",
        details={"size_at_walk": size_at_walk, "size_digested": size_digested},
    )


def symlink_details(root_forms: frozenset[bytes], link: bytes, target: bytes) -> JsonObject:
    """The target as declared, whether it is absolute, and whether it lexically stays in the root.

    Lexical means ``..`` is collapsed and nothing is resolved on disk: the walk never follows
    links, so this is a statement about the declared target, not about what it reaches.
    """
    absolute = target.startswith(b"/")
    if absolute:
        resolved = os.path.normpath(target)
        inside = any(resolved == form or resolved.startswith(form + b"/") for form in root_forms)
    else:
        parent = link.rsplit(b"/", 1)[0] if b"/" in link else b""
        resolved = os.path.normpath(parent + b"/" + target if parent else target)
        inside = resolved != b".." and not resolved.startswith(b"../")
    return {"absolute": absolute, "inside_root": inside, **bytes_field("target", target)}


def bytes_field(name: str, value: bytes) -> JsonObject:
    """``{name: text}`` when ``value`` is valid UTF-8, else ``{name_hex: hex}`` (ADR 0010 §1)."""
    try:
        return {name: value.decode("utf-8")}
    except UnicodeDecodeError:
        return {f"{name}_hex": value.hex()}


def text_field(name: str, value: str) -> JsonObject:
    """Like ``bytes_field`` for text that may hold ``surrogateescape`` bytes (tar member names)."""
    return bytes_field(name, value.encode("utf-8", "surrogateescape"))


def path_problem(name: str) -> str | None:
    """Why ``name``, a path declared inside a source, could escape a root; ``None`` if it cannot.

    ``.`` and empty components are tolerated: they normalise losslessly. Backslashes are
    characters, as everywhere else on the platforms Neptune runs on.
    """
    if not name:
        return "empty"
    if "\x00" in name:
        return "nul"
    if name.startswith("/"):
        return "absolute"
    if any(part == ".." for part in name.split("/")):
        return "parent_reference"
    return None


def root_forms(root: str | os.PathLike[str]) -> frozenset[bytes]:
    """The root as given (made absolute) and as resolved, so either spelling counts as inside."""
    path = Path(root)
    return frozenset(
        {os.fsencode(os.path.normpath(path.absolute())), os.fsencode(path.resolve(strict=False))}
    )
