"""M1's finish line: the gold questions asked of the generated live site (spec §10).

Each test generates the corpus, builds the fact log, runs its calls through the site query API
(spec §8) and checks that the expected items are in the answers and that the answers cite the
expected files.
"""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

GOLD = json.loads((Path(__file__).parent / "gold_questions.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in GOLD["questions"]}


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
        if call["tool"] == "confirm":  # G7: the operator says yes to the pending proposal
            pending = [i for i in answers[-1].items if i.state == "pending"]
            _site("confirm").confirm(site_dir, pending[0].fact_id, call["args"]["answer"])
            model = _site("views").load(site_dir)
            continue
        answers.append(getattr(query, call["tool"])(model, **call["args"]))
    return answers


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


def _log_bytes(site: Path) -> bytes:
    segments = sorted((site / ".neptune" / "log").glob("segment-*.jsonl"))
    return b"".join(segment.read_bytes() for segment in segments)


def test_site_gold_replay_same_corpus_same_model(tmp_path: Path) -> None:
    """Spec §1: the same corpus builds the same log, and folds to the same model, live or not."""
    site = tmp_path / "site"
    _site("corpus").generate(site)
    build, views = _site("build"), _site("views")

    build.build(site)
    first, first_model = _log_bytes(site), views.load(site)
    build.build(site)
    assert _log_bytes(site) == first
    assert views.load(site) == first_model

    build.replay(site, speed=1e9, sleep=lambda _: None)
    assert _log_bytes(site) == first
    live = views.SiteModel.empty()
    for entry in _site("log").read(site):  # one entry at a time, as a live reader folds it
        live = live.apply(entry)
    assert live == first_model
    assert first_model.pending(), "the amr-02/MR-1142 proposal is in the log"
