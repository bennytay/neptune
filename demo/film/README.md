# Plant 7 on film: the simulated plant, rendered in Isaac Sim

The simulator (`neptune_memory.site.sim`) records every second of a run in
`sim/timeline.jsonl`. This folder turns that run into a 3D film:

```
make site-sim-run          →  site-sim/sim/scene.json + timeline.jsonl     (any machine)
build_stage.py             →  film/plant.usda + film/shots/*.usdc          (any machine, no GPU)
render_isaac.py            →  film/frames/<shot>/rgb_NNNN.png              (RTX machine)
  or export_web.py + web/render.js                                         (any machine, CPU)
cut.sh                     →  film/video/<shot>.mp4 + film/plant7.mp4      (ffmpeg)
```

- `plant.usda` is the hall and everything in it: floor and lane markings, conveyor CV-2 with photo-eye
  PE3 and its beacon, the fenced cell, arm C3, three AMRs with totes and status lights, the people,
  chargers, racks and lights. It is laid out from the simulator's `scene.json`.
- Each `shots/<NN_name>.usdc` adds one shot's motion and camera as USD time samples, one per video
  frame (time code = frame, 30 fps): the parts on the belt, the arm's joints (solved from the tool
  position the simulator records), the AMRs, the people walking, the lamps. Nothing is simulated at
  render time, so a frame always shows what the simulator did.
- The shots are picked from what happened in the run, so any seed films: the hall, the line, the arm,
  the first jam, the starved arm, the jam being cleared, a protective stop and its reset, a robot
  stopping on the lane and its rescue, and the shift as a time-lapse. `film/shots.json` lists them.

## Without a GPU: render in a headless browser

The same shots render with three.js in headless Chromium on the CPU (SwiftShader): physically based
materials, soft shadows, ambient occlusion, bloom, a site clock and captions. About 3-5 s a frame at
1280x720 on 4 cores, so the whole film takes a few hours on a plain cloud machine.

```bash
python demo/film/build_stage.py site-sim --out film
```
```bash
python demo/film/export_web.py film
```
```bash
(cd demo/film/web && npm install)
```
```bash
node demo/film/web/render.js film --width 1280 --height 720 --workers 2
```
```bash
demo/film/cut.sh film
```

`python` here is any Python with `pip install usd-core`. `render.js` takes `--only`, `--every`,
`--frames A B` and `--quality draft|preview|final` like `render_isaac.py`. The page is
`web/render.html`; the materials, lights and captions are set there.

## On the RTX machine

Needs Isaac Sim 4.5 or later (5.x recommended) on an NVIDIA RTX GPU, and ffmpeg. `~/isaacsim` below
is where Isaac Sim is installed; with the pip install (`pip install isaacsim[all]`), use that
environment's `python` instead of `~/isaacsim/python.sh`.

1. Run the plant for a day:

   ```bash
   make setup
   ```
   ```bash
   make site-sim-run DAYS=1 SEED=7
   ```

2. Build the stages (Isaac Sim's Python has USD; any Python with `pip install usd-core` works too):

   ```bash
   ~/isaacsim/python.sh demo/film/build_stage.py site-sim --out film
   ```

3. A quick look at one shot: every 10th frame, real-time ray tracing.

   ```bash
   ~/isaacsim/python.sh demo/film/render_isaac.py film --only 04_jam --every 10
   ```

4. Every shot, then the path-traced version:

   ```bash
   ~/isaacsim/python.sh demo/film/render_isaac.py film
   ```
   ```bash
   ~/isaacsim/python.sh demo/film/render_isaac.py film --quality final --spp 64
   ```

5. Cut the video:

   ```bash
   demo/film/cut.sh film
   ```

The film is `film/plant7.mp4`: about 97 s across 10 shots at 1920x1080.

### Looking around in the Isaac Sim window instead

Open `film/shots/04_jam.usdc` (File > Open), pick `/World/Cameras/Shot` as the viewport camera and
scrub the timeline: each frame is a video frame. Window > Movie Capture renders the same frames.
Edits you make there are not kept; change the scripts and rebuild.

## Changing the film

- **Reframe a shot:** edit `film/shots.json` (site start time, video seconds, speed, camera `eye` and
  `target` keyframes in hall metres), then `build_stage.py site-sim --out film --shots film/shots.json`.
  `--only 04_jam 06_clear` rebuilds just those.
- **Another run:** `make site-sim-run SEED=3` (or `DAYS=3` and `--day 2026-10-22`), then rebuild.
- **Brighter or darker:** `--light-scale 1.5` on `build_stage.py`. The hall uses a dome light, a sun
  and a grid of bay lights; signal lamps carry their own small lights.
- **Sharper or faster renders:** `--spp` (path-traced samples), `--width/--height`, `--every`.

## What to expect, and what is not here yet

- The models are built from USD primitives with plain materials, so the stage needs no downloaded
  assets. It reads as a clean, stylised factory, not a photograph. Swapping in NVIDIA's robot,
  warehouse and character assets is the next step for realism; the animation does not change.
- People slide with a simple leg swing; there is no skinned walk cycle yet.
- No overlays (citations, captions, the Neptune panel) yet: they come as a separate pass over these
  frames.
- `build_stage.py` and `cut.sh` have been run and checked here: the composed stage puts the gripper
  exactly at the simulator's tool position, every camera aims at its subject, and test renders of
  each shot's middle frame show the intended framing. `render_isaac.py` has not been run (no GPU
  here); if Isaac Sim reports an error, the Replicator calls in it are the likely place.
