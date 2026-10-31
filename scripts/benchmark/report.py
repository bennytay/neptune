"""Turn bench-results/runs.json into an HTML report with inline SVG charts."""

import json
import statistics as st
import sys
from html import escape
from pathlib import Path

runs = json.loads(Path(sys.argv[1]).read_text())
OUT = Path(sys.argv[2])
METRICS = {
    "parts_out": "Parts out",
    "jams": "Conveyor jams",
    "conveyor_stopped_min": "Conveyor stopped (min)",
    "arm_starved_min": "Arm starved (min)",
    "arm_pstop_min": "Arm protective-stop (min)",
    "amr_stranded": "AMRs stranded",
}


def total(r, m):
    return sum(d[m] or 0 for d in r["days"])


def pick(fixes=(), speed=None, charge=None):
    return [
        r
        for r in runs
        if r["fixes"] == sorted(fixes) and r["speed"] == speed and r["charge"] == charge
    ]


def agg(rs, m):
    v = [total(r, m) for r in rs]
    return st.mean(v), min(v), max(v)


FIXES = ["speed", "pm", "charge", "stagger", "prestage"]
LABEL = {
    "baseline": "Baseline (as the site runs)",
    "speed": "Belt back to 0.80 m/s",
    "pm": "Clean PE3 lens every 4 h",
    "charge": "Charge AMRs at 35 % (not 20 %)",
    "stagger": "Top up idle AMRs one at a time",
    "prestage": "Pre-stage next AMR at pickup",
    "all": "All five together",
}
configs = {"baseline": pick(), **{f: pick([f]) for f in FIXES}, "all": pick(FIXES)}
base = {m: agg(configs["baseline"], m)[0] for m in METRICS}
allf = {m: agg(configs["all"], m)[0] for m in METRICS}
n_seeds = len(configs["baseline"])
n_days = len(configs["baseline"][0]["days"])

# --- SVG helpers -------------------------------------------------------------------------------


def nice_max(v):
    for step in (1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        top = step * 5
        if v <= top:
            return top, step
    return v, v / 5


def hbar(title, rows, unit="", fmt="{:.0f}", lower_better=True):
    """rows: (label, mean, lo, hi, highlight). One series, so no legend box."""
    w, lab, bh, gap = 760, 270, 22, 12
    vmax, step = nice_max(max(r[3] for r in rows))
    pw = w - lab - 70
    h = 40 + len(rows) * (bh + gap) + 24
    x = lambda v: lab + pw * v / vmax
    s = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}">']
    t = 0
    while t <= vmax + 1e-9:
        s.append(f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="30" y2="{h - 24}"/>')
        s.append(
            f'<text class="tick" x="{x(t):.1f}" y="{h - 8}" text-anchor="middle">{fmt.format(t)}</text>'
        )
        t += step
    for i, (label, m, lo, hi, hl) in enumerate(rows):
        y = 34 + i * (bh + gap)
        cls = "bar hl" if hl else "bar"
        tip = f"{label}: mean {fmt.format(m)}{unit} (seeds range {fmt.format(lo)} to {fmt.format(hi)})"
        s.append(f'<g class="mark" data-tip="{escape(tip)}">')
        s.append(f'<rect class="hit" x="0" y="{y - 6}" width="{w}" height="{bh + 12}"/>')
        s.append(
            f'<text class="lab" x="{lab - 10}" y="{y + bh / 2 + 4}" text-anchor="end">{escape(label)}</text>'
        )
        bw = max(x(m) - lab, 2)
        s.append(
            f'<path class="{cls}" d="M{lab},{y} h{bw - 4:.1f} a4,4 0 0 1 4,4 v{bh - 8} a4,4 0 0 1 -4,4 h-{bw - 4:.1f} z"/>'
        )
        s.append(
            f'<line class="range" x1="{x(lo):.1f}" x2="{x(hi):.1f}" y1="{y + bh / 2}" y2="{y + bh / 2}"/>'
        )
        s.append(
            f'<text class="val" x="{x(max(m, hi)) + 8:.1f}" y="{y + bh / 2 + 4}">{fmt.format(m)}{unit}</text>'
        )
        s.append("</g>")
    s.append("</svg>")
    return f"<figure><figcaption>{escape(title)}</figcaption>{''.join(s)}</figure>"


def lines(title, xs, series, xlabel, fmt="{:.0f}", xfmt="{:.2f}", mark_x=None, ymin=0):
    """series: (name, slot, [(mean, lo, hi)]). Legend plus direct end labels."""
    w, h, l, r, t, b = 760, 300, 60, 150, 20, 44
    pw, ph = w - l - r, h - t - b
    allv = [v for _, _, pts in series for p in pts for v in p]
    vmax, step = nice_max(max(allv) - ymin)
    vmax += ymin
    X = lambda i: l + pw * (xs[i] - xs[0]) / (xs[-1] - xs[0])
    Y = lambda v: t + ph * (1 - (v - ymin) / (vmax - ymin))
    s = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{escape(title)}">']
    v = ymin
    while v <= vmax + 1e-9:
        s.append(f'<line class="grid" x1="{l}" x2="{l + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>')
        s.append(
            f'<text class="tick" x="{l - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{fmt.format(v)}</text>'
        )
        v += step
    for i, xv in enumerate(xs):
        s.append(
            f'<text class="tick" x="{X(i):.1f}" y="{t + ph + 18}" text-anchor="middle">{xfmt.format(xv)}</text>'
        )
    s.append(
        f'<text class="tick" x="{l + pw / 2}" y="{h - 4}" text-anchor="middle">{escape(xlabel)}</text>'
    )
    if mark_x is not None:
        i = xs.index(mark_x)
        s.append(f'<line class="ref" x1="{X(i):.1f}" x2="{X(i):.1f}" y1="{t}" y2="{t + ph}"/>')
        s.append(f'<text class="tick" x="{X(i) + 4:.1f}" y="{t + 10}">site today</text>')
    for name, slot, pts in series:
        band = " ".join(f"{X(i):.1f},{Y(p[2]):.1f}" for i, p in enumerate(pts))
        band += " " + " ".join(
            f"{X(i):.1f},{Y(p[1]):.1f}" for i, p in reversed(list(enumerate(pts)))
        )
        s.append(f'<polygon class="band s{slot}" points="{band}"/>')
        d = " ".join(
            ("M" if i == 0 else "L") + f"{X(i):.1f},{Y(p[0]):.1f}" for i, p in enumerate(pts)
        )
        s.append(f'<path class="line s{slot}" d="{d}"/>')
        for i, p in enumerate(pts):
            tip = f"{name} at {xfmt.format(xs[i])}: mean {fmt.format(p[0])} (seeds {fmt.format(p[1])} to {fmt.format(p[2])})"
            s.append(
                f'<g class="mark" data-tip="{escape(tip)}"><circle class="hit" cx="{X(i):.1f}" cy="{Y(p[0]):.1f}" r="12"/>'
                f'<circle class="dot s{slot}" cx="{X(i):.1f}" cy="{Y(p[0]):.1f}" r="4"/></g>'
            )
        s.append(
            f'<text class="lab" x="{X(len(xs) - 1) + 10:.1f}" y="{Y(pts[-1][0]) + 4:.1f}">{escape(name)}</text>'
        )
    s.append("</svg>")
    legend = "".join(
        f'<span class="key"><i class="sw s{slot}"></i>{escape(n)}</span>' for n, slot, _ in series
    )
    return f'<figure><figcaption>{escape(title)}</figcaption><div class="legend">{legend}</div>{"".join(s)}</figure>'


def pct(a, b):
    return (b - a) / a * 100 if a else 0.0


# --- Sections ----------------------------------------------------------------------------------

tiles = [
    (
        "Conveyor downtime",
        f"{pct(base['conveyor_stopped_min'], allf['conveyor_stopped_min']):+.0f}%",
        f"{base['conveyor_stopped_min']:.0f} → {allf['conveyor_stopped_min']:.0f} min",
    ),
    (
        "Jams",
        f"{pct(base['jams'], allf['jams']):+.0f}%",
        f"{base['jams']:.1f} → {allf['jams']:.1f}",
    ),
    (
        "AMRs stranded",
        f"{base['amr_stranded']:.0f} → {allf['amr_stranded']:.0f}",
        "dead battery on the lane",
    ),
    (
        "Arm starved",
        f"{pct(base['arm_starved_min'], allf['arm_starved_min']):+.0f}%",
        f"{base['arm_starved_min']:.0f} → {allf['arm_starved_min']:.0f} min",
    ),
    (
        "Parts out",
        f"{pct(base['parts_out'], allf['parts_out']):+.1f}%",
        f"{base['parts_out']:.0f} → {allf['parts_out']:.0f}",
    ),
]
tile_html = "".join(
    f'<div class="tile"><div class="tl">{escape(a)}</div><div class="tv">{escape(b)}</div><div class="ts">{escape(c)}</div></div>'
    for a, b, c in tiles
)

order = ["baseline", *FIXES, "all"]


def rows(m):
    return [(LABEL[k], *agg(configs[k], m), k == "all") for k in order]


ablation = (
    hbar(f"Conveyor stopped, minutes per {n_days} shifts", rows("conveyor_stopped_min"), " min")
    + hbar(f"Parts out per {n_days} shifts", rows("parts_out"))
    + hbar(f"AMRs stranded per {n_days} shifts", rows("amr_stranded"), fmt="{:.1f}")
)

speeds = sorted({r["speed"] for r in runs if r["speed"] is not None})
sp = lambda pm, m: [agg(pick(pm, speed=v), m) for v in speeds]
speed_html = lines(
    "Parts out vs belt speed",
    speeds,
    [
        ("No lens cleaning", 2, sp((), "parts_out")),
        ("Lens cleaned every 4 h", 1, sp(("pm",), "parts_out")),
    ],
    "belt setpoint (m/s)",
    mark_x=0.95,
    ymin=2500,
) + lines(
    "Conveyor jams vs belt speed",
    speeds,
    [("No lens cleaning", 2, sp((), "jams")), ("Lens cleaned every 4 h", 1, sp(("pm",), "jams"))],
    "belt setpoint (m/s)",
    mark_x=0.95,
)

charges = sorted({r["charge"] for r in runs if r["charge"] is not None})
ch = lambda m: [agg(pick(charge=v), m) for v in charges]
charge_html = lines(
    "AMRs stranded vs charge threshold",
    charges,
    [("AMRs stranded", 1, ch("amr_stranded"))],
    "send to charge below (% as the robot reports it)",
    fmt="{:.0f}",
    xfmt="{:.0f}",
    mark_x=20.0,
) + lines(
    "Parts out vs charge threshold",
    charges,
    [("Parts out", 1, ch("parts_out"))],
    "send to charge below (% as the robot reports it)",
    xfmt="{:.0f}",
    mark_x=20.0,
    ymin=2500,
)

days = list(range(1, n_days + 1))


def per_day(k, m):
    return [
        (
            st.mean(r["days"][d][m] or 0 for r in configs[k]),
            min(r["days"][d][m] or 0 for r in configs[k]),
            max(r["days"][d][m] or 0 for r in configs[k]),
        )
        for d in range(n_days)
    ]


day_html = lines(
    "Conveyor stopped per shift (min)",
    days,
    [
        ("Baseline", 2, per_day("baseline", "conveyor_stopped_min")),
        ("All five fixes", 1, per_day("all", "conveyor_stopped_min")),
    ],
    "shift",
    xfmt="day {:.0f}",
)

head = ["Configuration", *METRICS.values()]
table = (
    "<table><thead><tr>" + "".join(f"<th>{escape(h)}</th>" for h in head) + "</tr></thead><tbody>"
)
for k in order:
    table += f"<tr><td>{escape(LABEL[k])}</td>"
    for m in METRICS:
        mean, lo, hi = agg(configs[k], m)
        table += f'<td>{mean:,.1f}<span class="rng">{lo:,.0f}–{hi:,.0f}</span></td>'
    table += "</tr>"
table += "</tbody></table>"

CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;
 --text-muted:#7a7974;--grid:#e6e5e1;--neutral:#b9b8b2;--s1:#2a78d6;--s2:#eb6834;--card:#ffffff}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
 --surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--grid:#2e2e2b;
 --neutral:#5a5955;--s1:#3987e5;--s2:#d95926;--card:#222220}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--text-primary:#f0efec;
 --text-secondary:#c3c2b7;--text-muted:#8f8e86;--grid:#2e2e2b;--neutral:#5a5955;--s1:#3987e5;--s2:#d95926;--card:#222220}
body{margin:0}.viz-root{background:var(--surface-1);color:var(--text-primary);
 font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif;padding:32px;max-width:860px;margin:auto}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:40px 0 4px}p{color:var(--text-secondary);margin:6px 0}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--grid);border-radius:10px;padding:12px}
.tl{font-size:12px;color:var(--text-secondary)}.tv{font-size:24px;font-weight:650;margin:2px 0}.ts{font-size:12px;color:var(--text-muted)}
figure{margin:18px 0}figcaption{font-weight:600;font-size:14px;margin-bottom:4px}
svg{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}.ref{stroke:var(--text-muted);stroke-dasharray:3 3}
.tick{fill:var(--text-muted);font-size:11px}.lab{fill:var(--text-secondary);font-size:12px}.val{fill:var(--text-primary);font-size:12px}
.bar{fill:var(--neutral)}.bar.hl{fill:var(--s1)}.range{stroke:var(--text-primary);stroke-width:1.5;opacity:.55}
.hit{fill:transparent}.mark:hover .bar{opacity:.85}
.line{fill:none;stroke-width:2}.line.s1{stroke:var(--s1)}.line.s2{stroke:var(--s2)}
.band{opacity:.14;stroke:none}.band.s1{fill:var(--s1)}.band.s2{fill:var(--s2)}
.dot{stroke:var(--surface-1);stroke-width:2}.dot.s1{fill:var(--s1)}.dot.s2{fill:var(--s2)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--text-secondary)}.key{display:flex;align-items:center;gap:6px}
.sw{width:12px;height:3px;border-radius:2px;display:inline-block}.sw.s1{background:var(--s1)}.sw.s2{background:var(--s2)}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:10px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)}th:first-child,td:first-child{text-align:left}
th{color:var(--text-secondary);font-weight:600}.rng{display:block;color:var(--text-muted);font-size:11px}
#tip{position:fixed;pointer-events:none;background:var(--card);color:var(--text-primary);border:1px solid var(--grid);
 border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;max-width:320px}
.note{border-left:3px solid var(--neutral);padding-left:12px}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('.mark').forEach(g=>{
 g.addEventListener('mousemove',e=>{tip.textContent=g.dataset.tip;tip.style.display='block';
  tip.style.left=Math.min(e.clientX+12,innerWidth-340)+'px';tip.style.top=(e.clientY+12)+'px'});
 g.addEventListener('mouseleave',()=>tip.style.display='none')});
"""

html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neptune Plant 7 benchmark</title><style>{CSS}</style></head><body><div class="viz-root">
<h1>Simulated Plant 7: what the fixes are worth</h1>
<p>{n_seeds} seeds × {n_days} simulated shifts per configuration, {len(runs)} runs in all. Totals are per {n_days} shifts;
bars and lines are the mean across seeds, whiskers and bands the lowest and highest seed.</p>
<p class="note"><b>Read this first.</b> The plant is simulated and its hidden faults are known to whoever wrote it.
These fixes were applied by hand, so this measures <i>how much there is to win</i>, not yet that Neptune finds it
on its own. That is the closed-loop benchmark, which is not built yet.</p>
<div class="tiles">{tile_html}</div>
<p>Baseline vs all five fixes together.</p>
<h2>1 · Each fix on its own</h2>
<p>Which fix buys what. Blue is all five together.</p>
{ablation}
<h2>2 · The belt-speed trade-off</h2>
<p>CHG-311 raised CV-2 from 0.80 to 0.95 m/s on day 1. Jams rise steeply with speed and each costs 25–35 minutes
under lockout. Without lens cleaning, throughput peaks near 0.85 m/s and the speed-up loses parts. With the lens
cleaned every 4 h it peaks at 0.90–0.95 m/s: the speed-up was right; skipping the cleaning was the problem.
Parts-out axes start at 2,500.</p>
{speed_html}
<h2>3 · The charge threshold and the lying gauge</h2>
<p>amr-03's pack has aged: it reports about 22 % when it is empty, so at the site's 20 % threshold it dies on the lane.</p>
{charge_html}
<h2>4 · Shift by shift</h2>
{day_html}
<h2>All numbers</h2>
<p>Mean, with the lowest and highest seed beneath.</p>
{table}
<p style="margin-top:24px">Reproduce: <code>uv run --all-packages python scripts/benchmark/experiments.py runs.json</code>, then <code>scripts/benchmark/report.py runs.json report.html</code>.
Simulator: <code>packages/neptune-memory/src/neptune_memory/site/sim/</code>.</p>
</div><div id="tip"></div><script>{JS}</script></body></html>"""
OUT.write_text(html)
print("wrote", OUT)
