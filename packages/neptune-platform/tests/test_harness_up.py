"""``make up``: a folder with no hand-written setup, through every stage, to Claude Code."""

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Final

import pytest
from harness import acceptance
from harness import up as U
from harness.stages import _shipped_presets, read_deploy

REPO: Final = Path(__file__).resolve().parents[3]


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The acceptance corpus's S-007 site alone: CSV exports, MCAPs, a PDF, URDFs, configs, and
    no manifest, deploy declaration, Memory declaration or assertion file."""
    corpus = acceptance.materialise(tmp_path_factory.mktemp("corpus") / "c")
    folder = tmp_path_factory.mktemp("site") / "S-007"
    shutil.copytree(corpus / "sites" / "S-007", folder)
    assert not any(folder.glob("neptune.*")) and not any(folder.rglob("*.assertions.json"))
    return folder


def test_the_default_declaration_names_every_shipped_preset_and_template(tmp_path: Path) -> None:
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(U.default_deploy()), encoding="utf-8")
    plan, problems = read_deploy(path)
    assert problems == [] and plan is not None
    assert set(plan.presets) == set(_shipped_presets()[0])
    assert plan.templates and plan.at_least == {} and not plan.require_assertion_scopes


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (None, {"tabular": {"csv_header": "first_row"}}),
        (
            'neptune: 1\nadapters:\n  tabular: {options: {csv_delimiter: ","}}\n',
            {"tabular": {"csv_header": "first_row"}},
        ),
        ("neptune: 1\nadapters:\n  tabular: {options: {csv_header: none}}\n", None),
        ("neptune: [unreadable\n", None),
    ],
)
def test_csv_header_is_said_only_where_the_folders_manifest_does_not(
    tmp_path: Path, manifest: str | None, expected: object
) -> None:
    if manifest is not None:
        (tmp_path / "neptune.yaml").write_text(manifest, encoding="utf-8")
    assert U.adapter_config(tmp_path, "first_row") == expected


def test_the_output_may_not_sit_inside_the_folder_nor_the_folder_inside_it(tmp_path: Path) -> None:
    folder = tmp_path / "logs"
    folder.mkdir()
    assert U.main([str(folder), "--out", str(folder / "out")]) == 2
    assert U.main([str(folder), "--out", str(tmp_path)]) == 2
    (tmp_path / "link").symlink_to(folder)
    assert U._refuse(tmp_path / "link", tmp_path / "o") is not None
    assert not any(folder.iterdir())


def test_an_output_folder_this_tool_did_not_write_is_refused_and_untouched(tmp_path: Path) -> None:
    folder, out = tmp_path / "logs", tmp_path / "home"
    folder.mkdir()
    (out / "work").mkdir(parents=True)
    (out / "work" / "thesis.tex").write_text("mine", encoding="utf-8")
    assert U.main([str(folder), "--out", str(out)]) == 2
    assert (out / "work" / "thesis.tex").read_text(encoding="utf-8") == "mine"
    (out / "up.json").write_text('{"ok": true}', encoding="utf-8")  # not this tool's shape
    assert U.main([str(folder), "--out", str(out)]) == 2


def test_a_folder_with_no_setup_reaches_memory_with_runs_machines_and_events(
    site: Path, tmp_path: Path
) -> None:
    before = _tree(site)
    out = tmp_path / "out"
    assert U.up(site, out, stream=None) == 0
    assert _tree(site) == before  # the folder is only read
    document = json.loads((out / "up.json").read_text(encoding="utf-8"))
    assert [s["status"] for s in document["stages"]] == ["ok"] * 5
    held = document["memory"]["holds"]
    assert held["run"] >= 4 and held["machine"] >= 4 and held["event"] >= 20
    deploy = next(s for s in document["stages"] if s["stage"] == "deploy")["output"]["cases"][0]
    assert deploy["records"]["maintenance_event"] == 6  # work_orders.csv, by cmms_generic
    assert deploy["records"]["change_record"] == 3  # servicenow_changes.csv
    for name in ("graph.json", "sources.json", "up.md", "claude/.mcp.json"):
        assert (out / name).is_file(), name
    config = json.loads((out / "claude" / ".mcp.json").read_text(encoding="utf-8"))
    args = config["mcpServers"]["neptune"]["args"]
    assert args[8] == str((out / "graph.json").resolve())

    assert U._refuse(site, out) is None  # an earlier run's output may be rewritten
    document = json.loads((out / "up.json").read_text(encoding="utf-8"))
    del document["isolation"]  # as the first version wrote it
    (out / "up.json").write_text(json.dumps(document), encoding="utf-8")
    assert U._refuse(site, out) is None
    again = tmp_path / "again"
    assert U.up(site, again, stream=None) == 0
    assert (again / "graph.json").read_bytes() == (out / "graph.json").read_bytes()
    assert (again / "sources.json").read_bytes() == (out / "sources.json").read_bytes()


def test_without_the_header_declaration_no_table_maps(site: Path, tmp_path: Path) -> None:
    assert U.up(site, tmp_path / "out", csv_header="none", stream=None) == 0
    document = json.loads((tmp_path / "out" / "up.json").read_text(encoding="utf-8"))
    deploy = next(s for s in document["stages"] if s["stage"] == "deploy")["output"]["cases"][0]
    assert "maintenance_event" not in deploy["records"]


def test_off_linux_up_needs_the_unconfined_run_said_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "logs"
    folder.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    assert U.main([str(folder), "--out", str(tmp_path / "out")]) == 2
    assert "--isolation in_process" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_an_unconfined_run_says_so(site: Path, tmp_path: Path) -> None:
    assert U.up(site, tmp_path / "out", isolation="in_process", stream=None) == 0
    document = json.loads((tmp_path / "out" / "up.json").read_text(encoding="utf-8"))
    assert document["isolation"] == "in_process"
    assert "unconfined" in (tmp_path / "out" / "up.md").read_text(encoding="utf-8")


def test_make_and_the_readme_name_it() -> None:
    assert "\nup: ## " in (REPO / "Makefile").read_text(encoding="utf-8")
    assert "make up SRC=" in (REPO / "README.md").read_text(encoding="utf-8")
