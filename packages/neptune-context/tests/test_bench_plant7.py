"""The Plant 7 benchmark set: written from the simulator alone, the same for the same seed."""

import ast
import json
from pathlib import Path

import pytest
from neptune_memory.site.sim import run as sim

from neptune_context.bench import plant7


def _bytes(site: Path) -> dict[str, bytes]:
    return {str(p.relative_to(site)): p.read_bytes() for p in sorted((site / "feed").glob("*"))}


@pytest.mark.slow
def test_bench_plant7_same_seed_same_site_and_answers(tmp_path: Path) -> None:
    plant7.write(tmp_path / "a", tmp_path / "a.json", days=1)
    plant7.write(tmp_path / "b", tmp_path / "b.json", days=1)
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    assert _bytes(tmp_path / "a") == _bytes(tmp_path / "b")
    sim.prepare(tmp_path / "c", days=1)
    sim.run(tmp_path / "c", days=1, seed=plant7.SEED, frames=0)
    assert _bytes(tmp_path / "a") == _bytes(tmp_path / "c"), "the feed is exactly sim run's"

    questions = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["questions"]
    kinds = {q["kind"] for q in questions}
    assert kinds == {"first_fault", "unsure", "lookup"}
    first = next(q for q in questions if q["kind"] == "first_fault")
    assert first["expect"][0] == {
        "subject": "machine:cv-2",
        "predicate": "health",
        "object": "fault:JAM_PE3",
        "state": "known",
    }
    assert "Lindqvist" in first["expect"][1]["object"]


def test_bench_plant7_answer_key_imports_nothing_of_neptune_but_the_simulator() -> None:
    tree = ast.parse(Path(plant7.__file__).read_text(encoding="utf-8"))
    modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    neptune = [m for m in modules if m.startswith("neptune")]
    assert neptune and all(m.startswith("neptune_memory.site.sim") for m in neptune), neptune
