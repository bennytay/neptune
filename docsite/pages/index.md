# Neptune

Neptune is a data compiler for physical AI. It takes the evidence a robotics team already has (MCAP and
ROS recordings, URDF, calibration files, configs, maintenance tickets, incident reports, PDFs, site
records) and turns it into **claims**. A claim says one thing about a robot, a run, a site or a
configuration. It also names the evidence behind it and says whether that thing was observed, stated by
someone, or inferred by a model. Agents, engineers and training pipelines read the claims; nothing
downstream parses the raw files again.

It works for every kind of robot: arms and manipulator cells, mobile bases and AMR fleets, legged
robots, humanoids, aerial, marine and underwater vehicles, autonomous cars and trucks, and mixed fleets.

## How the pieces fit

```text
raw evidence (recordings, descriptions, calibrations, configs, tickets, documents)
   │
   ▼  Compiler  ─ one canonical, provenance-preserving ingest package per hand-over
   ▼  Ledger    ─ registers packages; catalog of records, threads, lineage; transaction clock
   ▼  Memory    ─ bi-temporal claim graph: observed / stated / inferred, never merged
   ▼  Context   ─ typed queries → context packets with evidence links (SDK, MCP for agents)
   │
   └─ Deploy    ─ lifecycle evidence in (work orders, incidents, changes); evidence packs out
```

## Start here

New to Neptune? Read these short pages in order before the reference material.

```{toctree}
:maxdepth: 1
:caption: Start here

concepts/claims
concepts/evidence-and-inference
concepts/as-of
concepts/what-neptune-is-not
quickstart
deployment-targets
```

```{toctree}
:maxdepth: 1
:caption: Layers

layers/compiler
layers/ledger
layers/memory
layers/context
layers/deploy
layers/platform
```

```{toctree}
:maxdepth: 1
:caption: Reference

contracts/index
api/index
```
