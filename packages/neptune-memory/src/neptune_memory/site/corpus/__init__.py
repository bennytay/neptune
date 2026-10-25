"""The live-site corpus of Plant 7 (spec §9): ``generate(out)`` writes ``static/`` and ``feed/``.

Deterministic: the same code writes the same bytes. See ``feeds.py`` for the feed payloads and
``static.py`` for the static column headers.
"""

from pathlib import Path

from neptune_memory.site.corpus import docs, feeds, static


def generate(out: Path) -> None:
    """Write the corpus under ``out`` (created if needed), overwriting files of the same name."""
    files: dict[str, bytes] = {name: text.encode() for name, text in static.FILES.items()}
    files.update(docs.files())
    for name, lines in feeds.FEEDS.items():
        files[name] = ("\n".join(lines()) + "\n").encode()
    for name, data in sorted(files.items()):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
