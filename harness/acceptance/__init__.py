"""The acceptance corpus: one messy two-site deployment, generated, versioned and locked.

Platform ADR 0007. ``build()`` returns every file of the corpus by its path under the corpus root;
``materialise(root)`` writes them. Nothing here reads a clock, randomness or the network, so the
bytes are the same on every host; ``corpus.lock.json`` pins them, and a changed byte without a new
``VERSION`` fails the tests. ``gold.json`` holds the questions the corpus is built to answer, with
claims that cite evidence by source path and selector (``harness.acceptance.resolve``).
``deploy.json`` declares the Deploy presets and templates the harness maps the compiled corpus with
(Platform ADR 0008); like ``gold.json`` it names the corpus version it was written for.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Final

NAME: Final = "acceptance"
# MAJOR: a gold answer changes meaning or evidence is removed. MINOR: files or questions are added
# and every existing answer still holds. PATCH: bytes change and no answer does. ADR 0007 section 3.
VERSION: Final = "2.1.0"
HERE: Final = Path(__file__).resolve().parent
LOCK: Final = HERE / "corpus.lock.json"
GOLD: Final = HERE / "gold.json"
# What the harness's deploy stage maps over the compiled corpus (Platform ADR 0008).
DEPLOY: Final = HERE / "deploy.json"
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
        "files": {
            path: {"sha256": digest(data), "size": len(data)} for path, data in files.items()
        },
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
    """What a gate quotes: ``acceptance 2.0.0 (tree sha256:...)``, from the committed lock."""
    lock = read_lock()
    return f"{lock['corpus']} {lock['version']} (tree {lock['tree']})"


class CorpusError(ValueError):
    """The corpus cannot be written where it was asked to go."""


def _strays(root: Path, known: set[str]) -> list[str]:
    """What under ``root`` is not a file of the corpus: other files, and any symlink."""
    out = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or (not path.is_dir() and relative not in known):
            out.append(relative)
    return out


def materialise(root: Path) -> Path:
    """Write the corpus under ``root`` and return ``root``.

    An existing ``root`` is replaced only when it is a directory holding nothing but corpus files
    (an earlier build); anything else is refused, so ``build .`` cannot delete a checkout. No
    marker file is written: the folder is ingested whole, and a marker would be one more source.
    """
    files = build()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise CorpusError(f"{root} is not a directory")
    if root.exists():
        strays = _strays(root, set(files))
        if strays:
            raise CorpusError(
                f"{root} holds {len(strays)} path(s) that are not the acceptance corpus "
                f"(first: {strays[0]}); refusing to replace it"
            )
        shutil.rmtree(root)  # only an earlier build of the corpus
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def lock_problems(files: dict[str, bytes]) -> list[str]:
    """Why ``files`` (a fresh build) break the committed lock or the size rule; empty when sound."""
    lock = read_lock()
    out = [
        f"{path} is {len(data)} bytes, over {MAX_FILE_BYTES}"
        for path, data in files.items()
        if len(data) > MAX_FILE_BYTES
    ]
    if lock.get("version") != VERSION:
        out.append(f"the lock is for {lock.get('version')}, VERSION is {VERSION}")
    if lock != lock_document(files):
        locked = lock.get("files", {})
        changed = sorted(
            set(locked).symmetric_difference(files)
            | {p for p, d in files.items() if locked.get(p, {}).get("sha256") != digest(d)}
        )
        out.append(
            "the corpus no longer matches corpus.lock.json"
            + (f" ({', '.join(changed)})" if changed else "")
            + ": bump VERSION (ADR 0007 section 3) and run `python -m harness.acceptance lock`"
        )
    return out


def lock_status() -> dict[str, Any]:
    """What the harness report says about the corpus it ran: version, tree, and lock agreement."""
    files = build()
    problems = lock_problems(files)
    return {
        "locked": not problems,
        "problems": problems,
        "tree": tree_digest(files),
        "version": VERSION,
    }
