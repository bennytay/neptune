"""The workspace as the cache (ADR 0031): plans by source, derivatives, collection, upgrade."""

import hashlib
import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Final

import pytest

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.text import TextAdapter
from neptune.discovery.reader import BytesReader
from neptune.discovery.scan import scan
from neptune.discovery.source import LocalSource
from neptune.identity import canonical_json
from neptune.model.ids import ContentId, RecordId
from neptune.store.workspace import (
    DERIVATIVE_FILE,
    DerivativeKey,
    Held,
    Workspace,
    WorkspaceBusyError,
    WorkspaceError,
    derivative_key_from_json,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "adapters"
TEXT: Final = b"First paragraph.\n\nSecond paragraph,\non two lines.\n"


def _tally() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tally_adapter", FIXTURES / "tally_adapter.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TALLY: Final = _tally()


def text_output(data: bytes = TEXT, rule: str = "paragraph") -> SourceOutput:
    return ingest_source(TextAdapter(), BytesReader(data), {"block_rule": rule})


def keep(workspace: Workspace, output: SourceOutput) -> None:
    """Save the plan and commit every chunk, as a job would."""
    workspace.save_plan(output.config.transform, output.plan.chunks, output.plan.findings)
    for chunk, out in zip(output.plan.chunks, output.outputs, strict=True):
        workspace.commit(chunk, out.records, out.findings, out.series)


def owner(output: SourceOutput) -> tuple[ContentId, RecordId]:
    return output.plan.chunks[0].source, output.config.transform.id


def key(output: SourceOutput, recipe: str = "test.recipe/1", **inputs: str) -> DerivativeKey:
    chunks: list[JsonValue] = [chunk.id for chunk in output.plan.chunks]
    return DerivativeKey(recipe, {"chunks": chunks, **inputs}, (owner(output),))


def writes(files: dict[str, bytes], calls: list[int] | None = None) -> Callable[[Path], None]:
    def build(directory: Path) -> None:
        if calls is not None:
            calls.append(1)
        for name, data in files.items():
            (directory / name).write_bytes(data)

    return build


# --- Plans by source ---------------------------------------------------------------------------


def test_plans_are_kept_by_source_then_transform(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    paragraphs, lines = text_output(), text_output(rule="line")
    source = paragraphs.plan.chunks[0].source
    assert workspace.transforms_of(source) == ()
    keep(workspace, paragraphs)
    keep(workspace, lines)
    digest = source.removeprefix("sha256:")
    directory = tmp_path / "plans" / digest[:2] / digest[2:]
    assert sorted(p.name for p in directory.iterdir()) == sorted(
        f"{o.config.transform.id.removeprefix('rec:sha256:')}.json" for o in (paragraphs, lines)
    )
    kept = workspace.transforms_of(source)
    assert [t.id for t in kept] == sorted(o.config.transform.id for o in (paragraphs, lines))
    assert sorted(workspace.plans()) == sorted([owner(paragraphs), owner(lines)])
    assert workspace.transforms_of(text_output(b"other\n").plan.chunks[0].source) == ()


def test_a_plan_filed_under_another_transform_is_refused(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    paragraphs, lines = text_output(), text_output(rule="line")
    keep(workspace, paragraphs)
    source = paragraphs.plan.chunks[0].source
    digest = source.removeprefix("sha256:")
    filed = tmp_path / "plans" / digest[:2] / digest[2:]
    misplaced = filed / f"{lines.config.transform.id.removeprefix('rec:sha256:')}.json"
    (filed / f"{paragraphs.config.transform.id.removeprefix('rec:sha256:')}.json").rename(misplaced)
    with pytest.raises(WorkspaceError, match="another transform"):
        workspace.load_plan(source, lines.config.transform.id)
    (filed / "stray.txt").write_bytes(b"")
    with pytest.raises(WorkspaceError, match="is not a plan"):
        list(workspace.plans())
    # only a miss's explanation reads these: what is not a readable plan is passed over
    assert workspace.transforms_of(source) == ()
    with pytest.raises(WorkspaceError, match="different plan"):  # the misplaced one is in the way
        keep(workspace, lines)


def test_a_malformed_id_is_a_workspace_error(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="content id"):
        workspace.transforms_of(ContentId("sha256:nothex"))
    with pytest.raises(WorkspaceError, match="transform"):
        workspace.load_plan(ContentId("sha256:" + "0" * 64), RecordId("rec:md5:00"))


# --- Upgrading a format-1 workspace ------------------------------------------------------------


def format_1(home: Path, output: SourceOutput, *, local_only: bool = False) -> Path:
    """A workspace as format 1 wrote it: plans/<2 hex>/<62 hex>.json, by (source, transform)."""
    home.mkdir()
    settings: JsonObject = {"format": 1, "kind": "neptune_workspace", "local_only": local_only}
    (home / "workspace.json").write_bytes(canonical_json.dumps(settings))
    for directory in ("ledgers", "plans", "chunks", "staging"):
        (home / directory).mkdir()
    source, transform = owner(output)
    digest = hashlib.sha256(canonical_json.dumps([source, transform])).hexdigest()
    document: JsonObject = {
        "chunks": [chunk.to_json() for chunk in output.plan.chunks],
        "findings": [f.to_json() for f in output.plan.findings],
        "transform": output.config.transform.to_json(),
    }
    path = home / "plans" / digest[:2] / f"{digest[2:]}.json"
    path.parent.mkdir()
    path.write_bytes(canonical_json.dumps(document))
    return path


def test_a_format_1_workspace_is_upgraded_in_place(tmp_path: Path) -> None:
    output = text_output()
    old = format_1(tmp_path / "home", output)
    workspace = Workspace(tmp_path / "home")
    settings = canonical_json.loads((tmp_path / "home" / "workspace.json").read_bytes())
    assert settings == {"format": 2, "kind": "neptune_workspace", "local_only": False}
    assert not old.exists()
    stored = workspace.load_plan(*owner(output))
    assert stored is not None
    assert stored.chunks == tuple(chunk.to_json() for chunk in output.plan.chunks)
    assert list(workspace.plans()) == [owner(output)]
    assert (tmp_path / "home" / "derivatives").is_dir()
    assert workspace.local_only is False  # the setting was kept, not reset


def test_an_upgrade_killed_midway_finishes_on_the_next_open(tmp_path: Path) -> None:
    first, second = text_output(), text_output(b"another file\n")
    format_1(tmp_path / "home", first)
    format_1(tmp_path / "other", second)  # a second plan, moved before the "kill"
    Workspace(tmp_path / "other")
    moved = next((tmp_path / "other" / "plans").rglob("*.json"))
    target = tmp_path / "home" / moved.relative_to(tmp_path / "other")
    target.parent.mkdir(parents=True)
    moved.rename(target)
    workspace = Workspace(tmp_path / "home")
    assert sorted(workspace.plans()) == sorted([owner(first), owner(second)])


def test_a_plan_another_opener_moved_first_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes opening one format-1 workspace: the second finds the plan already moved."""
    first, second = text_output(), text_output(b"another file\n")
    gone = format_1(tmp_path / "home", first)
    format_1(tmp_path / "other", second)
    moved = next((tmp_path / "other" / "plans").rglob("*.json"))
    (tmp_path / "home" / "plans" / moved.parent.name).mkdir(exist_ok=True)
    moved.rename(tmp_path / "home" / "plans" / moved.parent.name / moved.name)
    read_bytes = Path.read_bytes

    def raced(path: Path) -> bytes:
        if path == gone:  # listed, then moved by the other process before this one read it
            gone.unlink()
            raise FileNotFoundError(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", raced)
    workspace = Workspace(tmp_path / "home")
    monkeypatch.undo()
    assert list(workspace.plans()) == [owner(second)]


def format_1_plan(home: Path, output: SourceOutput) -> Path:
    """Save ``output``'s plan where format 1 kept it, in a workspace that exists; its path."""
    source, transform = owner(output)
    digest = hashlib.sha256(canonical_json.dumps([source, transform])).hexdigest()
    path = home / "plans" / digest[:2] / f"{digest[2:]}.json"
    path.parent.mkdir(exist_ok=True)
    document: JsonObject = {
        "chunks": [chunk.to_json() for chunk in output.plan.chunks],
        "findings": [f.to_json() for f in output.plan.findings],
        "transform": output.config.transform.to_json(),
    }
    path.write_bytes(canonical_json.dumps(document))
    return path


@pytest.mark.parametrize(
    "damaged", [b'{"chunks":[],"transform":{}}', b"{not json", b'{"chunks":[{}],"transform":{}}']
)
def test_a_format_1_plan_that_cannot_be_read_never_stops_the_workspace(
    tmp_path: Path, damaged: bytes
) -> None:
    """A damaged old plan never stops a job: the upgrade leaves it, and collection removes it."""
    home = tmp_path / "home"
    path = format_1(home, text_output())
    readable = text_output(b"another file\n")
    format_1_plan(home, readable)
    path.write_bytes(damaged)
    workspace = Workspace(home)
    settings = canonical_json.loads((home / "workspace.json").read_bytes())
    assert isinstance(settings, dict) and settings["format"] == 2
    assert list(workspace.plans()) == [owner(readable)]  # the readable one was moved
    assert path.read_bytes() == damaged  # left where it was, and passed over
    Workspace(home)  # every later open succeeds too
    collected = workspace.collect({readable.config.transform.id})
    assert collected.plans == 2  # the damaged one, and the readable one no ledger holds
    assert not path.exists()
    assert list(workspace.plans()) == []


def test_a_format_1_plan_saved_after_the_upgrade_is_adopted_by_collection(
    scanned: tuple[Workspace, Path],
) -> None:
    """A job of the previous version, still running, saves a plan where format 1 kept plans."""
    workspace, _ = scanned
    output = text_output()  # notes.txt's bytes: the ledger holds them
    for chunk, out in zip(output.plan.chunks, output.outputs, strict=True):
        workspace.commit(chunk, out.records, out.findings, out.series)
    stray = format_1_plan(workspace.home, output)
    assert list(workspace.plans()) == []  # passed over, never an error
    collected = workspace.collect({output.config.transform.id})
    assert (collected.plans, collected.chunks) == (0, 0)  # moved under its source, and kept
    assert not stray.exists()
    assert list(workspace.plans()) == [owner(output)]
    stored = workspace.load_plan(*owner(output))
    assert stored is not None
    assert stored.chunks == tuple(chunk.to_json() for chunk in output.plan.chunks)
    # a stray copy of a plan format 2 already keeps is removed; the kept one is untouched
    format_1_plan(workspace.home, output)
    assert workspace.collect({output.config.transform.id}).plans == 1
    assert not stray.exists()
    assert list(workspace.plans()) == [owner(output)]


# --- Derivative keys ---------------------------------------------------------------------------


def test_a_derivative_key_names_everything_its_derivative_reads() -> None:
    output = text_output()
    base = key(output, settings="a")
    assert base.id == key(output, settings="a").id
    assert base.id.startswith("drv:sha256:") and len(base.id) == len("drv:sha256:") + 64
    more_owners = tuple(sorted((*base.owners, owner(text_output(b"x\n")))))
    others = [
        key(output, "test.recipe/2", settings="a"),  # the recipe's version
        key(output, "other.recipe/1", settings="a"),  # the recipe
        key(output, settings="b"),  # an input
        key(text_output(rule="line"), settings="a"),  # other chunks
        DerivativeKey(base.recipe, base.inputs, more_owners),  # the owners
    ]
    assert len({base.id, *(other.id for other in others)}) == 1 + len(others)
    stored = canonical_json.loads(canonical_json.dumps(base.to_json()))
    assert derivative_key_from_json(stored) == base


@pytest.mark.parametrize(
    ("recipe", "inputs", "owners", "message"),
    [
        ("no-version", {}, None, "recipe"),
        ("Upper/1", {}, None, "recipe"),
        ("name/0", {}, None, "recipe"),
        ("name/1", {"x": 1.5e400}, None, "canonical"),
        ("name/1", {"x": None}, None, "canonical"),
        ("name/1", {}, (), "at least one"),
        ("name/1", {}, (("sha256:x", "rec:sha256:" + "0" * 64),), "content id"),
        ("name/1", {}, (("sha256:" + "0" * 64, "chunk:sha256:" + "0" * 64),), "transform"),
    ],
)
def test_a_malformed_derivative_key_is_refused(
    recipe: str, inputs: dict[str, object], owners: tuple[tuple[str, str], ...] | None, message: str
) -> None:
    pair = (ContentId("sha256:" + "1" * 64), RecordId("rec:sha256:" + "2" * 64))
    with pytest.raises(WorkspaceError, match=message):
        DerivativeKey(recipe, inputs, owners if owners is not None else (pair,))  # type: ignore[arg-type]


def test_owners_are_sorted_and_unique() -> None:
    a = (ContentId("sha256:" + "1" * 64), RecordId("rec:sha256:" + "2" * 64))
    b = (ContentId("sha256:" + "3" * 64), RecordId("rec:sha256:" + "2" * 64))
    with pytest.raises(WorkspaceError, match="sorted"):
        DerivativeKey("name/1", {}, (b, a))
    with pytest.raises(WorkspaceError, match="sorted"):
        DerivativeKey("name/1", {}, (a, a))


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"inputs": {}, "owners": [], "recipe": "name/1"},
        {"inputs": {}, "owners": [], "recipe": "name/1", "scheme": "neptune.derivative-id/0"},
        {"inputs": [], "owners": [], "recipe": "name/1", "scheme": "neptune.derivative-id/1"},
        {"inputs": {}, "owners": [["a"]], "recipe": "name/1", "scheme": "neptune.derivative-id/1"},
    ],
)
def test_a_malformed_derivative_key_document_is_refused(data: object) -> None:
    with pytest.raises(WorkspaceError):
        derivative_key_from_json(data)  # type: ignore[arg-type]


# --- Materialising derivatives -----------------------------------------------------------------


def test_a_derivative_is_built_once_and_then_reused(tmp_path: Path) -> None:
    workspace, calls = Workspace(tmp_path), list[int]()
    derivative_key = key(text_output())
    assert workspace.derivative(derivative_key) is None
    built, held = workspace.materialise(derivative_key, writes({"out.bin": b"payload"}, calls))
    assert held is Held.BUILT and calls == [1]
    again, held = workspace.materialise(derivative_key, writes({"out.bin": b"other"}, calls))
    assert held is Held.HELD and calls == [1]
    assert again == built == workspace.derivative(derivative_key)
    assert again.read("out.bin") == b"payload"
    size, digest = again.files["out.bin"]
    assert size == 7 and digest.startswith("sha256:")
    assert list(workspace.derivatives()) == [derivative_key.id]
    assert not any((tmp_path / "staging").iterdir())


@pytest.mark.parametrize(
    "damage",
    [
        lambda path: (path / "out.bin").unlink(),
        lambda path: (path / "out.bin").write_bytes(b"short"),
        lambda path: (path / "extra.bin").write_bytes(b""),
        lambda path: (path / DERIVATIVE_FILE).write_bytes(b"not json"),
        lambda path: (path / DERIVATIVE_FILE).write_bytes(b'{"files":{},"key":{}}'),
    ],
)
def test_a_damaged_derivative_is_rebuilt(tmp_path: Path, damage: Callable[[Path], None]) -> None:
    workspace, calls = Workspace(tmp_path), list[int]()
    derivative_key = key(text_output())
    built, _ = workspace.materialise(derivative_key, writes({"out.bin": b"payload"}, calls))
    damage(built.path)
    with pytest.raises(WorkspaceError):
        workspace.derivative(derivative_key)
    rebuilt, held = workspace.materialise(derivative_key, writes({"out.bin": b"payload"}, calls))
    assert held is Held.REBUILT and calls == [1, 1]
    assert rebuilt.read("out.bin") == b"payload"
    assert workspace.materialise(derivative_key, writes({}))[1] is Held.HELD


def test_a_derivative_kept_under_another_key_is_damaged(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    first, second = key(text_output(), settings="a"), key(text_output(), settings="b")
    kept, _ = workspace.materialise(first, writes({"out.bin": b"a"}))
    target = tmp_path / "derivatives" / second.id[11:13] / second.id[13:]
    target.parent.mkdir(parents=True, exist_ok=True)
    kept.path.rename(target)
    with pytest.raises(WorkspaceError, match="does not hold"):
        workspace.derivative(second)
    assert workspace.materialise(second, writes({"out.bin": b"b"}))[1] is Held.REBUILT


def test_content_changed_in_place_is_caught_on_read(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    kept, _ = workspace.materialise(key(text_output()), writes({"out.bin": b"payload"}))
    (kept.path / "out.bin").write_bytes(b"PAYLOAD")  # same size: only the hash tells
    assert workspace.derivative(kept.key) is not None
    with pytest.raises(WorkspaceError, match="not what was kept"):
        kept.read("out.bin")
    with pytest.raises(WorkspaceError, match="no file"):
        kept.read("missing.bin")
    assert workspace.discard(kept.key) is True
    assert workspace.discard(kept.key) is False
    assert workspace.derivative(kept.key) is None


@pytest.mark.parametrize(
    "build",
    [
        lambda d: (d / "nested").mkdir(),
        lambda d: (d / DERIVATIVE_FILE).write_bytes(b"{}"),
        lambda d: (d / ".hidden").write_bytes(b""),
        lambda d: (d / "link").symlink_to("/etc/passwd"),
    ],
)
def test_a_build_that_writes_anything_but_plain_files_keeps_nothing(
    tmp_path: Path, build: Callable[[Path], None]
) -> None:
    workspace = Workspace(tmp_path)
    derivative_key = key(text_output())
    with pytest.raises(WorkspaceError):
        workspace.materialise(derivative_key, build)
    assert workspace.derivative(derivative_key) is None
    assert not any((tmp_path / "staging").iterdir())


def test_a_build_that_raises_keeps_nothing(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    def broken(directory: Path) -> None:
        (directory / "half.bin").write_bytes(b"half")
        raise RuntimeError("the merge failed")

    with pytest.raises(RuntimeError):
        workspace.materialise(key(text_output()), broken)
    assert list(workspace.derivatives()) == []
    assert not any((tmp_path / "staging").iterdir())


def test_an_empty_derivative_is_still_a_derivative(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    kept, held = workspace.materialise(key(text_output()), writes({}))
    assert held is Held.BUILT and dict(kept.files) == {}
    assert workspace.materialise(kept.key, writes({}))[1] is Held.HELD


def test_anything_else_in_derivatives_is_named(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "derivatives" / "zz").mkdir()
    with pytest.raises(WorkspaceError, match="prefix directory"):
        list(workspace.derivatives())


# --- Collection --------------------------------------------------------------------------------


@pytest.fixture
def scanned(tmp_path: Path) -> tuple[Workspace, Path]:
    """A workspace whose ledger holds one root with notes.txt holding TEXT."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.txt").write_bytes(TEXT)
    workspace = Workspace(tmp_path / "home")
    ledger = workspace.load_ledger(root)
    scan(LocalSource(root), ledger)
    workspace.save_ledger(root, ledger)
    return workspace, root


def test_collection_keeps_only_what_the_current_transforms_can_reuse(
    scanned: tuple[Workspace, Path],
) -> None:
    workspace, _ = scanned
    current, superseded = text_output(), text_output(rule="line")
    gone = text_output(b"bytes no ledger holds\n")
    for output in (current, superseded, gone):
        keep(workspace, output)
    orphan = text_output(b"orphan\n")
    first = orphan.plan.chunks[0]
    workspace.commit(first, orphan.outputs[0].records, orphan.outputs[0].findings, ())
    live, _ = workspace.materialise(key(current), writes({"a.bin": b"a"}))
    dead, _ = workspace.materialise(key(superseded), writes({"b.bin": b"b"}))
    (workspace.home / "staging" / "debris").mkdir()

    collected = workspace.collect({current.config.transform.id, gone.config.transform.id})

    # gone's transform is current but no ledger holds its bytes; superseded's is not current
    assert (collected.plans, collected.derivatives, collected.staging) == (2, 1, 1)
    assert collected.chunks == len(superseded.plan.chunks) + len(gone.plan.chunks) + 1
    assert list(workspace.plans()) == [owner(current)]
    assert set(workspace.chunks()) == {chunk.id for chunk in current.plan.chunks}
    assert list(workspace.derivatives()) == [live.key.id]
    assert workspace.derivative(dead.key) is None
    for chunk in current.plan.chunks:  # a kept chunk reads back whole, empty runs/ and all
        assert workspace.load(chunk.id).chunk["id"] == chunk.id
    assert not any((workspace.home / "staging").iterdir())
    for area in ("plans", "chunks", "derivatives"):  # no empty index directory is left
        for directory in (workspace.home / area).rglob("*"):
            if directory.is_dir() and directory.parent.parent == workspace.home / area:
                assert any(directory.iterdir())
    again = workspace.collect({current.config.transform.id})
    assert (again.plans, again.chunks, again.derivatives, again.staging) == (0, 0, 0, 0)


def test_a_damaged_plan_is_collected_and_a_damaged_ledger_stops_collection(
    scanned: tuple[Workspace, Path],
) -> None:
    workspace, _ = scanned
    output = text_output()
    keep(workspace, output)
    (plan,) = (workspace.home / "plans").rglob("*.json")
    plan.write_bytes(b"{not json")
    collected = workspace.collect({output.config.transform.id})
    assert (collected.plans, collected.chunks) == (1, len(output.plan.chunks))
    (ledger,) = (workspace.home / "ledgers").rglob("ledger.jsonl")
    ledger.write_bytes(b"{not json")
    with pytest.raises(WorkspaceError, match="cannot be read"):
        workspace.collect({output.config.transform.id})
    assert ledger.read_bytes() == b"{not json"  # history is never collected


def test_a_damaged_derivative_is_collected(scanned: tuple[Workspace, Path]) -> None:
    workspace, _ = scanned
    output = text_output()
    keep(workspace, output)
    kept, _ = workspace.materialise(key(output), writes({"a.bin": b"a"}))
    (kept.path / DERIVATIVE_FILE).write_bytes(b"garbage")
    assert workspace.collect({output.config.transform.id}).derivatives == 1


def test_a_source_gone_from_its_root_is_collected_once_the_root_is_scanned_again(
    scanned: tuple[Workspace, Path],
) -> None:
    workspace, root = scanned
    output = text_output()
    keep(workspace, output)
    assert workspace.collect({output.config.transform.id}).plans == 0
    (root / "notes.txt").unlink()
    ledger = workspace.load_ledger(root)
    scan(LocalSource(root), ledger)
    workspace.save_ledger(root, ledger)
    assert workspace.collect({output.config.transform.id}).plans == 1


def test_collection_is_refused_while_a_job_holds_the_workspace(
    scanned: tuple[Workspace, Path],
) -> None:
    workspace, _ = scanned
    output = text_output()
    keep(workspace, output)
    with workspace.in_use(), Workspace(workspace.home).in_use():  # jobs share it
        with pytest.raises(WorkspaceBusyError, match="in use"):
            workspace.collect(set())
        assert list(workspace.plans()) == [owner(output)]
    assert workspace.collect(set()).plans == 1


def test_ledgers_are_never_collected(scanned: tuple[Workspace, Path]) -> None:
    workspace, root = scanned
    before = workspace.load_ledger(root)
    workspace.collect(set())
    after = workspace.load_ledger(root)
    assert after.revisions() == before.revisions() and after.artifacts() == before.artifacts()
