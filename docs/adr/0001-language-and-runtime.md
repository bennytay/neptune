# 0001 — Language and runtime

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

Neptune parses a long tail of formats: MCAP, ROS 1 bags, ROS 2 bags, PX4 ULog, URDF/SDF, YAML/JSON configs, PDFs,
images, meshes, CAD, GIS, CSV and Parquet registers. Most of the mature open-source readers for these formats are
Python packages or Python bindings over C/C++. Examples: `mcap`, `rosbags`, `pyulog`, `pyarrow`, PDF and image
libraries, and `trimesh`. The robotics teams whose data we ingest work in Python. The agents that write most of
this code are strongest in Python.

The pressure the other way is throughput. Inputs can be hundreds of GB, and a pure-Python decode loop over
millions of small messages is slow. The audit ranks the Python throughput ceiling as risk #6.

Pinning an interpreter or toolchain late is expensive. Every adapter, lockfile and CI job depends on it.

## Decision

1. **Language.** Neptune is a Python package with a `src/` layout and a **Python ≥ 3.11** floor. The development
   and CI interpreter is pinned in `.python-version`, currently 3.12. The floor rises only through a new ADR.
2. **Toolchain.** `uv` manages the interpreter, the virtualenv and dependencies. `uv.lock` is committed and CI
   installs from it. The build backend is `hatchling`. Lint and format are `ruff`. Types are `mypy --strict` over
   `src` and `tests`. Tests are `pytest` + `hypothesis`. `make check` is the only gate (see `AGENTS.md`).
3. **No ROS runtime dependency.** Neptune never imports `rclpy`, `rospy` or any ROS distribution package. ROS data
   is read from bytes by standalone libraries, so Neptune is independent of the Python version a ROS distribution
   ships.
4. **Dependency placement.** A format library is imported only inside the `adapters/<format>/` subpackage that
   needs it. `model/` and `identity/` depend only on the standard library, plus at most one data-modelling
   library, chosen and justified in MVL-1. `store/` may depend on `pyarrow`.
5. **Parallelism uses processes, not threads.** CPU-bound work runs across worker processes. This is the same
   boundary as the adapter sandbox (ADR 0008, MVL-10), so the GIL never shapes a contract.
6. **Native-extension escape hatch: reserved, not used.** A compiled extension (Rust via PyO3/maturin preferred)
   is allowed only when all of these hold:
   - a benchmark shows a specific hot path dominates wall-clock;
   - a pure-Python reference implementation stays in the tree and produces byte-identical output, verified by a
     test;
   - the extension is recorded in its own ADR.
   Heavy columnar work goes through `pyarrow`, which is native already. The go/no-go decision on native
   extensions is taken explicitly at M9.

## Alternatives considered

- **Rust core with Python bindings.** Faster decode loops and memory safety. It lost because most format readers
  would need rewriting or FFI wrapping before any value ships. Contract iteration in M1–M2 would slow down. The
  bottleneck has not been measured yet. The escape hatch keeps this path open for proven hot spots.
- **C++.** Native to parts of the ROS ecosystem. Parsing hostile input in a memory-unsafe language raises the
  security cost (see `security.md`). Iteration speed is worse than Python and the tooling is less agent-friendly.
- **Go.** Good concurrency and single-binary deployment. The ecosystem for robotics, PDF, mesh and Parquet-nested
  formats is thin, so most adapters would start from zero.
- **Poetry / pip-tools instead of uv.** Both work. uv resolves and installs faster by an order of magnitude,
  manages the interpreter itself, and gives one lockfile for dev and CI.
- **Python 3.10 floor**, to match ROS 2 Humble's system Python. That is unnecessary because of decision 3. A
  3.10 floor would give up `tomllib`, `typing.Self`, `ExceptionGroup` and the 3.11 interpreter speedups.

## Consequences

- Adapters can wrap existing readers and ship quickly. The adapter ABI (ADR 0008) and the fixtures carry the
  correctness burden, not a bespoke parser stack.
- Per-message Python overhead is a known ceiling. Scale comes from chunk-level process parallelism, columnar
  batch writes through Arrow, and cheap `inspect` before `ingest`, not from micro-optimising loops.
- Every native extension adds a dual implementation to maintain. That cost is intended: it keeps the bar high.
- Byte-identical determinism (ADR 0002) is defined relative to a fixed `uv.lock`. Changing a dependency version is
  a potentially output-changing change (see ADR 0003 on adapter versions).
- Revisit if the M9 throughput benchmarks show Python-level decode dominating wall-clock on representative
  corpora after chunk parallelism. Also revisit if a required format has no usable Python reader.
