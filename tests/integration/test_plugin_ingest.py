"""``neptune ingest`` and the SDK with plugins installed (ADR 0058, MVL-200).

Plugins are real distributions installed into site directories (``make_plugin_dists``); the jobs
are real, sandboxed by default, over real files. What is checked:

- an installed plugin adapter is probed, selected and run, and its records name its distribution;
- the same plugins installed in a different order give byte-identical packages;
- a broken plugin is a finding in the package and the job commits;
- ``--no-plugins`` / ``plugins=False`` and ``--plugin`` / ``PluginPolicy(allow=...)`` decide what
  is read.
"""

import importlib
import io
import json
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.cli import exit_codes, run
from neptune.runtime.plugins import LOAD_FAILED, PLUGINS_ID
from neptune.sdk import (
    AsyncNeptune,
    ConfigurationError,
    IngestResult,
    JobOptions,
    Neptune,
    PluginPolicy,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("forget_plugins")]

TALLY_FILE: Final = b"TALLY1\n10 1\n20 2\n30 x\n40 4\n"
# A frame log (``tests/fixtures/adapters/framelog_adapter.py``): magic, then (time, size, payload).
FRAMELOG_FILE: Final = b"FRAMELOG1\n" + b"".join(
    struct.pack("<QI", t, len(p)) + p for t, p in ((1, b"ab"), (2, b"cde"))
)


def cli(*argv: str) -> tuple[int, dict[str, Any], str]:
    out, err = io.StringIO(), io.StringIO()
    code = run([*argv, "--json"], stdout=out, stderr=err)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines[-1] if lines else {}, err.getvalue()


def package_bytes(package: Path) -> dict[str, bytes]:
    """Every file of a package but ``volatile/`` (its envelope: clock, host, job; ADR 0022)."""
    return {
        str(path.relative_to(package)): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.relative_to(package).parts[0] != "volatile"
    }


def install_plugins(plugin_dists: ModuleType, tally: Path, others: Path) -> None:
    """Three distributions over two sites: tally, framelog and one that does not import."""
    plugin_dists.install(
        tally,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugin_dists.install(
        others,
        "neptune-test-framelog",
        "2.0.0",
        adapters={"framelog": ":FrameLogAdapter"},
        module=plugin_dists.FRAMELOG,
    )
    plugin_dists.install(
        others,
        "neptune-test-broken",
        "0.1.0",
        adapters={"broken": ":make"},
        module=plugin_dists.BROKEN_IMPORT,
    )


@pytest.fixture
def run_folder(tmp_path: Path) -> Path:
    """A cell's run: a tally of joint counts, a frame log from its camera, an operator note."""
    root = tmp_path / "work" / "run"
    root.mkdir(parents=True)
    (root / "joint_counts.tally").write_bytes(TALLY_FILE)
    (root / "camera.framelog").write_bytes(FRAMELOG_FILE)
    (root / "notes.txt").write_text("cell 3, second shift\n", encoding="utf-8")
    return root


def _transforms(result: IngestResult) -> dict[str, dict[str, str]]:
    return {t.adapter_id: dict(t.libraries) for t in result.read_receipt().transforms}


def test_neptune_ingest_probes_and_runs_an_installed_plugin_adapter(
    plugin_dists: ModuleType, plugin_site: Path, run_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugins(plugin_dists, plugin_site, plugin_site)
    monkeypatch.chdir(run_folder.parent)
    code, result, err = cli("ingest", "run", "--out", "pkg", "-w", "ws")
    assert code == exit_codes.OK and err == "" and result["state"] == "committed"
    assert result["sources"] == 3  # the tally, the frame log and the note
    found = result["findings"]["by_code"]
    assert found[LOAD_FAILED] == 1 and found["tally.bad_row"] == 1  # the plugin's own finding
    assert result["records"]["stream"] == 2  # one per plugin format

    receipt = json.loads((run_folder.parent / "pkg" / "receipt.json").read_bytes())
    libraries = {t["adapter_id"]: t["libraries"] for t in receipt["transforms"]}
    assert libraries["tally"] == {"neptune-test-tally": "1.0.0"}
    assert libraries["framelog"] == {"neptune-test-framelog": "2.0.0"}
    # The loader names every plugin the job could use, sorted (ADR 0058 §5): these, and any the
    # workspace itself installs (neptune-deploy).
    loaded = libraries[PLUGINS_ID]
    assert list(loaded) == sorted(loaded)
    assert {
        "neptune-test-framelog": "2.0.0",
        "neptune-test-tally": "1.0.0",
    }.items() <= loaded.items()
    (broken,) = [f for f in receipt["findings"] if f["code"] == LOAD_FAILED]
    assert broken["severity"] == "warning"


def test_without_plugins_the_plugin_formats_are_unsupported(
    plugin_dists: ModuleType, plugin_site: Path, run_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_plugins(plugin_dists, plugin_site, plugin_site)
    monkeypatch.chdir(run_folder.parent)
    code, result, _ = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--no-plugins")
    assert code == exit_codes.OK and result["state"] == "committed"
    assert result["records"]["stream"] == 0  # no adapter read the tally or the frame log
    assert LOAD_FAILED not in result["findings"]["by_code"]  # an unread plugin leaves no trace

    code, result, _ = cli(
        "ingest", "run", "--out", "only", "-w", "ws", "--plugin", "Neptune_Test_Tally"
    )
    assert code == exit_codes.OK and result["records"]["stream"] == 1  # the tally's, not the log's
    assert LOAD_FAILED not in result["findings"]["by_code"]


def test_plugins_installed_in_another_order_give_byte_identical_packages(
    plugin_dists: ModuleType, tmp_path: Path, run_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "site-a", tmp_path / "site-b"
    install_plugins(plugin_dists, first, second)
    base = list(sys.path)
    outputs, packages = [], []
    for attempt, order in (("one", [first, second]), ("two", [second, first])):
        monkeypatch.setattr(sys, "path", [*map(str, order), *base])
        importlib.invalidate_caches()
        for name in [name for name in sys.modules if name.startswith("neptune_test_")]:
            del sys.modules[name]
        here = tmp_path / attempt
        here.mkdir()
        monkeypatch.chdir(here)
        code, result, _ = cli("ingest", str(run_folder), "--out", "pkg", "-w", "ws")
        assert code == exit_codes.OK and result["state"] == "committed"
        outputs.append((result["package"], result["receipt"], result["findings"]))
        packages.append(package_bytes(here / "pkg"))
    assert outputs[0] == outputs[1]
    assert packages[0] == packages[1]


def test_a_broken_plugin_is_a_finding_and_the_job_commits(
    plugin_dists: ModuleType, plugin_site: Path, run_folder: Path, tmp_path: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-raising",
        "0.1.0",
        adapters={"raising": ":make"},
        module=plugin_dists.RAISING_FACTORY,
    )
    client = Neptune(tmp_path / "ws")
    assert [f.code for f in client.plugins.findings] == [LOAD_FAILED]
    result = client.ingest(run_folder, tmp_path / "pkg")
    assert result.committed
    (finding,) = client.plugins.findings
    assert finding.details["error"] == "RuntimeError" and "/home" not in finding.message
    assert [f.id for f in result.read_receipt().findings if f.code == LOAD_FAILED] == [finding.id]
    assert PLUGINS_ID in _transforms(result)
    # A dry run reports it too.
    planned = client.dry_run(run_folder)
    assert LOAD_FAILED in {f.code for f in planned.findings}


def test_the_sdk_reads_plugins_by_policy(
    plugin_dists: ModuleType, plugin_site: Path, tmp_path: Path
) -> None:
    install_plugins(plugin_dists, plugin_site, plugin_site)
    builtins = {a.descriptor.id for a in builtin_adapters()}

    every = Neptune(tmp_path / "ws")
    assert set(every.registry.descriptors()) - builtins >= {"tally", "framelog"}
    assert {a.descriptor.id for a in every.plugins.adapters} >= {"tally", "framelog"}

    none = Neptune(tmp_path / "ws", plugins=False)
    assert set(none.registry.descriptors()) == builtins and none.plugins.findings == ()

    only = Neptune(tmp_path / "ws", plugins=PluginPolicy(allow=("neptune-test-framelog",)))
    assert set(only.registry.descriptors()) - builtins == {"framelog"}

    # Given adapters are the whole adapter set; plugins add none to it.
    given = Neptune(tmp_path / "ws", adapters=builtin_adapters())
    assert set(given.registry.descriptors()) == builtins

    asynchronous = AsyncNeptune(tmp_path / "ws", plugins=True)
    assert asynchronous.plugins.findings == every.plugins.findings

    with pytest.raises(ConfigurationError):
        Neptune(tmp_path / "ws", plugins="all")  # type: ignore[arg-type]
    # A plugin's options are configured like a built-in's.
    with pytest.raises(ConfigurationError):
        Neptune(tmp_path / "ws", options=JobOptions(config={"tally": {"rows": 3}}))


def test_contradictory_or_bad_plugin_flags_are_refused(run_folder: Path) -> None:
    code, _, err = cli("ingest", str(run_folder), "--dry-run", "--no-plugins", "--plugin", "a")
    assert code == exit_codes.USAGE and "contradict" in err
    code, result, _ = cli("ingest", str(run_folder), "--dry-run", "--plugin", "not a name")
    assert result["error"]["code"] == "invalid_configuration"
    assert code == exit_codes.for_code("invalid_configuration")


def test_neptune_deploy_installed_its_adapter_is_probed_on_every_source(
    run_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workspace member Neptune Deploy registers ``deploy_lifecycle`` (Deploy ADR 0001).

    ``neptune ingest`` registers it and asks its probe about every source. It reads no format
    yet and claims nothing (Deploy ADR 0001 §6), so every record is the one a job without plugins
    makes; only an unread file's ``unsupported`` finding lists its decline too. Skipped until the
    member is installed in this environment.
    """
    pytest.importorskip("neptune_deploy")
    (run_folder / "commissioning.csv").write_text(
        "cell,test,result\nCELL-3,e-stop latency,pass\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    argv = ["ingest", str(run_folder), "--explain", "--json", "-w", "ws"]
    code = run(argv, stdout=out, stderr=err)
    assert code == exit_codes.OK
    (explanation,) = [
        json.loads(line)["explanation"]
        for line in out.getvalue().splitlines()
        if json.loads(line)["type"] == "explanation"
    ]
    assert "deploy_lifecycle" in {adapter["id"] for adapter in explanation["adapters"]}
    for source in explanation["sources"]:
        (verdict,) = [v for v in source["verdicts"] if v["adapter"] == "deploy_lifecycle"]
        assert verdict["verdict"] == "declined"
        assert [r["code"] for r in verdict["reasons"]] == ["deploy_lifecycle.no_reader"]
    packages = []
    for flags in ((), ("--no-plugins",)):
        code, result, _ = cli(
            "ingest", str(run_folder), "--out", f"pkg{len(flags)}", "-w", "ws", *flags
        )
        assert code == exit_codes.OK and result["state"] == "committed"
        packages.append(package_bytes(tmp_path / f"pkg{len(flags)}"))
    findings = "records/ingest_finding.jsonl"
    differ = {name for name in packages[0] if packages[0][name] != packages[1].get(name)}
    transforms = "records/transform_record.jsonl"
    assert differ == {"manifest.json", "receipt.json", "receipt.md", findings, transforms}
    assert b"neptune-deploy" in packages[0][transforms]  # the loaded plugin, in the package
    assert b"deploy_lifecycle.no_reader" in packages[0][findings]  # the frame log's decline
    assert b"deploy_lifecycle" not in packages[1][findings]


def test_the_package_names_the_loaded_plugins_even_when_none_reads_a_file(
    plugin_dists: ModuleType, plugin_site: Path, tmp_path: Path
) -> None:
    """Installing a plugin can change what existing folders give; the receipt says it was there.

    A plugin that ties the built-in ``text`` adapter turns a note into an ambiguity, and that
    ambiguity names the plugin's distribution."""
    plugin_dists.install(
        plugin_site,
        "neptune-test-shadow",
        "0.1.0",
        adapters={"shadow_text": ":Shadow"},
        module=plugin_dists.SHADOW,
    )
    root = tmp_path / "run"
    root.mkdir()
    (root / "notes.txt").write_text("cell 3, second shift\n", encoding="utf-8")
    only = PluginPolicy(allow=("neptune-test-shadow",))  # whatever else the workspace installs
    result = Neptune(tmp_path / "ws", plugins=only).ingest(root, tmp_path / "pkg")
    assert result.committed and result.ingested == ()
    assert _transforms(result)[PLUGINS_ID] == {"neptune-test-shadow": "0.1.0"}
    (tie,) = [f for f in result.findings if f.code == "neptune.probe.ambiguous"]
    assert tie.details["adapters"] == ["shadow_text", "text"]
    assert tie.details["plugins"] == {"shadow_text": "neptune-test-shadow 0.1.0"}
    assert "shadow_text (plugin neptune-test-shadow 0.1.0)" in tie.message

    plain = Neptune(tmp_path / "ws", plugins=False).ingest(root, tmp_path / "plain")
    assert PLUGINS_ID not in _transforms(plain) and len(plain.ingested) == 1
    assert "neptune.probe.ambiguous" not in {f.code for f in plain.read_receipt().findings}


def test_a_plugin_that_prints_never_breaks_json_lines(
    plugin_dists: ModuleType, plugin_site: Path, run_folder: Path, tmp_path: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-chatty",
        "0.1.0",
        adapters={"chatty": ":TallyAdapter"},
        module=plugin_dists.PRINTING,
    )
    out, err = io.StringIO(), io.StringIO()
    code = run(
        ["ingest", str(run_folder), "--dry-run", "--json", "-w", str(tmp_path / "ws")],
        stdout=out,
        stderr=err,
    )
    assert code == exit_codes.OK and err.getvalue() == ""
    lines = [json.loads(line) for line in out.getvalue().splitlines()]  # every line is JSON
    assert lines[-1]["type"] == "result"
    assert lines[-1]["findings"]["by_code"]["neptune.plugins.output"] == 1


def test_an_allowlist_naming_no_installed_plugin_is_refused(
    plugin_dists: ModuleType, plugin_site: Path, run_folder: Path, tmp_path: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    code, result, _ = cli(
        "ingest",
        str(run_folder),
        "--dry-run",
        "-w",
        str(tmp_path / "ws"),
        "--plugin",
        "neptune-test-tally",
        "--plugin",
        "neptune-test-taly",
    )
    assert code == exit_codes.for_code("invalid_configuration")
    assert "neptune-test-taly" in result["error"]["message"]
    with pytest.raises(ConfigurationError, match="neptune-test-taly"):
        Neptune(tmp_path / "ws", plugins=PluginPolicy(allow=("neptune-test-taly",)))
