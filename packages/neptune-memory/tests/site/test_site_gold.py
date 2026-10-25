"""M1's finish line: the gold questions asked of the generated live site (spec §10).

Each test generates the corpus, builds the fact log, runs its calls through the site query API
(spec §8) and checks that the expected items are in the answers and that the answers cite the
expected files.
They are expected to fail until #188 removes the xfail marker.
"""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

GOLD = json.loads((Path(__file__).parent / "gold_questions.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in GOLD["questions"]}
UNTIL_M1 = pytest.mark.xfail(
    strict=True, reason="site model lands in M1 (#182-#187); #188 flips it"
)


def _site(name: str) -> Any:
    return importlib.import_module(f"neptune_memory.site.{name}")


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site-demo")
    _site("corpus").generate(out)
    _site("build").build(out)
    return out


def _object(item: Any) -> Any:
    obj = item.object
    return obj.ref if hasattr(obj, "ref") else obj.value


def _matches(item: Any, want: dict[str, Any]) -> bool:
    checks = {
        "subject": item.subject,
        "predicate": item.predicate,
        "object": _object(item),
        "kind": item.kind,
        "state": item.state,
    }
    return all(checks[key] == value for key, value in want.items())


def _ask(site_dir: Path, question: dict[str, Any]) -> list[Any]:
    query = _site("query")
    model = _site("views").load(site_dir)
    answers: list[Any] = []
    for call in question["calls"]:
        if call["tool"] == "confirm":  # G7: the operator says yes to the pending proposal
            pending = [i for i in answers[-1].items if i.state == "pending"]
            _site("confirm").confirm(site_dir, pending[0].fact_id, call["args"]["answer"])
            model = _site("views").load(site_dir)
            continue
        answers.append(getattr(query, call["tool"])(model, **call["args"]))
    return answers


@UNTIL_M1
@pytest.mark.parametrize("qid", sorted(QUESTIONS))
def test_site_gold_question(site_dir: Path, qid: str) -> None:
    question = QUESTIONS[qid]
    answers = _ask(site_dir, question)
    items = [item for answer in answers for item in answer.items]
    missing = [w for w in question["expect"] if not any(_matches(i, w) for i in items)]
    assert not missing, f"{qid} lacks {missing}"
    cited = {c.file for answer in answers for c in answer.sources.values()}
    assert set(question["cites"]) <= cited, f"{qid} cites {sorted(cited)}"


def test_site_gold_questions_are_well_formed() -> None:
    assert len(QUESTIONS) == 8
    for question in QUESTIONS.values():
        assert question["calls"] and question["expect"] and question["cites"]
