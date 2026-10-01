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
