"""Question sets: the gold questions, as plain questions with the facts an answer must hold."""

from __future__ import annotations

import json
import tempfile
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


SETS = {"gold": ("p7", "harbor", "public"), "plant7": ("plant7-1d", "plant7-7d")}


def key_file(root: Path, site: str) -> Path:
    """Where a simulated site's answer key lives: beside the site, never inside it."""
    return root / f"{site}.answers.json"


def load(
    name: str, sites: tuple[str, ...] | None = None, root: Path | None = None
) -> list[Question]:
    """A named set. ``gold``: every gold and gold-insight question of the given sites.
    ``plant7``: the simulated Plant 7 sites' questions, written from the simulator
    (``bench.plant7``); each site and its answer key are generated under ``root`` if missing."""
    if name not in SETS:
        raise ValueError(f"unknown question set {name!r}; the sets are: {', '.join(SETS)}")
    sites = sites or SETS[name]
    if name == "plant7":
        from neptune_context.bench import plant7

        out: list[Question] = []
        for site in sites:
            if root is None:
                raise ValueError("the plant7 set needs the sites root")
            key = key_file(root, site)
            if not key.exists():
                if (root / site).exists():  # keep the built site; the key is the same either way
                    with tempfile.TemporaryDirectory() as scratch:
                        plant7.write(Path(scratch) / site, key, plant7.DAYS[site])
                else:
                    plant7.write(root / site, key, plant7.DAYS[site])
            data = json.loads(key.read_text(encoding="utf-8"))
            suffix = site.removeprefix("plant7-")
            out += [_question(site, {**q, "id": f"{q['id']}@{suffix}"}) for q in data["questions"]]
        return out
    out = []
    for site in sites:
        for file in GOLD_FILES[site]:
            data = json.loads((GOLD_DIR / file).read_text(encoding="utf-8"))
            out += [_question(site, q) for q in data["questions"]]
    return out
