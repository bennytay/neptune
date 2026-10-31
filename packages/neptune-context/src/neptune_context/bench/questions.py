"""Question sets: the gold questions, as plain questions with the facts an answer must hold."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[5]
GOLD_DIR = ROOT / "packages" / "neptune-memory" / "tests" / "site"
GOLD_FILES: dict[str, tuple[str, ...]] = {
    "p7": ("gold_questions.json", "gold_insights.json"),
    "harbor": ("harbor_gold_questions.json", "harbor_gold_insights.json"),
    "public": ("public_gold_questions.json",),
}
# Facts read from the demo sites' whiteboard photos. The photos are placeholder patterns and the
# text is a recorded extraction (README, "What's real"), so no reader of the raw files can get it.
UNANSWERABLE = frozenset({"note"})


@dataclass(frozen=True)
class Question:
    id: str
    site: str
    text: str
    expect: tuple[dict[str, Any], ...]
    cites: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = field(default=())


def _question(site: str, raw: dict[str, Any]) -> Question:
    text = raw["question"]
    expect = [dict(e) for e in raw["expect"] if e["predicate"] not in UNANSWERABLE]
    if any(call["tool"] == "confirm" for call in raw.get("calls", [])):
        # Before-and-after questions: an agent cannot confirm, so ask the "before" half only.
        text = text.split(" (before")[0] + " (No operator has confirmed anything yet.)"
        expect = [e for e in expect if e.get("state") == "pending"]
    return Question(
        id=raw["id"],
        site=site,
        text=text,
        expect=tuple(expect),
        cites=tuple(raw.get("cites", ())),
        rows=tuple(tuple(r) for r in raw.get("rows", ())),
    )


def load(name: str, sites: tuple[str, ...] = ("p7", "harbor", "public")) -> list[Question]:
    """A named set. ``gold``: every gold and gold-insight question of the given sites."""
    if name != "gold":
        raise ValueError(f"unknown question set {name!r}; the sets are: gold")
    out = []
    for site in sites:
        for file in GOLD_FILES[site]:
            data = json.loads((GOLD_DIR / file).read_text(encoding="utf-8"))
            out += [_question(site, q) for q in data["questions"]]
    return out
