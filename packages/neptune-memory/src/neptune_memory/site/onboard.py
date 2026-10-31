"""Onboarding: a folder of a robot's data and a goal become a cited memory with a mission (#225).

An agent drives it, one step at a time (MCP tools ``neptune_setup_*``, or ``python -m
neptune_memory.site setup ...``); Neptune does the deterministic part, the agent the judgement:

1. ``start(site_dir, folder)`` copies every file under ``static/files/`` (the originals are never
   touched), writes a starting ``layout.yaml`` and an empty mapping, and builds. Everything is in
   memory at once: an unmapped table or document is stored as cited text, so it can be searched.
2. ``inventory`` says what each file is, how many facts it gave, and what it holds (a table's
   columns, a document's first blocks), and whether a mapping reads it yet.
3. ``draft(file)`` drafts a mapping entry for a table (``draft.draft_mapping``) and lists the
   questions only a person can answer: a column it cannot name, a reading with no unit, a table
   with no time. The agent answers what the files answer and asks the person the rest.
4. ``save("mappings" | "layout", text)`` checks the agent's YAML and writes it.
5. ``mission(prompt, goals, constraints)`` writes ``static/goal.md`` (the person's words, verbatim:
   a document, so it is cited like any other) and ``static/mission.yaml``.
6. ``ask`` / ``answer`` keep the open questions in ``setup/questions.jsonl``. A question nobody
   answered changes nothing: what it asks about stays ``unknown`` in the memory.
7. ``build`` again, then ``status``: what the memory holds, file by file, and what is open.

The same folder, drafts and answers give the same files, so the same memory.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from neptune_memory.site.build import build as build_log
from neptune_memory.site.draft import draft_mapping
from neptune_memory.site.log import read
from neptune_memory.site.mappers.config import MAPPINGS_FILE, Mapping, load_mapping
from neptune_memory.site.mappers.layout import MISSION

FILES = "files"  # under static/: the copied folder
GOAL = "goal.md"  # under static/: the person's goal, verbatim
QUESTIONS = "setup/questions.jsonl"
SOURCE = "files"  # the layout's source for every copied file (the unmapped records' source too)

KINDS: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".csv", ".tsv", ".jsonl", ".xlsx", ".xls"), "table"),
    ((".pdf", ".md", ".txt", ".docx", ".html"), "document"),
    ((".png", ".jpg", ".jpeg", ".webp"), "image"),
    ((".mcap", ".bag", ".db3"), "recording"),
    ((".urdf", ".xacro", ".sdf", ".yaml", ".yml", ".json", ".toml", ".xml"), "config"),
)
PREVIEW = 3


def kind_of(path: PurePosixPath) -> str:
    suffix = path.suffix.lower()
    return next((kind for suffixes, kind in KINDS if suffix in suffixes), "other")


# --- 1. start ---------------------------------------------------------------------------------


def start(site_dir: Path, folder: Path) -> str:
    """Copy ``folder`` into the site, write a starting layout and mapping, build; the status."""
    folder, site_dir = Path(folder), Path(site_dir)
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    static = site_dir / "static"
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = path.relative_to(folder)
        if any(part.startswith(".") for part in rel.parts):
            continue
        dest = static / FILES / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.read_bytes() != path.read_bytes():
            shutil.copyfile(path, dest)
    layout = static / "layout.yaml"
    if not layout.exists():
        name = folder.resolve().name
        layout.write_text(
            "# The declared site: what the agent and the person state. Edit it with what the\n"
            "# files name (robots, their parts, places), then build again.\n"
            f"site: {{id: {_q('site:' + _slug(name))}, name: {_q(name)}}}\n"
            "sources:\n"
            f"  - {{id: {SOURCE}, name: {_q('Files from ' + name)}}}\n",
            encoding="utf-8",
        )
    mapping = static / MAPPINGS_FILE
    if not mapping.exists():
        mapping.write_text(
            "# How this site's files become facts. Drafts from `setup draft`, checked, go here.\n"
            "mappings: 1\n",
            encoding="utf-8",
        )
    build_log(site_dir)
    return status(site_dir)


def build(site_dir: Path) -> str:
    """Build the memory from ``static/`` again; the status."""
    build_log(Path(site_dir))
    return status(site_dir)


# --- 2. inventory -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FileInfo:
    path: str  # under static/
    kind: str
    size: int
    mapped: bool
    facts: int
    preview: tuple[str, ...]


def files(site_dir: Path) -> list[FileInfo]:
    site_dir = Path(site_dir)
    static = site_dir / "static"
    mapping = load_mapping(site_dir)
    cited: Counter[str] = Counter()
    quotes: dict[str, list[str]] = {}
    for entry in read(site_dir) if (site_dir / ".neptune" / "log").exists() else []:
        fact = entry.fact
        if fact is None:
            continue
        for citation in fact.citations[:1]:
            rel = citation.file.removeprefix("static/")
            cited[rel] += 1
            quote = " ".join((citation.quote or "").split())[:120]
            seen = quotes.setdefault(rel, [])
            if quote and quote not in seen and len(seen) < PREVIEW:
                seen.append(quote)
    out = []
    for path in sorted(p for p in (static / FILES).rglob("*") if p.is_file()):
        rel = path.relative_to(static).as_posix()
        kind = kind_of(PurePosixPath(rel))
        preview = tuple(quotes.get(rel, []))
        if kind == "table":
            columns = _columns(site_dir, rel)
            preview = (f"columns: {', '.join(columns)}",) if columns else preview
        out.append(
            FileInfo(rel, kind, path.stat().st_size, _mapped(mapping, rel), cited[rel], preview)
        )
    return out


def inventory(site_dir: Path) -> str:
    lines = ["Files in the site (under static/):"]
    for info in files(site_dir):
        state = "mapped" if info.mapped else "not mapped (kept as cited text)"
        lines.append(f"- {info.path}: {info.kind}, {info.size} bytes, {info.facts} facts, {state}")
        lines += [f"    {text}" for text in info.preview]
    if len(lines) == 1:
        lines.append("- none: run setup start with a folder first")
    return "\n".join(lines)


def _mapped(mapping: Mapping, rel: str) -> bool:
    return any(
        mapping.match(section, rel) is not None
        for section in ("tables", "documents", "extractions")
    )


def _columns(site_dir: Path, rel: str) -> list[str]:
    draft = _draft_text(site_dir, rel)
    return re.findall(r'column: ("(?:[^"\\]|\\.)*")', draft) if draft else []


# --- 3. draft ---------------------------------------------------------------------------------


def _draft_text(site_dir: Path, rel: str) -> str | None:
    try:
        return draft_mapping(Path(site_dir), Path(site_dir) / "static" / rel)
    except (SystemExit, OSError, ValueError, UnicodeDecodeError):
        return None


def draft(site_dir: Path, file: str) -> str:
    """A mapping draft for one table, then the questions it leaves for a person."""
    rel = file.removeprefix("static/")
    text = _draft_text(site_dir, rel)
    if text is None:
        return f"{rel}: no table the compiler could read; it stays in memory as cited text."
    questions = questions_for(rel, text)
    lines = [text.rstrip(), "", "Questions this draft leaves (answer from the files, else ask):"]
    lines += [f"- {q}" for q in questions] or ["- none"]
    return "\n".join(lines)


_UNIT = re.compile(r"(°|deg|\b[ckf]\b|celsius|kelvin|fahrenheit|\[|\(|_c$|_k$|_f$)", re.I)


def questions_for(rel: str, draft_text: str) -> list[str]:
    """What a person must say before the draft can be saved, from the draft's own comments."""
    out = []
    for column, sample in re.findall(
        r'# - \{predicate: \?, column: ("(?:[^"\\]|\\.)*")\}\s+# e\.g\. ("(?:[^"\\]|\\.)*")',
        draft_text,
    ):
        name, example = json.loads(column), json.loads(sample)
        out.append(f"{rel}: what is column {name!r} (e.g. {example!r})? Map it, or leave it out.")
    for column in re.findall(r'predicate: temperature, column: ("(?:[^"\\]|\\.)*")', draft_text):
        name = json.loads(column)
        if not _UNIT.search(name):
            out.append(f"{rel}: what unit is {name!r} in? If nobody knows, it stays without one.")
    if "\n  time:" not in draft_text:
        out.append(f"{rel}: no column reads as a time. When were these rows written?")
    if "kind: stated" in draft_text:
        out.append(f"{rel}: did a machine write these rows (observed) or a person (stated)?")
    return out


# --- 4. save ----------------------------------------------------------------------------------


def save(site_dir: Path, which: str, text: str) -> str:
    """Check and write the agent's ``mappings`` or ``layout`` YAML into ``static/``."""
    site_dir = Path(site_dir)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("expected a YAML mapping at the top level")
    if which == "mappings":
        missing = [
            entry.get("file")
            for entry in data.get("tables", []) or []
            if not any((site_dir / "static").glob(str(entry.get("file", ""))))
        ]
        if missing:
            raise ValueError(f"no file under static/ matches {missing}")
        target = site_dir / "static" / MAPPINGS_FILE
    elif which == "layout":
        if "site" not in data:
            raise ValueError("a layout names its site: `site: {id: site:..., name: ...}`")
        target = site_dir / "static" / "layout.yaml"
    else:
        raise ValueError(f"save which? 'mappings' or 'layout', not {which!r}")
    target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return f"saved static/{target.name}; build again to see it in the memory"


# --- 5. mission -------------------------------------------------------------------------------


def mission(
    site_dir: Path,
    prompt: str,
    goals: list[dict[str, str]],
    constraints: list[str] | None = None,
) -> str:
    """Write the person's goal verbatim (``goal.md``) and as a mission (``mission.yaml``).

    ``goals``: ``{"name", "metric", "target"}`` each; ``constraints``: what any change must
    respect, as the person put it."""
    static = Path(site_dir) / "static"
    static.mkdir(parents=True, exist_ok=True)
    if not goals:
        raise ValueError("a mission needs at least one goal: {name, metric, target}")
    (static / GOAL).write_text(f"# Goal, as the person put it\n\n{prompt.strip()}\n", "utf-8")
    lines = [
        "# The mission: what this robot or site is for, and what any change must respect.",
        f"# Written from the person's goal in {GOAL}. Everything here is stated.",
        "goals:",
    ]
    for goal in goals:
        name = goal["name"]
        fields = [
            f"id: {_q('goal:' + _slug(name))}",
            f"name: {_q(name)}",
            f"metric: {_q(goal['metric'])}",
            f"target: {_q(goal['target'])}",
        ]
        lines.append(f"  - {{{', '.join(fields)}}}")
    if constraints:
        lines.append("constraints:")
        for text in constraints:
            lines.append(f"  - {{id: {_q('constraint:' + _slug(text)[:48])}, name: {_q(text)}}}")
    (static / MISSION).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"wrote static/{GOAL} and static/{MISSION}; build again to see the mission in memory"


# --- 6. questions -----------------------------------------------------------------------------


def _questions_path(site_dir: Path) -> Path:
    return Path(site_dir) / QUESTIONS


def open_questions(site_dir: Path) -> list[dict[str, Any]]:
    path = _questions_path(site_dir)
    if not path.exists():
        return []
    asked: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            asked.setdefault(item["id"], {}).update(item)
    return list(asked.values())


def ask(site_dir: Path, questions: list[str]) -> str:
    """Record questions for the person; asking one twice keeps one."""
    path = _questions_path(site_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    known = {q["id"] for q in open_questions(site_dir)}
    added = []
    with path.open("a", encoding="utf-8") as handle:
        for text in questions:
            qid = "q-" + _slug(text)[:40]
            if qid in known:
                continue
            known.add(qid)
            handle.write(json.dumps({"id": qid, "question": text}, ensure_ascii=False) + "\n")
            added.append(qid)
    return f"recorded {len(added)} question(s): {', '.join(added) or 'none new'}"


def answer(site_dir: Path, question: str, text: str, by: str = "person") -> str:
    """Record a person's answer. The agent then applies it (a unit, a mapping) and builds."""
    ids = {q["id"] for q in open_questions(site_dir)}
    if question not in ids:
        raise ValueError(f"no question {question!r}; open: {sorted(ids)}")
    with _questions_path(site_dir).open("a", encoding="utf-8") as handle:
        item = {"id": question, "answer": text, "by": by}
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return f"{question}: answered; apply it to the mapping or layout, then build"


# --- 7. status --------------------------------------------------------------------------------


def status(site_dir: Path) -> str:
    """What the memory holds, file by file, the mission, and what is still open."""
    site_dir = Path(site_dir)
    if not (site_dir / ".neptune" / "log").exists():
        return f"{site_dir}: no memory yet; run setup start"
    kinds: Counter[str] = Counter()
    entities: set[str] = set()
    goals: list[str] = []
    for entry in read(site_dir):
        fact = entry.fact
        if fact is None:
            continue
        kinds[fact.kind] += 1
        entities.add(fact.subject)
        if fact.subject.startswith("goal:") and fact.predicate == "name":
            goals.append(f"{fact.subject} ({fact.citations[0].file} {fact.citations[0].locator})")
    infos = files(site_dir)
    total = sum(kinds.values())
    split = ", ".join(f"{kinds[k]} {k}" for k in ("observed", "stated", "inferred"))
    lines = [f"Memory: {total} facts ({split}) about {len(entities)} entities."]
    mapped = [i for i in infos if i.mapped]
    lines.append(f"Files: {len(infos)}, {len(mapped)} mapped; the rest are kept as cited text.")
    lines += [f"- {i.path}: {i.facts} facts{' (mapped)' if i.mapped else ''}" for i in infos]
    lines.append("Mission: " + ("; ".join(goals) if goals else "none yet (setup mission)"))
    waiting = [q for q in open_questions(site_dir) if "answer" not in q]
    lines.append(f"Open questions: {len(waiting)}" + ("" if waiting else " (none)"))
    lines += [f"- {q['id']}: {q['question']}" for q in waiting]
    return "\n".join(lines)


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _slug(text: str) -> str:
    """An id-safe form: lower case, words joined by ``-``."""
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())) or "x"
