"""Import boundaries from AGENTS.md, checked on the AST so they hold without running any code."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "neptune_memory"
MODEL_CLIENTS = (
    "anthropic",
    "openai",
    "google.generativeai",
    "google.genai",
    "cohere",
    "mistralai",
    "ollama",
    "transformers",
    "torch",
    "langchain",
    "litellm",
)
SUBPACKAGES = ("schema", "store", "consolidate", "derived", "spatial", "episodes", "cli")


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # resolve relative imports against neptune_memory
                base = path.relative_to(SRC).parent.parts
                module = ".".join(["neptune_memory", *base[: len(base) - node.level + 1], module])
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_every_repo_map_subpackage_exists_with_a_rule_docstring() -> None:
    for sub in SUBPACKAGES:
        init = SRC / sub / "__init__.py"
        doc = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8")))
        assert doc is not None and "Rule:" in doc, sub


def test_consolidate_never_imports_derived_or_model_clients() -> None:
    forbidden = ("neptune_memory.derived", *MODEL_CLIENTS)
    for path in sorted((SRC / "consolidate").rglob("*.py")):
        for name in _imports(path):
            assert not any(_under(name, f) for f in forbidden), f"{path.name} imports {name}"


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_nothing_reads_the_compiler_store_directly(path: Path) -> None:
    for name in _imports(path):
        assert not _under(name, "neptune.store"), f"{path.name} imports {name}"


def test_only_derived_may_import_model_clients() -> None:
    for path in sorted(SRC.rglob("*.py")):
        if "derived" in path.relative_to(SRC).parts:
            continue
        for name in _imports(path):
            assert not any(_under(name, c) for c in MODEL_CLIENTS), f"{path.name} imports {name}"
