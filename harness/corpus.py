"""The input corpus: cases the compiler stage ingests, one folder of sources per case.

Today the corpus is the compiler's four worked examples (``tests/fixtures/model/<example>/sources``:
a drone, a manipulator, a mobile robot and a quadruped). The Deploy D1 archetype fixtures replace
them when they land: that is ``ARCHETYPES``, the one place to wire them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
WORKED_EXAMPLES: Final = REPO / "tests" / "fixtures" / "model"
EXAMPLE_NAMES: Final = ("drone", "manipulator", "mobile_robot", "quadruped")

# HOOK (Deploy D1): the archetype fixtures, one folder per archetype, each a folder of sources.
# When the Deploy project lands them, point this at the directory (its location is that
# project's to choose) and the harness uses them instead of the worked examples; nothing else
# changes. Until the directory exists, this is skipped on purpose.
ARCHETYPES: Final = REPO / "packages" / "neptune-deploy" / "fixtures" / "archetypes"


@dataclass(frozen=True)
class Case:
    """One robot's sources: ``id`` names it in the report, ``sources`` is the folder to ingest."""

    id: str
    sources: Path


def _cases_in(root: Path) -> list[Case]:
    return [Case(path.name, path) for path in sorted(root.iterdir()) if path.is_dir()]


def select(override: Path | None = None) -> tuple[str, list[Case]]:
    """(corpus name, cases): ``--corpus`` if given, else the archetypes if present, else the
    worked examples. The name is what the report says the corpus is."""
    if override is not None:
        return "custom", _cases_in(override)
    if ARCHETYPES.is_dir():
        return "deploy-d1-archetypes", _cases_in(ARCHETYPES)
    return "worked-examples", [
        Case(name, WORKED_EXAMPLES / name / "sources") for name in EXAMPLE_NAMES
    ]
