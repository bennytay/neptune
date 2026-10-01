import ast
from pathlib import Path

import neptune


def test_version_is_dotted_integers() -> None:
    parts = neptune.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_model_imports_nothing_outside_model() -> None:
    """``model/`` is the contract: it never depends on identity, adapters, runtime or derived/."""
    model = Path(neptune.__file__).parent / "model"
    for path in model.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                if name.startswith("neptune"):
                    assert name.startswith("neptune.model"), f"{path.name} imports {name}"


def test_store_imports_only_model_and_identity() -> None:
    """``store/`` writes and reads packages; it never depends on adapters, runtime or derived/."""
    store = Path(neptune.__file__).parent / "store"
    for path in store.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("neptune"):
                allowed = ("neptune.model", "neptune.identity", "neptune.store")
                assert (node.module or "").startswith(allowed), f"{path.name} imports {node.module}"


def test_format_adapters_are_leaves() -> None:
    """A format subpackage sees only the model, identity and the contract (ADR 0008 §4)."""
    adapters = Path(neptune.__file__).parent / "adapters"
    allowed = ("neptune.model", "neptune.identity", "neptune.adapters.contract")
    formats = [path for path in adapters.iterdir() if path.is_dir() and path.name != "__pycache__"]
    assert formats, "the reference adapter is a subpackage"
    for package in formats:
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.startswith("neptune"):
                        assert name.startswith(allowed), (
                            f"{package.name}/{path.name} imports {name}"
                        )


def test_the_adapter_contract_does_not_reach_into_the_runtime_or_store() -> None:
    adapters = Path(neptune.__file__).parent / "adapters"
    for path in adapters.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("neptune"):
                forbidden = ("neptune.runtime", "neptune.store", "neptune.derived")
                assert not (node.module or "").startswith(forbidden), f"{path.name}: {node.module}"
