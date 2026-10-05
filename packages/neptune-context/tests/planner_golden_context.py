"""Builds the planner's golden set (MVL-185, ADR 0005 §7): world, questions, expected Queries.

The world is a small set of declared identifiers spread over every embodiment (AMR fleet and
manipulator cell from the archetype deployments, a humanoid, a drone, an ROV, a quadruped, an
autonomous truck, sensors, documents), each with the clock and frames its records declare. Every
case is a question, the plan expected for it and the model's response. Most responses are what a
good model returns (the expected query itself); the rest are the ways a model goes wrong (an
invented id, a guessed clock, an answer smuggled in, a refusal) and what the planner must do.

All recordings written here are ``synthetic``: they were authored with this file, not returned by a
model. ``packages/neptune-context/scripts/record_planner_golden.py`` replaces them with live
responses; the report always says how many of each there are.

``python packages/neptune-context/tests/planner_golden_context.py`` rewrites
``tests/golden/planner/``;
``test_planner_golden_context.py`` fails when the files drift from what this module builds.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from neptune_context.eval.planner_golden import defaults_from_json, entity_from_json
from neptune_context.query import validate
from neptune_context.query.codec import clock_to_json, to_json
from neptune_context.query.model import (
    HEAD,
    AsOf,
    Box,
    Budget,
    CivilTime,
    ClockBridge,
    Diff,
    Direction,
    DomainClock,
    During,
    FrameBridge,
    FrameRef,
    FrameRegion,
    GraphClause,
    Instant,
    Query,
    SiteScope,
    Sphere,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    Why,
)
from neptune_context.query.plan import (
    DeclaredIdentifierIndex,
    Recording,
    build_request,
    dump_recordings,
)

GOLDEN: Final = Path(__file__).resolve().parent / "golden" / "planner"
STOPS: Final = ("end", "max_tokens", "refusal", "other")


def rec(name: str) -> str:
    return "rec:sha256:" + hashlib.sha256(name.encode()).hexdigest()


def claim(name: str) -> str:
    return "claim:sha256:" + hashlib.sha256(name.encode()).hexdigest()


def ns(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """POSIX nanoseconds of a UTC date-time (the model's arithmetic, done once, correctly)."""
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()) * 10**9


UTC_NS: Final = CivilTime("utc", "unix", Fraction(1, 10**9))
OPS: Final = DomainClock(rec("fleet-ops-clock"))
AMR07_CLOCK: Final = DomainClock(rec("amr-07-clock"))
ARM_CLOCK: Final = DomainClock(rec("arm-3a-clock"))
UAV_CLOCK: Final = DomainClock(rec("uav-21-clock"))
ROV_CLOCK: Final = DomainClock(rec("rov-3-clock"))
QUAD_CLOCK: Final = DomainClock(rec("quad-12-clock"))
TRUCK_CLOCK: Final = DomainClock(rec("truck-clock"))
AMR07_UTC: Final = ClockBridge(rec("amr-07-clock-to-utc"), AMR07_CLOCK, UTC_NS)
TRUCK_UTC: Final = ClockBridge(rec("truck-clock-to-utc"), TRUCK_CLOCK, UTC_NS)

ARM_GRAPH: Final = rec("arm-3a-frames")
BASE: Final = FrameRef("base_link", ARM_GRAPH)
TOOL0: Final = FrameRef("tool0", ARM_GRAPH)
UAV_GRAPH: Final = rec("uav-21-frames")
UAV_MAP: Final = FrameRef("map", UAV_GRAPH)
UAV_ODOM: Final = FrameRef("odom", UAV_GRAPH)
UAV_BRIDGE: Final = FrameBridge(rec("uav-21-map-to-odom"), UAV_MAP, UAV_ODOM)
AMR07_GRAPH: Final = rec("amr-07-frames")
SITE_MAP: Final = FrameRef("site_map", rec("s007-site-frames"))


def _e(
    kind: str,
    declared_id: str,
    aliases: tuple[str, ...] = (),
    clock: Any = None,
    frames: tuple[FrameRef, ...] = (),
    clock_bridges: tuple[ClockBridge, ...] = (),
    frame_bridges: tuple[FrameBridge, ...] = (),
) -> dict[str, Any]:
    out: dict[str, Any] = {"declared_id": declared_id, "kind": kind}
    if aliases:
        out["aliases"] = list(aliases)
    if clock is not None:
        out["primary_clock"] = clock_to_json(clock)
    if frames:
        out["frames"] = [{"frame_id": f.frame_id, "graph_id": f.graph_id} for f in frames]
    if clock_bridges:
        out["clock_bridges"] = [
            {
                "mapping_id": b.mapping_id,
                "source": clock_to_json(b.source),
                "target": clock_to_json(b.target),
            }
            for b in clock_bridges
        ]
    if frame_bridges:
        out["frame_bridges"] = [
            {
                "transform_id": b.transform_id,
                "parent": {"frame_id": b.parent.frame_id, "graph_id": b.parent.graph_id},
                "child": {"frame_id": b.child.frame_id, "graph_id": b.child.graph_id},
            }
            for b in frame_bridges
        ]
    return out


ENTITIES: Final = [
    # Warehouse AMR fleet (archetype deployment): wheeled mobile bases, logs on POSIX ns UTC.
    _e("machine", "asset_tag:AMR-05", ("tug five",), UTC_NS),
    _e("machine", "asset_tag:AMR-06", (), UTC_NS),
    _e(
        "machine",
        "asset_tag:AMR-07",
        ("the north tug",),
        AMR07_CLOCK,
        (FrameRef("map", AMR07_GRAPH), FrameRef("base_footprint", AMR07_GRAPH)),
        (AMR07_UTC,),
    ),
    _e("machine", "asset_tag:AMR-08", (), UTC_NS),
    _e("machine", "asset_tag:AMR-09", (), UTC_NS),
    _e("asset", "cmms_asset:AMR-09"),  # the CMMS asset record shares the robot's tag: ambiguous
    _e("fleet", "fleet_registry:amr-north", ("the north fleet",)),
    _e("site", "site_registry:S-007", ("the north warehouse",)),
    _e("zone", "zone_map:DOCK-1", ("dock 1",)),
    _e("zone", "zone_map:PICK-A", ("pick area a",)),
    # Manipulator cell (archetype deployment).
    _e("machine", "asset_tag:ARM-3A", ("the cell arm",), ARM_CLOCK, (BASE, TOOL0)),
    _e("site", "cell_registry:CELL-3"),
    _e("document", "document:risk-assessment-cell3", ("the cell risk assessment",)),
    _e("deployment", "deployment_log:cell3-2026-q1"),
    # Humanoid on a plant floor (no declared primary clock).
    _e("machine", "asset_tag:hx-02", ("the humanoid",)),
    _e("site", "site_registry:plant-7"),
    _e("zone", "zone_map:cell-a"),
    _e("zone", "zone_map:aisle-3"),
    _e("person", "person:j.alvarez", ("J. Alvarez",)),
    _e("task", "task_board:inspect-pump-deck"),
    # Aerial.
    _e(
        "machine",
        "airframe:uav-21",
        ("the drone",),
        UAV_CLOCK,
        (UAV_MAP, UAV_ODOM),
        frame_bridges=(UAV_BRIDGE,),
    ),
    # Marine.
    _e("machine", "asset_tag:rov-3", ("the rov",), ROV_CLOCK),
    _e("run", "run_log:rov-3-dive-41", (), ROV_CLOCK),
    _e("fleet", "fleet_registry:rov-pool", ("the rov pool",)),
    _e("episode", "episode_log:rov-3-dive-41-ep2"),
    # Legged.
    _e("machine", "asset_tag:quad-12", ("the quadruped",), QUAD_CLOCK),
    _e("run", "run_log:quad-12-2026-09-14-0812", (), QUAD_CLOCK),
    # Autonomous vehicle.
    _e(
        "machine",
        "vin:5yj3e1ea7kf317000",
        ("the truck",),
        TRUCK_CLOCK,
        clock_bridges=(TRUCK_UTC,),
    ),
    # Sensors, software, models, policy.
    _e("sensor", "sensor_serial:imu-0042"),
    _e("software_version", "software_version:nav2-1.2.0"),
    _e("configuration", "config_rev:nav2-params-12"),
    _e("model_version", "model_registry:vla-pick-v7"),
    _e("policy", "policy_register:speed-limit-zone-a", ("the zone a speed limit",)),
]

PROFILES: Final[dict[str, Any]] = {
    "agent": {"caller": "agent", "budget": {"items": 100}},
    "policy": {
        "caller": "policy",
        "budget": {"items": 32, "tokens": 2048, "latency_ms": 50},
    },
    "agent_utc": {
        "caller": "agent",
        "budget": {"items": 100},
        "civil_time": clock_to_json(UTC_NS),
    },
    "agent_ops": {"caller": "agent", "budget": {"items": 100}, "clock": clock_to_json(OPS)},
    "agent_geo": {
        "caller": "agent",
        "budget": {"items": 100},
        "frames": [{"frame_id": SITE_MAP.frame_id, "graph_id": SITE_MAP.graph_id}],
        "length_unit": "m",
    },
}


def world_json() -> dict[str, Any]:
    return {"entities": ENTITIES, "profiles": PROFILES}


# --- Cases ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    question: str
    expected: Query | None
    status: str
    blocking: tuple[str, ...]
    info: tuple[str, ...]
    profile: str
    as_of: AsOf
    output: str | None  # the model's text; ``None`` is an empty response
    stop: str
    source: str  # "model": re-recorded live; "scripted": a fixed bad response; "none": unrecorded


SPECS: list[Spec] = []


def q(
    *,
    inf: bool = True,
    items: int = 100,
    budget: Budget | None = None,
    subjects: tuple[Subject, ...] = (),
    **kw: Any,
) -> Query:
    return Query(
        include_inferred=inf,
        budget=budget or Budget(items=items),
        subjects=frozenset(subjects),
        **kw,
    )


def say(query: Query) -> str:
    """What a good model returns for ``query``: its JSON document."""
    return json.dumps(to_json(query), sort_keys=True)


def case(
    question: str,
    expected: Query | None,
    *,
    profile: str = "agent",
    as_of: AsOf = HEAD,
    status: str = "ready",
    blocking: tuple[str, ...] = (),
    info: tuple[str, ...] = (),
    output: Query | str | tuple[()] | None = (),
    stop: str = "end",
    recorded: bool = True,
) -> None:
    """``output`` defaults to the expected query as the model's answer."""
    text: str | None
    if output == ():
        assert expected is not None
        text = say(expected)
    elif isinstance(output, Query):
        text = say(output)
    else:
        text = output  # type: ignore[assignment]
    source = "none" if not recorded else ("model" if output == () else "scripted")
    SPECS.append(
        Spec(question, expected, status, blocking, info, profile, as_of, text, stop, source)
    )


def m(declared_id: str, kind: str = "machine", depth: int = 0) -> Subject:
    return Subject(kind, declared_id, depth)


def g(*predicates: str, hops: int = 1, d: Direction = Direction.OUT) -> GraphClause:
    return GraphClause(frozenset(predicates), hops, d)


IN, OUT, BOTH = Direction.IN, Direction.OUT, Direction.BOTH
AMR05, AMR06, AMR07, AMR08 = (m(f"asset_tag:AMR-0{n}") for n in (5, 6, 7, 8))
ARM = m("asset_tag:ARM-3A")
UAV = m("airframe:uav-21")
ROV = m("asset_tag:rov-3")
QUAD = m("asset_tag:quad-12")
TRUCK = m("vin:5yj3e1ea7kf317000")
HX = m("asset_tag:hx-02")
S007 = "site_registry:S-007"
PLANT7 = "site_registry:plant-7"


def build_cases() -> None:
    # Subjects and graph.
    case("Which runs does AMR-07 appear in?", q(subjects=(AMR07,), graph=g("recorded_by", d=IN)))
    case(
        "What configuration is AMR-08 running?", q(subjects=(AMR08,), graph=g("has_configuration"))
    )
    case("What software version does AMR-05 run?", q(subjects=(AMR05,), graph=g("runs_software")))
    case("Which fleet is AMR-06 a member of?", q(subjects=(AMR06,), graph=g("member_of_fleet")))
    case(
        "Show everything within two hops of AMR-07.",
        q(subjects=(AMR07,), graph=GraphClause(None, 2, BOTH)),
    )
    case("Which models does the cell arm run?", q(subjects=(ARM,), graph=g("runs_model")))
    case("What calibrations does ARM-3A have?", q(subjects=(ARM,), graph=g("has_calibration")))
    case("What is ARM-3A mounted on?", q(subjects=(ARM,), graph=g("mounted_on")))
    case(
        "Which machines are members of the north fleet?",
        q(subjects=(m("fleet_registry:amr-north", "fleet"),), graph=g("member_of_fleet", d=IN)),
    )
    case(
        "Which software does the north fleet run, and what is configured on it?",
        q(
            subjects=(m("fleet_registry:amr-north", "fleet"),),
            graph=g("runs_software", "has_configuration", hops=2, d=BOTH),
        ),
    )
    case(
        "What does the rov pool contain?",
        q(subjects=(m("fleet_registry:rov-pool", "fleet"),), graph=g("member_of_fleet", d=IN)),
    )
    case("Which runs did rov-3 record?", q(subjects=(ROV,), graph=g("recorded_by", d=IN)))
    case(
        "What software and model does the quadruped run?",
        q(subjects=(QUAD,), graph=g("runs_software", "runs_model")),
    )
    case("Who operates hx-02?", q(subjects=(HX,), graph=g("operated_by")))
    case("What task is the humanoid executing?", q(subjects=(HX,), graph=g("executes_task")))
    case("What is the rated payload of AMR-06?", q(subjects=(AMR06,), graph=g("rated_payload")))
    case(
        "What maintenance state is the truck vin:5yj3e1ea7kf317000 in?",
        q(subjects=(TRUCK,), graph=g("maintenance_state")),
    )
    case("Summarise the drone.", q(subjects=(UAV,), graph=g("has_summary")))
    case("Where is the cell arm deployed?", q(subjects=(ARM,), graph=g("deployed_at")))
    case(
        "Which machines does the zone a speed limit govern?",
        q(
            subjects=(m("policy_register:speed-limit-zone-a", "policy"),),
            graph=g("governed_by", d=IN),
        ),
    )
    # Time on a named clock.
    case(
        "What did AMR-05 record between 2026-09-14 02:00 and 2026-09-14 06:00 UTC?",
        q(
            subjects=(AMR05,),
            during=During(UTC_NS, ns(2026, 9, 14, 2), ns(2026, 9, 14, 6)),
            graph=g("recorded_by", d=IN),
        ),
        profile="agent_utc",
    )
    case(
        "Which runs of AMR-08 were recorded from 2026-09-14 08:00 to 2026-09-14 09:00 UTC?",
        q(
            subjects=(AMR08,),
            during=During(UTC_NS, ns(2026, 9, 14, 8), ns(2026, 9, 14, 9)),
            graph=g("recorded_by", d=IN),
        ),
        profile="agent_utc",
    )
    case(
        "Between ticks 1000 and 2000 on its own clock, what did AMR-07 record?",
        q(subjects=(AMR07,), during=During(AMR07_CLOCK, 1000, 2000), graph=g("recorded_by", d=IN)),
    )
    case(
        "Which calibrations did the cell arm hold from tick 7200000000 on its own clock?",
        q(
            subjects=(ARM,),
            during=During(ARM_CLOCK, 7_200_000_000, None),
            graph=g("has_calibration"),
        ),
    )
    case(
        "What did the drone record between 2026-09-14 10:00 and 2026-09-14 10:05 UTC?",
        q(
            subjects=(UAV,),
            during=During(UTC_NS, ns(2026, 9, 14, 10), ns(2026, 9, 14, 10, 5)),
            graph=g("recorded_by", d=IN),
        ),
        profile="agent_utc",
    )
    case(
        "For quad-12, take ticks 0 to 2700000000000 on its own clock: what configuration held?",
        q(
            subjects=(QUAD,),
            during=During(QUAD_CLOCK, 0, 2_700_000_000_000),
            graph=g("has_configuration"),
        ),
    )
    case(
        "Where was the humanoid between 2026-08-01 09:00 and 2026-08-01 10:00 UTC?",
        q(
            subjects=(HX,),
            during=During(UTC_NS, ns(2026, 8, 1, 9), ns(2026, 8, 1, 10)),
            graph=g("located_at"),
        ),
        profile="agent_utc",
    )
    case(
        "What was the rov doing between ticks 500 and 900?",
        q(subjects=(ROV,), during=During(ROV_CLOCK, 500, 900), graph=g("executes_task")),
        info=("clock_defaulted_to_primary",),
    )
    case(
        "Everything about the drone from tick 3600000000 on its own clock.",
        q(
            subjects=(UAV,),
            during=During(UAV_CLOCK, 3_600_000_000, None),
            graph=GraphClause(None, 1, BOTH),
        ),
    )
    case(
        "Which findings were recorded for sensor imu-0042 between ticks 10 and 20?",
        q(
            subjects=(m("sensor_serial:imu-0042", "sensor"),),
            during=During(OPS, 10, 20),
            graph=g("evidenced_by"),
        ),
        profile="agent_ops",
        info=("clock_defaulted_to_caller",),
    )
    # as_of: the snapshot is the caller's unless the question states a transaction.
    case(
        "What did we know about AMR-07 as of transaction 1842?",
        q(subjects=(AMR07,), as_of=1842, graph=GraphClause(None, 1, BOTH)),
    )
    case(
        "Show the configuration of ARM-3A as of transaction 900.",
        q(subjects=(ARM,), as_of=900, graph=g("has_configuration")),
    )
    case(
        "What did the north fleet look like as of transaction 15?",
        q(
            subjects=(m("fleet_registry:amr-north", "fleet"),),
            as_of=15,
            graph=g("member_of_fleet", d=IN),
        ),
    )
    case(
        "Which runs did AMR-06 record?",
        q(subjects=(AMR06,), as_of=1842, graph=g("recorded_by", d=IN)),
        as_of=1842,
    )
    case(
        "Which runs did AMR-06 record, as the snapshot has them?",
        q(subjects=(AMR06,), as_of=1842, graph=g("recorded_by", d=IN)),
        as_of=1842,
        output=q(subjects=(AMR06,), graph=g("recorded_by", d=IN)),  # the model said head
        info=("as_of_overridden",),
    )
    # Space.
    r_map = FrameRegion(UAV_MAP, "m", Sphere((120.0, 48.5, 30.0), 15.0))
    r_odom = FrameRegion(UAV_ODOM, "m", Box((-5.0, -5.0, -2.0), (5.0, 5.0, 2.0)))
    case(
        "Which obstacles are within 15 m of (120, 48.5, 30) in the map frame around the drone?",
        q(subjects=(UAV,), regions=frozenset({r_map})),
    )
    case(
        "Around the drone, what is in the odom frame box from (-5, -5, -2) to (5, 5, 2) metres?",
        q(subjects=(UAV,), regions=frozenset({r_odom})),
    )
    case(
        "Obstacles for the drone: a 15 m sphere at (120, 48.5, 30) in map, and the odom box "
        "(-5,-5,-2) to (5,5,2) m.",
        q(
            subjects=(UAV,),
            regions=frozenset({r_map, r_odom}),
            frame_bridges=frozenset({UAV_BRIDGE}),
        ),
    )
    case(
        "What is in the workspace of ARM-3A: the base_link box from (-0.2, -0.6, 0) "
        "to (0.9, 0.6, 1.1) metres?",
        q(
            subjects=(ARM,),
            regions=frozenset({FrameRegion(BASE, "m", Box((-0.2, -0.6, 0.0), (0.9, 0.6, 1.1)))}),
        ),
    )
    case(
        "Anything within 2 m of (0, 0, 0.1) in the tool0 frame of the cell arm?",
        q(
            subjects=(ARM,),
            regions=frozenset({FrameRegion(TOOL0, "m", Sphere((0.0, 0.0, 0.1), 2.0))}),
        ),
    )
    case(
        "What is within 30 metres of (10, 20, 0) in the site_map frame?",
        q(regions=frozenset({FrameRegion(SITE_MAP, "m", Sphere((10.0, 20.0, 0.0), 30.0))})),
        profile="agent_geo",
    )
    case(
        "What is within 50 ft of (1, 2, 3) in the site_map frame?",
        q(regions=frozenset({FrameRegion(SITE_MAP, "ft", Sphere((1.0, 2.0, 3.0), 50.0))})),
        profile="agent_geo",
    )
    lidar = FrameRef("lidar_link", rec("amr-07-lidar-frames"))
    case(
        "What is within 5 m of (0, 0, 0) in the lidar_link frame of AMR-07?",
        q(subjects=(AMR07,)),
        status="needs_input",
        blocking=("frame_not_declared",),
        output=q(
            subjects=(AMR07,),
            regions=frozenset({FrameRegion(lidar, "m", Sphere((0.0, 0.0, 0.0), 5.0))}),
        ),
    )
    case(
        "What is within 5 of (0, 0, 0) in the base_link frame of ARM-3A?",
        q(subjects=(ARM,)),
        status="needs_input",
        blocking=("unit_not_stated",),
        output=q(
            subjects=(ARM,),
            regions=frozenset({FrameRegion(BASE, "m", Sphere((0.0, 0.0, 0.0), 5.0))}),
        ),
    )
    fake_bridge = FrameBridge(rec("invented-transform"), UAV_MAP, UAV_ODOM)
    case(
        "Obstacles for the drone: 15 m sphere at (120, 48.5, 30) in map and the odom box "
        "(-5,-5,-2) to (5,5,2) m, related by whatever transform applies.",
        None,
        status="needs_input",
        blocking=("bridge_not_declared", "draft_withdrawn"),
        output=q(
            subjects=(UAV,),
            regions=frozenset({r_map, r_odom}),
            frame_bridges=frozenset({fake_bridge}),
        ),
    )
    # Text.
    finding = frozenset({TextField.FINDING})
    both = frozenset({TextChannel.LEXICAL, TextChannel.VECTOR})
    case(
        "Find findings that mention a stalled thruster.",
        q(text=TextClause("stalled thruster", finding, both)),
    )
    case(
        "Which documents talk about the near miss at CELL-3?",
        q(
            site=SiteScope("cell_registry:CELL-3"),
            text=TextClause(
                "near miss",
                frozenset({TextField.DOCUMENT, TextField.CLAIM_TEXT}),
                both,
            ),
        ),
    )
    case(
        "Search the runs for thruster stall during descent.",
        q(
            subjects=(Subject("run"),),
            text=TextClause(
                "thruster stall during descent",
                frozenset({TextField.FINDING, TextField.DOCUMENT, TextField.CLAIM_TEXT}),
                both,
            ),
        ),
    )
    case(
        "Lexical search only for 'firmware change' in documents.",
        q(
            text=TextClause(
                "firmware change", frozenset({TextField.DOCUMENT}), frozenset({TextChannel.LEXICAL})
            )
        ),
    )
    case(
        "Semantic search only for 'unexpected stop near dock' in findings.",
        q(text=TextClause("unexpected stop near dock", finding, frozenset({TextChannel.VECTOR}))),
    )
    case(
        "Search records for requalification.",
        q(text=TextClause("requalification", frozenset({TextField.RECORD}), both)),
    )
    case(
        "Find declared ids containing amr, lexical only.",
        q(
            text=TextClause(
                "amr", frozenset({TextField.DECLARED_ID}), frozenset({TextChannel.LEXICAL})
            )
        ),
    )
    case(
        "Which claims say the gripper was replaced?",
        q(text=TextClause("gripper was replaced", frozenset({TextField.CLAIM_TEXT}), both)),
    )
    case(
        "Mentions of battery brownout in the rov pool.",
        q(
            subjects=(m("fleet_registry:rov-pool", "fleet"),),
            text=TextClause(
                "battery brownout", frozenset({TextField.FINDING, TextField.CLAIM_TEXT}), both
            ),
        ),
    )
    case(
        "Documents mentioning a risk assessment for the cell arm.",
        q(
            subjects=(ARM,),
            text=TextClause("risk assessment", frozenset({TextField.DOCUMENT}), both),
        ),
    )
    # Explain.
    c1, c2 = claim("amr-07-firmware"), claim("arm-3a-recal")
    case(f"Why does memory hold {c1}?", q(explain=(Why(c1),)))
    case(
        f"Why does memory hold {c1} and {c2}, as of transaction 2051?",
        q(as_of=2051, explain=(Why(c1), Why(c2))),
    )
    case(
        "What changed about AMR-07 between transactions 1500 and 1842?",
        q(subjects=(AMR07,), explain=(Diff(AMR07, 1500, 1842),)),
    )
    case(
        "What changed about ARM-3A between transaction 10 and 20, as of transaction 20?",
        q(subjects=(ARM,), as_of=20, explain=(Diff(ARM, 10, 20),)),
    )
    case(
        "What changed about the drone between ticks 100 and 200 on its own clock?",
        q(
            subjects=(UAV,),
            explain=(Diff(UAV, Instant(UAV_CLOCK, 100), Instant(UAV_CLOCK, 200)),),
        ),
    )
    case(
        "What held about the truck between 2026-08-30 09:00 UTC and tick 912345678000 "
        "on its own clock?",
        q(
            subjects=(TRUCK,),
            clock_bridges=frozenset({TRUCK_UTC}),
            explain=(
                Diff(
                    TRUCK,
                    Instant(UTC_NS, ns(2026, 8, 30, 9)),
                    Instant(TRUCK_CLOCK, 912_345_678_000),
                ),
            ),
        ),
        profile="agent_utc",
    )
    case(
        f"Explain {c1}, and what changed about hx-02 between transactions 5 and 9.",
        q(subjects=(HX,), explain=(Why(c1), Diff(HX, 5, 9))),
    )
    case(
        f"The note says AMR-06 was recalibrated ({c2}). Why does memory hold that?",
        q(subjects=(AMR06,), explain=(Why(c2),)),
        info=("include_inferred_default",),
    )
    case(
        "What changed about rov-3 between transactions 40 and 41?",
        q(subjects=(ROV,), explain=(Diff(ROV, 40, 41),)),
    )
    # Site and zones.
    case(
        "Which assets are in zones DOCK-1 and PICK-A of S-007?",
        q(
            subjects=(Subject("asset"),),
            site=SiteScope(S007, frozenset({"zone_map:DOCK-1", "zone_map:PICK-A"})),
            graph=g("located_at", "zone_of", hops=2, d=IN),
        ),
    )
    case(
        "What is at site plant-7?",
        q(site=SiteScope(PLANT7), graph=g("located_at", d=IN)),
    )
    case(
        "Which machines are authorised in cell-a at plant-7?",
        q(
            subjects=(Subject("machine"),),
            site=SiteScope(PLANT7, frozenset({"zone_map:cell-a"})),
            graph=g("governed_by", "located_at", hops=2, d=BOTH),
        ),
    )
    case(
        "Where was the humanoid located in aisle-3 of plant-7?",
        q(
            subjects=(HX,),
            site=SiteScope(PLANT7, frozenset({"zone_map:aisle-3"})),
            graph=g("located_at"),
        ),
    )
    case("Which zones does S-007 have?", q(site=SiteScope(S007), graph=g("zone_of", d=IN)))
    case(
        "Which zone is AMR-07 in at the north warehouse?",
        q(subjects=(AMR07,), site=SiteScope(S007), graph=g("located_at")),
    )
    case(
        "What is located in dock 1?",
        q(subjects=(m("zone_map:DOCK-1", "zone"),), graph=g("located_at", d=IN)),
    )
    case(
        "Which policies govern pick area a?",
        q(subjects=(m("zone_map:PICK-A", "zone"),), graph=g("governed_by", d=IN)),
    )
    # include_inferred and budget.
    case(
        "Evidence only: which configuration does AMR-08 run?",
        q(inf=False, subjects=(AMR08,), graph=g("has_configuration")),
    )
    case(
        "Include inferred identity candidates when listing the runs of AMR-05.",
        q(subjects=(AMR05,), graph=g("recorded_by", d=IN)),
    )
    case(
        "What calibrations does ARM-3A have?",
        q(
            inf=False,
            budget=Budget(32, 2048, None, 50),
            subjects=(ARM,),
            graph=g("has_calibration"),
        ),
        profile="policy",
        info=("include_inferred_default", "budget_default"),
    )
    case(
        "Which software does AMR-06 run?",
        q(
            inf=False,
            budget=Budget(32, 2048, None, 50),
            subjects=(AMR06,),
            graph=g("runs_software"),
        ),
        profile="policy",
    )
    case(
        "List the runs of AMR-05, excluding inferred claims.",
        q(inf=False, subjects=(AMR05,), graph=g("recorded_by", d=IN)),
    )
    case(
        "Allow inferred claims: which fleet is AMR-08 in?",
        q(
            inf=True,
            budget=Budget(32, 2048, None, 50),
            subjects=(AMR08,),
            graph=g("member_of_fleet"),
        ),
        profile="policy",
    )
    case(
        "Give me at most 20 items about the configuration of AMR-07.",
        q(budget=Budget(20), subjects=(AMR07,), graph=g("has_configuration")),
    )
    case(
        "List the runs of AMR-05 within 2000 tokens and 50 items.",
        q(budget=Budget(50, tokens=2000), subjects=(AMR05,), graph=g("recorded_by", d=IN)),
    )
    case(
        "Within 50 milliseconds, the calibrations of the cell arm, up to 10 items.",
        q(budget=Budget(10, latency_ms=50), subjects=(ARM,), graph=g("has_calibration")),
    )
    case(
        "Limit 8000000 bytes and 300 items: which assets are in dock 1?",
        q(
            budget=Budget(300, bytes=8_000_000),
            subjects=(Subject("asset"),),
            site=SiteScope(S007, frozenset({"zone_map:DOCK-1"})),
            graph=g("located_at", d=IN),
        ),
    )
    # Ambiguity: the model uses the first candidate as a placeholder; the plan blocks.
    amb = m("cmms_asset:AMR-09", "asset")
    amb_machine = m("asset_tag:AMR-09")
    case(
        "Which runs does AMR-09 appear in?",
        q(subjects=(amb,), graph=g("recorded_by", d=IN)),
        status="needs_choice",
        blocking=("ambiguous_entity",),
    )
    case(
        "What configuration does AMR-09 run?",
        q(subjects=(amb,), graph=g("has_configuration")),
        status="needs_choice",
        blocking=("ambiguous_entity",),
    )
    case(
        "What did AMR-09 record between 2026-09-14 02:00 and 2026-09-14 06:00 UTC?",
        q(
            subjects=(amb,),
            during=During(UTC_NS, ns(2026, 9, 14, 2), ns(2026, 9, 14, 6)),
            graph=g("recorded_by", d=IN),
        ),
        profile="agent_utc",
        status="needs_choice",
        blocking=("ambiguous_entity",),
    )
    case(
        "Is the config on AMR-09 stale?",
        q(subjects=(amb_machine,), graph=g("has_configuration")),
        status="needs_choice",
        blocking=("ambiguous_entity",),  # the model took the second candidate: still flagged
    )
    # An entity the Ledger does not declare: a good model selects by kind and searches the words.
    case(
        "What does the forklift fk-9 run?",
        q(
            subjects=(Subject("machine"),),
            text=TextClause("forklift fk-9", frozenset({TextField.CLAIM_TEXT}), both),
        ),
    )
    # The ways a model goes wrong.
    case(
        "What did AMR-07 do overnight?",
        q(subjects=(AMR07,), graph=g("recorded_by", d=IN)),
        status="needs_input",
        blocking=("time_phrase_unresolved",),
        output=q(
            subjects=(AMR07,),
            during=During(AMR07_CLOCK, 82_800_000_000, 111_600_000_000),
            graph=g("recorded_by", d=IN),
        ),
    )
    case(
        "What was imu-0042 doing at 2026-09-14 08:12?",
        q(subjects=(m("sensor_serial:imu-0042", "sensor"),)),
        status="needs_input",
        blocking=("clock_not_stated",),
        output=q(
            subjects=(m("sensor_serial:imu-0042", "sensor"),),
            during=During(UTC_NS, ns(2026, 9, 14, 8, 12), ns(2026, 9, 14, 8, 13)),
        ),
    )
    case(
        "Between ticks 10 and 20 on the drone clock, what did the quadruped see?",
        q(subjects=(QUAD,)),
        status="needs_input",
        blocking=("clock_not_declared",),
        output=q(subjects=(QUAD,), during=During(DomainClock(rec("invented-drone-clock")), 10, 20)),
    )
    case(
        "What does forklift fk-9 run, by its tag?",
        q(subjects=(m("asset_tag:fk-9"),), graph=g("runs_software")),
        status="needs_input",
        blocking=("unknown_entity",),
        output=q(subjects=(m("asset_tag:fk-9"),), graph=g("runs_software")),
    )
    case(
        "Why does memory hold that AMR-07 changed firmware?",
        q(subjects=(AMR07,)),
        status="needs_input",
        blocking=("claim_not_quoted",),
        output=q(subjects=(AMR07,), explain=(Why(claim("invented")),)),
    )
    case(
        "What is the firmware of AMR-07?",
        q(subjects=(m("asset_tag:AMR-07", "run"),), graph=g("runs_software")),
        status="needs_input",
        blocking=("entity_kind_mismatch",),
        output=q(subjects=(m("asset_tag:AMR-07", "run"),), graph=g("runs_software")),
    )
    case(
        "Why does memory hold that the drone was grounded?",
        None,
        status="needs_input",
        blocking=("claim_not_quoted", "draft_withdrawn"),
        output=q(explain=(Why(claim("invented-too")),)),
    )
    case(
        "Which runs did AMR-07 do?",
        None,
        status="invalid",
        blocking=("model_output_invalid",),
        output="Sure! AMR-07 did three runs today.",
    )
    case(
        "How many missions did AMR-07 complete?",
        None,
        status="invalid",
        blocking=("model_output_invalid",),
        output=json.dumps(
            {**to_json(q(subjects=(AMR07,))), "answer": "AMR-07 completed 3 missions"}
        ),
    )
    case(
        "Which runs did AMR-06 do?",
        None,
        status="failed",
        blocking=("model_refused",),
        output=None,
        stop="refusal",
    )
    case(
        "Which runs did AMR-06 do, with every detail?",
        None,
        status="failed",
        blocking=("model_truncated",),
        output='{"as_of":"head","budget":{"items":',
        stop="max_tokens",
    )
    case(
        "What is AMR-06 doing?",
        None,
        status="invalid",
        blocking=("model_output_invalid",),
        output=None,
    )
    case(
        "What is AMR-05 doing?",
        None,
        status="failed",
        blocking=("model_unavailable",),
        output=None,
        recorded=False,
    )


def build() -> dict[str, str]:
    """The three golden files, as text, from this module's definitions."""
    SPECS.clear()
    build_cases()
    index = DeclaredIdentifierIndex(entity_from_json(e) for e in ENTITIES)
    profiles = {name: defaults_from_json(p) for name, p in PROFILES.items()}
    cases: list[dict[str, Any]] = []
    recordings: list[Recording] = []
    for number, spec in enumerate(SPECS, start=1):
        if spec.expected is not None:
            assert not validate(spec.expected), (number, spec.question, validate(spec.expected))
        snapshot = None if spec.as_of == HEAD else int(spec.as_of)
        mentions = tuple(index.find(spec.question, as_of=snapshot))
        request = build_request(spec.question, spec.as_of, profiles[spec.profile], mentions)
        if spec.source != "none":
            assert all(r.request_sha256 != request.sha256 for r in recordings), spec.question
            recordings.append(
                Recording(request.sha256, "claude-sonnet-5-5", spec.stop, spec.output, "synthetic")  # type: ignore[arg-type]
            )
        cases.append(
            {
                "as_of": spec.as_of,
                "expected": {
                    "blocking": list(spec.blocking),
                    "info": list(spec.info),
                    "query": None if spec.expected is None else to_json(spec.expected),
                    "status": spec.status,
                },
                "id": f"g{number:03d}",
                "profile": spec.profile,
                "question": spec.question,
                "source": spec.source,
            }
        )
    return {
        "world.json": json.dumps(world_json(), indent=1, sort_keys=True) + "\n",
        "cases.jsonl": "".join(json.dumps(c, sort_keys=True) + "\n" for c in cases),
        "recordings.jsonl": dump_recordings(recordings),
    }


def main() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, text in build().items():
        (GOLDEN / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(SPECS)} cases to {GOLDEN}")  # noqa: T201


if __name__ == "__main__":
    main()
