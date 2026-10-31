"""Plant 7 benchmark: hand-applied fixes, one at a time and together, plus belt-speed and
charge-threshold sweeps. Usage: experiments.py <runs.json>; then report.py <runs.json> <report.html>."""

import json
import sys
import tempfile
from multiprocessing import Pool
from pathlib import Path

from neptune_memory.site.sim import run
from neptune_memory.site.sim.engine import Maintenance

SITE = Path(tempfile.mkdtemp(prefix="p7-bench-"))
DAYS = 5
SEEDS = list(range(1, 11))
FIXES = ["speed", "pm", "charge", "stagger", "prestage"]


def sim(job):
    seed, fx, speed, charge = job
    e = run.make_engine(SITE, seed)
    if "speed" in fx or speed is not None:
        e._scripted = [s for s in e._scripted if s[1] != "setpoint"]
        if speed is not None:
            e.at(e.t.replace(hour=5, minute=40), "setpoint", speed)
    if "pm" in fx:
        e.set_maintenance([Maintenance("PM-X", "CV-2", "Clean PE3 lens", 4.0)])
    if "charge" in fx:
        e.plan.charge_below = 35.0
    if charge is not None:
        e.plan.charge_below = charge
    if "stagger" in fx:
        e.plan.top_up_below = 60.0
    if "prestage" in fx:
        e.plan.prestage = True
    for start, end in run.windows(DAYS):
        if e.t < start:
            e.jump(start)
        while e.t < end:
            e.step()
    return {
        "seed": seed,
        "fixes": sorted(fx),
        "speed": speed,
        "charge": charge,
        "days": [v.summary() for _, v in sorted(e.kpis.items())],
    }


jobs = []
configs = {"baseline": [], **{f: [f] for f in FIXES}, "all": FIXES}
for name, fx in configs.items():
    jobs += [(s, tuple(fx), None, None) for s in SEEDS]
for sp in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00):
    for pm in ((), ("pm",)):
        jobs += [(s, pm, sp, None) for s in SEEDS]
for ch in (15, 20, 25, 30, 35, 40, 45):
    jobs += [(s, (), None, float(ch)) for s in SEEDS]
run.prepare(SITE, DAYS)
with Pool(4) as p:
    out = p.map(sim, jobs, chunksize=2)
Path(sys.argv[1]).write_text(json.dumps(out))
print(len(out), "runs")
