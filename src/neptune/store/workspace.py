"""The local workspace: Neptune's metadata, index and cache on this machine (ADRs 0026, 0031).

Everything an ingest needs to remember between runs lives here, never beside the evidence:

    workspace.json                     format version and settings (local-only mode)
    lock                               held shared by every running job, exclusively by ``collect``
    ledgers/<root key>/ledger.jsonl    one ingest root's source ledger, keyed by its resolved path
    ledgers/<root key>/root            the root's resolved path, as the host names it
    plans/<2 hex>/<62 hex>/<64 hex>.json
                                       one source's plan under one transform, with the transform:
                                       by the source's content id, then the transform's id
    chunks/<2 hex>/<62 hex>/           one committed chunk output, by chunk id:
        chunk.json                     the chunk
        records.jsonl, findings.jsonl  its records and findings, canonical lines sorted by id
        runs/<64 hex>.parquet          its rows of each stream, sorted (``store.series``)
    derivatives/<2 hex>/<62 hex>/      one derivative, by the id of its ``DerivativeKey``:
        derivative.json                the key, and each file's size and sha256
        <name>                         its files
    staging/                           work in progress; anything here is incomplete

A chunk is committed by renaming its finished, flushed directory into ``chunks/``: a commit is
atomic, so a process killed midway leaves no partial chunk, and committing a chunk again changes
nothing. A derivative is kept the same way. Its sources are never copied here: the workspace holds
what was derived from them and where they are. Each directory in ``staging/`` is locked (``flock``)
by the process writing it, so ``clear_staging`` removes what dead processes left and never what a
live one is writing. An entry is removed by one rename into ``staging/`` first, so nothing is ever
half there.

The workspace is the cache (ADR 0031): a plan is kept by (source, transform), a chunk's output by
its chunk id, and a derivative (a series file, a source's verdict) by a key covering everything it
reads. ``collect`` removes what no job with the current adapters and config can ask for again.

The workspace is local-first. It is local-only by default: anything that would use the network asks
``require_network`` first and is refused until the workspace allows it.
"""

import fcntl
import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any, Final, Protocol, TypeAlias

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import digest_stream
from neptune.identity.provenance import check_transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.ids import ContentId, RecordId, parse_content_id, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import RECORD_KINDS, record_key
from neptune.model.provenance import TransformRecord, transform_record_from_json
from neptune.model.series import SeriesBatch
from neptune.model.source import SourceAbsence, SourceArtifact, SourceRevision
from neptune.store.durable import fsync_directory, fsync_tree
from neptune.store.series import write_run

FORMAT: Final = 2
UPGRADABLE: Final = (1,)  # older formats this version upgrades in place when it opens them
WORKSPACE_KIND: Final = "neptune_workspace"
HOME_VARIABLE: Final = "NEPTUNE_HOME"
LOCK: Final = "lock"
DERIVATIVE_FILE: Final = "derivative.json"
DERIVATIVE_ID_SCHEME: Final = "neptune.derivative-id/1"


class WorkspaceError(ValueError):
    """The workspace is not one this version can use, or what it holds is inconsistent."""


class WorkspaceBusyError(WorkspaceError):
    """A job holds the workspace, so it cannot be collected now."""


class LocalOnlyError(Exception):
    """Something asked for the network while the workspace is local-only.

    A policy refusal, not an I/O failure: it is no ``OSError``, so code that retries or skips on
    ``OSError`` cannot swallow it.
    """


def default_home() -> Path:
    """``$NEPTUNE_HOME``, else ``$XDG_CACHE_HOME/neptune``, else ``~/.cache/neptune``."""
    if home := os.environ.get(HOME_VARIABLE):
        return Path(home)
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "neptune"


_PREFIX: Final = re.compile(r"[0-9a-f]{2}")
_REST: Final = re.compile(r"[0-9a-f]{62}")
_RUN: Final = re.compile(r"([0-9a-f]{64})\.parquet")
_PLAN: Final = re.compile(r"([0-9a-f]{64})\.json")  # a transform's plan, in its source's directory
_PLAN_1: Final = re.compile(r"[0-9a-f]{62}\.json")  # format 1: plans/<2 hex>/<62 hex>.json
_RECIPE: Final = re.compile(r"[a-z][a-z0-9_.\-]*/[1-9][0-9]*")
_FILE: Final = re.compile(r"[a-z0-9][a-z0-9_.\-]*")

# A derivative's owner: the (source content id, transform id) pair whose chunks it reads.
Owner: TypeAlias = tuple[ContentId, RecordId]


def _hex(identifier: str, scheme: str) -> str:
    """The 64 hex digits of a ``<scheme>:sha256:<hex>`` id; any other id is refused."""
    match = re.fullmatch(rf"{scheme}:sha256:([0-9a-f]{{64}})", identifier)
    if match is None:
        raise WorkspaceError(f"not a {scheme}:sha256 id: {identifier!r}")
    return match[1]


def _root_key(root: Path) -> bytes:
    """An ingest root as the ledger knows it: resolved, so every spelling of it is one root."""
    return os.fsencode(Path(root).resolve())


def _lines(items: Iterable[Any]) -> bytes:
    """Canonical JSON lines, sorted by record key, as ADR 0002 writes tables."""
    ordered = sorted(items, key=record_key)
    return b"".join(canonical_json.dumps(item.to_json()) + b"\n" for item in ordered)


def _read_lines(data: bytes) -> list[Any]:
    records = []
    for line in data.splitlines():
        value = canonical_json.loads(line)
        kind = value.get("kind") if isinstance(value, dict) else None
        if not isinstance(kind, str) or kind not in RECORD_KINDS:
            raise WorkspaceError(f"a stored line is not a record: {line[:80]!r}")
        records.append(RECORD_KINDS[kind][1](value))
    return records


def _is_directory(path: Path, name: re.Pattern[str]) -> bool:
    """A real directory (not a link to one) whose name is ``name``."""
    return bool(name.fullmatch(path.name)) and not path.is_symlink() and path.is_dir()


def _is_file(path: Path) -> bool:
    """A regular file, not a link to one."""
    return not path.is_symlink() and path.is_file()


def _ids(
    directory: Path, what: str, passed: re.Pattern[str] | None = None
) -> Iterator[tuple[str, Path]]:
    """The 64 hex digits and path of each ``<2 hex>/<62 hex>/`` entry under ``directory``.

    Entries named as ``passed`` are passed over. Anything else there is a ``WorkspaceError``
    naming it, never a crash or a made-up id.
    """
    for prefix in sorted(directory.iterdir()):
        if not _is_directory(prefix, _PREFIX):
            raise WorkspaceError(f"{prefix} is not a {what}'s prefix directory")
        for entry in sorted(prefix.iterdir()):
            if passed is not None and passed.fullmatch(entry.name):
                continue
            if not _is_directory(entry, _REST):
                raise WorkspaceError(f"{entry} is not a {what}")
            yield prefix.name + entry.name, entry


def _source_hex(source: str) -> str:
    try:
        return parse_content_id(source).removeprefix("sha256:")
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(f"not a source's content id: {source!r}") from exc


def _transform_hex(transform: str) -> str:
    try:
        return parse_record_id(transform).removeprefix("rec:sha256:")
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(f"not a transform's id: {transform!r}") from exc


def _same_file(path: Path, descriptor: int) -> bool:
    """Whether ``path`` still names the file or directory open at ``descriptor``."""
    try:
        named = path.lstat()
    except FileNotFoundError:
        return False
    held = os.fstat(descriptor)
    return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


class ChunkLike(Protocol):
    """What the workspace needs of a chunk (``neptune.adapters.contract.Chunk``)."""

    @property
    def id(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def transform(self) -> str: ...

    def to_json(self) -> JsonObject: ...


@dataclass(frozen=True)
class StoredPlan:
    """A source's plan as the workspace keeps it: the transform, the chunks, the findings.

    Chunks stay JSON here, since the store does not import the adapter contract;
    ``neptune.adapters.contract.chunk_from_json`` reads and checks them.
    """

    transform: TransformRecord
    chunks: tuple[JsonObject, ...]
    findings: tuple[IngestFinding, ...]


@dataclass(frozen=True)
class CommittedChunk:
    """One committed chunk output: its records, its findings, and its sorted run of each stream."""

    chunk: JsonObject
    records: tuple[Any, ...]
    findings: tuple[IngestFinding, ...]
    runs: dict[RecordId, Path]


@dataclass(frozen=True)
class DerivativeKey:
    """What a derivative is a function of, and so what the workspace keeps it by (ADR 0031 §4).

    ``recipe`` names the computation and its version (``neptune.store.series/1``): anything that
    changes what it computes is a new version, so a new key. ``inputs`` is everything else it
    reads, as canonical JSON: chunk ids, settings, the version of the code it runs. ``owners`` are
    the (source, transform) pairs those chunks belong to, sorted, so that collection knows when no
    job can ask for the derivative again.
    """

    recipe: str
    inputs: JsonObject
    owners: tuple[Owner, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.recipe, str) or not _RECIPE.fullmatch(self.recipe):
            raise WorkspaceError(f"a recipe is '<name>/<version>': {self.recipe!r}")
        if not isinstance(self.inputs, Mapping):
            raise WorkspaceError("a derivative's inputs are a JSON object")
        try:
            canonical_json.dumps(self.inputs)
        except ValueError as exc:
            raise WorkspaceError(f"a derivative's inputs are not canonical JSON: {exc}") from exc
        if not self.owners:
            raise WorkspaceError("a derivative derives from at least one (source, transform)")
        for owner in self.owners:
            if not isinstance(owner, tuple) or len(owner) != 2:
                raise WorkspaceError(f"an owner is a (source, transform) pair: {owner!r}")
            _source_hex(owner[0])
            _transform_hex(owner[1])
        if list(self.owners) != sorted(set(self.owners)):
            raise WorkspaceError("a derivative's owners are sorted, each once")

    @cached_property
    def id(self) -> str:
        """``drv:sha256:<64 hex>`` over the key's canonical JSON."""
        return "drv:sha256:" + hashlib.sha256(canonical_json.dumps(self.to_json())).hexdigest()

    def to_json(self) -> JsonObject:
        return {
            "inputs": self.inputs,
            "owners": [list(owner) for owner in self.owners],
            "recipe": self.recipe,
            "scheme": DERIVATIVE_ID_SCHEME,
        }


def derivative_key_from_json(data: JsonValue) -> DerivativeKey:
    """Parse strictly: exactly ``inputs``, ``owners``, ``recipe`` and this version's ``scheme``."""
    if not isinstance(data, Mapping) or data.keys() != {"inputs", "owners", "recipe", "scheme"}:
        raise WorkspaceError(f"a derivative key is {{inputs, owners, recipe, scheme}}: {data!r}")
    if data["scheme"] != DERIVATIVE_ID_SCHEME:
        raise WorkspaceError(f"derivative id scheme {data['scheme']!r} is not this version's")
    recipe, inputs, owners = data["recipe"], data["inputs"], data["owners"]
    if not isinstance(recipe, str) or not isinstance(inputs, Mapping):
        raise WorkspaceError("a derivative's recipe is text and its inputs an object")
    if not isinstance(owners, list) or not all(
        isinstance(o, list) and len(o) == 2 and all(isinstance(i, str) for i in o) for o in owners
    ):
        raise WorkspaceError("a derivative's owners are [source, transform] pairs")
    pairs = tuple(
        (ContentId(str(o[0])), RecordId(str(o[1]))) for o in owners if isinstance(o, list)
    )
    return DerivativeKey(recipe, inputs, pairs)


class Held(StrEnum):
    """How ``materialise`` came by a derivative."""

    HELD = "held"  # kept, and it reads back whole: reused
    BUILT = "built"  # not kept: built now
    REBUILT = "rebuilt"  # kept but damaged: removed and built again


@dataclass(frozen=True)
class Derivative:
    """A kept derivative: its key, its directory, and each file's size and sha256 when kept."""

    key: DerivativeKey
    path: Path
    files: Mapping[str, tuple[int, ContentId]] = field(default_factory=dict)

    def file(self, name: str) -> Path:
        if name not in self.files:
            raise WorkspaceError(f"derivative {self.key.id} has no file {name!r}")
        return self.path / name

    def read(self, name: str) -> bytes:
        """A small file's bytes, checked against the size and hash kept for it.

        A file that no longer matches is a ``WorkspaceError``: the caller discards the derivative
        and builds it again. Large files are copied and checked as streams instead.
        """
        data = self.file(name).read_bytes()
        size, digest = self.files[name]
        if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise WorkspaceError(f"derivative {self.key.id}: {name} is not what was kept")
        return data


@dataclass(frozen=True)
class Collected:
    """What ``collect`` removed: plans, chunks, derivatives, and staging debris."""

    plans: int
    chunks: int
    derivatives: int
    staging: int


class Workspace:
    """A workspace directory, created on first use. Several processes may share one."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home is not None else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        fsync_directory(self.home.parent)  # the home's own name, whoever made it
        settings = self.home / "workspace.json"
        existing = self._check(settings, (FORMAT, *UPGRADABLE)) if settings.exists() else None
        for directory in ("ledgers", "plans", "chunks", "derivatives", "staging"):
            (self.home / directory).mkdir(exist_ok=True)
        fsync_directory(self.home)  # its folders' names, every time: one may have been remade
        if existing is None:
            self._save_settings({"local_only": True})
        elif existing["format"] == 1:
            self._upgrade_from_1()
            self._save_settings({k: v for k, v in existing.items() if k not in ("format", "kind")})
        self._settings = self._check(settings, (FORMAT,))

    def _check(self, settings: Path, formats: tuple[int, ...]) -> dict[str, JsonValue]:
        try:
            data = canonical_json.loads(settings.read_bytes())
        except ValueError as exc:
            raise WorkspaceError(f"{self.home} is not a Neptune workspace: {exc}") from exc
        if not isinstance(data, dict) or data.get("kind") != WORKSPACE_KIND:
            raise WorkspaceError(f"{self.home} is not a Neptune workspace")
        version = data.get("format")
        if isinstance(version, bool) or version not in formats:
            raise WorkspaceError(
                f"workspace format {version!r}; this version reads format {FORMAT}"
            )
        return data

    def _upgrade_from_1(self) -> None:
        """Format 1 kept plans at ``plans/<2 hex>/<62 hex>.json``; move each under its source.

        Each move is one rename and the format changes last, so a process killed midway leaves a
        format-1 workspace that the next open finishes upgrading. Nothing else moves. A plan that
        cannot be read is left where it is: no job can reuse it, it never stops the workspace
        from opening, and collection removes it (ADR 0031 §7).
        """
        for entry in self._plans_1():
            target = self._plan_1_target(entry)
            if target is not None:
                self._move_plan_1(entry, target)

    def _plans_1(self) -> list[Path]:
        """Every entry under ``plans/`` named as format 1 named a plan, in order.

        After the upgrade, one is a plan a job of the previous version saved while the upgrade
        ran, or one that could not be read; ``plans`` passes them over and ``collect`` settles them.
        """
        found: list[Path] = []
        for prefix in sorted((self.home / "plans").iterdir()):
            if not _is_directory(prefix, _PREFIX):
                raise WorkspaceError(f"{prefix} is not a plan's prefix directory")
            found.extend(e for e in sorted(prefix.iterdir()) if _PLAN_1.fullmatch(e.name))
        return found

    def _plan_1_target(self, entry: Path) -> Path | None:
        """Where format 2 files the format-1 plan at ``entry``; ``None`` if it is no readable plan.

        A plan another process moved first is no plan here either: ``None``.
        """
        if not _is_file(entry):
            return None
        try:
            data = canonical_json.loads(entry.read_bytes())
            if not isinstance(data, dict):
                raise ValueError("not an object")
            transform, chunks = data["transform"], data["chunks"]
            if not isinstance(transform, dict) or not isinstance(chunks, list) or not chunks:
                raise ValueError("no transform or no chunks")
            first = chunks[0]
            if not isinstance(first, dict):
                raise ValueError("a chunk is not an object")
            return self._plan_path(ContentId(str(first["source"])), RecordId(str(transform["id"])))
        except (KeyError, ValueError, OSError):  # FileNotFoundError and WorkspaceError included
            return None

    def _move_plan_1(self, entry: Path, target: Path) -> None:
        """Move a format-1 plan to ``target``, its place in format 2, in one rename."""
        self._directories(target.parent)
        try:
            entry.replace(target)
        except FileNotFoundError:
            return  # moved by another process between the read and the rename
        fsync_directory(target.parent)
        fsync_directory(entry.parent)

    # --- Settings ------------------------------------------------------------------------------

    def _save_settings(self, settings: dict[str, JsonValue]) -> None:
        document = {**settings, "format": FORMAT, "kind": WORKSPACE_KIND}
        self._replace(self.home / "workspace.json", canonical_json.dumps(document))

    @property
    def local_only(self) -> bool:
        return self._settings.get("local_only") is not False

    def allow_network(self, allowed: bool) -> None:
        """Leave or enter local-only mode. Remembered by the workspace."""
        self._settings = {**self._settings, "local_only": not allowed}
        self._save_settings(self._settings)

    def require_network(self, purpose: str) -> None:
        """Call before any network use; refused while the workspace is local-only."""
        if self.local_only:
            raise LocalOnlyError(f"{purpose} needs the network, and this workspace is local-only")

    # --- Atomic writes -------------------------------------------------------------------------

    @contextmanager
    def _staging(self) -> Iterator[Path]:
        """A new directory in ``staging/``, locked while in use and removed after, if still there.

        The lock is what tells ``clear_staging`` the directory is in use. It is taken without
        waiting: if it is held, or the directory is gone by the time it is taken, a clearer got
        there first and the directory is abandoned for a new one.
        """
        while True:
            path = Path(tempfile.mkdtemp(dir=self.home / "staging"))
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                continue
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                continue
            if not _same_file(path, descriptor):
                os.close(descriptor)
                continue
            break
        try:
            yield path
        finally:
            try:
                if _same_file(path, descriptor):  # not renamed into place
                    shutil.rmtree(path)
            finally:
                os.close(descriptor)

    def _directory(self, path: Path) -> None:
        """Make ``path``, a directory in one of the home's folders, and flush its name into it.

        A rename into a directory is only durable if the directory's own name is. The folder is
        flushed even when ``path`` already exists: another process may have made it and not yet
        flushed it. The folders themselves are made, and flushed, when the workspace is opened.
        """
        path.mkdir(exist_ok=True)
        fsync_directory(path.parent)

    def _directories(self, path: Path) -> None:
        """``_directory`` for each level from below the home's folder down to ``path``.

        Plans are two levels deep (``plans/<2 hex>/<62 hex>/``); each level's name is flushed.
        """
        folder, *levels = path.relative_to(self.home).parts
        current = self.home / folder
        for level in levels:
            current = current / level
            self._directory(current)

    def _replace(self, path: Path, data: bytes) -> None:
        """Write ``path`` whole or not at all."""
        if path.parent != self.home:  # the home's own entries are flushed with workspace.json
            self._directories(path.parent)
        with self._staging() as staging:
            staged = staging / path.name
            with staged.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            staged.replace(path)
            fsync_directory(path.parent)

    def _remove(self, path: Path) -> bool:
        """Take ``path`` out of the workspace in one rename, then delete it; ``False`` if absent.

        A reader sees the entry whole or not at all: a chunk directory half deleted would still
        look committed. A process killed after the rename leaves staging debris, nothing else.
        """
        with self._staging() as trash:
            try:
                path.rename(trash / "removed")
            except FileNotFoundError:
                return False
            fsync_directory(path.parent)
        return True  # the locked staging directory, and what it held, went on exit

    @contextmanager
    def in_use(self) -> Iterator[None]:
        """Hold the workspace for a job: a shared lock on ``lock``, so ``collect`` waits for it.

        Any number of jobs hold it at once; a job waits while a collection holds it.
        """
        try:
            descriptor = os.open(self.home / LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        except OSError as exc:
            raise WorkspaceError(f"the workspace's lock cannot be opened: {exc}") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    def clear_staging(self) -> int:
        """Remove what interrupted writes left behind; return how many entries were removed.

        An entry still locked by the process writing it, this one or another, is left alone.
        """
        removed = 0
        for entry in sorted((self.home / "staging").iterdir()):
            try:
                mode = entry.lstat().st_mode
            except FileNotFoundError:
                continue  # finished meanwhile
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                entry.unlink()  # nothing Neptune stages; no process holds it
                removed += 1
                continue
            try:
                descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue  # finished meanwhile
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if not _same_file(entry, descriptor):
                    continue  # renamed into place or removed while this waited
                shutil.rmtree(entry) if stat.S_ISDIR(mode) else entry.unlink()
                removed += 1
            except BlockingIOError:
                continue  # in use
            finally:
                os.close(descriptor)
        return removed

    # --- Ledgers -------------------------------------------------------------------------------

    def _ledger_dir(self, root: Path) -> Path:
        return self.home / "ledgers" / hashlib.sha256(_root_key(root)).hexdigest()

    def load_ledger(self, root: Path) -> SourceLedger:
        """The source ledger of ``root``'s earlier scans, or an empty one.

        ``root`` is resolved: a relative path, ``..``, or a symlink to the same directory all
        name one ledger.
        """
        table = self._ledger_dir(root) / "ledger.jsonl"
        if not table.exists():
            return SourceLedger()
        return _ledger(table)

    def save_ledger(self, root: Path, ledger: SourceLedger) -> None:
        directory = self._ledger_dir(root)
        records = (*ledger.artifacts(), *ledger.revisions(), *ledger.absences())
        self._replace(directory / "ledger.jsonl", _lines(records))
        self._replace(directory / "root", _root_key(root))

    # --- Plans ---------------------------------------------------------------------------------

    def _plan_directory(self, source: ContentId) -> Path:
        digest = _source_hex(source)
        return self.home / "plans" / digest[:2] / digest[2:]

    def _plan_path(self, source: ContentId, transform: RecordId) -> Path:
        return self._plan_directory(source) / f"{_transform_hex(transform)}.json"

    def save_plan(
        self,
        transform: TransformRecord,
        chunks: Iterable[ChunkLike],
        findings: Iterable[IngestFinding],
    ) -> None:
        """Keep a source's plan: its chunks, in order, and the findings planning made.

        Planning is deterministic, so a source has one plan under one transform: saving it again
        changes nothing, and saving a different one is refused.
        """
        chunks = tuple(chunks)
        if not chunks:
            raise WorkspaceError("a plan has at least one chunk")
        sources = {chunk.source for chunk in chunks}
        if len(sources) != 1 or {chunk.transform for chunk in chunks} != {transform.id}:
            raise WorkspaceError("a plan's chunks are of one source and its transform")
        document: dict[str, JsonValue] = {
            "chunks": [chunk.to_json() for chunk in chunks],
            "findings": [f.to_json() for f in sorted(findings, key=lambda f: f.id)],
            "transform": transform.to_json(),
        }
        source = ContentId(sources.pop())
        path, data = self._plan_path(source, transform.id), canonical_json.dumps(document)
        if path.exists():
            if path.read_bytes() != data:
                raise WorkspaceError(
                    f"a different plan of {source} under transform {transform.id} is already"
                    " kept; planning must be deterministic"
                )
            return
        self._replace(path, data)

    def load_plan(self, source: ContentId, transform: RecordId) -> StoredPlan | None:
        path = self._plan_path(source, transform)
        if not path.exists():
            return None
        data = canonical_json.loads(path.read_bytes())
        if not isinstance(data, dict) or data.keys() != {"chunks", "findings", "transform"}:
            raise WorkspaceError(f"{path} is not a stored plan")
        record = check_transform_record(transform_record_from_json(data["transform"]))
        if record.id != transform:
            raise WorkspaceError(f"{path} holds a plan under another transform, {record.id}")
        chunks, findings = data["chunks"], data["findings"]
        if not isinstance(chunks, list) or not all(isinstance(c, dict) for c in chunks):
            raise WorkspaceError(f"{path}: chunks must be JSON objects")
        if not isinstance(findings, list):
            raise WorkspaceError(f"{path}: findings must be an array")
        return StoredPlan(
            record,
            tuple(chunks),
            tuple(check_ingest_finding(ingest_finding_from_json(f)) for f in findings),
        )

    def plans(self) -> Iterator[Owner]:
        """Every (source, transform) the workspace holds a plan of, in order.

        A file where format 1 kept a plan is passed over: a job of the previous version may save
        one after the upgrade (ADR 0031 §7). ``collect`` moves it under its source, or removes it.
        """
        for source, directory in _ids(self.home / "plans", "source's plans", passed=_PLAN_1):
            for entry in sorted(directory.iterdir()):
                match = _PLAN.fullmatch(entry.name)
                if match is None or not _is_file(entry):
                    raise WorkspaceError(f"{entry} is not a plan")
                yield ContentId(f"sha256:{source}"), RecordId(f"rec:sha256:{match[1]}")

    def transforms_of(self, source: ContentId) -> tuple[TransformRecord, ...]:
        """Every transform the workspace holds a readable plan of ``source`` under, sorted by id.

        What a cache miss is explained by (ADR 0031 §3): the adapters, versions and configs the
        source was planned with before. Only an explanation hangs on it, so an entry that is not
        a readable plan is passed over, never an error: a damaged old plan must not stop a job
        that is about to plan the source again. ``plans`` and ``collect`` are the strict readers.
        """
        directory = self._plan_directory(source)
        if not directory.is_dir():
            return ()
        found = []
        for entry in sorted(directory.iterdir()):
            match = _PLAN.fullmatch(entry.name)
            if match is None or not _is_file(entry):
                continue
            try:
                stored = self.load_plan(source, RecordId(f"rec:sha256:{match[1]}"))
            except (ValueError, TypeError, KeyError, OSError):
                continue
            if stored is not None:  # None only if collected since it was listed
                found.append(stored.transform)
        return tuple(found)

    # --- Chunks --------------------------------------------------------------------------------

    def chunk_path(self, chunk: str) -> Path:
        digest = _hex(chunk, "chunk")
        return self.home / "chunks" / digest[:2] / digest[2:]

    def committed(self, chunk: str) -> bool:
        return self.chunk_path(chunk).is_dir()

    def commit(
        self,
        chunk: ChunkLike,
        records: Iterable[Any],
        findings: Iterable[IngestFinding],
        series: Iterable[SeriesBatch],
    ) -> bool:
        """Commit one chunk's output, whole or not at all. ``False`` if it was already committed.

        Outputs are deterministic, so a chunk committed twice, by two runs or two processes, is
        the same output: the second commit changes nothing.
        """
        final = self.chunk_path(chunk.id)
        if final.is_dir():
            return False
        with self._staging() as staged:
            (staged / "chunk.json").write_bytes(canonical_json.dumps(chunk.to_json()))
            (staged / "records.jsonl").write_bytes(_lines(records))
            (staged / "findings.jsonl").write_bytes(_lines(findings))
            (staged / "runs").mkdir()
            by_stream: dict[RecordId, list[SeriesBatch]] = defaultdict(list)
            for batch in series:
                by_stream[batch.stream].append(batch)
            for stream, batches in sorted(by_stream.items()):
                write_run(batches, staged / "runs" / f"{_hex(stream, 'rec')}.parquet")
            fsync_tree(staged)
            self._directory(final.parent)  # chunks/<2 hex>/, whose name chunks/ must keep
            try:
                staged.rename(final)
            except OSError:
                if final.is_dir():  # another process committed it first: the same output
                    return False
                raise
            fsync_directory(final.parent)
            return True

    def load(self, chunk: str) -> CommittedChunk:
        """A committed chunk's output."""
        path = self.chunk_path(chunk)
        if not path.is_dir():
            raise WorkspaceError(f"chunk {chunk} is not committed")
        data = canonical_json.loads((path / "chunk.json").read_bytes())
        if not isinstance(data, dict) or data.get("id") != chunk:
            raise WorkspaceError(f"{path} does not hold chunk {chunk}")
        findings = _read_lines((path / "findings.jsonl").read_bytes())
        runs: dict[RecordId, Path] = {}
        for run in sorted((path / "runs").iterdir()):
            match = _RUN.fullmatch(run.name)
            if match is None or run.is_symlink() or not run.is_file():
                raise WorkspaceError(
                    f"{run} is not a stream's run; a chunk's runs/ holds only those"
                )
            runs[RecordId(f"rec:sha256:{match[1]}")] = run
        return CommittedChunk(
            chunk=data,
            records=tuple(_read_lines((path / "records.jsonl").read_bytes())),
            findings=tuple(check_ingest_finding(f) for f in findings),
            runs=runs,
        )

    def chunks(self) -> Iterator[str]:
        """Every committed chunk's id. Anything else in ``chunks/`` is a ``WorkspaceError``."""
        for digest, _ in _ids(self.home / "chunks", "committed chunk"):
            yield f"chunk:sha256:{digest}"

    # --- Derivatives ---------------------------------------------------------------------------

    def _derivative_path(self, derivative: str) -> Path:
        digest = _hex(derivative, "drv")
        return self.home / "derivatives" / digest[:2] / digest[2:]

    def _read_derivative(self, path: Path, identifier: str) -> Derivative:
        """The derivative kept at ``path``, checked whole: its key, and every file it lists.

        Sizes are checked, not hashes: a large file is hashed as it is copied (or ``read``).
        """
        if path.is_symlink() or not path.is_dir():
            raise WorkspaceError(f"{path} is not a derivative's directory")
        try:
            data = canonical_json.loads((path / DERIVATIVE_FILE).read_bytes())
        except (OSError, ValueError) as exc:
            raise WorkspaceError(f"{path} holds no readable {DERIVATIVE_FILE}: {exc}") from exc
        if not isinstance(data, dict) or data.keys() != {"files", "key"}:
            raise WorkspaceError(f"{path}/{DERIVATIVE_FILE} is not {{files, key}}")
        key = derivative_key_from_json(data["key"])
        if key.id != identifier:
            raise WorkspaceError(f"{path} does not hold derivative {identifier}")
        listed = data["files"]
        if not isinstance(listed, dict):
            raise WorkspaceError(f"{path}: files must be an object")
        files: dict[str, tuple[int, ContentId]] = {}
        for name, entry in listed.items():
            if not isinstance(entry, dict) or entry.keys() != {"sha256", "size"}:
                raise WorkspaceError(f"{path}: file {name!r} is not {{sha256, size}}")
            size, digest = entry["size"], entry["sha256"]
            if isinstance(size, bool) or not isinstance(size, int) or not isinstance(digest, str):
                raise WorkspaceError(f"{path}: file {name!r} has no size or hash")
            files[name] = (size, _content(digest))
        present = {entry.name for entry in path.iterdir()} - {DERIVATIVE_FILE}
        if present != set(files):
            raise WorkspaceError(f"{path} holds {sorted(present)}, not {sorted(files)}")
        for name, (size, _) in files.items():
            file = path / name
            if not _is_file(file) or file.stat().st_size != size:
                raise WorkspaceError(f"{file} is not the file that was kept")
        return Derivative(key, path, files)

    def derivative(self, key: DerivativeKey) -> Derivative | None:
        """The derivative kept under ``key``, or ``None``. A damaged one is a ``WorkspaceError``."""
        path = self._derivative_path(key.id)
        if not os.path.lexists(path):
            return None
        return self._read_derivative(path, key.id)

    def materialise(
        self, key: DerivativeKey, build: Callable[[Path], None]
    ) -> tuple[Derivative, Held]:
        """The derivative under ``key``: the kept one, or ``build`` run now and its output kept.

        ``build(directory)`` writes the derivative's files (plain names, no directories) into an
        empty directory. It runs only when nothing whole is kept, so a derivative is computed the
        first time something asks for it and never again while its key holds. One kept but
        damaged (its record unreadable, a file missing or resized) is removed and built again.
        Derivatives are deterministic: if two processes build one, the first kept wins and the
        second is the same.
        """
        final = self._derivative_path(key.id)
        held = Held.BUILT
        if os.path.lexists(final):
            try:
                return self._read_derivative(final, key.id), Held.HELD
            except WorkspaceError:
                self._remove(final)
                held = Held.REBUILT
        with self._staging() as staged:
            build(staged)
            files: dict[str, tuple[int, ContentId]] = {}
            for entry in sorted(staged.iterdir()):
                if entry.name == DERIVATIVE_FILE or not _FILE.fullmatch(entry.name):
                    raise WorkspaceError(f"a derivative cannot hold a file named {entry.name!r}")
                if not _is_file(entry):
                    raise WorkspaceError(f"a derivative holds plain files only: {entry.name!r}")
                with entry.open("rb") as stream:
                    artifact = digest_stream(stream)
                files[entry.name] = (artifact.size, artifact.content_id)
            document: JsonObject = {
                "files": {n: {"sha256": h, "size": s} for n, (s, h) in files.items()},
                "key": key.to_json(),
            }
            (staged / DERIVATIVE_FILE).write_bytes(canonical_json.dumps(document))
            fsync_tree(staged)
            self._directory(final.parent)  # derivatives/<2 hex>/, as for a chunk
            try:
                staged.rename(final)
            except OSError:
                if not final.is_dir():
                    raise
                return self._read_derivative(final, key.id), held  # another process kept it
            fsync_directory(final.parent)
        return Derivative(key, final, files), held

    def discard(self, key: DerivativeKey) -> bool:
        """Remove the derivative under ``key``, found damaged; ``False`` if none was kept."""
        return self._remove(self._derivative_path(key.id))

    def derivatives(self) -> Iterator[str]:
        """Every kept derivative's id. Anything else in ``derivatives/`` is a ``WorkspaceError``."""
        for digest, _ in _ids(self.home / "derivatives", "derivative"):
            yield f"drv:sha256:{digest}"

    # --- Collection ----------------------------------------------------------------------------

    def _held_sources(self) -> set[ContentId]:
        """The content ids at the head of some location in any ledger the workspace keeps.

        A ledger that cannot be read stops collection: ledgers are history, and without one
        there is no telling which sources are still held, so nothing may be judged unreachable.
        """
        held: set[ContentId] = set()
        for directory in sorted((self.home / "ledgers").iterdir()):
            table = directory / "ledger.jsonl"
            if _is_file(table):
                try:
                    ledger = _ledger(table)
                except (ValueError, TypeError, KeyError) as exc:
                    raise WorkspaceError(f"{table} cannot be read: {exc}") from exc
                held.update(h.content_id for h in ledger.heads() if isinstance(h, SourceRevision))
        return held

    def _readable_plan(self, source: ContentId, transform: RecordId) -> StoredPlan | None:
        """The plan, or ``None`` if it is damaged: no job can reuse it, so it is not kept."""
        try:
            return self.load_plan(source, transform)
        except (ValueError, TypeError, KeyError, OSError):
            return None

    def collect(self, transforms: Collection[RecordId]) -> Collected:
        """Remove everything no job with these transforms can reuse (ADR 0031 §6).

        Kept: each plan whose transform is in ``transforms`` and whose source some ledger holds
        at the head of a location; the chunks those plans list; the derivatives whose owners are
        all kept plans. Removed: every other plan, chunk and derivative (superseded adapter
        versions and configs, sources gone from every root, orphans of interrupted work, plans
        and derivatives that cannot be read), and staging debris. A plan where format 1 kept it
        is first moved under its source, or removed if it cannot be read. Ledgers are history
        and are never collected; one that cannot be read stops collection (``WorkspaceError``).
        Refused with ``WorkspaceBusyError`` while a job holds the workspace (``in_use``). Each
        removal is one rename, so a collection killed midway leaves only debris the next one
        clears.
        """
        current = set(transforms)
        descriptor = os.open(self.home / LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceBusyError(f"{self.home} is in use by a job") from exc
            staging = self.clear_staging()
            held = self._held_sources()
            kept: set[Owner] = set()
            kept_chunks: set[str] = set()
            plans = chunks = derivatives = 0
            for entry in self._plans_1():  # format 1's: saved after the upgrade, or unreadable
                target = self._plan_1_target(entry)
                if target is None or target.exists():  # damaged, or a copy of one kept
                    plans += self._remove(entry)
                else:
                    self._move_plan_1(entry, target)  # then judged like any other plan
            for source, transform in list(self.plans()):
                live = source in held and transform in current
                stored = self._readable_plan(source, transform) if live else None
                if stored is not None:
                    kept.add((source, transform))
                    kept_chunks.update(str(chunk.get("id")) for chunk in stored.chunks)
                else:  # unreachable, or damaged (a job would fail on it): planned again if needed
                    plans += self._remove(self._plan_path(source, transform))
            for chunk in list(self.chunks()):
                if chunk not in kept_chunks:
                    chunks += self._remove(self.chunk_path(chunk))
            for identifier in list(self.derivatives()):
                path = self._derivative_path(identifier)
                try:
                    owners: set[Owner] | None = set(
                        self._read_derivative(path, identifier).key.owners
                    )
                except WorkspaceError:
                    owners = None  # damaged: no job can use it
                if owners is None or not owners <= kept:
                    derivatives += self._remove(path)
            for area, levels in (("plans", 2), ("chunks", 1), ("derivatives", 1)):
                _prune(self.home / area, levels)
            return Collected(plans, chunks, derivatives, staging)
        finally:
            os.close(descriptor)


def _ledger(table: Path) -> SourceLedger:
    records = _read_lines(table.read_bytes())
    return SourceLedger(
        artifacts=(r for r in records if isinstance(r, SourceArtifact)),
        revisions=(r for r in records if isinstance(r, SourceRevision)),
        absences=(r for r in records if isinstance(r, SourceAbsence)),
    )


def _content(digest: str) -> ContentId:
    try:
        return parse_content_id(digest)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc


def _prune(area: Path, levels: int) -> None:
    """Remove the index directories collection emptied in the top ``levels`` levels of ``area``.

    Never deeper: a committed chunk's ``runs/`` may be empty and is part of the chunk.
    """
    for depth in range(levels, 0, -1):
        for directory in sorted(area.glob("/".join(["*"] * depth))):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                directory.rmdir()
            except OSError:
                continue  # not empty
