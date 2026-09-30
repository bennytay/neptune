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
