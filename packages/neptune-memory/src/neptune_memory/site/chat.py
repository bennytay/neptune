"""Ask the site (spec §16): a chat in the web view where Claude answers from the site model.

Claude gets Neptune's read-only query tools (``query.py``, ``insights.py``, the latest improvement
run) and calls them in a loop; each call is one step of the turn's trace. Tool results are
``render(answer)``: cited lines and a ``Sources:`` footer. Claude cites sources inline as
``file locator`` (``feed/plc_cv2.jsonl line 3423``), which the page opens in its source viewer.
Nothing here writes to the log. Conversations live in the server's memory; the key is the one
the person gave the improvement engine.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from neptune_memory.site import improve, insights, query
from neptune_memory.site.render import render

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from neptune_memory.site.query import Answer
    from neptune_memory.site.views import SiteModel

MODEL = improve.MODEL
EFFORT = "medium"
MAX_TOKENS = 16000
MAX_STEPS = 12
RESULT_CHARS = 12000
"""A tool result longer than this is cut, saying so: the model asks a narrower question."""

SYSTEM = """\
You answer questions about one robot site from Neptune's site model, using the tools. Every fact \
in the model is observed (a feed reported it), stated (a record, document or the mission says it) \
or inferred (a rule or a model concluded it), and each tool result ends with its sources.

- Use the tools before answering; never answer about the site from memory. Start with \
insights for "what is going on" and resolve for names.
- Cite every claim inline as the source's file and locator in parentheses, exactly as the tool \
gave them, e.g. (feed/plc_cv2.jsonl line 3423) or (static/roster/qualifications.csv row 2 \
col "Qualification").
- Say observed, stated or inferred where it matters. Inferred facts are correlations, not causes.
- When the model is silent, stale or ambiguous, say unknown, stale or ambiguous; never guess.
- Name things by their entity ref (robot:amr-01) so the page can link them.
- Be brief: a few sentences or a short list. Times are the site clock's, never converted.
"""

_STR = {"type": "string"}
_AT = {"type": "string", "description": "site-clock time YYYY-MM-DDTHH:MM:SS; default: now"}
_TIME = {"type": "string", "description": "site-clock time YYYY-MM-DDTHH:MM:SS"}


@dataclass(frozen=True)
class Tool:
    function: Callable[..., Answer]
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...] = ()

    def schema(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


def _improvements(model: SiteModel, site_dir: Path) -> Answer:
    answer = query.Answer(tool="improvements", at=model.clock(), as_of_seq=model.seq)
    every = improve.runs(site_dir, model)
    if not every:
        answer.gaps.append("no improvement run yet")
        return answer
    for item in every[-1].items:
        answer.add(item.fact, relation=f"rank {item.rank}, {item.move}, {item.status}")
    return answer


TOOLS: dict[str, Tool] = {
    "insights": Tool(
        insights.insights,
        "Neptune's inferred findings at a time (starvation chains, qualification gaps, stale "
        "sources, changes before faults, work orders checked against rules), cited.",
        {"at": _AT},
    ),
    "resolve": Tool(
        query.resolve,
        "Find entities by name or id (cv-2, okafor, MR-1142): the refs to pass to other tools.",
        {"name": _STR},
        ("name",),
    ),
    "state_at": Tool(
        query.state_at,
        "Everything known about one entity at a time, each known, stale, unknown or ambiguous.",
        {"entity": _STR, "at": _AT},
        ("entity",),
    ),
    "find": Tool(
        query.find,
        "Who or what has predicate = obj at a time (obj: an entity ref or a value as text).",
        {"predicate": _STR, "obj": _STR, "at": _AT},
        ("predicate", "obj"),
    ),
    "timeline": Tool(
        query.timeline,
        "One entity's history between two times: each value and the interval it held.",
        {
            "entity": _STR,
            "start": _TIME,
            "end": _TIME,
            "predicates": {"type": "array", "items": _STR},
        },
        ("entity", "start", "end"),
    ),
    "why": Tool(
        query.why,
        "One fact (fact:...) with its citations and what it replaced or was replaced by.",
        {"fact_id": _STR},
        ("fact_id",),
    ),
    "impact": Tool(
        query.impact,
        "What is downstream of an entity (what it feeds, what sits there, their tasks).",
        {"entity": _STR, "at": _AT},
        ("entity",),
    ),
    "gaps": Tool(
        query.gaps,
        "What the model does not know at a time: stale, unknown, ambiguous, unconfirmed.",
        {"at": _AT},
    ),
    "search_evidence": Tool(
        query.search_evidence,
        "Keyword search over the cited source text (log lines, CSV cells, document blocks).",
        {"text": _STR},
        ("text",),
    ),
    "sql": Tool(
        query.sql,
        "Read-only SQLite: facts(fact_id, subject, predicate, object, object_is_ref, unit, "
        "valid_from, valid_to, kind, source, model, pending, citations), intervals(fact_id, "
        'subject, predicate, object, start, "end", kind), seen(source, t), entities(ref, type, '
        'id, names), downtime(entity, state, start, "end", seconds, cause, work_order, fact_id), '
        "utilisation(robot, mode, seconds).",
        {"query": _STR},
        ("query",),
    ),
    "improvements": Tool(
        _improvements,
        "The latest improvement run: ranked improvements against the mission, with status.",
        {},
    ),
}


@dataclass
class Step:
    tool: str
    input: dict[str, Any]
    summary: str
    error: bool = False


def call(name: str, arguments: dict[str, Any], model: SiteModel, site_dir: Path) -> Step:
    """Run one tool; the step's summary is the rendered answer (cut at ``RESULT_CHARS``)."""
    tool = TOOLS.get(name)
    if tool is None:
        return Step(name, arguments, f"unknown tool {name!r}", error=True)
    try:
        if tool.function is _improvements:
            answer = _improvements(model, site_dir)
        else:
            answer = tool.function(model, **arguments)
    except Exception as error:
        return Step(name, arguments, f"{type(error).__name__}: {error}", error=True)
    text = render(answer)
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + "\n! cut here: ask a narrower question\n"
    return Step(name, arguments, text)


class Chat:
    """Conversations by id, each a list of API messages (assistant content kept as returned)."""

    def __init__(
        self,
        site_dir: Path,
        client: Callable[[], Any],
        *,
        model: str = MODEL,
        guard: threading.Lock | None = None,
    ) -> None:
        self.site_dir = site_dir
        self.model = model
        self._client = client
        self._talks: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._guard = guard or threading.Lock()
        """Held while tools read the live model, which the web view folds on other threads."""

    def clear(self, talk: str) -> None:
        self._talks.pop(talk, None)

    def ask(self, talk: str, text: str, model: Callable[[], SiteModel]) -> dict[str, Any]:
        """One turn: Claude calls tools until it answers. ``model()`` is the live site model."""
        if not text.strip():
            raise ValueError("ask something")
        with self._lock:
            messages = self._talks.setdefault(talk, [])
            client = self._client()
            messages.append({"role": "user", "content": text})
            steps: list[Step] = []
            reply = ""
            for _ in range(MAX_STEPS):
                message = client.beta.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    thinking={"type": "adaptive"},
                    output_config={"effort": EFFORT},
                    system=SYSTEM,
                    tools=[tool.schema(name) for name, tool in TOOLS.items()],
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": message.content})
                if message.stop_reason == "refusal":
                    reply = "Claude declined to answer this."
                    break
                uses = [b for b in message.content if b.type == "tool_use"]
                if message.stop_reason != "tool_use" or not uses:
                    reply = "".join(b.text for b in message.content if b.type == "text")
                    break
                results = []
                with self._guard:
                    site = model()
                    done = [call(u.name, dict(u.input), site, self.site_dir) for u in uses]
                for use, step in zip(uses, done, strict=True):
                    steps.append(step)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "content": step.summary,
                            "is_error": step.error,
                        }
                    )
                messages.append({"role": "user", "content": results})
            else:
                reply = (
                    f"stopped after {MAX_STEPS} steps without an answer; ask a narrower question"
                )
            return {
                "reply": reply,
                "trace": [
                    {
                        "tool": s.tool,
                        "input": json.dumps(s.input),
                        "head": _head(s),
                        "error": s.error,
                    }
                    for s in steps
                ],
            }


def _head(step: Step) -> str:
    """A glance at a tool result for the trace: its first item, cut short, and how many more."""
    lines = step.summary.splitlines()
    items = [line for line in lines if line.startswith("- ")]
    if step.error or not items:
        return (lines[0] if lines else "")[:160]
    first = items[0][2:]
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    return (first[:150] + "…" if len(first) > 150 else first) + more
