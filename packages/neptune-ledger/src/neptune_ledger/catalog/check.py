"""Check a package directory the way registration and verification must (Ledger ADR 0006 §1, §2).

Every path is opened component by component relative to the root's directory descriptor with
``O_NOFOLLOW``, so no symlink is ever followed, whatever is swapped in after the walk. The checks
run in ADR 0006 §1's order and each stage reports every problem it finds before the next stage is
skipped:

1. ``unsafe_entry``: an entry at any depth that is not a regular file or a directory.
2. ``manifest.json``: absent or not a file (``package_unreadable`` on register, ``file_missing``
   on verify), not a manifest (``manifest_invalid``), another schema version
   (``unsupported_schema_version``), or no table count for some record kind (``manifest_invalid``).
3. Files: ``file_missing``, ``unexpected_file``, ``file_digest_mismatch``. A listed path that is
   absolute or escapes the root can never equal a walked entry, so it is ``file_missing`` and
   nothing outside the root is opened. A file whose size differs is never read.
4. Records: the compiler's own package verification (``neptune.store.package.read_files``, the core
   of ``read_package``) over the bytes read in stage 3, so what is indexed is what was hashed.
"""

import errno
import hashlib
import os
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from neptune.identity import canonical_json
from neptune.model.kinds import RECORD_KINDS
from neptune.model.package import package_manifest_from_json
from neptune.model.record import SCHEMA_VERSION
from neptune.store.package import MANIFEST, VOLATILE, IngestPackage, PackageError, read_files
from neptune_ledger.api.types import CatalogFinding, FindingCode

# Files the compiler streams instead of holding in memory (neptune.store.package): series and
# blobs. They are hashed here as streams and handed to the compiler's checks by path.
_LARGE: Final = re.compile(r"series/[0-9a-f]{64}\.parquet|blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}")
_READ_SIZE: Final = 1024 * 1024
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

Mode = Literal["register", "verify"]


@dataclass(frozen=True)
class Checked:
    """The outcome of checking one package directory.

    ``package_id`` is the sha256 of ``manifest.json`` when it could be read. ``package`` is the
    compiler's verified package and ``lines`` each record table's lines (without the newline), both
    present only when every check passed. ``files_checked`` counts the listed files compared.
    """

    findings: tuple[CatalogFinding, ...]
    package_id: str | None = None
    schema_version: int | None = None
    files_checked: int = 0
    package: IngestPackage | None = None
    lines: Mapping[str, tuple[bytes, ...]] = field(default_factory=dict)
    manifest: Mapping[str, Any] = field(default_factory=dict)


def open_root(path: str) -> int | None:
    """A descriptor for the directory at the absolute, resolved ``path``, or None.

    Every component is opened with ``O_NOFOLLOW`` relative to its parent, so ``path`` must name
    the directory with no link anywhere on it: a component that is missing, not a directory, or
    a link (including one swapped in after the path was resolved) gives None (ADR 0006 §3).
    """
    if not path.startswith("/"):
        return None
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.split("/"):
            if part:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
    except OSError:
        os.close(fd)
        return None
    return fd


def check_package(root_fd: int, mode: Mode, expected_id: str | None = None) -> Checked:
    """Check the package whose root directory is open as ``root_fd``.

    ``mode`` is ``register`` (every stage, records included) or ``verify`` (entries, the manifest
    against ``expected_id`` and the files; ADR 0006 §2). Never raises for anything in the package.
    """
    entries = dict(_walk(root_fd))
    unsafe = sorted(path for path, kind in entries.items() if kind == "unsafe")
    if unsafe:
        return Checked(
            tuple(
                CatalogFinding(
                    "unsafe_entry", path, "not a regular file or directory; it was not followed"
                )
                for path in unsafe
            )
        )

    if entries.get(MANIFEST) != "file":
        code: FindingCode = "file_missing" if mode == "verify" else "package_unreadable"
        return Checked((CatalogFinding(code, MANIFEST, "the package has no readable manifest"),))
    manifest_bytes = _read_small(root_fd, MANIFEST)
    if manifest_bytes is None:
        return Checked((CatalogFinding("package_unreadable", MANIFEST, "cannot read it"),))
    package_id = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if mode == "verify" and package_id != expected_id:
        return Checked(
            (
                CatalogFinding(
                    "manifest_digest_mismatch", MANIFEST, "it no longer hashes to the package id"
                ),
            ),
            package_id=package_id,
        )
    manifest, problem = _manifest(manifest_bytes)
    version = manifest.get("schema_version") if manifest else None
    version = version if isinstance(version, int) and not isinstance(version, bool) else None
    if problem is not None:
        kind: FindingCode = (
            "unsupported_schema_version" if problem == "version" else "manifest_invalid"
        )
        detail = (
            f"schema version {version}; this Ledger reads {SCHEMA_VERSION}"
            if problem == "version"
            else problem
        )
        return Checked((CatalogFinding(kind, MANIFEST, detail),), package_id, version)

    listed: dict[str, tuple[int, str]] = {
        f["path"]: (f["size"], f["sha256"]) for f in manifest["files"]
    }
    present = {
        path
        for path, kind in entries.items()
        if kind == "file" and path != MANIFEST and not _volatile(path)
    }
    findings = [
        CatalogFinding("file_missing", path, "listed in the manifest, not present")
        for path in sorted(set(listed) - present)
    ]
    findings += [
        CatalogFinding("unexpected_file", path, "present, not listed in the manifest")
        for path in sorted(present - set(listed))
    ]
    files: dict[str, bytes | Path] = {MANIFEST: manifest_bytes}
    parents: dict[str, int] = {}  # directories of large files, held open for read_files
    try:
        for path in sorted(set(listed) & present):
            size, digest = listed[path]
            large = bool(_LARGE.fullmatch(path))
            got = _hash(root_fd, path, size, keep=mode == "register" and not large)
            if got is None or got[0] != digest:
                detail = "size or sha256 differs from the manifest"
                findings.append(CatalogFinding("file_digest_mismatch", path, detail))
            elif mode == "register" and not large:
                files[path] = got[1] or b""
            elif mode == "register":
                try:
                    files[path] = _pinned(root_fd, path, parents)
                except OSError:  # its directory changed since the walk
                    detail = "its directory changed while it was being checked"
                    findings.append(CatalogFinding("file_digest_mismatch", path, detail))
        checked = len(listed)
        if findings or mode == "verify":
            return Checked(tuple(findings), package_id, version, files_checked=checked)
        try:
            package_manifest_from_json(manifest)
            package = read_files(files)
        except (PackageError, ValueError, TypeError, KeyError, OSError) as exc:
            return Checked(
                (CatalogFinding("record_invalid", package_id, _first_line(exc)),),
                package_id,
                version,
                files_checked=checked,
            )
    finally:
        for descriptor in parents.values():
            os.close(descriptor)
    lines: dict[str, tuple[bytes, ...]] = {}
    for table in RECORD_KINDS:
        data = files[f"records/{table}.jsonl"]
        assert isinstance(data, bytes)
        lines[table] = tuple(data.split(b"\n")[:-1])
    return Checked((), package_id, version, checked, package, lines, manifest)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:500]


def _volatile(path: str) -> bool:
    return path == VOLATILE or path.startswith(f"{VOLATILE}/")


def _manifest(data: bytes) -> tuple[dict[str, Any], str | None]:
    """The manifest as JSON and the first structural problem: what stages 2 and 3 rely on.

    The compiler's strict parser runs later (stage 4), so a listed path that escapes the root is
    reported as ``file_missing`` (ADR 0006 §1) rather than hidden behind a parse error.
    """
    try:
        value = canonical_json.loads(data)
    except ValueError as exc:
        return {}, f"not canonical JSON: {_first_line(exc)}"
    if not isinstance(value, dict) or value.get("kind") != "package_manifest":
        return {}, "not a package manifest"
    version = value.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return value, "no integer schema_version"
    if version != SCHEMA_VERSION:
        return value, "version"
    tables = value.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(RECORD_KINDS):
        return value, "it must count a table for every record kind of the schema version"
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in tables.values()):
        return value, "a table count is not a non-negative integer"
    files = value.get("files")
    if not isinstance(files, list) or not all(_is_file_entry(f) for f in files):
        return value, "files must be a list of {path, size, sha256}"
    if len({f["path"] for f in files}) != len(files):
        return value, "a file is listed twice"
    return value, None


def _is_file_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("sha256"), str)
        and isinstance(entry.get("size"), int)
        and not isinstance(entry.get("size"), bool)
    )


def _walk(root_fd: int) -> Iterator[tuple[str, str]]:
    """``(package-relative path, "dir" | "file" | "unsafe")`` for every entry, never following one.

    A directory is opened only when it is listed, component by component from the root with
    ``O_NOFOLLOW`` (``_open_dir``), so at most two descriptors are held whatever the tree's width,
    and a directory replaced by a link after it was seen is reported, not entered.
    """
    stack = [""]
    while stack:
        prefix = stack.pop()
        try:
            fd = _open_dir(root_fd, prefix) if prefix else os.dup(root_fd)
        except OSError:
            yield prefix, "unsafe"
            continue
        try:
            with os.scandir(fd) as found:
                names = sorted(entry.name for entry in found)
            for name in names:
                path = f"{prefix}/{name}" if prefix else name
                try:
                    mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                except OSError:
                    yield path, "unsafe"
                    continue
                if stat.S_ISREG(mode):
                    yield path, "file"
                elif stat.S_ISDIR(mode):
                    yield path, "dir"
                    stack.append(path)
                else:
                    yield path, "unsafe"  # symlink, FIFO, socket, device
        except OSError:
            yield prefix or ".", "unsafe"
        finally:
            os.close(fd)


def _open_dir(root_fd: int, path: str) -> int:
    """Open the directory ``path`` under the root, every component with ``O_NOFOLLOW``."""
    fd = os.dup(root_fd)
    try:
        for part in path.split("/"):
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_below(root_fd: int, path: str) -> int:
    """Open the regular file ``path`` under the root, every component with ``O_NOFOLLOW``.

    A FIFO or device is refused without blocking on it (``O_NONBLOCK``); any failure is OSError.
    """
    head, _, name = path.rpartition("/")
    fd = _open_dir(root_fd, head) if head else os.dup(root_fd)
    try:
        target = os.open(name, _FILE_FLAGS, dir_fd=fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(target).st_mode):
        os.close(target)
        raise OSError(errno.EINVAL, f"{path} is not a regular file")
    os.set_blocking(target, True)
    return target


def _read_small(root_fd: int, path: str) -> bytes | None:
    try:
        with os.fdopen(open_below(root_fd, path), "rb") as stream:
            return stream.read()
    except OSError:
        return None


def _hash(root_fd: int, path: str, size: int, *, keep: bool) -> tuple[str, bytes | None] | None:
    """``(sha256, bytes if keep)`` of the file, or None if it cannot be read or has another size.

    The size is compared before reading, so a hostile file of another size is never read.
    """
    try:
        descriptor = open_below(root_fd, path)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size != size:
            return None
        digest, kept, total = hashlib.sha256(), [], 0
        while block := stream.read(_READ_SIZE):
            digest.update(block)
            total += len(block)
            if total > size:
                return None
            if keep:
                kept.append(block)
    if total != size:
        return None
    return "sha256:" + digest.hexdigest(), b"".join(kept) if keep else None


def _pinned(root_fd: int, path: str, parents: dict[str, int]) -> Path:
    """A path for the compiler's streaming checks that cannot leave the checked tree.

    The file's directory is opened component by component with ``O_NOFOLLOW`` and held open;
    ``/proc/self/fd/N/<name>`` then names that directory, whatever its path names now, and the
    compiler opens ``<name>`` itself with ``O_NOFOLLOW``. This needs Linux's ``/proc``.
    """
    head, _, name = path.rpartition("/")
    if head not in parents:
        parents[head] = _open_dir(root_fd, head)
    return Path(f"/proc/self/fd/{parents[head]}") / name
