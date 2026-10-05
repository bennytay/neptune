"""The acceptance corpus: one messy two-site deployment, generated, versioned and locked.

Platform ADR 0006. ``build()`` returns every file of the corpus by its path under the corpus root;
``materialise(root)`` writes them. Nothing here reads a clock, randomness or the network, so the
bytes are the same on every host; ``corpus.lock.json`` pins them, and a changed byte without a new
``VERSION`` fails the tests. ``gold.json`` holds the questions the corpus is built to answer, with
claims that cite evidence by source path and selector (``harness.acceptance.resolve``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Final

NAME: Final = "acceptance"
# MAJOR: a gold answer changes meaning or evidence is removed. MINOR: files or questions are added
# and every existing answer still holds. PATCH: bytes change and no answer does. ADR 0006 section 3.
VERSION: Final = "1.0.0"
HERE: Final = Path(__file__).resolve().parent
LOCK: Final = HERE / "corpus.lock.json"
GOLD: Final = HERE / "gold.json"
MAX_FILE_BYTES: Final = 512 * 1024
LOCK_FORMAT: Final = 1


def build() -> dict[str, bytes]:
    """Every file of the corpus, by POSIX path relative to its root, in sorted order."""
    from harness.acceptance import generate

    files = generate.build()
    return {path: files[path] for path in sorted(files)}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tree_digest(files: dict[str, bytes]) -> str:
    """One id for the whole tree: the sha256 of its sorted ``path NUL digest LF`` lines."""
    lines = "".join(f"{path}\0{digest(data)}\n" for path, data in sorted(files.items()))
    return digest(lines.encode())


def lock_document(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "corpus": NAME,
        "files": {path: {"sha256": digest(data), "size": len(data)} for path, data in files.items()},
        "lock_format": LOCK_FORMAT,
        "tree": tree_digest(files),
        "version": VERSION,
    }


def render_lock(files: dict[str, bytes]) -> str:
    return json.dumps(lock_document(files), indent=2, sort_keys=True) + "\n"


def read_lock() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(LOCK.read_text(encoding="utf-8"))
    return document


def label() -> str:
    """What a gate quotes: ``acceptance 1.0.0 (tree sha256:...)``, from the committed lock."""
    lock = read_lock()
    return f"{lock['corpus']} {lock['version']} (tree {lock['tree']})"


def materialise(root: Path) -> Path:
    """Write the corpus under ``root`` (replacing what is there) and return ``root``."""
    if root.exists():
        shutil.rmtree(root)  # only a directory the caller names for the generated corpus
    for relative, data in build().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root
