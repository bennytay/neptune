"""The planner's prompt and output contract (ADR 0005 §4-§5).

The system prompt is a fixed template over the pinned vocabularies; its SHA-256, together with the
output schema's, is the lineage's ``template_sha256``. The output contract is the query JSON
Schema (ADR 0002 §6) reduced to the subset a constrained-decoding API accepts: constraints the
API cannot enforce are removed from what the model sees and are still enforced afterwards by the
query decoder, which is the authority on what a valid query is.
"""

# ruff: noqa: E501  (the system prompt is prose, wrapped for the model, not for the editor)
from __future__ import annotations

import hashlib
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from neptune.identity import canonical_json
from neptune_context.query.codec import clock_to_json, frame_to_json
from neptune_context.query.model import QUERY_VERSION, TextChannel, TextField
from neptune_context.query.plan.client import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, ModelRequest
from neptune_context.query.schema import SCHEMA_ID, query_schema
from neptune_context.query.validate import PREDICATES, SUBJECT_KINDS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune_context.query.model import AsOf
    from neptune_context.query.plan.planner import Defaults
    from neptune_context.query.plan.resolver import Mention

# Keywords a constrained-decoding API does not enforce; the query decoder enforces them instead.
_UNSUPPORTED: Final = frozenset(
    {
        "$id",
        "$schema",
        "title",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
_MAPS: Final = frozenset({"properties", "$defs"})


def _reduce(node: Any) -> Any:
    if isinstance(node, list):
        return [_reduce(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED:
            continue
        if key in _MAPS:
            out[key] = {name: _reduce(sub) for name, sub in value.items()}
        else:
            out["anyOf" if key == "oneOf" else key] = _reduce(value)
    if "const" in out and "type" not in out:
        out["type"] = "string" if isinstance(out["const"], str) else "integer"
    return out


def output_schema() -> JsonObject:
    """The query schema as the model's output contract: one object schema, no ``$ref`` at the
    root, ``oneOf`` as ``anyOf`` and only keywords the API enforces."""
    schema = query_schema()
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    root_name = str(schema["$ref"]).rsplit("/", 1)[1]
    root = defs[root_name]
    assert isinstance(root, dict)
    reduced: JsonObject = _reduce(
        {**root, "$defs": {k: v for k, v in defs.items() if k != root_name}}
    )
    return reduced


_SYSTEM: Final = """\
You translate one English question about robotics evidence into one typed Query, a JSON document.
You never answer the question: the answer comes later from a deterministic engine, and a person will
read your Query first. Reply with the Query document only.

How a Query reads: it selects entities (subjects), says on which snapshot (as_of), in which world-time
interval on which named clock (during), in which frame and unit (regions), and how far to follow claims
(graph). Clauses combine by conjunction. query_version is {version}. Every list member is always present.

Rules. Each one exists because a wrong guess here silently changes the answer.
1. Entities. The user message lists entities found in the question, each with its declared ids. Use only
   those declared ids, never invent one. If a mention is ambiguous, use the first candidate listed as a
   placeholder: the planner shows every candidate and blocks the query until a person chooses. If the
   question names something that is not listed, do not give it a declared_id: select by kind and put
   its words in the text clause.
2. Subject kinds are one of: {kinds}.
3. Graph predicates are one of: {predicates}. A graph clause needs an anchor (a declared subject or a
   site); hops are 1 to 4.
4. as_of is the value given in the user message, unless the question states a Ledger transaction number.
5. during only when the question states an interval, in integer ticks on one clock. The clock is one
   of the clocks listed in the user message (an entity's primary_clock or the default clock), or the
   default civil_time when the question names its timescale (UTC, TAI, GPS, POSIX or Unix). Never
   convert between clocks, never use local time, never add a clock bridge that is not listed. A
   relative time ("yesterday", "last week", "overnight") cannot be resolved without a clock reading:
   omit during.
6. regions only when the question states a place, in a frame listed in the user message and a length
   unit the question states (or the default unit). Never guess a frame, a unit or a transform.
7. include_inferred is the default in the user message unless the question asks for evidence only
   (false) or asks to include inferences (true). budget is the default unless the question states a limit.
8. text only for words to search for, never to restate a subject. Fields are {fields}; channels are
   {channels}; use both channels unless the question asks for one.
9. explain: a Why only for a claim id quoted in the question; a Diff for "what changed", between two
   transactions or two instants on the same clock.

Two examples (ids illustrative).
Question: Which runs does asset_tag:AMR-07 appear in? (entity listed as machine asset_tag:AMR-07)
{"as_of":"head","budget":{"items":100},"clock_bridges":[],"explain":[],"frame_bridges":[],"graph":{"direction":"in","hops":1,"predicates":["recorded_by"]},"include_inferred":false,"query_version":1,"regions":[],"subjects":[{"declared_id":"asset_tag:AMR-07","kind":"machine","same_as_depth":0}]}
Question: Find findings that mention a stalled thruster. (no entity listed)
{"as_of":"head","budget":{"items":100},"clock_bridges":[],"explain":[],"frame_bridges":[],"include_inferred":false,"query_version":1,"regions":[],"subjects":[],"text":{"channels":["lexical","vector"],"fields":["finding"],"text":"stalled thruster"}}
"""


def system_prompt() -> str:
    fills = {
        "{version}": str(QUERY_VERSION),
        "{kinds}": ", ".join(sorted(SUBJECT_KINDS)),
        "{predicates}": ", ".join(sorted(PREDICATES)),
        "{fields}": ", ".join(sorted(str(f) for f in TextField)),
        "{channels}": ", ".join(sorted(str(c) for c in TextChannel)),
    }
    text = _SYSTEM
    for placeholder, value in fills.items():
        text = text.replace(placeholder, value)
    return text


@cache
def template_sha256() -> str:
    """The lineage's template hash: the system prompt and the output schema it is bound to."""
    payload: JsonObject = {
        "schema": output_schema(),
        "schema_id": SCHEMA_ID,
        "system": system_prompt(),
    }
    return hashlib.sha256(canonical_json.dumps(payload)).hexdigest()


def user_message(text: str, as_of: AsOf, defaults: Defaults, mentions: Sequence[Mention]) -> str:
    """The per-question message: canonical JSON, so equal inputs are equal bytes."""
    given: dict[str, JsonValue] = {
        "budget": {k: v for k, v in _budget(defaults).items() if v is not None},
        "include_inferred": defaults.include_inferred,
    }
    if defaults.clock is not None:
        given["clock"] = clock_to_json(defaults.clock)
    if defaults.civil_time is not None:
        given["civil_time"] = clock_to_json(defaults.civil_time)
    if defaults.frames:
        given["frames"] = [frame_to_json(f) for f in defaults.frames]
    if defaults.length_unit is not None:
        given["length_unit"] = defaults.length_unit
    message: JsonObject = {
        "as_of": as_of,
        "defaults": given,
        "entities": [
            {
                "ambiguous": mention.ambiguous,
                "candidates": [c.to_json() for c in mention.candidates],
                "mention": mention.text,
            }
            for mention in mentions
        ],
        "question": text,
    }
    return canonical_json.dumps(message).decode("utf-8")


def _budget(defaults: Defaults) -> dict[str, int | None]:
    b = defaults.budget
    return {"items": b.items, "tokens": b.tokens, "bytes": b.bytes, "latency_ms": b.latency_ms}


def build_request(
    text: str,
    as_of: AsOf,
    defaults: Defaults,
    mentions: Sequence[Mention],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        system=system_prompt(),
        user=user_message(text, as_of, defaults, mentions),
        schema=output_schema(),
        max_tokens=max_tokens,
    )
