"""Render the film's shots with Isaac Sim's RTX renderer, frame by frame, to PNGs.

    ~/isaacsim/python.sh demo/film/render_isaac.py film                 # every shot, preview
    ~/isaacsim/python.sh demo/film/render_isaac.py film --quality final # path traced
    ~/isaacsim/python.sh demo/film/render_isaac.py film --only 04_jam --frames 0 30

Reads ``<film>/shots/*.usdc`` (``build_stage.py``) and writes ``<film>/frames/<shot>/rgb_NNNN.png``.
Each frame sets the stage's time to the frame, so the time samples ``build_stage.py`` wrote are all
the animation there is: nothing simulates here. ``cut.sh`` turns the frames into video.

Quality: ``preview`` is real-time ray tracing (seconds per frame); ``final`` is the path tracer with
``--spp`` samples per frame and the denoiser (tens of seconds per frame on one RTX GPU).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

QUALITY = {
    "preview": {"mode": "RaytracedLighting", "subframes": 4},
    "final": {"mode": "PathTracing", "subframes": 1},
}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the film's shots with Isaac Sim")
    parser.add_argument("film", type=Path, help="the --out folder of build_stage.py")
    parser.add_argument("--only", nargs="*", help="render only these shots (names or prefixes)")
    parser.add_argument("--quality", choices=sorted(QUALITY), default="preview")
    parser.add_argument("--spp", type=int, default=64, help="path-traced samples per frame (final)")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--frames", type=int, nargs=2, metavar=("FIRST", "LAST"), help="a frame range"
    )
    parser.add_argument("--every", type=int, default=1, help="render every Nth frame (quick looks)")
    parser.add_argument("--window", action="store_true", help="open the Isaac Sim window")
    return parser.parse_args()


def main() -> int:
    args = _args()
    shots = sorted((args.film / "shots").glob("*.usdc"))
    if args.only:
        shots = [s for s in shots if any(s.stem.startswith(p) for p in args.only)]
    if not shots:
        sys.stderr.write(f"no shots in {args.film / 'shots'}: run build_stage.py first\n")
        return 2
    quality = QUALITY[args.quality]

    try:
        from isaacsim import SimulationApp  # Isaac Sim 4.5 and later
    except ImportError:
        from omni.isaac.kit import SimulationApp  # older releases

    app = SimulationApp(
        {
            "headless": not args.window,
            "width": args.width,
            "height": args.height,
            "renderer": quality["mode"],
        }
    )
    import carb
    import omni.kit.app
    import omni.timeline
    import omni.usd

    manager = omni.kit.app.get_app().get_extension_manager()
    manager.set_extension_enabled_immediate("omni.replicator.core", True)
    import omni.replicator.core as rep

    settings = carb.settings.get_settings()
    settings.set("/rtx/rendermode", quality["mode"])
    if quality["mode"] == "PathTracing":
        settings.set("/rtx/pathtracing/spp", args.spp)
        settings.set("/rtx/pathtracing/totalSpp", args.spp)
        settings.set("/rtx/pathtracing/optixDenoiser/enabled", True)
    settings.set("/omni/replicator/captureOnPlay", False)

    context = omni.usd.get_context()
    timeline = omni.timeline.get_timeline_interface()
    for shot in shots:
        context.open_stage(str(shot.resolve()))
        while context.get_stage_loading_status()[2] > 0:
            app.update()
        for _ in range(10):
            app.update()
        stage = context.get_stage()
        fps = stage.GetTimeCodesPerSecond()
        last = int(stage.GetEndTimeCode())
        first, stop = (args.frames[0], min(args.frames[1], last)) if args.frames else (0, last)
        out = args.film / "frames" / shot.stem
        out.mkdir(parents=True, exist_ok=True)
        timeline.stop()
        timeline.set_time_codes_per_second(fps)
        product = rep.create.render_product("/World/Cameras/Shot", (args.width, args.height))
        writer = rep.WriterRegistry.get("BasicWriter")
        writer.initialize(output_dir=str(out.resolve()), rgb=True, frame_padding=4)
        writer.attach([product])
        began = time.monotonic()
        frames = list(range(first, stop + 1, args.every))
        for i, frame in enumerate(frames):
            timeline.set_current_time(frame / fps)
            app.update()
            rep.orchestrator.step(rt_subframes=quality["subframes"], pause_timeline=True)
            if i % 30 == 0:
                done = (time.monotonic() - began) / (i + 1)
                sys.stdout.write(f"{shot.stem}: frame {frame}/{stop} ({done:.1f} s a frame)\n")
                sys.stdout.flush()
        rep.orchestrator.wait_until_complete()
        writer.detach()
        product.destroy()
        sys.stdout.write(f"{shot.stem}: {len(frames)} frames in {out}\n")
    app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
