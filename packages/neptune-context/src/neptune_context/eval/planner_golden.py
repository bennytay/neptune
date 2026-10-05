"""The planner's golden set: English questions with expected Queries, replayed, graded, reported.

``tests/golden/planner/`` holds three files: ``world.json`` (the declared identifiers and caller
profiles the questions are planned against), ``cases.jsonl`` (one question per line with the
expected status, expected query and expected findings) and ``recordings.jsonl`` (the model's
responses, keyed by request hash; see ``plan.client``). ``run`` replays every case through
``plan`` with a ``ReplayClient``, so it reaches no network, and returns a ``Report``.

A case passes when the plan's status equals the expected one, its query's canonical JSON equals the
expected query (or both are absent), its blocking finding codes equal the expected ones and the
expected info codes are present. The report counts recordings by who made them, so a pass rate
over synthetic recordings is never read as a measure of a live model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from neptune_context.query.codec import to_json
from neptune_context.query.model import (
    Budget,
    Caller,
    CivilTime,
    Clock,
    ClockBridge,
    DomainClock,
    FrameBridge,
    FrameRef,
)
from neptune_context.query.plan import (
    DeclaredIdentifierIndex,
    Defaults,
    Entity,
    PlannedQuery,
    ReplayClient,
    load_recordings,
    plan,
)
if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from neptune.model.jsonvalue import JsonObject
    from neptune_context.query.plan.client import Recording
    from neptune_context.query.model import AsOf


def clock_from_json(value: Any) -> Clock:
    if value["kind"] == "domain":
        return DomainClock(value["domain_id"])
    r = value["resolution"]
    return CivilTime(value["timescale"], value["epoch"], Fraction(r["numerator"], r["denominator"]))


def frame_from_json(value: Any) -> FrameRef:
    return FrameRef(value["frame_id"], value["graph_id"])


def entity_from_json(value: Any) -> Entity:
    return Entity(
        kind=value["kind"],
        declared_id=value["declared_id"],
        label=value.get("label"),
        aliases=tuple(value.get("aliases", ())),
        primary_clock=clock_from_json(value["primary_clock"]) if "primary_clock" in value else None,
        frames=tuple(frame_from_json(f) for f in value.get("frames", ())),
        clock_bridges=tuple(
            ClockBridge(b["mapping_id"], clock_from_json(b["source"]), clock_from_json(b["target"]))
            for b in value.get("clock_bridges", ())
        ),
        frame_bridges=tuple(
            FrameBridge(b["transform_id"], frame_from_json(b["parent"]), frame_from_json(b["child"]))
            for b in value.get("frame_bridges", ())
        ),
    )


def defaults_from_json(value: Any) -> Defaults:
    b = value["budget"]
    civil = clock_from_json(value["civil_time"]) if "civil_time" in value else None
    if civil is not None and not isinstance(civil, CivilTime):
        raise ValueError("a profile's civil_time is a civil clock")
    return Defaults(
        caller=Caller(value["caller"]),
        budget=Budget(b["items"], b.get("tokens"), b.get("bytes"), b.get("latency_ms")),
        clock=clock_from_json(value["clock"]) if "clock" in value else None,
        civil_time=civil,
        frames=tuple(frame_from_json(f) for f in value.get("frames", ())),
        length_unit=value.get("length_unit"),
    )


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    as_of: AsOf
    profile: str
    status: str
    query: JsonObject | None
    blocking: tuple[str, ...]
    info: tuple[str, ...]
    source: str  # "model" (re-recorded live), "scripted" (a fixed bad response) or "none"


@dataclass(frozen=True)
class Outcome:
    case: Case
    planned: PlannedQuery
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Report:
    outcomes: tuple[Outcome, ...]
    live: int
    synthetic: int

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(o.passed for o in self.outcomes)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def summary(self) -> str:
        lines = [
            f"planner golden: {self.passed}/{self.total} passed ({self.pass_rate:.1%}); "
            f"recordings: {self.live} live, {self.synthetic} synthetic"
        ]
        lines += [f"  FAIL {o.case.id}: {'; '.join(o.problems)}" for o in self.outcomes if not o.passed]
        return "\n".join(lines)


def load_world(directory: Path) -> tuple[DeclaredIdentifierIndex, dict[str, Defaults]]:
    world = json.loads((directory / "world.json").read_text(encoding="utf-8"))
    index = DeclaredIdentifierIndex(entity_from_json(e) for e in world["entities"])
    return index, {name: defaults_from_json(p) for name, p in world["profiles"].items()}


def load_cases(directory: Path) -> list[Case]:
    cases = []
    for line in (directory / "cases.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        v = json.loads(line)
        e = v["expected"]
        cases.append(
            Case(
                v["id"], v["question"], v["as_of"], v["profile"], e["status"], e["query"],
                tuple(e["blocking"]), tuple(e["info"]), v["source"],
            )
        )
    return cases


def grade(case: Case, planned: PlannedQuery) -> tuple[str, ...]:
    problems: list[str] = []
    if str(planned.status) != case.status:
        problems.append(f"status {planned.status} != {case.status}")
    got = None if planned.query is None else to_json(planned.query)
    if got != case.query:
        problems.append("query differs from expected")
    blocking = tuple(sorted({str(f.code) for f in planned.blocking}))
    if blocking != tuple(sorted(case.blocking)):
        problems.append(f"blocking {blocking} != {tuple(sorted(case.blocking))}")
    missing = set(case.info) - {str(f.code) for f in planned.findings}
    if missing:
        problems.append(f"missing info findings {sorted(missing)}")
    return tuple(problems)


def run(directory: Path, recordings: Mapping[str, Recording] | None = None) -> Report:
    """Replay every case in ``directory`` and grade it. No network: a missing recording is a
    ``model_unavailable`` plan, which fails any case that did not expect it."""
    index, profiles = load_world(directory)
    recorded = load_recordings(directory / "recordings.jsonl") if recordings is None else recordings
    client = ReplayClient(recorded)
    outcomes = []
    for case in load_cases(directory):
        planned = plan(case.question, case.as_of, profiles[case.profile], resolver=index, client=client)
        outcomes.append(Outcome(case, planned, grade(case, planned)))
    live = sum(r.recorded_by == "live" for r in recorded.values())
    return Report(tuple(outcomes), live, len(recorded) - live)
