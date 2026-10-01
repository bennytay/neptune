"""Deterministic synthetic deployment graph: a fleet of every robot type over five years of claims.

Shape (at the issue's full scale, ``claims=10**8``): 200 robots across six embodiments (AMR, arm,
quadruped, humanoid, marine, aerial), 40 sites, 5 years. Each robot carries high-rate observed state
(pose zone, operating mode, health, energy), events (fault codes), mission assignments, and slow
stated facts (site, firmware, which component sits in which slot). Each mounted component carries
calibrations and wear. About 6% of claims are later superseded by a correction recorded hours to
weeks after the original, with a new transform version (a parser upgrade is new lineage).

Smaller ``claims`` targets scale every rate by the same factor (each subject keeps at least one
claim per predicate), so the entity graph stays the same size and only history depth changes:
this is the axis the scale ladder measures and extrapolates.

Invariant the as-of queries rely on: for one ``(subject, predicate)``, the valid intervals of the
claims visible at any transaction time never overlap (every predicate is functional; multi-valued
relations such as mounts are split into one predicate per slot). Corrections keep the original's
valid interval.

Deterministic: ``random.Random`` seeded per robot from a string (stable across Python versions and
platforms); no wall clock, no numpy. Same spec, same bytes.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from neptune_memory.store.records import ClaimRecord

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from neptune_memory.store.records import AssertionKind

NS: Final = 1_000_000_000
DAY: Final = 86_400 * NS
YEAR_DAYS: Final = 365.25
VALID_CLOCK: Final = "fleet_utc"
FULL_SCALE_CLAIMS: Final = 10**8

#: Embodiments with their share of the fleet, component slots and operating modes.
EMBODIMENTS: Final[tuple[tuple[str, int, tuple[str, ...], tuple[str, ...]], ...]] = (
    (
        "amr",
        60,
        (
            "lidar_front",
            "lidar_rear",
            "drive_left",
            "drive_right",
            "battery",
            "imu",
            "camera",
            "compute",
        ),
        ("idle", "navigating", "docking", "charging", "estop"),
    ),
    (
        "arm",
        50,
        (
            "joint_1",
            "joint_2",
            "joint_3",
            "joint_4",
            "joint_5",
            "joint_6",
            "gripper",
            "wrist_camera",
        ),
        ("idle", "picking", "placing", "homing", "fault_hold"),
    ),
    (
        "quadruped",
        25,
        ("leg_fl", "leg_fr", "leg_rl", "leg_rr", "battery", "imu", "depth_camera", "compute"),
        ("standing", "walking", "stairs", "docked"),
    ),
    (
        "humanoid",
        20,
        (
            "arm_left",
            "arm_right",
            "leg_left",
            "leg_right",
            "hand_left",
            "hand_right",
            "head_camera",
        ),
        ("idle", "walking", "manipulating", "teleop"),
    ),
    (
        "marine",
        20,
        ("thruster_port", "thruster_stbd", "dvl", "sonar", "gnss", "battery", "compute"),
        ("surface", "transit", "survey", "station_keep"),
    ),
    (
        "aerial",
        25,
        ("motor_1", "motor_2", "motor_3", "motor_4", "flight_controller", "gnss", "battery"),
        ("grounded", "takeoff", "mission", "return", "landed"),
    ),
)

Kind = Literal["state", "event", "entity"]


@dataclass(frozen=True, slots=True)
class _Pred:
    name: str
    per_day: float  # change rate at full scale
    kind: Kind
    assertion: AssertionKind
    source: str  # evidence family the claim is derived from


ROBOT_PREDICATES: Final = (
    _Pred("pose_zone", 150.0, "state", "observed", "log"),
    _Pred("operating_mode", 60.0, "state", "observed", "log"),
    _Pred("health_status", 24.0, "state", "observed", "log"),
    _Pred("energy_state", 24.0, "state", "observed", "log"),
    _Pred("fault_code", 5.0, "event", "observed", "log"),
    _Pred("assigned_episode", 8.0, "entity", "stated", "mission"),
    _Pred("firmware_version", 1 / 30, "state", "stated", "config"),
    _Pred("located_at", 1 / 120, "entity", "stated", "site"),
)
MOUNT_PER_DAY: Final = 1 / 365
COMPONENT_PREDICATES: Final = (
    _Pred("calibrated_by", 1 / 7, "entity", "observed", "calibration"),
    _Pred("wear_index", 1.0, "state", "observed", "log"),
)

CLAIM_COLUMNS: Final = (
    "claim_id",
    "subject",
    "predicate",
    "object_entity",
    "object_value",
    "valid_clock",
    "valid_from",
    "valid_to",
    "recorded_at",
    "superseded_at",
    "assertion_kind",
    "source_id",
    "transform_id",
    "supersedes",
)


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    """What to generate. ``claims`` is a target; the realised count is within a few percent."""

    claims: int
    robots: int = 200
    sites: int = 40
    years: int = 5
    supersede_rate: float = 0.06
    seed: int = 104

    def __post_init__(self) -> None:
        if self.claims < 1 or self.robots < 1 or self.sites < 1 or self.years < 1:
            raise ValueError("claims, robots, sites and years must be positive")
        if not 0.0 <= self.supersede_rate < 1.0:
            raise ValueError("supersede_rate must be in [0, 1)")

    @property
    def span(self) -> int:
        return int(self.years * YEAR_DAYS * DAY)

    @property
    def scale(self) -> float:
        """Rate multiplier relative to the full-scale model (10**8 claims, 200 robots, 5 years)."""
        days = self.years * YEAR_DAYS
        slots = sum(len(e[2]) * e[1] for e in EMBODIMENTS) / sum(e[1] for e in EMBODIMENTS)
        per_robot_day = sum(p.per_day for p in ROBOT_PREDICATES) + slots * (
            MOUNT_PER_DAY + sum(p.per_day for p in COMPONENT_PREDICATES)
        )
        base = per_robot_day * days * self.robots * (1 + self.supersede_rate)
        return self.claims / base


def robot_ids(spec: DeploymentSpec) -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    """``(robot_id, kind, slots, modes)`` for the fleet, embodiments interleaved by fleet share."""
    total = sum(e[1] for e in EMBODIMENTS)
    out: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    counts = dict.fromkeys((e[0] for e in EMBODIMENTS), 0)
    for i in range(spec.robots):
        # Weighted round-robin: pick the embodiment furthest below its share so far.
        kind, _, slots, modes = min(
            EMBODIMENTS, key=lambda e: (counts[e[0]] - (i + 1) * e[1] / total, e[0])
        )
        counts[kind] += 1
        out.append((f"robot:{kind}-{counts[kind]:03d}", kind, slots, modes))
    return out


def site_id(i: int) -> str:
    return f"site:{i:02d}"


class _Emitter:
    """Assigns claim ids and applies supersession; yields records in a fixed order."""

    def __init__(self, spec: DeploymentSpec) -> None:
        self.spec = spec
        self.next_id = 1

    def emit(
        self,
        rng: random.Random,
        pred: _Pred,
        subject: str,
        obj: tuple[str | None, str | None],
        valid: tuple[int, int | None],
        source_id: str,
    ) -> Iterator[ClaimRecord]:
        valid_from, valid_to = valid
        if pred.assertion == "observed":
            lag = rng.randrange(30 * NS, 36 * 3600 * NS)
        else:
            lag = rng.randrange(-3 * DAY, DAY)
        recorded = valid_from + lag
        transform = f"memory.consolidate.{pred.source}@1.0.0"
        corrected = rng.random() < self.spec.supersede_rate
        superseded = recorded + rng.randrange(3600 * NS, 60 * DAY) if corrected else None
        first = ClaimRecord(
            claim_id=self.next_id,
            subject=subject,
            predicate=pred.name,
            object_entity=obj[0],
            object_value=obj[1],
            valid_clock=VALID_CLOCK,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_at=recorded,
            superseded_at=superseded,
            assertion_kind=pred.assertion,
            source_id=source_id,
            transform_id=transform,
        )
        self.next_id += 1
        yield first
        if superseded is not None:
            value = None if obj[1] is None else f"{obj[1]}~corrected"
            yield ClaimRecord(
                claim_id=self.next_id,
                subject=subject,
                predicate=pred.name,
                object_entity=obj[0],
                object_value=value,
                valid_clock=VALID_CLOCK,
                valid_from=valid_from,
                valid_to=valid_to,
                recorded_at=superseded,
                superseded_at=None,
                assertion_kind=pred.assertion,
                source_id=source_id,
                transform_id=f"memory.consolidate.{pred.source}@1.1.0",
                supersedes=first.claim_id,
            )
            self.next_id += 1


def _count(spec: DeploymentSpec, per_day: float, lo: int, hi: int) -> int:
    return max(1, round(per_day * (hi - lo) / DAY * spec.scale))


def _intervals(
    rng: random.Random, kind: Kind, k: int, lo: int, hi: int, open_end: bool
) -> list[tuple[int, int | None]]:
    """``k`` non-overlapping valid intervals in ``[lo, hi)``; states tile it, events are short."""
    k = min(k, max(1, hi - lo - 1))
    if kind == "event":
        points = sorted(rng.sample(range(lo, hi), k))
        out: list[tuple[int, int | None]] = []
        for i, p in enumerate(points):
            nxt = points[i + 1] if i + 1 < len(points) else hi
            out.append((p, min(nxt, p + rng.randrange(NS, 600 * NS))))
        return out
    points = [lo, *sorted(rng.sample(range(lo + 1, hi), k - 1))]
    ends: list[int | None] = [*points[1:], None if open_end else hi]
    return list(zip(points, ends, strict=True))


def _source(subject_tag: str, family: str, t: int) -> str:
    return f"ev:{family}:{subject_tag}:{t // DAY:04d}"


def generate(spec: DeploymentSpec) -> Iterator[ClaimRecord]:
    """Stream every claim of the deployment, robot by robot, in a fixed order."""
    em = _Emitter(spec)
    span = spec.span
    for index, (robot, _kind, slots, modes) in enumerate(robot_ids(spec)):
        rng = random.Random(f"mvl-104:{spec.seed}:{robot}")
        tag = robot.split(":", 1)[1]
        for pred in ROBOT_PREDICATES:
            for n, (vf, vt) in enumerate(
                _intervals(rng, pred.kind, _count(spec, pred.per_day, 0, span), 0, span, True)
            ):
                obj: tuple[str | None, str | None]
                if pred.name == "located_at":
                    site = index % spec.sites if n == 0 else rng.randrange(spec.sites)
                    obj = (site_id(site), None)
                elif pred.name == "assigned_episode":
                    obj = (f"episode:{tag}-{n:06d}", None)
                elif pred.name == "operating_mode":
                    obj = (None, rng.choice(modes))
                elif pred.name == "pose_zone":
                    obj = (None, f"zone-{rng.randrange(64):02d}")
                elif pred.name == "health_status":
                    obj = (None, rng.choices(("ok", "degraded", "fault"), (90, 8, 2))[0])
                elif pred.name == "energy_state":
                    obj = (None, f"soh={rng.randrange(60, 101) / 100:.2f}")
                elif pred.name == "fault_code":
                    obj = (None, f"E{rng.randrange(1, 400):03d}")
                else:  # firmware_version
                    obj = (None, f"fw-{1 + n // 6}.{n % 6}")
                yield from em.emit(rng, pred, robot, obj, (vf, vt), _source(tag, pred.source, vf))
        for slot in slots:
            mount_pred = _Pred(f"mounts/{slot}", MOUNT_PER_DAY, "entity", "stated", "maintenance")
            lives = _intervals(rng, "state", _count(spec, MOUNT_PER_DAY, 0, span), 0, span, True)
            for gen, (mf, mt) in enumerate(lives, start=1):
                component = f"component:{tag}/{slot}#{gen}"
                yield from em.emit(
                    rng, mount_pred, robot, (component, None), (mf, mt), _source(tag, "maint", mf)
                )
                ctag = f"{tag}/{slot}#{gen}"
                end = span if mt is None else mt
                for pred in COMPONENT_PREDICATES:
                    k = _count(spec, pred.per_day, mf, end)
                    for n, (vf, vt) in enumerate(
                        _intervals(rng, pred.kind, k, mf, end, mt is None)
                    ):
                        if pred.name == "calibrated_by":
                            obj = (f"calibration:{ctag}-{n:05d}", None)
                        else:
                            obj = (None, f"{rng.randrange(0, 1000) / 1000:.3f}")
                        yield from em.emit(
                            rng, pred, component, obj, (vf, vt), _source(ctag, pred.source, vf)
                        )


def _cell(value: object) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    claims: int
    superseded: int
    entity_claims: int
    entities: int


def write_dataset(spec: DeploymentSpec, claims_csv: Path, entities_csv: Path) -> DatasetSummary:
    """Write claims and the entity list as CSV (header row, empty cell = NULL), streaming."""
    entities: dict[str, str] = {}
    n = superseded = edges = 0
    with claims_csv.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh, lineterminator="\n")
        out.writerow(CLAIM_COLUMNS)
        for c in generate(spec):
            out.writerow([_cell(getattr(c, col)) for col in CLAIM_COLUMNS])
            n += 1
            superseded += c.superseded_at is not None
            entities.setdefault(c.subject, c.subject.split(":", 1)[0])
            if c.object_entity is not None:
                edges += 1
                entities.setdefault(c.object_entity, c.object_entity.split(":", 1)[0])
    with entities_csv.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh, lineterminator="\n")
        out.writerow(("entity_id", "kind"))
        for eid in sorted(entities):
            out.writerow((eid, entities[eid]))
    return DatasetSummary(
        claims=n, superseded=superseded, entity_claims=edges, entities=len(entities)
    )
