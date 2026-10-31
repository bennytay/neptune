"""The benchmark harness without a model: questions, the citation check, one scripted answer."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from neptune_context.bench import agent, arms, cites, questions, run


def test_bench_gold_set_has_every_gold_question() -> None:
    qs = questions.load("gold")
    assert len(qs) == 23
    g7 = next(q for q in qs if q.id == "G7")
    assert "before" not in g7.text
    assert [e["state"] for e in g7.expect] == ["pending"]


def _site(tmp_path: Path) -> Path:
    (tmp_path / "static").mkdir()
    (tmp_path / "feed").mkdir()
    (tmp_path / "static" / "work_orders.csv").write_text(
        "WO,Action\nWO-1,Cleared jam at PE3\n", encoding="utf-8"
    )
    (tmp_path / "feed" / "plc.jsonl").write_text(
        '{"t":"10:00","alarm":"OK"}\n{"t":"10:05","alarm":"JAM_PE3"}\n', encoding="utf-8"
    )
    return tmp_path


def test_bench_citations_are_checked_against_the_files(tmp_path: Path) -> None:
    site = _site(tmp_path)
    answer = (
        "CV-2 jammed.\n\nSources:\n"
        '[S1] feed/plc.jsonl line 2 "JAM_PE3"\n'
        '[S2] static/work_orders.csv row 1 col "Action" "Cleared jam at PE3"\n'
        '[S3] feed/plc.jsonl line 1 "nowhere"\n'
        '[S4] static/missing.csv row 1 "x"\n'
    )
    checked = cites.check(answer, site)
    assert [d["status"] for d in checked["details"]] == [
        "verified",
        "verified",
        "quote not found",
        "no such file",
    ]
    assert checked["details"][1]["where"] == 'row 1 col "Action"'


class _Stream:
    def __init__(self, message: Any) -> None:
        self.message = message

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.message


def _block(**fields: Any) -> Any:
    return SimpleNamespace(**fields, model_dump=lambda exclude_none=True: dict(fields))


def _message(stop: str, *blocks: Any) -> Any:
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model="fake")


class _Api:
    """Reads a file once, answers with a source, and grades every fact stated."""

    def __init__(self) -> None:
        self.turns = [
            _message(
                "tool_use", _block(type="tool_use", id="t1", name="grep", input={"pattern": "JAM"})
            ),
            _message(
                "end_turn",
                _block(type="text", text='Jammed.\nSources:\n[S1] feed/plc.jsonl line 2 "JAM_PE3"'),
            ),
        ]
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs: Any) -> _Stream:
        if "format" in kwargs["output_config"]:
            verdict = {"index": 0, "verdict": "stated", "made_up": False, "why": "says so"}
            return _Stream(
                _message("end_turn", _block(type="text", text=json.dumps({"items": [verdict]})))
            )
        return _Stream(self.turns.pop(0))


def test_bench_one_scripted_answer_is_recorded_and_summarised(tmp_path: Path) -> None:
    site = _site(tmp_path)
    question = questions.Question(
        id="Q1",
        site="t",
        text="What jammed?",
        expect=({"subject": "machine:cv-2", "predicate": "health", "object": "fault:JAM_PE3"},),
    )
    record = run.answer(_Api(), arms.Files(site), question, site, run.Settings(model="fake"))
    assert record["tool_calls"] == 1
    assert "plc.jsonl:2:" in record["transcript"][2]["content"][0]["content"]
    assert record["cites"]["verified"] == 1
    summary = run.summarise([record])["files"]
    assert (summary["correct"], summary["citations_verified"], summary["median_tokens"]) == (
        1,
        1,
        220,
    )
    assert agent.strip_images([{"type": "image", "source": {}}]) == [
        {"type": "image", "omitted": True}
    ]
