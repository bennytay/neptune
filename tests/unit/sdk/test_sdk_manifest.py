"""Which manifest an SDK call uses, read safely and before anything runs (ADR 0047 §3, §4)."""

from dataclasses import replace
from pathlib import Path

import pytest

from neptune.discovery.source import LocalSource
from neptune.manifest import MANIFEST_ID, read
from neptune.model.source import LocalPath
from neptune.sdk import ConfigurationError, Isolation, JobOptions, Neptune

OPTIONS = JobOptions(isolation=Isolation.IN_PROCESS)


def folder(tmp_path: Path) -> Path:
    root = tmp_path / "rover"
    (root / "drive").mkdir(parents=True)
    (root / "drive" / "notes.txt").write_text("wheel odometry drifted after the ramp\n")
    return root


def client(tmp_path: Path) -> Neptune:
    return Neptune(tmp_path / "ws", options=OPTIONS)


def manifest_findings(tmp_path: Path, root: Path, **kwargs: object) -> list[str]:
    result = client(tmp_path).dry_run(root, **kwargs)  # type: ignore[arg-type]
    return sorted(f.code for f in result.findings if f.code.startswith(MANIFEST_ID))


def test_the_roots_manifest_is_found_and_applied(tmp_path: Path) -> None:
    root = folder(tmp_path)
    (root / "neptune.yaml").write_text("neptune: 1\nsources:\n  - {path: gone, adapter: text}\n")
    assert manifest_findings(tmp_path, root) == [f"{MANIFEST_ID}.rule_unmatched"]
    assert manifest_findings(tmp_path, root, manifest=False) == []


def test_an_explicit_manifest_inside_the_root(tmp_path: Path) -> None:
    root = folder(tmp_path)
    (root / "meta").mkdir()
    (root / "meta" / "v2.json").write_text(
        '{"neptune": 1, "sources": [{"path": "gone", "adapter": "text"}]}'
    )
    found = manifest_findings(tmp_path, root, manifest=root / "meta" / "v2.json")
    assert found == [f"{MANIFEST_ID}.rule_unmatched"]


@pytest.mark.parametrize(
    "arrange",
    ["outside", "two", "symlink", "broken", "directory", "huge", "ignored", "root", "parent"],
)
def test_unusable_manifests_are_configuration_errors(tmp_path: Path, arrange: str) -> None:
    root = folder(tmp_path)
    choice: object = None
    if arrange == "outside":
        (tmp_path / "elsewhere.yaml").write_text("neptune: 1\n")
        choice = tmp_path / "elsewhere.yaml"
    elif arrange == "two":
        (root / "neptune.yaml").write_text("neptune: 1\n")
        (root / "neptune.json").write_text('{"neptune": 1}')
    elif arrange == "symlink":
        (tmp_path / "real.yaml").write_text("neptune: 1\n")
        (root / "neptune.yaml").symlink_to(tmp_path / "real.yaml")
    elif arrange == "broken":
        (root / "neptune.yaml").write_text("neptune: 1\nunknown: key\n")
    elif arrange == "directory":
        (root / "neptune.yaml").mkdir()
    elif arrange == "huge":
        (root / "neptune.yaml").write_bytes(b"neptune: 1\n" + b"#" * 300_000)
    elif arrange == "root":
        choice = root
    elif arrange == "parent":
        choice = tmp_path
    elif arrange == "ignored":
        (root / "neptune.yaml").write_text("neptune: 1\n")
        (root / ".neptune-ignore").write_text("neptune.yaml\n")
    with pytest.raises(ConfigurationError) as error:
        client(tmp_path).dry_run(root, manifest=choice)  # type: ignore[arg-type]
    assert error.value.code == "invalid_configuration"


def test_a_single_file_source_has_no_manifest(tmp_path: Path) -> None:
    root = folder(tmp_path)
    notes = root / "drive" / "notes.txt"
    assert client(tmp_path).dry_run(notes).planned
    with pytest.raises(ConfigurationError):
        client(tmp_path).dry_run(notes, manifest=root / "neptune.yaml")


def test_a_bad_choice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        client(tmp_path).dry_run(folder(tmp_path), manifest=True)  # type: ignore[arg-type]


def test_the_call_decides_over_the_clients_options(tmp_path: Path) -> None:
    root = folder(tmp_path)
    (root / "neptune.yaml").write_text("neptune: 1\nsources:\n  - {path: gone, adapter: text}\n")
    loaded = read(LocalSource(root), LocalPath("neptune.yaml"))
    pinned = Neptune(tmp_path / "ws", options=replace(OPTIONS, manifest=loaded))
    codes = [f.code for f in pinned.dry_run(root).findings if f.code.startswith(MANIFEST_ID)]
    assert codes == [f"{MANIFEST_ID}.rule_unmatched"]
    assert not [
        f for f in pinned.dry_run(root, manifest=False).findings if f.code.startswith(MANIFEST_ID)
    ]
