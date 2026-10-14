"""``make demo-folder DIR=<path>`` writes a clean folder for Claude Code over the demo: the server
by absolute paths, the skill, the output-token setting, and nothing else; it refuses a folder that
holds anything else and rewrites a previous demo folder."""

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from harness import demo_folder as D

REPO: Final = Path(__file__).resolve().parents[3]


def _files(folder: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()
    }


def _skill_files() -> set[str]:
    return {
        f".claude/skills/neptune/{p.relative_to(D.SKILL)}"
        for p in D.SKILL.rglob("*")
        if p.is_file()
    }


def test_an_empty_folder_gets_the_server_the_skill_and_the_setting_only(tmp_path: Path) -> None:
    folder = tmp_path / "new" / "demo-folder"  # created when missing
    graph, sources = D.write(folder, demo=tmp_path / "no-demo", uv="/opt/uv")
    assert (graph, sources) == (D.SNAPSHOT, D.SOURCES)  # `make demo` has not run
    files = _files(folder)
    assert set(files) == {".mcp.json", ".claude/settings.json"} | _skill_files()
    assert json.loads(files[".claude/settings.json"]) == {
        "env": {"MAX_MCP_OUTPUT_TOKENS": "100000"}
    }
    server = json.loads(files[".mcp.json"])["mcpServers"]["neptune"]
    assert server["command"] == "/opt/uv" and server["type"] == "stdio"
    args = server["args"]
    assert args[args.index("--directory") + 1] == str(REPO)
    assert args[args.index("--memory") + 1] == str(D.SNAPSHOT)
    assert args[args.index("--sources") + 1] == str(D.SOURCES)
    assert args[args.index("-m") + 1] == "neptune_context.mcp"
    for flag in ("--directory", "--memory", "--sources"):
        path = Path(args[args.index(flag) + 1])
        assert path.is_absolute() and path.exists(), flag
    skill = folder / ".claude" / "skills" / "neptune" / "SKILL.md"
    assert skill.read_bytes() == (D.SKILL / "SKILL.md").read_bytes()
    assert not (folder / "CLAUDE.md").exists() and not (folder / "AGENTS.md").exists()


def test_it_serves_what_make_demo_wrote_when_it_wrote_both(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "graph.json").write_text("{}", encoding="utf-8")
    assert D.served(demo) == (D.SNAPSHOT, D.SOURCES)  # the graph alone is not enough
    (demo / "sources.json").write_text("{}", encoding="utf-8")
    assert D.served(demo) == ((demo / "graph.json").resolve(), (demo / "sources.json").resolve())
    D.write(tmp_path / "f", demo=demo, uv="uv")
    args = json.loads((tmp_path / "f" / ".mcp.json").read_text())["mcpServers"]["neptune"]["args"]
    assert args[args.index("--memory") + 1] == str((demo / "graph.json").resolve())


def test_a_previous_demo_folder_is_rewritten_byte_for_byte(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    D.write(folder, demo=tmp_path / "none", uv="uv")
    first = _files(folder)
    skill = folder / ".claude" / "skills" / "neptune" / "SKILL.md"
    skill.write_text("an older skill", encoding="utf-8")  # the tool's own file: refreshed
    approval = folder / ".claude" / "settings.local.json"
    approval.write_text('{"enabledMcpjsonServers": ["neptune"]}', encoding="utf-8")
    D.write(folder, demo=tmp_path / "none", uv="/other/uv")  # another uv, another demo: ours
    D.write(folder, demo=tmp_path / "none", uv="uv")
    assert approval.read_text() == '{"enabledMcpjsonServers": ["neptune"]}'  # Claude Code's own
    again = _files(folder)
    del again[".claude/settings.local.json"]
    assert again == first


def _ours() -> dict[str, Any]:
    return D.mcp_config(D.SNAPSHOT, D.SOURCES, "uv")


def _spoof(change: Any) -> str:
    document = _ours()
    change(document["mcpServers"]["neptune"])
    return json.dumps(document)


_SETTINGS = json.dumps(D.SETTINGS)


@pytest.mark.parametrize(
    "plant",
    [
        {"notes.txt": "mine"},
        {"CLAUDE.md": "repository instructions"},
        {".claude/agents/x.md": "an agent"},
        {".claude/skills/other/SKILL.md": "another skill"},
        {".mcp.json": '{"mcpServers": {"neptune": {}, "github": {}}}'},
        {".mcp.json": "not json"},
        {".claude/settings.json/inner": "a directory where a file belongs"},
        # Review of #173: a layout that looks like a demo folder is not proof it is one.
        {".claude/settings.json": '{"model": "opus"}'},  # a user's own settings, alone
        {".claude/skills/neptune/notes.txt": "my notes"},  # a user's file in the skill dir
        {".claude/skills/neptune/SKILL.md": "a skill of my own"},  # no .mcp.json: not ours
        {".claude/settings.json": _SETTINGS},  # our settings, but no .mcp.json
        # A .mcp.json that is not the tool's, beside otherwise-ours files.
        {".mcp.json": json.dumps({"mcpServers": {"neptune": {"command": "uv", "args": []}}})},
        {".mcp.json": _spoof(lambda s: s["args"].__setitem__(6, "evil.module"))},
        {".mcp.json": _spoof(lambda s: s.__setitem__("env", {"X": "1"}))},
        {".mcp.json": _spoof(lambda s: s["args"].append("--extra"))},
        {".mcp.json": json.dumps({**_ours(), "other": 1})},
        {".mcp.json": json.dumps(_ours()), ".claude/settings.json": '{"env": {}}'},
        {".mcp.json": json.dumps(_ours()), ".claude/skills/neptune/notes.txt": "my notes"},
        {".mcp.json": json.dumps(_ours()), ".claude/settings.local.json/x": "a directory"},
    ],
)
def test_a_folder_holding_anything_else_is_refused_and_untouched(
    tmp_path: Path, plant: dict[str, str]
) -> None:
    folder = tmp_path / "f"
    for name, text in plant.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    before = _files(folder)
    with pytest.raises(D.Refused):
        D.write(folder, demo=tmp_path / "none", uv="uv")
    assert _files(folder) == before


def test_the_reviewers_two_reproductions_exit_2_and_change_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = tmp_path / "settings-only"
    (mine / ".claude").mkdir(parents=True)
    (mine / ".claude" / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    notes = tmp_path / "notes-only"
    (notes / ".claude" / "skills" / "neptune").mkdir(parents=True)
    (notes / ".claude" / "skills" / "neptune" / "notes.txt").write_text("x", encoding="utf-8")
    for folder in (mine, notes):
        before = _files(folder)
        assert D.main([str(folder), "--demo", str(tmp_path / "none")]) == 2
        assert "not a demo folder" in capsys.readouterr().err
        assert _files(folder) == before
    assert (notes / ".claude" / "skills" / "neptune" / "notes.txt").read_text() == "x"


def test_an_interrupted_run_is_still_recognised(tmp_path: Path) -> None:
    """``.mcp.json`` is written first, so a folder with it and part of the rest is ours."""
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / ".mcp.json").write_text(json.dumps(_ours()), encoding="utf-8")
    D.write(folder, demo=tmp_path / "none", uv="uv")
    assert (folder / ".claude" / "skills" / "neptune" / "SKILL.md").is_file()


def test_symlinks_and_files_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    folder = tmp_path / "f"
    (folder / ".claude").mkdir(parents=True)
    (folder / ".mcp.json").write_text(json.dumps(_ours()), encoding="utf-8")
    (folder / ".claude" / "skills").symlink_to(target)
    with pytest.raises(D.Refused, match="skills"):
        D.write(folder, demo=tmp_path / "none", uv="uv")
    assert list(target.iterdir()) == []
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(D.Refused, match="not a directory"):
        D.check(link)
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(D.Refused, match="not a directory"):
        D.check(plain)


def test_the_command_line_refuses_with_status_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "notes.txt").write_text("mine", encoding="utf-8")
    assert D.main([str(folder)]) == 2
    assert "not a demo folder" in capsys.readouterr().err
    assert D.main([str(tmp_path / "g"), "--demo", str(tmp_path / "none")]) == 0
    assert "use it: cd " in capsys.readouterr().out


def test_make_and_the_readme_name_it() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^demo-folder: ## ", makefile, re.MULTILINE)
    assert '> $(RUN) python -m harness.demo_folder "$(DIR)" --demo "$(DEMO_DIR)"' in makefile
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "make demo-folder DIR=" in readme
