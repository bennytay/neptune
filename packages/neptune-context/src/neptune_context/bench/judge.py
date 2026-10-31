"""The judge: does an answer hold each expected fact, and did it make up a value?

The judge sees the question, the expected facts in plain words and the answer, never which arm
wrote it. Its verdicts are recorded with the run, so a summary never calls it again.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from neptune_context.bench.agent import _call

if TYPE_CHECKING:
    from neptune_context.bench.questions import Question

UNSURE = ("unknown", "stale", "pending")

SYSTEM = """You grade one answer about an industrial robot site against the facts it must hold.

Grade meaning, not wording: the answer may name things differently (machine:cv-2 is CV-2, \
person:okafor is Okafor, robot:amr-03 is AMR-03) and say values in other words. A fact about a \
finding (a starvation chain, a qualification gap, a stale source, a change before a fault, a \
regulation check) is stated when the answer makes the same point, whatever it calls it. \
Ignore the Sources section and how the answer is formatted.

For each expected fact give:
- verdict: "stated" (the answer says it), "missing" (it does not), or "contradicted" (it says \
something incompatible);
- made_up: true only when the fact's status is unknown, stale or pending and the answer instead \
gives a definite current value, or treats an unconfirmed identity as confirmed; else false;
- why: one short sentence."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["stated", "missing", "contradicted"]},
                    "made_up": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["index", "verdict", "made_up", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_STATUS = {
    "known": "the answer must state it",
    "stale": "its latest report is stale: the answer must say so (giving the old value with when "
    "it was reported is fine), not give it as the current value",
    "unknown": "the evidence does not say: the answer must say it is unknown, not give a value",
    "pending": "likely but unconfirmed: the answer must say it is probable but not confirmed",
}


def describe(item: dict[str, Any]) -> str:
    """One expected fact in plain words."""
    text = f"{item['subject']} · {item['predicate']}"
    if item.get("object") is not None:
        text += f" = {item['object']}"
    if item.get("rule"):
        text += f" (a {item['rule']} finding"
        text += f" involving {', '.join(item['mentions'])})" if item.get("mentions") else ")"
    state = item.get("state", "known")
    return f"{text}. Status: {state}: {_STATUS.get(state, _STATUS['known'])}."


def expected(question: Question) -> list[dict[str, Any]]:
    """The facts to grade, the question's rows (exact figures) as one more."""
    items = [dict(e) for e in question.expect]
    if question.rows:
        rows = "; ".join(", ".join(str(v) for v in row) for row in question.rows)
        items.append({"subject": "figures", "predicate": "match", "object": rows, "state": "known"})
    return items


def judge(api: Any, question: Question, answer: str, *, model: str) -> list[dict[str, Any]]:
    items = expected(question)
    facts = "\n".join(f"{n}. {describe(item)}" for n, item in enumerate(items))
    text = (
        f"Question: {question.text}\n\nExpected facts (by index):\n{facts}\n\n"
        f"Answer to grade:\n<answer>\n{answer}\n</answer>"
    )
    message = _call(
        api,
        model=model,
        max_tokens=8000,
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": text}],
    )
    body = next((b.text for b in message.content if b.type == "text"), "{}")
    verdicts = {v["index"]: v for v in json.loads(body).get("items", [])}
    out = []
    for n, item in enumerate(items):
        v = verdicts.get(n, {"verdict": "missing", "made_up": False, "why": "not graded"})
        out.append(
            {
                "fact": describe(item),
                "state": item.get("state", "known"),
                "verdict": v["verdict"],
                "made_up": bool(v["made_up"]) and item.get("state", "known") in UNSURE,
                "why": v["why"],
            }
        )
    return out
