"""Shots: the simulator's timeline, resampled at video frame rate onto the plant (one USD per shot).

A shot is a stretch of site time played at ``speed`` x for ``seconds`` of video. Its file
sublayers ``plant.usda`` and authors only time samples: robot and person poses, arm joints (solved
from the tool position the timeline records), parts on the belt and in totes, signal lamps, and the
shot's camera. Time code = video frame (``timeCodesPerSecond`` = fps), so any USD renderer plays
it frame by frame with no knowledge of the simulator.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import factory
from pxr import Gf, Usd, UsdGeom

if TYPE_CHECKING:
    from pathlib import Path

Vec = tuple[float, float, float]


@dataclass
class Shot:
    name: str
    start: datetime  # site time of the first frame
    seconds: float  # video length
    speed: float  # site seconds per video second
    eye: list[Vec]  # camera keyframes, spread evenly over the shot
    target: list[Vec]
    focal: float = 28.0
    note: str = ""
    follow: str | None = None  # an entity whose position is added to target (and eye) keyframes
    follow_eye: bool = False

    def frames(self, fps: int) -> int:
        return max(1, round(self.seconds * fps))


@dataclass
class Timeline:
    """The simulator's frames by whole site second (``sim/timeline.jsonl``)."""

    frames: dict[int, dict[str, Any]] = field(default_factory=dict)
    first: datetime | None = None
    events: list[tuple[datetime, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, day: str | None = None) -> Timeline:
        out = cls()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if day and line[6:16] != day:
                    continue
                frame = json.loads(line)
                t = datetime.fromisoformat(frame["t"])
                out.first = out.first or t
                out.frames[_key(t)] = frame
                out.events += [(t, e) for e in frame["events"]]
        if not out.frames:
            raise SystemExit(f"no frames in {path}" + (f" for {day}" if day else ""))
        return out

    def at(self, t: datetime) -> tuple[dict[str, Any], dict[str, Any], float]:
        """The frames either side of ``t`` and how far between them it is."""
        whole = t.replace(microsecond=0)
        k = (t - whole).total_seconds()
        a = self._near(_key(whole))
        b = self.frames.get(_key(whole) + 1, a)
        return a, b, k

    def _near(self, key: int) -> dict[str, Any]:
        for d in range(0, 120):
            for k in (key - d, key + d):
                if k in self.frames:
                    return self.frames[k]
        raise SystemExit(f"no simulator frame near {datetime.fromtimestamp(key)}")


def _key(t: datetime) -> int:
    return int(t.timestamp())


def _lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def _ease(k: float) -> float:
    return k * k * (3 - 2 * k)


def solve_arm(base: tuple[float, float], tcp: Vec) -> tuple[float, float, float, float]:
    """Joint angles (degrees, as the stage's rotate ops take them) that put the tool tip at ``tcp``
    pointing straight down: yaw, shoulder, elbow, wrist."""
    bx, by = base
    x, y, z = tcp
    yaw = math.atan2(y - by, x - bx)
    r = math.hypot(x - bx, y - by)
    wz = z + factory.ARM_TOOL - factory.ARM_SHOULDER  # wrist centre, relative to the shoulder
    l1, l2 = factory.ARM_L1, factory.ARM_L2
    d = min(math.hypot(r, wz), l1 + l2 - 1e-3)
    beta = math.atan2(wz, r)
    alpha = math.acos(max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d))))
    a2 = beta + alpha  # elbow up
    ex, ez = l1 * math.cos(a2), l1 * math.sin(a2)
    a3 = math.atan2(wz - ez, r - ex)
    # rotateY by +phi turns +X towards -Z, so an elevation of a is rotateY(-a).
    return (
        math.degrees(yaw),
        -math.degrees(a2),
        -math.degrees(a3 - a2),
        math.degrees(math.pi / 2 + a3),
    )


def arm_tip(base: tuple[float, float], joints: tuple[float, float, float, float]) -> Vec:
    """Forward kinematics of ``solve_arm``'s joints: where the tool tip is."""
    yaw, s, e, w = (math.radians(j) for j in joints)
    a2 = -s
    a3 = a2 - e
    a4 = a3 - w
    r = (
        factory.ARM_L1 * math.cos(a2)
        + factory.ARM_L2 * math.cos(a3)
        + factory.ARM_TOOL * math.cos(a4)
    )
    z = factory.ARM_SHOULDER + factory.ARM_L1 * math.sin(a2) + factory.ARM_L2 * math.sin(a3)
    z += factory.ARM_TOOL * math.sin(a4)
    return base[0] + r * math.cos(yaw), base[1] + r * math.sin(yaw), z


def _unwrap(previous: float | None, angle: float) -> float:
    if previous is None:
        return angle
    while angle - previous > 180:
        angle -= 360
    while angle - previous < -180:
        angle += 360
    return angle


def _lerp_angle(a: float, b: float, k: float) -> float:
    d = (b - a + 180) % 360 - 180
    return a + d * k


class _Writer:
    """Authors time samples, skipping a visibility sample that repeats the last one."""

    def __init__(self, stage: Usd.Stage) -> None:
        self.stage = stage
        self.ops: dict[tuple[str, str], Usd.Attribute] = {}
        self.shown: dict[str, bool] = {}

    def op(self, path: str, name: str) -> Usd.Attribute:
        key = (path, name)
        if key not in self.ops:
            attr = self.stage.GetPrimAtPath(path).GetAttribute(f"xformOp:{name}")
            if not attr:
                raise SystemExit(f"{path} has no xformOp:{name}")
            self.ops[key] = attr
        return self.ops[key]

    def set(self, path: str, name: str, value: Any, frame: int) -> None:
        self.op(path, name).Set(value, Usd.TimeCode(frame))

    def show(self, path: str, visible: bool, frame: int) -> None:
        if self.shown.get(path) == visible:
            return
        self.shown[path] = visible
        attr = UsdGeom.Imageable(self.stage.GetPrimAtPath(path)).GetVisibilityAttr()
        attr.Set(
            UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible, Usd.TimeCode(frame)
        )


def _status(amr: dict[str, Any]) -> str:
    if amr["stranded"]:
        return "bad"
    if amr["charging"]:
        return "ok"
    if amr["mode"] == "PAUSED" or amr["order_state"] in ("waiting", "blocked"):
        return "warn"
    return "info"


def _signal(state: str) -> str:
    return {"running": "ok", "stopped": "bad"}.get(state, "warn")


def _arm_signal(mode: str) -> str:
    return {"running": "ok", "protective_stop": "bad", "idle": "off"}.get(mode, "warn")


def write_shot(
    out: Path,
    plant: str,
    shot: Shot,
    timeline: Timeline,
    scene: dict[str, Any],
    fps: int,
) -> tuple[Path, float]:
    """Write one shot's USD; returns its path and the worst arm reach error (m)."""
    stage = Usd.Stage.CreateNew(str(out))
    stage.GetRootLayer().subLayerPaths.append(plant)
    n = shot.frames(fps)
    stage.SetTimeCodesPerSecond(fps)
    stage.SetFramesPerSecond(fps)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(n - 1)
    stage.SetMetadata("comment", f"{shot.name}: {shot.note}")
    w = _Writer(stage)
    belt_x0, belt_y = scene["conveyor"]["start"]
    base = tuple(scene["arm"]["base"])
    cam = UsdGeom.Camera.Define(stage, "/World/Cameras/Shot")
    cam.CreateFocalLengthAttr(shot.focal)
    cam.CreateHorizontalApertureAttr(36.0)
    cam.CreateVerticalApertureAttr(20.25)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.05, 500.0))
    cam_op = cam.AddTransformOp()
    angles: dict[str, float | None] = {}
    stride: dict[str, float] = {}
    last_xy: dict[str, tuple[float, float]] = {}
    worst = 0.0
    for f in range(n):
        t = shot.start + timedelta(seconds=f * shot.speed / fps)
        a, b, k = timeline.at(t)
        # Conveyor parts, by id.
        later = {p["id"]: p for p in b["conveyor"]["parts"]}
        used = set()
        for part in a["conveyor"]["parts"]:
            slot = part["id"] % factory.PARTS
            used.add(slot)
            s = part["s"]
            if part["id"] in later and not part["jammed"]:
                s = _lerp(s, later[part["id"]]["s"], k)
            path = f"/World/Dyn/Parts/P{slot:02d}"
            w.set(path, "translate", Gf.Vec3d(belt_x0 + s, belt_y, factory.BELT_TOP), f)
            w.set(path, "rotateZ", 35.0 if part["jammed"] else 0.0, f)
            w.show(path, True, f)
        for slot in range(factory.PARTS):
            if slot not in used:
                w.show(f"/World/Dyn/Parts/P{slot:02d}", False, f)
        # The arm, solved from the tool position.
        tcp = tuple(_lerp(p, q, k) for p, q in zip(a["arm"]["tcp"], b["arm"]["tcp"], strict=True))
        joints = solve_arm(base, tcp)  # type: ignore[arg-type]
        yaw = _unwrap(angles.get("arm"), joints[0])
        angles["arm"] = yaw
        w.set("/World/Dyn/Arm/Yaw", "rotateZ", yaw, f)
        w.set("/World/Dyn/Arm/Yaw/Shoulder", "rotateY", joints[1], f)
        w.set("/World/Dyn/Arm/Yaw/Shoulder/Elbow", "rotateY", joints[2], f)
        w.set("/World/Dyn/Arm/Yaw/Shoulder/Elbow/Wrist", "rotateY", joints[3], f)
        tip = arm_tip(base, joints)  # type: ignore[arg-type]
        worst = max(worst, math.dist(tip, tcp))
        held = a["arm"]["holding"]
        w.set("/World/Dyn/Held", "translate", Gf.Vec3d(*tcp) if held else Gf.Vec3d(0, 0, -5), f)
        w.show("/World/Dyn/Held", held, f)
        # AMRs.
        for ra, rb in zip(a["amrs"], b["amrs"], strict=True):
            path = f"/World/Dyn/AMRs/{factory._name(ra['id'])}"
            jump = math.hypot(rb["x"] - ra["x"], rb["y"] - ra["y"]) > 3
            kk = 0.0 if jump else k
            x, y = _lerp(ra["x"], rb["x"], kk), _lerp(ra["y"], rb["y"], kk)
            heading = _unwrap(angles.get(path), math.degrees(_lerp_angle(ra["yaw"], rb["yaw"], kk)))
            angles[path] = heading
            w.set(path, "translate", Gf.Vec3d(x, y, 0), f)
            w.set(path, "rotateZ", heading, f)
            lit = _status(ra)
            for lamp in factory.LAMPS:
                w.show(f"{path}/Status_{lamp}", lamp == lit, f)
            for i in range(factory.TOTE_SLOTS):
                w.show(f"{path}/Tote/Part{i}", i < ra["tote"], f)
        # People: position, heading, a stride while walking.
        for pa, pb in zip(a["people"], b["people"], strict=True):
            path = f"/World/Dyn/People/{factory._name(pa['id'])}"
            present = pa["present"]
            w.show(path, present, f)
            if not present:
                continue
            kk = k if pb["present"] else 0.0
            x, y = _lerp(pa["x"], pb["x"], kk), _lerp(pa["y"], pb["y"], kk)
            prev = last_xy.get(path, (x, y))
            step = math.hypot(x - prev[0], y - prev[1])
            last_xy[path] = (x, y)
            moving = step > 0.002 and step < 1.0
            if moving:
                heading = math.degrees(math.atan2(y - prev[1], x - prev[0]))
            else:
                heading = angles.get(path) or math.degrees(pa["yaw"])
            heading = _unwrap(angles.get(path), heading)
            angles[path] = heading
            stride[path] = stride.get(path, 0.0) + (step if moving else 0.0)
            swing = 28.0 * math.sin(2 * math.pi * stride[path] / 1.4) if moving else 0.0
            w.set(path, "translate", Gf.Vec3d(x, y, 0), f)
            w.set(path, "rotateZ", heading, f)
            w.set(f"{path}/HipL", "rotateY", swing, f)
            w.set(f"{path}/HipR", "rotateY", -swing, f)
        # Signal lamps.
        conveyor, arm_mode = a["conveyor"], a["arm"]["mode"]
        for colour in ("bad", "warn", "ok"):
            on = _signal(conveyor["state"]) == colour
            w.show(f"/World/Dyn/Signals/CV2/{colour}_on", on, f)
            w.show(f"/World/Dyn/Signals/CV2/{colour}_light", on, f)
            on = _arm_signal(arm_mode) == colour
            w.show(f"/World/Dyn/Signals/C3/{colour}_on", on, f)
            w.show(f"/World/Dyn/Signals/C3/{colour}_light", on, f)
        jam = bool(conveyor["alarm"]) and (f // max(1, fps // 3)) % 2 == 0
        w.show("/World/Dyn/Signals/PE3/bad_on", jam, f)
        w.show("/World/Dyn/Signals/PE3/bad_light", jam, f)
        # Camera.
        eye, target = _camera(shot, f / max(1, n - 1), a, b, k)
        view = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*eye), Gf.Vec3d(*target), Gf.Vec3d(0, 0, 1))
        cam_op.Set(view.GetInverse(), Usd.TimeCode(f))
    stage.GetRootLayer().Save()
    return out, worst


def _camera(
    shot: Shot, u: float, a: dict[str, Any], b: dict[str, Any], k: float
) -> tuple[Vec, Vec]:
    def along(keys: list[Vec]) -> Vec:
        if len(keys) == 1:
            return keys[0]
        x = _ease(u) * (len(keys) - 1)
        i = min(int(x), len(keys) - 2)
        kk = x - i
        return tuple(_lerp(p, q, kk) for p, q in zip(keys[i], keys[i + 1], strict=True))  # type: ignore[return-value]

    eye, target = along(shot.eye), along(shot.target)
    if shot.follow:
        ra = next(r for r in a["amrs"] if r["id"] == shot.follow)
        rb = next(r for r in b["amrs"] if r["id"] == shot.follow)
        x, y = _lerp(ra["x"], rb["x"], k), _lerp(ra["y"], rb["y"], k)
        target = (target[0] + x, target[1] + y, target[2])
        if shot.follow_eye:
            eye = (eye[0] + x, eye[1] + y, eye[2])
    return eye, target
