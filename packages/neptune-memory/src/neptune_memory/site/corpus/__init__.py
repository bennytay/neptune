"""The live-site corpora (spec §9): ``generate(out)`` writes ``static/`` and ``feed/``.

Two sites: Plant 7 (``p7``, the default) and Harbor DC (``harbor``, a warehouse hub of other robot
morphologies in other export shapes; ``harbor_feeds.py`` and ``harbor_static.py``).

Deterministic: the same code writes the same bytes. See ``feeds.py`` for the feed payloads and
``static.py`` for the static column headers.
"""

from pathlib import Path

from neptune_memory.site.corpus import docs, feeds, harbor_feeds, harbor_static, static

SITES = {"p7": "Plant 7", "harbor": "Harbor DC"}


def generate(out: Path, *, site: str = "p7") -> None:
    """Write the corpus of ``site`` under ``out`` (created if needed), overwriting its files."""
    if site == "harbor":
        files = harbor_static.files()
        for name, lines in harbor_feeds.FEEDS.items():
            files[name] = ("\n".join(lines()) + "\n").encode()
    elif site == "p7":
        files = {name: text.encode() for name, text in static.FILES.items()}
        files.update(docs.files())
        for name, lines in feeds.FEEDS.items():
            files[name] = ("\n".join(lines()) + "\n").encode()
    else:
        raise ValueError(f"unknown site {site!r}; choose one of {sorted(SITES)}")
    for name, data in sorted(files.items()):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
