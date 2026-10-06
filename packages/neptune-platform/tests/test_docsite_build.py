"""The site's build: offline output, the Sphinx configuration, and its place in check and CI."""

import re
import subprocess
from pathlib import Path
from typing import Final

import pytest
from docsite import build, conf

REPO: Final = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("a.html", '<script src="https://cdn.example/x.js"></script>'),
        ("a.html", "<link rel='stylesheet' href='//fonts.googleapis.com/css'>"),
        ("a.html", '<img alt="" src="http://x/y.png">'),
        ("a.css", "@font-face { src: url('https://x/f.woff2') }"),
        ("a.css", '@import "https://x/y.css";'),
        ("a.js", 'fetch("https://telemetry.example/p")'),
    ],
)
def test_a_remote_load_is_a_problem(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")
    (problem,) = build.remote_loads(tmp_path)
    assert problem.startswith(f"{name}: loads a remote resource")


def test_links_and_local_resources_are_not_remote_loads(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.html").write_text(
        '<a href="https://github.com/o/r">r</a><script src="../_static/x.js"></script>'
        '<link rel="stylesheet" href="_static/furo.css">',
        encoding="utf-8",
    )
    (tmp_path / "a.css").write_text("x { background: url('img/a.png') }", encoding="utf-8")
    assert build.remote_loads(tmp_path) == []


def test_the_configuration_reads_no_network_and_keeps_strictness() -> None:
    assert conf.extensions == ["myst_parser", "sphinx.ext.autodoc"]  # no intersphinx, no analytics
    assert conf.html_theme == "furo"
    assert conf.suppress_warnings == ["ref.python"]
    assert conf.html_last_updated_fmt is None
    assert not re.search(r"\d{4}", conf.copyright)


def test_the_docstring_shim_ends_a_glued_literal_and_nothing_else() -> None:
    lines = ["are ``ValueError``s, never", "``a`` b and ``c``.", "plain"]
    conf._docstring(None, "module", "m", None, None, lines)
    assert lines == ["are ``ValueError``\\ s, never", "``a`` b and ``c``.", "plain"]


def test_sphinx_runs_strict_with_a_fresh_environment() -> None:
    source = Path(build.__file__).read_text(encoding="utf-8")
    for flag in ('"-W"', '"--keep-going"', '"-E"', '"-n"'):
        assert flag in source


def _make(*args: str) -> str:
    result = subprocess.run(
        ["make", "-n", "-C", str(REPO), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def test_a_whole_workspace_check_builds_the_site_and_a_package_check_does_not() -> None:
    assert "python -m docsite" in _make("check")
    assert "python -m docsite" not in _make("check", "PKG=neptune-platform")


def test_ci_builds_the_site_on_every_event_and_check_requires_it() -> None:
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  docs:\n", 1)[1].split("\n  check:\n", 1)[0]
    assert "run: make docs" in job
    assert "\n    if:" not in job and "needs:" not in job  # unfiltered: no path can be skipped
    (needs,) = re.findall(r"\n  check:\n    needs: \[([^\]]*)\]", workflow)
    assert "docs" in [n.strip() for n in needs.split(",")]


def test_the_build_output_is_ignored_by_git() -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", "build/docs/html/index.html"], check=False
    )
    assert result.returncode == 0
