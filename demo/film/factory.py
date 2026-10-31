"""Plant 7 in USD: the hall and everything in it, at rest (``plant.usda``).

Built from the simulator's ``sim/scene.json`` (metres, z up), so the 3D plant has the floor plan the
simulated one runs on. Static things (floor, walls, conveyor, cell fence, chargers, racks, lights)
are final here; moving things (arm joints, AMRs, parts, people, signal lamps) are defined at rest
under ``/World/Dyn`` and animated by each shot's layer (``motion.py``).

Geometry is built from USD primitives with ``UsdPreviewSurface`` materials, which Isaac Sim's RTX
renderer draws as is, so the stage needs no downloaded assets.
"""

from __future__ import annotations

import math
from typing import Any

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade

Vec = tuple[float, float, float]

# Arm C3: a large six-axis industrial arm, drawn as yaw + shoulder + elbow + wrist + tool.
ARM_SHOULDER = 1.0  # m above the floor
ARM_L1 = 1.5
ARM_L2 = 1.4
ARM_TOOL = 0.35
PARTS = 48  # pool of part prims; a part's id picks its slot (id % PARTS)
TOTE_SLOTS = 6
BELT_TOP = 0.8
LAMPS = ("info", "warn", "bad", "ok")

COLORS: dict[str, tuple[Vec, float, float]] = {  # name: (rgb, roughness, metallic)
    "floor": ((0.36, 0.37, 0.38), 0.85, 0.0),
    "aisle": ((0.30, 0.33, 0.36), 0.8, 0.0),
    "charging_floor": ((0.27, 0.34, 0.30), 0.8, 0.0),
    "wall": ((0.72, 0.73, 0.74), 0.9, 0.0),
    "wall_band": ((0.16, 0.30, 0.48), 0.7, 0.0),
    "yellow": ((0.95, 0.75, 0.08), 0.6, 0.0),
    "white_line": ((0.88, 0.88, 0.86), 0.6, 0.0),
    "steel": ((0.55, 0.57, 0.6), 0.35, 0.9),
    "dark_steel": ((0.16, 0.17, 0.19), 0.45, 0.7),
    "belt": ((0.06, 0.06, 0.07), 0.9, 0.0),
    "aluminium": ((0.78, 0.79, 0.8), 0.3, 0.9),
    "arm_orange": ((0.95, 0.42, 0.05), 0.35, 0.1),
    "joint": ((0.12, 0.12, 0.13), 0.4, 0.3),
    "part": ((0.80, 0.52, 0.22), 0.35, 0.8),
    "part_jam": ((0.85, 0.2, 0.15), 0.4, 0.5),
    "amr_body": ((0.11, 0.12, 0.13), 0.5, 0.2),
    "amr_deck": ((0.62, 0.64, 0.66), 0.4, 0.6),
    "tote": ((0.12, 0.33, 0.62), 0.6, 0.0),
    "rack": ((0.10, 0.28, 0.55), 0.5, 0.4),
    "rack_beam": ((0.92, 0.45, 0.08), 0.5, 0.3),
    "carton": ((0.62, 0.48, 0.32), 0.85, 0.0),
    "pallet": ((0.55, 0.42, 0.27), 0.9, 0.0),
    "glass": ((0.75, 0.85, 0.9), 0.05, 0.0),
    "fence": ((0.85, 0.7, 0.1), 0.5, 0.3),
    "mesh": ((0.25, 0.25, 0.25), 0.5, 0.5),
    "charger": ((0.85, 0.86, 0.87), 0.4, 0.2),
    "panel": ((0.82, 0.83, 0.84), 0.4, 0.1),
    "skin": ((0.68, 0.52, 0.42), 0.6, 0.0),
    "trousers": ((0.14, 0.16, 0.2), 0.8, 0.0),
    "vest_tech": ((1.0, 0.45, 0.02), 0.6, 0.0),
    "vest_lead": ((0.2, 0.45, 0.85), 0.6, 0.0),
    "hat_tech": ((0.98, 0.85, 0.1), 0.4, 0.0),
    "hat_lead": ((0.95, 0.95, 0.95), 0.4, 0.0),
    "lamp_off": ((0.08, 0.08, 0.08), 0.3, 0.0),
    "bench": ((0.35, 0.36, 0.38), 0.6, 0.3),
}
GLOW: dict[str, Vec] = {  # emissive lamp colours
    "info": (0.2, 0.6, 1.0),
    "warn": (1.0, 0.6, 0.05),
    "bad": (1.0, 0.08, 0.05),
    "ok": (0.15, 1.0, 0.3),
}


class Builder:
    """Small helpers over a stage: materials by name, boxes, cylinders, spheres, capsules."""

    def __init__(self, stage: Usd.Stage) -> None:
        self.stage = stage
        self.materials: dict[str, UsdShade.Material] = {}
        UsdGeom.Scope.Define(stage, "/World/Looks")

    def material(self, name: str) -> UsdShade.Material:
        if name in self.materials:
            return self.materials[name]
        path = f"/World/Looks/{name}"
        mat = UsdShade.Material.Define(self.stage, path)
        shader = UsdShade.Shader.Define(self.stage, f"{path}/Surface")
        shader.CreateIdAttr("UsdPreviewSurface")
        if name.startswith("glow_"):
            rgb = GLOW[name.removeprefix("glow_")]
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
            shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*(c * 12 for c in rgb))
            )
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.3)
        else:
            rgb, rough, metal = COLORS[name]
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
            shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)
            if name in ("glass", "mesh"):
                opacity = 0.18 if name == "glass" else 0.45
                shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(opacity)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        self.materials[name] = mat
        return mat

    def _finish(self, prim: Usd.Prim, mat: str) -> None:
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(self.material(mat))

    def xform(self, path: str, at: Vec = (0, 0, 0), yaw: float = 0.0) -> UsdGeom.Xform:
        x = UsdGeom.Xform.Define(self.stage, path)
        x.AddTranslateOp().Set(Gf.Vec3d(*at))
        x.AddRotateZOp().Set(yaw)
        return x

    def box(self, path: str, size: Vec, at: Vec, mat: str, yaw: float = 0.0) -> UsdGeom.Cube:
        cube = UsdGeom.Cube.Define(self.stage, path)
        cube.CreateSizeAttr(1.0)
        cube.AddTranslateOp().Set(Gf.Vec3d(*at))
        if yaw:
            cube.AddRotateZOp().Set(yaw)
        cube.AddScaleOp().Set(Gf.Vec3f(*size))
        self._finish(cube.GetPrim(), mat)
        return cube

    def cylinder(
        self, path: str, radius: float, height: float, at: Vec, mat: str, axis: str = "Z"
    ) -> UsdGeom.Cylinder:
        cyl = UsdGeom.Cylinder.Define(self.stage, path)
        cyl.CreateRadiusAttr(radius)
        cyl.CreateHeightAttr(height)
        cyl.CreateAxisAttr(axis)
        cyl.AddTranslateOp().Set(Gf.Vec3d(*at))
        self._finish(cyl.GetPrim(), mat)
        return cyl

    def sphere(self, path: str, radius: float, at: Vec, mat: str, squash: float = 1.0) -> None:
        sph = UsdGeom.Sphere.Define(self.stage, path)
        sph.CreateRadiusAttr(radius)
        sph.AddTranslateOp().Set(Gf.Vec3d(*at))
        if squash != 1.0:
            sph.AddScaleOp().Set(Gf.Vec3f(1, 1, squash))
        self._finish(sph.GetPrim(), mat)

    def capsule(self, path: str, radius: float, height: float, at: Vec, mat: str) -> None:
        cap = UsdGeom.Capsule.Define(self.stage, path)
        cap.CreateRadiusAttr(radius)
        cap.CreateHeightAttr(height)
        cap.CreateAxisAttr("Z")
        cap.AddTranslateOp().Set(Gf.Vec3d(*at))
        self._finish(cap.GetPrim(), mat)

    def outline(self, path: str, rect: list[float], z: float, mat: str, w: float = 0.08) -> None:
        """Four floor lines around ``rect`` (x0, y0, x1, y1)."""
        x0, y0, x1, y1 = rect
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.box(f"{path}_S", (x1 - x0, w, 0.004), (cx, y0, z), mat)
        self.box(f"{path}_N", (x1 - x0, w, 0.004), (cx, y1, z), mat)
        self.box(f"{path}_W", (w, y1 - y0, 0.004), (x0, cy, z), mat)
        self.box(f"{path}_E", (w, y1 - y0, 0.004), (x1, cy, z), mat)


def _hall(b: Builder, scene: dict[str, Any]) -> None:
    w, d = scene["hall"]["width"], scene["hall"]["depth"]
    b.box("/World/Hall/Floor", (w + 8, d + 8, 0.1), (w / 2, d / 2, -0.05), "floor")
    height = 7.0
    for name, size, at in (
        ("North", (w + 0.6, 0.3, height), (w / 2, d + 0.15, height / 2)),
        ("South", (w + 0.6, 0.3, 0.4), (w / 2, -0.15, 0.2)),  # a curb: the cutaway side
        ("West", (0.3, d, height), (-0.15, d / 2, height / 2)),
        ("East", (0.3, d, height), (w + 0.15, d / 2, height / 2)),
    ):
        b.box(f"/World/Hall/Wall{name}", size, at, "wall")
    b.box("/World/Hall/BandNorth", (w, 0.02, 1.2), (w / 2, d - 0.01, 0.6), "wall_band")
    b.box("/World/Hall/BandWest", (0.02, d, 1.2), (0.01, d / 2, 0.6), "wall_band")
    zones = scene["zones"]
    for zone, mat in (("zone:aisle-a", "aisle"), ("zone:aisle-b", "aisle")):
        x0, y0, x1, y1 = zones[zone]
        b.box(
            f"/World/Floor/{_name(zone)}",
            (x1 - x0, y1 - y0, 0.004),
            ((x0 + x1) / 2, (y0 + y1) / 2, 0.002),
            mat,
        )
        b.outline(f"/World/Floor/{_name(zone)}_edge", zones[zone], 0.005, "yellow", 0.1)
    x0, y0, x1, y1 = zones["zone:charging"]
    b.box(
        "/World/Floor/charging",
        (x1 - x0, y1 - y0, 0.004),
        ((x0 + x1) / 2, (y0 + y1) / 2, 0.002),
        "charging_floor",
    )
    b.outline("/World/Floor/charging_edge", zones["zone:charging"], 0.005, "white_line", 0.08)
    lanes = scene["lanes"]
    nodes = lanes["nodes"]
    for a, outs in lanes["edges"].items():
        for c in outs:
            (xa, ya), (xc, yc) = nodes[a], nodes[c]
            length = math.hypot(xc - xa, yc - ya)
            yaw = math.degrees(math.atan2(yc - ya, xc - xa))
            path = f"/World/Floor/lane_{a}_{c}"
            b.box(
                path,
                (length, 0.05, 0.004),
                ((xa + xc) / 2, (ya + yc) / 2, 0.006),
                "white_line",
                yaw,
            )
    spots = [
        lanes["pickup"],
        lanes["drop"],
        lanes["stage"],
        *lanes["chargers"],
        *lanes["parking"].values(),
    ]
    for spot in spots:
        x, y = nodes[spot]
        b.outline(
            f"/World/Floor/spot_{spot}",
            [x - 0.7, y - 0.5, x + 0.7, y + 0.5],
            0.007,
            "white_line",
            0.06,
        )


def _conveyor(b: Builder, scene: dict[str, Any]) -> None:
    conv = scene["conveyor"]
    (x0, y), (x1, _) = conv["start"], conv["end"]
    length, cx = x1 - x0, (x0 + x1) / 2
    root = "/World/Conveyor"
    b.box(f"{root}/Frame", (length, 0.9, 0.12), (cx, y, BELT_TOP - 0.08), "aluminium")
    b.box(f"{root}/Belt", (length - 0.1, 0.72, 0.03), (cx, y, BELT_TOP - 0.005), "belt")
    for side in (-1, 1):
        b.box(
            f"{root}/Rail{side + 1}",
            (length, 0.04, 0.12),
            (cx, y + side * 0.43, BELT_TOP + 0.04),
            "aluminium",
        )
    for i, s in enumerate([*range(0, int(length), 2), length]):
        for side in (-1, 1):
            b.box(
                f"{root}/Leg{i}_{side + 1}",
                (0.06, 0.06, BELT_TOP - 0.1),
                (x0 + s, y + side * 0.38, (BELT_TOP - 0.1) / 2),
                "aluminium",
            )
    b.box(f"{root}/Motor", (0.5, 0.4, 0.4), (x0 + 0.3, y - 0.7, 0.55), "dark_steel")
    # Infeed: pallets of parts waiting at the dock.
    for i in range(3):
        b.box(f"{root}/Pallet{i}", (1.2, 1.0, 0.15), (x0 - 2.2, y - 2 + i * 1.6, 0.075), "pallet")
        b.box(f"{root}/Load{i}", (1.1, 0.9, 0.9), (x0 - 2.2, y - 2 + i * 1.6, 0.6), "carton")
    # Photo-eye 3: two posts across the belt, and a beacon that lights on a jam.
    px, py = conv["pe3"]
    for side in (-1, 1):
        b.box(
            f"{root}/PE3_{side + 1}",
            (0.05, 0.05, 0.35),
            (px, py + side * 0.47, BELT_TOP + 0.15),
            "dark_steel",
        )
    b.cylinder(f"{root}/PE3Beacon/Base", 0.06, 0.08, (px, py - 0.47, BELT_TOP + 0.37), "dark_steel")
    _lamp(b, "/World/Dyn/Signals/PE3", (px, py - 0.47, BELT_TOP + 0.47), ("bad",), radius=0.07)
    # Andon stack light at the belt's end: green running, amber idle, red stopped.
    ax, ay = x1 - 0.6, y + 0.75
    b.cylinder(f"{root}/AndonPole", 0.025, 1.6, (ax, ay, 0.8), "steel")
    _lamp(b, "/World/Dyn/Signals/CV2", (ax, ay, 1.65), ("bad", "warn", "ok"), stacked=True)


def _lamp(
    b: Builder,
    root: str,
    at: Vec,
    colours: tuple[str, ...],
    radius: float = 0.06,
    stacked: bool = False,
) -> None:
    """Signal lamps: a dark lens always, plus a glowing lens and a light per colour (hidden until
    a shot shows them)."""
    for i, colour in enumerate(colours):
        z = at[2] + (i * 2 * radius if stacked else 0.0)
        pos = (at[0], at[1], z)
        if stacked:
            b.cylinder(f"{root}/{colour}_off", radius, radius * 1.8, pos, "lamp_off")
            b.cylinder(f"{root}/{colour}_on", radius * 1.03, radius * 1.85, pos, f"glow_{colour}")
        else:
            b.sphere(f"{root}/{colour}_off", radius, pos, "lamp_off")
            b.sphere(f"{root}/{colour}_on", radius * 1.05, pos, f"glow_{colour}")
        light = UsdLux.SphereLight.Define(b.stage, f"{root}/{colour}_light")
        light.CreateRadiusAttr(0.05)
        light.CreateIntensityAttr(4000.0)
        light.CreateColorAttr(Gf.Vec3f(*GLOW[colour]))
        UsdGeom.Xformable(light).AddTranslateOp().Set(Gf.Vec3d(pos[0], pos[1], pos[2] + 0.15))
        for name in (f"{colour}_on", f"{colour}_light"):
            UsdGeom.Imageable(b.stage.GetPrimAtPath(f"{root}/{name}")).MakeInvisible()


def _cell(b: Builder, scene: dict[str, Any]) -> None:
    x0, y0, x1, y1 = scene["cells"]["cell:c3"]
    belt_y = scene["conveyor"]["start"][1]
    pick_x = scene["arm"]["place"][0]
    h = 2.0
    root = "/World/Cell"
    posts = []
    for x in [x0 + i for i in range(int(x1 - x0) + 1)]:
        posts += [(x, y0), (x, y1)]
    for y in [y0 + i for i in range(1, int(y1 - y0))]:
        posts += [(x0, y), (x1, y)]
    for i, (x, y) in enumerate(posts):
        b.box(f"{root}/Post{i}", (0.06, 0.06, h), (x, y, h / 2), "fence")
    panels = []
    for i in range(int(x1 - x0)):  # south and north sides
        xa = x0 + i + 0.5
        if abs(xa - pick_x) > 1.0:  # opening where the arm reaches the AMR
            panels.append(((xa, y0), 0.0))
        panels.append(((xa, y1), 0.0))
    for i in range(int(y1 - y0)):  # west and east sides
        ya = y0 + i + 0.5
        if abs(ya - belt_y) > 0.6:  # the belt enters on the west; the gate is on the east
            panels.append(((x0, ya), 90.0))
            panels.append(((x1, ya), 90.0))
    for i, ((x, y), yaw) in enumerate(panels):
        b.box(
            f"{root}/Panel{i}", (0.94, 0.02, h - 0.25), (x, y, (h - 0.25) / 2 + 0.15), "mesh", yaw
        )
    gx, gy = scene["work_at"]["reset_arm"]
    b.box(f"{root}/ResetPanel", (0.15, 0.4, 0.55), (gx - 0.3, gy + 0.6, 1.25), "panel")
    b.outline(f"{root}/Hatch", [x0, y0, x1, y1], 0.006, "yellow", 0.15)
    _lamp(
        b, "/World/Dyn/Signals/C3", (x1 - 0.2, y1 - 0.2, 2.1), ("bad", "warn", "ok"), stacked=True
    )
    b.cylinder(f"{root}/AndonPole", 0.025, 2.1, (x1 - 0.2, y1 - 0.2, 1.05), "steel")


def _arm(b: Builder, scene: dict[str, Any]) -> None:
    bx, by = scene["arm"]["base"]
    root = "/World/Dyn/Arm"
    b.xform(root, (bx, by, 0))
    b.cylinder(f"{root}/Pedestal", 0.3, 0.45, (0, 0, 0.225), "dark_steel")
    yaw = UsdGeom.Xform.Define(b.stage, f"{root}/Yaw")
    yaw.AddRotateZOp().Set(0.0)
    b.cylinder(f"{root}/Yaw/Turret", 0.26, 0.4, (0, 0, 0.66), "arm_orange")
    shoulder = UsdGeom.Xform.Define(b.stage, f"{root}/Yaw/Shoulder")
    shoulder.AddTranslateOp().Set(Gf.Vec3d(0, 0, ARM_SHOULDER))
    shoulder.AddRotateYOp().Set(-60.0)
    s = f"{root}/Yaw/Shoulder"
    b.cylinder(f"{s}/Joint", 0.22, 0.5, (0, 0, 0), "joint", axis="Y")
    b.box(f"{s}/Link", (ARM_L1, 0.3, 0.32), (ARM_L1 / 2, 0, 0), "arm_orange")
    elbow = UsdGeom.Xform.Define(b.stage, f"{s}/Elbow")
    elbow.AddTranslateOp().Set(Gf.Vec3d(ARM_L1, 0, 0))
    elbow.AddRotateYOp().Set(100.0)
    e = f"{s}/Elbow"
    b.cylinder(f"{e}/Joint", 0.18, 0.42, (0, 0, 0), "joint", axis="Y")
    b.box(f"{e}/Link", (ARM_L2, 0.24, 0.24), (ARM_L2 / 2, 0, 0), "arm_orange")
    wrist = UsdGeom.Xform.Define(b.stage, f"{e}/Wrist")
    wrist.AddTranslateOp().Set(Gf.Vec3d(ARM_L2, 0, 0))
    wrist.AddRotateYOp().Set(50.0)
    w = f"{e}/Wrist"
    b.cylinder(f"{w}/Joint", 0.12, 0.3, (0, 0, 0), "joint", axis="Y")
    b.cylinder(
        f"{w}/Flange", 0.09, ARM_TOOL - 0.1, ((ARM_TOOL - 0.1) / 2, 0, 0), "dark_steel", axis="X"
    )
    b.box(f"{w}/Gripper", (0.1, 0.32, 0.16), (ARM_TOOL - 0.05, 0, 0), "dark_steel")
    for side in (-1, 1):
        b.box(
            f"{w}/Finger{side + 1}", (0.14, 0.03, 0.1), (ARM_TOOL + 0.06, side * 0.13, 0), "steel"
        )


def _amrs(b: Builder, ids: list[str]) -> None:
    for robot in ids:
        root = f"/World/Dyn/AMRs/{_name(robot)}"
        b.xform(root)
        b.box(f"{root}/Chassis", (1.1, 0.75, 0.28), (0, 0, 0.19), "amr_body")
        b.box(f"{root}/Deck", (1.04, 0.69, 0.04), (0, 0, 0.35), "amr_deck")
        b.box(f"{root}/BumperF", (0.04, 0.72, 0.12), (0.56, 0, 0.12), "yellow")
        b.box(f"{root}/BumperB", (0.04, 0.72, 0.12), (-0.56, 0, 0.12), "yellow")
        b.cylinder(f"{root}/Lidar", 0.06, 0.07, (0.42, 0, 0.405), "dark_steel")
        for lamp in LAMPS:  # status strip around the deck, one colour shown at a time
            b.box(f"{root}/Status_{lamp}", (1.12, 0.77, 0.035), (0, 0, 0.31), f"glow_{lamp}")
            UsdGeom.Imageable(b.stage.GetPrimAtPath(f"{root}/Status_{lamp}")).MakeInvisible()
        t = f"{root}/Tote"
        b.box(f"{t}/Bottom", (0.6, 0.4, 0.02), (0, 0, 0.38), "tote")
        for side, size, at in (
            ("N", (0.6, 0.02, 0.22), (0, 0.2, 0.48)),
            ("S", (0.6, 0.02, 0.22), (0, -0.2, 0.48)),
            ("E", (0.02, 0.4, 0.22), (0.3, 0, 0.48)),
            ("W", (0.02, 0.4, 0.22), (-0.3, 0, 0.48)),
        ):
            b.box(f"{t}/{side}", size, at, "tote")
        for i in range(TOTE_SLOTS):
            x, y = -0.18 + (i % 3) * 0.18, -0.09 + (i // 3) * 0.18
            b.box(f"{t}/Part{i}", (0.15, 0.15, 0.1), (x, y, 0.44), "part")
            UsdGeom.Imageable(b.stage.GetPrimAtPath(f"{t}/Part{i}")).MakeInvisible()


def _parts(b: Builder) -> None:
    for i in range(PARTS):
        root = f"/World/Dyn/Parts/P{i:02d}"
        b.xform(root, (0, 0, -5))
        b.box(f"{root}/Body", (0.3, 0.22, 0.12), (0, 0, 0.06), "part")
        b.cylinder(f"{root}/Boss", 0.06, 0.06, (0, 0, 0.15), "steel")
        UsdGeom.Imageable(b.stage.GetPrimAtPath(root)).MakeInvisible()
    b.xform("/World/Dyn/Held", (0, 0, -5))
    b.box("/World/Dyn/Held/Body", (0.3, 0.22, 0.12), (0, 0, -0.06), "part")
    UsdGeom.Imageable(b.stage.GetPrimAtPath("/World/Dyn/Held")).MakeInvisible()


def _people(b: Builder, people: list[tuple[str, str]]) -> None:
    for pid, role in people:
        lead = role == "line lead"
        root = f"/World/Dyn/People/{_name(pid)}"
        b.xform(root)
        for side, y in (("L", 0.11), ("R", -0.11)):
            hip = UsdGeom.Xform.Define(b.stage, f"{root}/Hip{side}")
            hip.AddTranslateOp().Set(Gf.Vec3d(0, y, 0.88))
            hip.AddRotateYOp().Set(0.0)
            b.capsule(f"{root}/Hip{side}/Leg", 0.075, 0.72, (0, 0, -0.44), "trousers")
            b.box(f"{root}/Hip{side}/Boot", (0.24, 0.11, 0.08), (0.04, 0, -0.84), "dark_steel")
        b.capsule(f"{root}/Torso", 0.19, 0.38, (0, 0, 1.25), "vest_lead" if lead else "vest_tech")
        for side, y in (("L", 0.25), ("R", -0.25)):
            b.capsule(
                f"{root}/Arm{side}", 0.055, 0.5, (0, y, 1.2), "vest_lead" if lead else "vest_tech"
            )
        b.sphere(f"{root}/Head", 0.12, (0.01, 0, 1.66), "skin")
        b.sphere(
            f"{root}/Hat", 0.135, (0.01, 0, 1.71), "hat_lead" if lead else "hat_tech", squash=0.6
        )
        UsdGeom.Imageable(b.stage.GetPrimAtPath(root)).MakeInvisible()


def _rooms(b: Builder, scene: dict[str, Any]) -> None:
    places = scene["places"]
    sx, sy = places["shop"]
    b.box("/World/Rooms/Workbench", (2.0, 0.7, 0.9), (sx, sy + 1.4, 0.45), "bench")
    b.box("/World/Rooms/ToolWall", (2.4, 0.08, 1.4), (sx, sy + 2.0, 1.6), "panel")
    b.box("/World/Rooms/Cabinet", (0.8, 0.5, 1.8), (sx + 1.8, sy + 1.5, 0.9), "rack")
    rx, ry = places["break_room"]
    b.cylinder("/World/Rooms/Table", 0.6, 0.05, (rx, ry + 0.8, 0.74), "panel")
    b.cylinder("/World/Rooms/TableLeg", 0.05, 0.72, (rx, ry + 0.8, 0.36), "steel")
    ox, oy = places["office"]
    b.box("/World/Rooms/OfficeGlass", (4.0, 3.0, 2.8), (ox + 0.5, oy + 1.2, 1.4), "glass")
    b.box("/World/Rooms/OfficeDesk", (1.6, 0.7, 0.75), (ox + 0.5, oy + 1.8, 0.375), "panel")
    # Pallet racks along the north wall of the assembly hall.
    for row, x0 in enumerate((13.0, 21.0)):
        root = f"/World/Racks/Row{row}"
        for i in range(4):
            x = x0 + i * 1.9
            for side in (0, 1):
                b.box(
                    f"{root}/Up{i}_{side}", (0.08, 0.08, 4.0), (x, 21.2 + side * 1.1, 2.0), "rack"
                )
            for level, z in enumerate((0.15, 1.6, 3.05)):
                if i < 3:
                    b.box(
                        f"{root}/Beam{i}_{level}",
                        (1.9, 1.2, 0.08),
                        (x + 0.95, 21.75, z),
                        "rack_beam",
                    )
                    b.box(
                        f"{root}/Load{i}_{level}",
                        (1.6, 1.0, 1.0),
                        (x + 0.95, 21.75, z + 0.55),
                        "carton",
                    )
    for i in range(3):  # empty totes stacked at the drop station
        b.box(f"/World/Rooms/ToteStack{i}", (0.6, 0.4, 0.25), (31.5, 0.6, 0.13 + i * 0.26), "tote")


def _chargers(b: Builder, scene: dict[str, Any]) -> None:
    nodes = scene["lanes"]["nodes"]
    for name in scene["lanes"]["chargers"]:
        x, y = nodes[name]
        b.box(f"/World/Chargers/{name}/Unit", (0.6, 0.3, 1.1), (x, y + 0.85, 0.55), "charger")
        b.box(f"/World/Chargers/{name}/Plate", (0.4, 0.06, 0.1), (x, y + 0.65, 0.18), "dark_steel")
        b.box(f"/World/Chargers/{name}/Led", (0.3, 0.02, 0.05), (x, y + 0.69, 0.95), "glow_ok")


def _lights(stage: Usd.Stage, scene: dict[str, Any], scale: float) -> None:
    w, d = scene["hall"]["width"], scene["hall"]["depth"]
    dome = UsdLux.DomeLight.Define(stage, "/World/Lights/Dome")
    dome.CreateIntensityAttr(350.0 * scale)
    dome.CreateColorAttr(Gf.Vec3f(0.85, 0.9, 1.0))
    sun = UsdLux.DistantLight.Define(stage, "/World/Lights/Sun")
    sun.CreateIntensityAttr(1500.0 * scale)
    sun.CreateAngleAttr(1.0)
    sun.CreateColorAttr(Gf.Vec3f(1.0, 0.95, 0.88))
    UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(35, 20, 30))
    i = 0
    for x in range(5, int(w), 7):
        for y in range(4, int(d), 6):
            light = UsdLux.RectLight.Define(stage, f"/World/Lights/Bay{i}")
            light.CreateWidthAttr(2.4)
            light.CreateHeightAttr(0.6)
            light.CreateIntensityAttr(12000.0 * scale)
            light.CreateColorAttr(Gf.Vec3f(1.0, 0.97, 0.92))
            UsdGeom.Xformable(light).AddTranslateOp().Set(Gf.Vec3d(x, y, 6.6))
            i += 1


def _name(ref: str) -> str:
    return ref.split(":")[-1].replace("-", "_")


def build(
    path: str,
    scene: dict[str, Any],
    amrs: list[str],
    people: list[tuple[str, str]],
    light_scale: float = 1.0,
) -> None:
    """Write ``plant.usda``: the hall, its fixed equipment, and every moving thing at rest."""
    stage = Usd.Stage.CreateNew(path)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    b = Builder(stage)
    _hall(b, scene)
    _conveyor(b, scene)
    _cell(b, scene)
    _arm(b, scene)
    _amrs(b, amrs)
    _parts(b)
    _people(b, people)
    _rooms(b, scene)
    _chargers(b, scene)
    _lights(stage, scene, light_scale)
    UsdGeom.Scope.Define(stage, "/World/Cameras")
    stage.GetRootLayer().Save()
