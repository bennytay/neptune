"""Context reads Memory and the Ledger through published surfaces and writes neither (ADR 0001)."""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "neptune_context"

# What Context may import from each upstream: the read surface only. Memory's store, consolidators,
# derived annotators, episodes/spatial helpers, ledger seam and CLI are Memory's to write with.
ALLOWED = {
    "neptune_ledger": ("neptune_ledger.api",),
    "neptune_memory": ("neptune_memory.schema",),
}
# From the compiler Context may use only the canonical model and identity; its store, runtime and
# adapters write or parse packages, and Context reads packages only via the Ledger.
ALLOWED_COMPILER = ("neptune.model", "neptune.identity")
SUBPACKAGES = ("query", "retrieve", "packets", "render", "sdk", "mcp", "explain", "eval")


def _imports(path: Path) -> Iterator[str]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def _files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(SRC)))
def test_imports_stay_on_the_published_read_surface(path: Path) -> None:
    for name in _imports(path):
        for package, allowed in ALLOWED.items():
            if _under(name, package):
                assert any(_under(name, a) for a in allowed), f"{path.name} imports {name}"
        if _under(name, "neptune"):
            assert any(_under(name, a) for a in ALLOWED_COMPILER), f"{path.name} imports {name}"


def test_the_repo_map_subpackages_exist() -> None:
    assert [d for d in SUBPACKAGES if not (SRC / d / "__init__.py").is_file()] == []


def test_the_agents_repo_map_names_every_subpackage() -> None:
    agents = (SRC.parents[1] / "AGENTS.md").read_text(encoding="utf-8")
    assert [d for d in SUBPACKAGES if f"`{d}/`" not in agents] == []
