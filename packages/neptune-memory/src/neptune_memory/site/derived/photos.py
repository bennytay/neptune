"""Real photo extraction (spec §7): send each photo to Claude, record the answer next to it.

``extract_photos(site_dir)`` writes ``static/photos/<name>.extraction.json``, the same recorded
model output the corpus ships (model id, prompt, prompt hash, regions). The build only ever reads
that file, so replays stay deterministic and never call a model. Without ``ANTHROPIC_API_KEY`` the
recorded files are kept as they are.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

PROMPT = "List each handwritten note on the whiteboard photo with its bounding box."
MODEL = "claude-opus-5-5"
SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_TOOL = {
    "name": "record_notes",
    "description": "Record every handwritten note found in the photo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "regions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "box": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": "x0, y0, x1, y1 in pixels from the top-left corner",
                        },
                        "text": {"type": "string", "description": "the note as written"},
                    },
                    "required": ["box", "text"],
                },
            }
        },
        "required": ["regions"],
    },
}


def prompt_sha256() -> str:
    return hashlib.sha256(PROMPT.encode()).hexdigest()


def recorded(image: str, regions: list[dict[str, Any]], model: str = MODEL) -> str:
    """The ``.extraction.json`` text for a model's answer: regions in reading order, ids from 1."""
    ordered = sorted(regions, key=lambda r: (r["box"][1], r["box"][0], r["text"]))
    record = {
        "image": image,
        "model": model,
        "prompt": PROMPT,
        "prompt_sha256": prompt_sha256(),
        "regions": [
            {"id": number, "box": [int(v) for v in r["box"]], "text": r["text"]}
            for number, r in enumerate(ordered, start=1)
        ],
    }
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def ask(client: Any, path: Path, model: str = MODEL) -> list[dict[str, Any]]:
    """One photo to Claude; the notes it records through the forced ``record_notes`` tool."""
    data = base64.standard_b64encode(path.read_bytes()).decode()
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": SUFFIXES[path.suffix.lower()], "data": data},
    }
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_notes"},
        messages=[{"role": "user", "content": [image, {"type": "text", "text": PROMPT}]}],
    )
    for block in message.content:
        if block.type == "tool_use":
            return list(block.input["regions"])
    return []


def extract_photos(site_dir: Path, *, client: Any = None) -> list[Path]:
    """Re-record every photo under ``<site_dir>/static/photos``; ``[]`` when there is no API key.

    ``client`` defaults to ``anthropic.Anthropic()`` (it reads ``ANTHROPIC_API_KEY``).
    """
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return []
        import anthropic

        client = anthropic.Anthropic()
    written: list[Path] = []
    folder = site_dir / "static" / "photos"
    for photo in sorted(p for p in folder.glob("*") if p.suffix.lower() in SUFFIXES):
        target = photo.with_suffix(".extraction.json")
        target.write_text(recorded(photo.name, ask(client, photo)), encoding="utf-8")
        written.append(target)
    return written
