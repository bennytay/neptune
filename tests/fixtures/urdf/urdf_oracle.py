"""The official readers' view of the fixtures; run by ``make_urdf.py --oracle``, never by tests.

Runs outside the project, under ``uv run --no-project --with xacro==2.1.1 --with
urdf-parser-py==0.0.4``, and writes ``oracle/``:

- ``<robot>.json``: what ``urdf_parser_py`` reads from each robot in ``robots/``: links, joints
  (type, parent, child, origin, axis, limit) and materials;
- ``quadrotor.expanded.urdf``: real xacro's expansion of ``xacro/quadrotor.urdf.xacro``, and
  ``quadrotor.xacro.json``: ``urdf_parser_py``'s reading of that expansion.
"""

import json
import sys
from pathlib import Path
from typing import Any

import xacro  # type: ignore[import-not-found]
from urdf_parser_py.urdf import URDF  # type: ignore[import-not-found]


def summary(text: str) -> dict[str, Any]:
    model = URDF.from_xml_string(text)

    def pose(origin: Any) -> dict[str, list[float]] | None:
        if origin is None:
            return None
        return {"rpy": list(origin.rpy or [0, 0, 0]), "xyz": list(origin.xyz or [0, 0, 0])}

    joints = []
    for joint in model.joints:
        limit = joint.limit
        joints.append(
            {
                "axis": None if joint.axis is None else list(joint.axis),
                "child": joint.child,
                "limit": None
                if limit is None
                else {
                    "effort": limit.effort,
                    "lower": limit.lower,
                    "upper": limit.upper,
                    "velocity": limit.velocity,
                },
                "name": joint.name,
                "origin": pose(joint.origin),
                "parent": joint.parent,
                "type": joint.type,
            }
        )
    return {
        "joints": joints,
        "links": [link.name for link in model.links],
        "materials": [material.name for material in model.materials],
        "name": model.name,
    }


def write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def main(root: Path) -> None:
    out = root / "oracle"
    out.mkdir(exist_ok=True)
    for path in sorted((root / "robots").glob("*.urdf")):
        write(out / f"{path.stem}.json", summary(path.read_text()))
    document = xacro.process_file(str(root / "xacro" / "quadrotor.urdf.xacro"))
    expanded = document.documentElement.toxml()
    (out / "quadrotor.expanded.urdf").write_text(expanded + "\n")
    write(out / "quadrotor.xacro.json", summary(expanded))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
