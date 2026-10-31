"""The benchmark's sites: each built once for Neptune, and copied raw for the other arms.

``<root>/<site>/`` is the built site (``static/``, ``feed/``, ``.neptune/``); ``<root>/<site>-raw/``
holds only the evidence a site already has: ``static/`` and ``feed/`` without Neptune's mapping
file or its recorded photo extraction, so the baselines read the photo themselves.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from neptune_memory.site import build, corpus

from neptune_context.bench.questions import ROOT

if TYPE_CHECKING:
    from pathlib import Path

NEPTUNE_ONLY = ("neptune.mappings.yaml", "*.extraction.json")


def prepare(site: str, root: Path) -> tuple[Path, Path]:
    """The built site and its raw copy, made if missing."""
    built, raw = root / site, root / f"{site}-raw"
    if not (built / ".neptune").exists():
        if site == "public":
            subprocess.run([str(ROOT / "scripts" / "fetch_public_site.sh"), str(built)], check=True)
        else:
            corpus.generate(built, site=site)
        build.build(built)
    if not raw.exists():
        for part in ("static", "feed"):
            if (built / part).exists():
                shutil.copytree(
                    built / part, raw / part, ignore=shutil.ignore_patterns(*NEPTUNE_ONLY)
                )
    return built, raw


def files(raw: Path) -> list[Path]:
    """The raw files, relative to the site folder, in a stable order."""
    return sorted(p.relative_to(raw) for p in raw.rglob("*") if p.is_file())
