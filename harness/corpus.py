"""The input corpus: cases the compiler stage ingests, one folder of sources per case.

The default is the acceptance corpus (``harness.acceptance``, Platform ADR 0007): one generated,
versioned hand-over folder of two sites and three robot types, ingested as one case, with gold
answers whose evidence the compiler stage resolves and the Deploy mappings the deploy stage runs.
``worked-examples`` is the compiler's four worked examples
(``tests/fixtures/model/<example>/sources``: a drone, a manipulator, a mobile robot and a
quadruped), each its own case; ``--corpus DIR`` is any folder of case folders.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness import acceptance

REPO: Final = Path(__file__).resolve().parents[1]
WORKED_EXAMPLES: Final = REPO / "tests" / "fixtures" / "model"
EXAMPLE_NAMES: Final = ("drone", "manipulator", "mobile_robot", "quadruped")
ACCEPTANCE: Final = acceptance.NAME
NAMES: Final = (ACCEPTANCE, "worked-examples")
# Where the acceptance corpus is written when the caller names no directory.
DEFAULT_BUILD: Final = REPO / "harness" / ".run" / "corpus"


@dataclass(frozen=True)
class Case:
    """One case: ``id`` names it in the report, ``sources`` is the folder to ingest, ``gold`` the
    gold answers whose evidence the compiler stage resolves against the case's package, and
    ``deploy`` the Deploy mappings the deploy stage runs over that package (none: none to run).
    ``memory`` is Memory's consolidator declaration for the corpus (``--config``), ``snapshot`` the
    graph Memory committed for it (the memory stage must rebuild it byte for byte), and
    ``answers`` the pinned agent answers the context stage checks (Platform ADR 0011)."""

    id: str
    sources: Path
    gold: Path | None = None
    deploy: Path | None = None
    memory: Path | None = None
    snapshot: Path | None = None
    answers: Path | None = None


def _cases_in(root: Path) -> list[Case]:
    return [Case(path.name, path) for path in sorted(root.iterdir()) if path.is_dir()]


def select(
    override: Path | None = None, *, name: str = ACCEPTANCE, into: Path | None = None
) -> tuple[str, list[Case]]:
    """(corpus label, cases). ``override`` (``--corpus DIR``) wins; else ``name``. The acceptance
    corpus is generated under ``into`` (default ``harness/.run/corpus``); its label carries the
    version, which is what a gate quotes."""
    if override is not None:
        return "custom", _cases_in(override)
    if name == "worked-examples":
        return name, [Case(n, WORKED_EXAMPLES / n / "sources") for n in EXAMPLE_NAMES]
    if name != ACCEPTANCE:
        raise ValueError(f"unknown corpus {name!r}; choose one of {', '.join(NAMES)}")
    root = acceptance.materialise((into or DEFAULT_BUILD) / f"{ACCEPTANCE}-{acceptance.VERSION}")
    return f"{ACCEPTANCE} {acceptance.VERSION}", [
        Case(
            f"{ACCEPTANCE}-{acceptance.VERSION}",
            root,
            acceptance.GOLD,
            acceptance.DEPLOY,
            acceptance.MEMORY_CONFIG,
            acceptance.MEMORY_SNAPSHOT,
            acceptance.ANSWERS,
        )
    ]
