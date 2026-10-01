"""The local workspace: Neptune's metadata and index store on this machine (ADR 0026).

Everything an ingest needs to remember between runs lives here, never beside the evidence:

    workspace.json                     format version and settings (local-only mode)
    ledgers/<root key>/ledger.jsonl    one ingest root's source ledger
    ledgers/<root key>/root            the root's path, as the host names it
    plans/<2 hex>/<62 hex>.json        one source's plan under one transform, with the transform
    chunks/<2 hex>/<62 hex>/           one committed chunk output, by chunk id:
        chunk.json                     the chunk
        records.jsonl, findings.jsonl  its records and findings, canonical lines sorted by id
        runs/<64 hex>.parquet          its rows of each stream, sorted (``store.series``)
    staging/                           work in progress; anything here is incomplete

A chunk is committed by renaming its finished directory into ``chunks/``: a commit is atomic, so a
process killed midway leaves no partial chunk, and committing a chunk again changes nothing. Its
sources are never copied here: the workspace holds what was derived from them and where they are.

The workspace is local-first. It is local-only by default: anything that would use the network asks
``require_network`` first and is refused until the workspace allows it.
"""

import hashlib
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding
from neptune.identity.provenance import check_transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import IngestFinding, ingest_finding_from_json
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import RECORD_KINDS, record_key
from neptune.model.provenance import TransformRecord, transform_record_from_json
from neptune.model.series import SeriesBatch
from neptune.model.source import SourceAbsence, SourceArtifact, SourceRevision
from neptune.store.series import write_run

FORMAT: Final = 1
WORKSPACE_KIND: Final = "neptune_workspace"
HOME_VARIABLE: Final = "NEPTUNE_HOME"


class WorkspaceError(ValueError):
    """The workspace is not one this version can use, or what it holds is inconsistent."""


class LocalOnlyError(PermissionError):
    """Something asked for the network while the workspace is local-only."""


def default_home() -> Path:
    """``$NEPTUNE_HOME``, else ``$XDG_CACHE_HOME/neptune``, else ``~/.cache/neptune``."""
    if home := os.environ.get(HOME_VARIABLE):
        return Path(home)
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "neptune"


def _hex(identifier: str) -> str:
    """The 64 hex digits of a ``…sha256:<hex>`` id."""
    digest = identifier.rsplit(":", 1)[-1]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise WorkspaceError(f"not a sha256 id: {identifier!r}")
    return digest


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durably(path: Path, data: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


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


class Workspace:
    """A workspace directory, created on first use. Several processes may share one."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home is not None else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        settings = self.home / "workspace.json"
        if settings.exists():
            self._check(settings)
        for directory in ("ledgers", "plans", "chunks", "staging"):
            (self.home / directory).mkdir(exist_ok=True)
        if not settings.exists():
            self._save_settings({"local_only": True})
        self._settings = self._check(settings)

    def _check(self, settings: Path) -> dict[str, JsonValue]:
        try:
            data = canonical_json.loads(settings.read_bytes())
        except ValueError as exc:
            raise WorkspaceError(f"{self.home} is not a Neptune workspace: {exc}") from exc
        if not isinstance(data, dict) or data.get("kind") != WORKSPACE_KIND:
            raise WorkspaceError(f"{self.home} is not a Neptune workspace")
        if data.get("format") != FORMAT:
            raise WorkspaceError(
                f"workspace format {data.get('format')!r}; this version reads format {FORMAT}"
            )
        return data

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
        self._save_settings({"local_only": not allowed})

    def require_network(self, purpose: str) -> None:
        """Call before any network use; refused while the workspace is local-only."""
        if self.local_only:
            raise LocalOnlyError(f"{purpose} needs the network, and this workspace is local-only")

    # --- Atomic writes -------------------------------------------------------------------------

    def _stage(self) -> Path:
        return Path(tempfile.mkdtemp(dir=self.home / "staging"))

    def _replace(self, path: Path, data: bytes) -> None:
        """Write ``path`` whole or not at all."""
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(dir=self.home / "staging")) / path.name
        _write_durably(staged, data)
        staged.replace(path)
        _fsync_directory(path.parent)
        staged.parent.rmdir()

    def clear_staging(self) -> int:
        """Remove what interrupted writes left behind; return how many entries were removed."""
        removed = 0
        for entry in (self.home / "staging").iterdir():
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            removed += 1
        return removed

    # --- Ledgers -------------------------------------------------------------------------------

    def _ledger_dir(self, root: Path) -> Path:
        path = os.fsencode(Path(root).absolute())
        return self.home / "ledgers" / hashlib.sha256(path).hexdigest()

    def load_ledger(self, root: Path) -> SourceLedger:
        """The source ledger of ``root``'s earlier scans, or an empty one."""
        table = self._ledger_dir(root) / "ledger.jsonl"
        if not table.exists():
            return SourceLedger()
        records = _read_lines(table.read_bytes())
        return SourceLedger(
            artifacts=(r for r in records if isinstance(r, SourceArtifact)),
            revisions=(r for r in records if isinstance(r, SourceRevision)),
            absences=(r for r in records if isinstance(r, SourceAbsence)),
        )

    def save_ledger(self, root: Path, ledger: SourceLedger) -> None:
        directory = self._ledger_dir(root)
        records = (*ledger.artifacts(), *ledger.revisions(), *ledger.absences())
        self._replace(directory / "ledger.jsonl", _lines(records))
        self._replace(directory / "root", os.fsencode(Path(root).absolute()))

    # --- Plans ---------------------------------------------------------------------------------

    def _plan_path(self, source: ContentId, transform: RecordId) -> Path:
        key = hashlib.sha256(canonical_json.dumps([source, transform])).hexdigest()
        return self.home / "plans" / key[:2] / f"{key[2:]}.json"

    def save_plan(
        self,
        transform: TransformRecord,
        chunks: Iterable[ChunkLike],
        findings: Iterable[IngestFinding],
    ) -> None:
        """Keep a source's plan: its chunks, in order, and the findings planning made."""
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
        path = self._plan_path(ContentId(sources.pop()), transform.id)
        self._replace(path, canonical_json.dumps(document))

    def load_plan(self, source: ContentId, transform: RecordId) -> StoredPlan | None:
        path = self._plan_path(source, transform)
        if not path.exists():
            return None
        data = canonical_json.loads(path.read_bytes())
        if not isinstance(data, dict) or data.keys() != {"chunks", "findings", "transform"}:
            raise WorkspaceError(f"{path} is not a stored plan")
        record = check_transform_record(transform_record_from_json(data["transform"]))
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

    # --- Chunks --------------------------------------------------------------------------------

    def chunk_path(self, chunk: str) -> Path:
        digest = _hex(chunk)
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
        staged = self._stage()
        try:
            _write_durably(staged / "chunk.json", canonical_json.dumps(chunk.to_json()))
            _write_durably(staged / "records.jsonl", _lines(records))
            _write_durably(staged / "findings.jsonl", _lines(findings))
            (staged / "runs").mkdir()
            by_stream: dict[RecordId, list[SeriesBatch]] = defaultdict(list)
            for batch in series:
                by_stream[batch.stream].append(batch)
            for stream, batches in sorted(by_stream.items()):
                write_run(batches, staged / "runs" / f"{_hex(stream)}.parquet")
            for path in (*(staged / "runs").iterdir(), staged / "runs", staged):
                if path.is_file():
                    with path.open("rb") as written:
                        os.fsync(written.fileno())
                else:
                    _fsync_directory(path)
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                staged.rename(final)
            except OSError:
                if final.is_dir():  # another process committed it first: the same output
                    return False
                raise
            _fsync_directory(final.parent)
            return True
        finally:
            if staged.exists():
                shutil.rmtree(staged)

    def load(self, chunk: str) -> CommittedChunk:
        """A committed chunk's output."""
        path = self.chunk_path(chunk)
        if not path.is_dir():
            raise WorkspaceError(f"chunk {chunk} is not committed")
        data = canonical_json.loads((path / "chunk.json").read_bytes())
        if not isinstance(data, dict) or data.get("id") != chunk:
            raise WorkspaceError(f"{path} does not hold chunk {chunk}")
        findings = _read_lines((path / "findings.jsonl").read_bytes())
        runs = {
            RecordId(f"rec:sha256:{run.stem}"): run for run in sorted((path / "runs").iterdir())
        }
        return CommittedChunk(
            chunk=data,
            records=tuple(_read_lines((path / "records.jsonl").read_bytes())),
            findings=tuple(check_ingest_finding(f) for f in findings),
            runs=runs,
        )

    def chunks(self) -> Iterator[str]:
        """Every committed chunk's id."""
        for prefix in sorted((self.home / "chunks").iterdir()):
            for entry in sorted(prefix.iterdir()):
                yield f"chunk:sha256:{prefix.name}{entry.name}"
