"""Harbor DC's gold questions, asked of the generated warehouse site (issue #201).

Harbor is a second, differently shaped site: legged, humanoid, goods-to-person and conveyor feeds,
a JSON CMMS, an XLSX change log. Its mapping file is a draft in the config format of the "mappings
from config" lane, so these questions are ``xfail(strict=True)`` until the M2 gate wires the
mapper to it. Then the markers come off and they pass like Plant 7's eight.
"""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

GOLD = json.loads(
    (Path(__file__).parent / "harbor_gold_questions.json").read_text(encoding="utf-8")
)
QUESTIONS = {q["id"]: q for q in GOLD["questions"]}
_BUILT: dict[str, Path] = {}


def _site(name: str) -> Any:
    return importlib.import_module(f"neptune_memory.site.{name}")


def _harbor(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if "dir" not in _BUILT:
        out = tmp_path_factory.mktemp("harbor-demo")
        _site("corpus").generate(out, site="harbor")
        _site("build").build(out)
        _BUILT["dir"] = out
    return _BUILT["dir"]


def _object(item: Any) -> Any:
    obj = item.object
    if obj is None:  # an unknown reading has no value
        return None
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
        if call["tool"] == "confirm":  # H5: the operator says yes to the pending proposal
            pending = [i for i in answers[-1].items if i.state == "pending"]
            _site("confirm").confirm(site_dir, pending[0].fact_id, call["args"]["answer"])
            model = _site("views").load(site_dir)
            continue
        answers.append(getattr(query, call["tool"])(model, **call["args"]))
    return answers


@pytest.mark.xfail(strict=True, reason="Harbor's mapping file lands with the M2 gate")
@pytest.mark.parametrize("qid", sorted(QUESTIONS))
def test_site_harbor_gold_question(tmp_path_factory: pytest.TempPathFactory, qid: str) -> None:
    question = QUESTIONS[qid]
    answers = _ask(_harbor(tmp_path_factory), question)
    items = [item for answer in answers for item in answer.items]
    missing = [w for w in question["expect"] if not any(_matches(i, w) for i in items)]
    assert not missing, f"{qid} lacks {missing}"
    cited = {c.file for answer in answers for c in answer.sources.values()}
    assert set(question["cites"]) <= cited, f"{qid} cites {sorted(cited)}"


def test_site_harbor_gold_questions_are_well_formed(tmp_path: Path) -> None:
    assert len(QUESTIONS) == 5
    _site("corpus").generate(tmp_path, site="harbor")
    present = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    for question in QUESTIONS.values():
        assert question["calls"] and question["expect"] and question["cites"]
        assert set(question["cites"]) <= present, question["id"]
