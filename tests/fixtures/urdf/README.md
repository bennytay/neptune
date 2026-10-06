# URDF and Xacro fixtures

`make_urdf.py` generates every file here deterministically; `test_urdf_adapter.py` checks the
committed files are exactly what it gives. Each is far below 512 KB.

| File | Shaped like | What it exercises |
|---|---|---|
| `robots/arm6.urdf` | a UR5e-like 6-DoF arm | revolute joints with limits, dynamics, safety controllers; inertials; mesh visuals and scaled collisions; materials and a texture; transmissions; `ros2_control`; a wrist camera in `<gazebo>` |
| `robots/diff_drive.urdf` | a TurtleBot-like differential-drive base | continuous wheels, primitive geometry, lidar, IMU and camera sensors in `<gazebo>`, a drive plugin |
| `robots/quadrotor.urdf` | an Iris-like quadrotor | four rotors, a URDF `<sensor>` (downward camera) at its parent link, motor plugins |
| `renamed/robot_description` | the diff-drive base without an extension | probing decides from the bytes |
| `xacro/quadrotor.urdf.xacro` | the quadrotor in Xacro | properties, math, a block property, a macro with defaults and a block, `if`/`unless`, args; expands exactly as real xacro |
| `xacro/diff_drive.urdf.xacro` | the base in Xacro, split across files | `$(find)`, `$(env)`, an argument with no default, an include and a macro from it, an undefined property: `NotCovered` and findings, never guesses |
| `corrupt/*` | | empty, truncated, invalid UTF-8, a declared Latin-1 encoding, a non-robot root, and bad values (unparsable numbers, repeated elements and names, missing type and links, a `nan` origin, a joint that is its own parent) |
| `hostile/*` | | billion laughs, an external entity, 5,000-deep nesting, a 100 KB attribute, a macro bomb (10⁹ elements), unbounded recursion, a circular property, expression attacks (`9**9**9`, `'a'*10**9`, `__import__`, dunder access, lambdas, comprehensions, 400 nested parentheses), a self-include and an include of `/etc/passwd`, a property doubling past 64 KiB |

## Oracles

`python tests/fixtures/urdf/make_urdf.py --oracle` runs the official readers once, outside the
project, with `uv run --no-project --with xacro==2.1.1 --with urdf-parser-py==0.0.4` (network on
first use), and writes `oracle/`:

- `arm6.json`, `diff_drive.json`, `quadrotor.json`: `urdf_parser_py`'s reading of each robot
  (links, joint types, parents, children, origins, axes, limits);
- `quadrotor.expanded.urdf`: real xacro's expansion of `xacro/quadrotor.urdf.xacro`, and
  `quadrotor.xacro.json`, `urdf_parser_py`'s reading of it.

Tests compare the adapter with these files offline. Neither library is a project dependency.
`urdf_parser_py` fills in the specification's defaults (an absent lower limit reads 0); the
adapter does not, and the tests check exactly that difference.
