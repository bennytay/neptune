"""The site's source tree: mirrored paths, generated pages, link problems, determinism."""

import json
from pathlib import Path
from typing import Final

import pytest
from docsite import assemble
from docsite.sources import API_MODULES, REPOSITORY_URL, SECTIONS

REPO: Final = Path(__file__).resolve().parents[3]


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


SCHEMA: Final = {"title": "C", "$defs": {"A": {"type": "string"}}}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(
        root,
        {
            "README.md": "# R\n\n## 15-minute quickstart\n\nRun [this](scripts/q.sh).\n\n"
            "```sh\n## not a heading\n```\n\n## License\n\nMIT\n",
            "scripts/q.sh": "echo\n",
            "docs/architecture.md": "# Architecture\n\n"
            "See [the sdk](sdk.md) and [code](../src/m.py).\n",
            "docs/sdk.md": "# SDK\n\n![diagram](img/d.png)\n",
            "docs/img/d.png": "png",
            "docs/zeta.md": "# Zeta\n",
            "docs/adr/0001-first.md": "# 0001 First\n",
            "docs/adr/README.md": "# Decisions\n\n[0001](0001-first.md)\n",
            "docs/reviews/m1.md": "# M1\n",
            "src/m.py": "",
            # the contracts index cites the registry's decision record
            "packages/neptune-platform/docs/adr/0002-contracts-registry-and-version-policy.md": "",
            "contracts/compatibility.md": "# Compatibility\n",
            "contracts/x/contract.toml": 'title = "X"\nstatus = "active"\n'
            '[owner]\npackage = "neptune"\n',
            "contracts/x/v1.0.0/schema.json": json.dumps(SCHEMA),
            "contracts/x/v1.0.0/version.json": json.dumps({"status": "stable"}),
            "contracts/x/v1.2.0/schema.json": json.dumps(SCHEMA),
            "contracts/y/contract.toml": 'title = "Y"\nstatus = "planned"\npart_of = "x"\n',
        },
    )
    # the decisions layer pages link first (outside the sections' fake documents)
    _write(root, {path: "# Key\n" for section in SECTIONS for _, path in section.key})
    return root


@pytest.fixture
def pages(tmp_path: Path) -> Path:
    root = tmp_path / "pages"
    _write(
        root,
        {
            "index.md": "# Home\n\n[arch](docs/architecture.md)\n",
            "quickstart.md": f"# Quickstart\n\n{assemble.QUICKSTART_MARKER}\n",
        },
    )
    return root


def test_documents_keep_their_repository_paths_and_links(repo: Path, pages: Path) -> None:
    tree = assemble.assemble(repo, pages)
    assert tree.problems == []
    text = tree.files["docs/architecture.md"].decode()
    assert "[the sdk](sdk.md)" in text
    assert f"[code]({REPOSITORY_URL}/blob/main/src/m.py)" in text
    assert tree.files["docs/img/d.png"] == b"png"  # an image is carried, not linked remotely
    assert tree.files["contracts/compatibility.md"] == b"# Compatibility\n"
    assert "README.md" not in tree.files


def test_the_compiler_section_leads_with_its_order_and_puts_decisions_apart(
    repo: Path, pages: Path
) -> None:
    page = assemble.assemble(repo, pages).files["layers/compiler.md"].decode()
    guides = page.split("## Decisions", 1)[0]
    assert (
        guides.index("/docs/architecture") < guides.index("/docs/sdk") < guides.index("/docs/zeta")
    )
    decisions = page.split("## Decisions", 1)[1].split("## Gate reviews", 1)
    assert "/docs/adr/README\n" in decisions[0]
    assert ":hidden:\n\n/docs/adr/0001-first\n" in decisions[0]
    assert "/docs/reviews/m1\n" in decisions[1]


def test_a_layer_page_links_its_key_decisions_first(repo: Path, pages: Path) -> None:
    page = assemble.assemble(repo, pages).files["layers/context.md"].decode()
    start = page.index("Start with:")
    assert start < page.index("```{toctree}")
    assert (
        "- [The query language](../packages/neptune-context/docs/adr/0002-query-language.md)"
        in page
    )
    assert "- [The context packet format](../" in page


def test_contracts_are_rendered_newest_first_and_pending_ones_listed(
    repo: Path, pages: Path
) -> None:
    files = assemble.assemble(repo, pages).files
    index = files["contracts/x/index.md"].decode()
    assert index.index("v1.2.0/schema") < index.index("v1.0.0/schema")
    assert files["contracts/x/v1.0.0/schema.md"].decode().startswith("# x 1.0.0\n")
    assert json.loads(files["contracts/x/v1.0.0/schema.json"]) == SCHEMA
    listing = files["contracts/index.md"].decode()
    assert "x/index" in listing and "y/index" not in listing
    assert "- `y` (planned; part of `x`): Y" in listing


def test_the_api_reference_has_a_page_per_surface(repo: Path, pages: Path) -> None:
    files = assemble.assemble(repo, pages).files
    for api in API_MODULES:
        page = files[f"api/{api.page}.md"].decode()
        for module in api.modules:
            assert f".. automodule:: {module}\n" in page
        assert (":imported-members:" in page) == api.facade


def test_the_quickstart_embeds_the_readme_section(repo: Path, pages: Path) -> None:
    page = assemble.assemble(repo, pages).files["quickstart.md"].decode()
    assert f"Run [this]({REPOSITORY_URL}/blob/main/scripts/q.sh)." in page
    assert "## not a heading" in page  # a fenced line is not the next section
    assert "MIT" not in page and assemble.QUICKSTART_MARKER not in page


def test_the_quickstart_points_at_the_readme_until_it_has_the_section(
    repo: Path, pages: Path
) -> None:
    (repo / "README.md").write_text("# R\n\n## Status\n\nx\n", encoding="utf-8")
    page = assemble.assemble(repo, pages).files["quickstart.md"].decode()
    assert f"[repository README]({REPOSITORY_URL}/blob/main/README.md)" in page


def test_broken_links_and_double_claims_are_problems(repo: Path, pages: Path) -> None:
    _write(repo, {"docs/zeta.md": "# Zeta\n\n[gone](gone.md) [out](../../x.md)\n"})
    _write(pages, {"docs/sdk.md": "# Shadow\n"})
    problems = assemble.assemble(repo, pages).problems
    assert "docs/sdk.md: claimed twice (again by pages/)" in problems
    assert any(
        p.startswith("docs/zeta.md: broken link: 'gone.md' does not exist") for p in problems
    )
    assert any("'../../x.md' leaves the repository" in p for p in problems)


def test_a_schema_that_does_not_parse_is_a_problem(repo: Path, pages: Path) -> None:
    _write(repo, {"contracts/x/v1.2.0/schema.json": "{"})
    problems = assemble.assemble(repo, pages).problems
    assert any(p.startswith("contracts/x/v1.2.0/schema.json: ") for p in problems)


def test_assembly_is_deterministic_and_write_mirrors_the_tree(
    repo: Path, pages: Path, tmp_path: Path
) -> None:
    first = assemble.assemble(repo, pages)
    assert first.files == assemble.assemble(repo, pages).files
    assert list(first.files) == sorted(first.files)
    assemble.write(first, tmp_path / "out")
    written = {
        p.relative_to(tmp_path / "out").as_posix(): p.read_bytes()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file()
    }
    assert written == first.files


def test_the_real_repository_assembles_without_problems() -> None:
    tree = assemble.assemble(REPO)
    assert tree.problems == []
    for section in SECTIONS:
        assert f"layers/{section.slug}.md" in tree.files
        for lead in section.lead:
            assert f"{section.root}/{lead}" in tree.files, "a lead document was renamed"
    for path in ("index.md", "quickstart.md", "deployment-targets.md", "concepts/as-of.md"):
        assert path in tree.files
