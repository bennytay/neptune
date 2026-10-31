"""Export the film's USD shots for the browser renderer (``web/``): no GPU anywhere.

    python demo/film/export_web.py film

Reads ``<film>/shots/*.usdc`` (``build_stage.py``) and writes ``<film>/web/``:

- ``scene.json``: the prim tree (xforms and cube, cylinder, sphere and capsule shapes) with each
  prim's rest transform, its material, and the materials' colours;
- ``<shot>.json`` + ``<shot>.bin``: per frame, the local transform (16 float32) of every prim whose
  transform is animated, and the visibility of every prim whose visibility is animated, plus the
  camera's lens.

The USD stays the one description of the film: the browser renders exactly what Isaac Sim would.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

from pxr import Usd, UsdGeom, UsdLux, UsdShade

SHAPES = ("Cube", "Cylinder", "Sphere", "Capsule")


def _matrix(m: Any) -> list[float]:
    return [float(m[i][j]) for i in range(4) for j in range(4)]


def _material(stage: Usd.Stage, prim: Usd.Prim, materials: dict[str, Any]) -> str | None:
    mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    if not mat:
        return None
    name = mat.GetPath().name
    if name not in materials:
        shader = UsdShade.Shader(stage.GetPrimAtPath(mat.GetPath().AppendChild("Surface")))

        def value(key: str, default: Any) -> Any:
            attr = shader.GetInput(key)
            return attr.Get() if attr and attr.Get() is not None else default

        emissive = value("emissiveColor", None)
        materials[name] = {
            "color": list(value("diffuseColor", (0.5, 0.5, 0.5))),
            "roughness": value("roughness", 0.5),
            "metallic": value("metallic", 0.0),
            "opacity": value("opacity", 1.0),
            "emissive": list(emissive) if emissive is not None else None,
        }
    return name


def _animated(prim: Usd.Prim) -> tuple[bool, bool]:
    xf = UsdGeom.Xformable(prim)
    moves = bool(xf) and any(op.GetAttr().GetNumTimeSamples() > 0 for op in xf.GetOrderedXformOps())
    vis = UsdGeom.Imageable(prim).GetVisibilityAttr()
    return moves, bool(vis) and vis.GetNumTimeSamples() > 0


def scene(stage: Usd.Stage) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    materials: dict[str, Any] = {}
    rest = Usd.TimeCode(0)
    for prim in stage.Traverse():
        kind = prim.GetTypeName()
        if kind not in (
            "",  # an untyped prim: a group
            "Xform",
            "Scope",
            "SphereLight",
            *SHAPES,
        ) or prim.GetPath().pathString.startswith("/World/Looks"):
            continue
        parent = index.get(prim.GetParent().GetPath().pathString, -1)
        node: dict[str, Any] = {"path": prim.GetPath().pathString, "parent": parent, "type": kind}
        xf = UsdGeom.Xformable(prim)
        if xf:
            node["m"] = _matrix(xf.GetLocalTransformation(rest))
        if UsdGeom.Imageable(prim).GetVisibilityAttr().Get(rest) == UsdGeom.Tokens.invisible:
            node["hidden"] = True
        if kind == "Cube":
            node["size"] = prim.GetAttribute("size").Get()
        elif kind in ("Cylinder", "Capsule"):
            node["radius"] = prim.GetAttribute("radius").Get()
            node["height"] = prim.GetAttribute("height").Get()
            node["axis"] = prim.GetAttribute("axis").Get()
        elif kind == "Sphere":
            node["radius"] = prim.GetAttribute("radius").Get()
        elif kind == "SphereLight":
            light = UsdLux.SphereLight(prim)
            node["color"] = list(light.GetColorAttr().Get())
            node["intensity"] = light.GetIntensityAttr().Get()
        if kind in SHAPES:
            node["material"] = _material(stage, prim, materials)
        index[node["path"]] = len(nodes)
        nodes.append(node)
    return {"nodes": nodes, "materials": materials}


def shot(stage: Usd.Stage, nodes: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    moving, toggled = [], []
    for i, node in enumerate(nodes):
        prim = stage.GetPrimAtPath(node["path"])
        moves, vis = _animated(prim)
        if moves:
            moving.append(i)
        if vis:
            toggled.append(i)
    camera = UsdGeom.Camera(stage.GetPrimAtPath("/World/Cameras/Shot"))
    frames = int(stage.GetEndTimeCode()) + 1
    visible: list[list[int]] = [[] for _ in toggled]
    with out.with_suffix(".bin").open("wb") as handle:
        for f in range(frames):
            t = Usd.TimeCode(f)
            for i in moving:
                xf = UsdGeom.Xformable(stage.GetPrimAtPath(nodes[i]["path"]))
                handle.write(struct.pack("<16f", *_matrix(xf.GetLocalTransformation(t))))
            handle.write(struct.pack("<16f", *_matrix(camera.ComputeLocalToWorldTransform(t))))
            for k, i in enumerate(toggled):
                attr = UsdGeom.Imageable(stage.GetPrimAtPath(nodes[i]["path"])).GetVisibilityAttr()
                visible[k].append(0 if attr.Get(t) == UsdGeom.Tokens.invisible else 1)
    return {
        "frames": frames,
        "fps": stage.GetTimeCodesPerSecond(),
        "moving": moving,
        "toggled": toggled,
        "visible": [_runs(v) for v in visible],
        "focal": camera.GetFocalLengthAttr().Get(),
        "vertical_aperture": camera.GetVerticalApertureAttr().Get(),
        "note": stage.GetMetadata("comment") or "",
    }


def _runs(values: list[int]) -> list[list[int]]:
    """[[first frame, value], ...] at each change."""
    out: list[list[int]] = []
    for f, v in enumerate(values):
        if not out or out[-1][1] != v:
            out.append([f, v])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the film's shots for the browser renderer")
    parser.add_argument("film", type=Path)
    parser.add_argument("--only", nargs="*", help="only these shots (names or prefixes)")
    args = parser.parse_args()
    shots = sorted((args.film / "shots").glob("*.usdc"))
    if args.only:
        shots = [s for s in shots if any(s.stem.startswith(p) for p in args.only)]
    out = args.film / "web"
    out.mkdir(parents=True, exist_ok=True)
    first = Usd.Stage.Open(str(shots[0]))
    static = scene(first)
    (out / "scene.json").write_text(json.dumps(static), encoding="utf-8")
    listing = []
    for path in shots:
        stage = Usd.Stage.Open(str(path))
        meta = shot(stage, static["nodes"], out / path.stem)
        (out / f"{path.stem}.json").write_text(json.dumps(meta), encoding="utf-8")
        listing.append({"name": path.stem, "frames": meta["frames"], "note": meta["note"]})
        sys.stdout.write(
            f"exported {path.stem}: {meta['frames']} frames, {len(meta['moving'])} moving prims\n"
        )
    (out / "shots.json").write_text(json.dumps(listing, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
