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
    """A format subpackage sees only the model, identity, the contract and itself (ADR 0008 §4),
    and the shared structured-text readers, which are no adapter and obey the same rule (ADR 0055).
    """
    adapters = Path(neptune.__file__).parent / "adapters"
    formats = [path for path in adapters.iterdir() if path.is_dir() and path.name != "__pycache__"]
    assert formats, "the reference adapter is a subpackage"
    for package in formats:
        own = f"neptune.adapters.{package.name}"  # its own modules, never another adapter
        allowed = (
            "neptune.model",
            "neptune.identity",
            "neptune.adapters.contract",
            "neptune.adapters.structured",
        )
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                own = f"neptune.adapters.{package.name}"
                for name in names:
                    if name.startswith("neptune"):
                        assert (
                            name.startswith(allowed) or name == own or name.startswith(own + ".")
                        ), f"{package.name}/{path.name} imports {name}"


def test_the_adapter_contract_does_not_reach_into_the_runtime_or_store() -> None:
    adapters = Path(neptune.__file__).parent / "adapters"
    for path in adapters.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("neptune"):
                forbidden = ("neptune.runtime", "neptune.store", "neptune.derived")
                assert not (node.module or "").startswith(forbidden), f"{path.name}: {node.module}"


def test_evidence_never_imports_interpretation() -> None:
    """``derived/`` reads the evidence layers; nothing below the runtime reads ``derived/``.

    Interpretation depends on evidence, never the reverse (ADR 0036 §8): ``derived/`` may import
    the model, identity and discovery's observed layout, and model, identity, discovery, store and
    adapters never import it.
    """
    root = Path(neptune.__file__).parent
    for package in ("model", "identity", "discovery", "store", "adapters"):
        for path in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("neptune"):
                    assert not (node.module or "").startswith("neptune.derived"), (
                        f"{package}/{path.name} imports {node.module}"
                    )
    allowed = ("neptune.model", "neptune.identity", "neptune.discovery", "neptune.derived")
    for path in (root / "derived").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("neptune"):
                assert (node.module or "").startswith(allowed), f"{path.name} imports {node.module}"
