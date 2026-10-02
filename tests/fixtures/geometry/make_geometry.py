"""Writes the geometry fixtures: small meshes for robots of every kind.

``python tests/fixtures/geometry/make_geometry.py`` rewrites every file here from these
definitions; the output is deterministic. Coordinates are multiples of 1/16, so they are exact in
float32 and every reader (the adapter, trimesh, pygltflib) agrees on them. ``oracle.json`` records
what an independent reader reports for each file (``test_geometry_fixtures.py`` checks it).

| file                      | robot                | format                                    |
|---------------------------|----------------------|-------------------------------------------|
| arm_link.obj              | manipulator link     | OBJ, o name, mtllib, quads                |
| quadruped_foot.stl        | quadruped foot       | binary STL                                |
| quadruped_hip.stl         | quadruped hip        | ASCII STL, named solid                    |
| mobile_base_chassis.ply   | mobile base chassis  | ASCII PLY, faces, TextureFile comment     |
| agv_fork.ply              | AGV fork             | binary little-endian PLY                  |
| av_lidar_scan.ply         | autonomous vehicle   | binary big-endian PLY point cloud         |
| marine_hull.gltf          | marine hull          | glTF JSON, external buffer and textures   |
| humanoid_torso.glb        | humanoid torso       | GLB, stored buffer chunk                  |
| amr_chassis.usda          | AMR chassis          | USD ASCII, metersPerUnit, sublayers       |
| rov_scene.usdc            | ROV scene            | USD crate bootstrap                       |
"""

import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
Vec = tuple[float, float, float]
Quad = tuple[int, int, int, int]


def box(low: Vec, high: Vec) -> tuple[list[Vec], list[Quad]]:
    """Eight corners and the six quad faces (0-based, counter-clockwise from outside)."""
    (x0, y0, z0), (x1, y1, z1) = low, high
    corners = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]  # fmt: skip
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return corners, quads


def triangles(quads: list[Quad]) -> list[tuple[int, int, int]]:
    return [tri for a, b, c, d in quads for tri in ((a, b, c), (a, c, d))]


def normal(p: Vec, q: Vec, r: Vec) -> list[float]:
    u = [q[i] - p[i] for i in range(3)]
    v = [r[i] - p[i] for i in range(3)]
    n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    length = sum(c * c for c in n) ** 0.5 or 1.0
    return [c / length for c in n]


def arm_link() -> bytes:
    corners, quads = box((0.0, -0.0625, -0.0625), (0.5, 0.0625, 0.0625))
    lines = [
        "# shoulder-to-elbow link of a 6-axis arm, metres",
        "mtllib arm_link.mtl",
        "o shoulder_link",
    ]
    lines += [f"v {x} {y} {z}" for x, y, z in corners]
    lines += ["usemtl anodised_aluminium"]
    lines += ["f " + " ".join(str(i + 1) for i in quad) for quad in quads]
    return ("\n".join(lines) + "\n").encode()


def quadruped_foot() -> bytes:
    corners, quads = box((-0.0625, -0.0625, 0.0), (0.0625, 0.0625, 0.125))
    out = bytearray(b"quadruped foot, LF leg".ljust(80, b"\x00"))
    tris = triangles(quads)
    out += struct.pack("<I", len(tris))
    for a, b, c in tris:
        p, q, r = corners[a], corners[b], corners[c]
        out += struct.pack("<12fH", *normal(p, q, r), *p, *q, *r, 0)
    return bytes(out)


def quadruped_hip() -> bytes:
    corners, quads = box((-0.125, -0.0625, -0.0625), (0.125, 0.0625, 0.0625))
    lines = ["solid hip_abduction"]
    for a, b, c in triangles(quads):
        p, q, r = corners[a], corners[b], corners[c]
        n = normal(p, q, r)
        lines += [f"  facet normal {n[0]:e} {n[1]:e} {n[2]:e}", "    outer loop"]
        lines += [
            f"      vertex {corners[i][0]:e} {corners[i][1]:e} {corners[i][2]:e}" for i in (a, b, c)
        ]
        lines += ["    endloop", "  endfacet"]
    lines.append("endsolid hip_abduction")
    return ("\n".join(lines) + "\n").encode()


def chassis_ply() -> bytes:
    corners, quads = box((-0.25, -0.1875, 0.0), (0.25, 0.1875, 0.125))
    lines = [
        "ply", "format ascii 1.0", "comment mobile base chassis, metres",
        "comment TextureFile chassis_paint.png",
        f"element vertex {len(corners)}", "property float x", "property float y",
        "property float z", f"element face {len(quads)}", "property list uchar int vertex_indices", "end_header",
    ]  # fmt: skip
    lines += [f"{x} {y} {z}" for x, y, z in corners]
    lines += [f"4 {a} {b} {c} {d}" for a, b, c, d in quads]
    return ("\n".join(lines) + "\n").encode()


def fork_ply() -> bytes:
    corners, quads = box((0.0, -0.0625, 0.0), (1.0, 0.0625, 0.0625))
    header = "\n".join([
        "ply", "format binary_little_endian 1.0", "comment AGV fork tine",
        f"element vertex {len(corners)}", "property float x", "property float y",
        "property float z", "property uchar intensity",
        f"element face {len(quads)}", "property list uchar int vertex_indices", "end_header", "",
    ])  # fmt: skip
    body = b"".join(struct.pack("<fffB", *c, 200) for c in corners)
    body += b"".join(struct.pack("<B4i", 4, *quad) for quad in quads)
    return header.encode() + body


def lidar_ply() -> bytes:
    points = [
        (1.0, 0.0, 0.5),
        (2.0, 0.25, 0.5),
        (-3.0, 1.5, 0.75),
        (0.5, -4.0, 1.0),
        (10.0, 2.0, 2.0),
    ]
    header = "\n".join([
        "ply", "format binary_big_endian 1.0", "comment roof lidar, one sweep",
        f"element vertex {len(points)}", "property double x", "property double y",
        "property double z", "end_header", "",
    ])  # fmt: skip
    return header.encode() + b"".join(struct.pack(">ddd", *p) for p in points)


def hull_arrays() -> tuple[list[Vec], list[tuple[int, int, int]], bytes, int]:
    corners, quads = box((-1.5, -0.5, 0.0), (1.5, 0.5, 0.75))
    tris = triangles(quads)
    positions = b"".join(struct.pack("<3f", *c) for c in corners)
    indices = b"".join(struct.pack("<3H", *t) for t in tris)
    return corners, tris, positions + indices, len(positions)


def hull_document(uri: str | None) -> dict[str, Any]:
    corners, tris, blob, split = hull_arrays()
    buffer: dict[str, Any] = {"byteLength": len(blob)}
    if uri is not None:
        buffer["uri"] = uri
    return {
        "asset": {"version": "2.0", "generator": "neptune fixture generator"},
        "scene": 0,
        "scenes": [{"name": "hull", "nodes": [0]}],
        "nodes": [{"name": "hull_node", "mesh": 0}],
        "meshes": [{"name": "hull_mesh", "primitives": [
            {"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [
            {"name": "gelcoat", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}
        ],
        "textures": [{"source": 0}],
        "images": [{"uri": "textures/hull%20diffuse.png"}],
        "buffers": [buffer],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": split, "target": 34962},
            {"buffer": 0, "byteOffset": split, "byteLength": len(blob) - split, "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(corners), "type": "VEC3",
             "min": [-1.5, -0.5, 0.0], "max": [1.5, 0.5, 0.75]},
            {"bufferView": 1, "componentType": 5123, "count": len(tris) * 3, "type": "SCALAR"},
        ],
    }  # fmt: skip


def marine_hull() -> bytes:
    document = hull_document("hull.bin")
    document["images"].append({"uri": "../shared/barnacle_mask.png"})
    document["images"].append({"uri": "data:image/png;base64,iVBORw0KGgo="})
    return (json.dumps(document, indent=2) + "\n").encode()


def humanoid_torso() -> bytes:
    document = hull_document(None)
    document["scenes"][0]["name"] = "torso"
    document["images"] = []
    document["textures"] = []
    document["materials"] = [{"name": "carbon_fibre"}]
    document["meshes"][0]["primitives"][0].pop("material", None)
    document["meshes"][0]["primitives"][0]["material"] = 0
    text = json.dumps(document, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    blob = hull_arrays()[2]
    blob += b"\x00" * (-len(blob) % 4)
    total = 12 + 8 + len(text) + 8 + len(blob)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(text), 0x4E4F534A) + text
        + struct.pack("<II", len(blob), 0x004E4942) + blob
    )  # fmt: skip


def amr_chassis() -> bytes:
    return b'''#usda 1.0
(
    defaultPrim = "chassis"
    doc = """AMR chassis. A doc string may say upAxis = "Y" and metersPerUnit = 1 without meaning it."""
    metersPerUnit = 0.01
    subLayers = [
        @./materials/steel.usda@,
        @../shared/wheels.usda@
    ]
    upAxis = "Z"
)

def Xform "chassis"
{
    def Mesh "body"
    {
        point3f[] points = [(-25, -18.75, 0), (25, -18.75, 0), (25, 18.75, 12.5)]
    }
}
'''


def rov_scene() -> bytes:
    bootstrap = b"PXR-USDC" + bytes([0, 8, 0, 0, 0, 0, 0, 0]) + struct.pack("<Q", 88)
    return bootstrap + b"\x00" * 64 + b"\x01" * 24


FILES: dict[str, Callable[[], bytes]] = {
    "arm_link.obj": arm_link,
    "quadruped_foot.stl": quadruped_foot,
    "quadruped_hip.stl": quadruped_hip,
    "mobile_base_chassis.ply": chassis_ply,
    "agv_fork.ply": fork_ply,
    "av_lidar_scan.ply": lidar_ply,
    "marine_hull.gltf": marine_hull,
    "humanoid_torso.glb": humanoid_torso,
    "amr_chassis.usda": amr_chassis,
    "rov_scene.usdc": rov_scene,
}


if __name__ == "__main__":
    for name, build in FILES.items():
        (HERE / name).write_bytes(build())
