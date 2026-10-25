"""Draft a mapping entry for an unmapped file (spec §12): ``draft-mapping <site_dir> <file>``.

Deterministic heuristics over the file's header and first rows: a CSV or JSONL table becomes a
``tables:`` entry, and so does any other table the compiler reads (an XLSX workbook's data sheet,
read back from the site's last ``build``); a file of feed envelopes (``t``, ``source``, ``topic``,
``payload``) becomes a ``feeds:`` entry. Columns or fields whose predicate cannot be guessed are
left as comments. The draft is for a person to read and fix before it is saved into
``static/neptune.mappings.yaml``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

SAMPLE = 5
_TIME = re.compile(r'^"?\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?)?"?$')

# Words in a column or field name -> the vocabulary predicate it most likely is (first match wins).
_PREDICATES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("serial",), "serial"),
    (("firmware", "version"), "firmware"),
    (("battery", "charge", "soc"), "battery"),
    (("alarm", "fault", "error", "stop"), "health"),
    (("mode", "state", "status"), "mode"),
    (("setpoint",), "setpoint"),
    (("temperature", "temp"), "temperature"),
    (("cycle",), "cycle"),
    (("zone", "location", "area"), "in_zone"),
    (("order", "task", "job", "mission"), "current_task"),
    (("parts", "count", "output"), "parts_out"),
    (("qualification", "skill", "cert"), "holds_qualification"),
    (("role", "position", "title"), "has_role"),
    (("note", "comment", "remark"), "note"),
    (("name", "description", "label"), "name"),
)
# Words in a file name -> the entity type its rows are about.
_TYPES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("roster", "staff", "people", "person", "employee", "operator", "crew"), "person"),
    (("work_order", "workorder", "wo", "maintenance"), "work_order"),
    (("change",), "change"),
    (("task", "order", "mission", "job"), "task"),
    (("zone", "area", "dock", "bay"), "zone"),
    (("robot", "amr", "agv", "arm", "fleet", "drone", "vessel", "vehicle"), "robot"),
)
_REF_TYPES = {
    "in_zone": "zone",
    "has_role": "role",
    "holds_qualification": "qualification",
    "current_task": "task",
}


def draft_mapping(site_dir: Path, file: Path) -> str:
    """A draft mapping entry (YAML text) for ``file``, a path under ``site_dir`` or absolute."""
    path = file if file.is_absolute() else Path(site_dir) / file
    try:
        rel = path.resolve().relative_to((Path(site_dir) / "static").resolve()).as_posix()
    except ValueError:
        rel = path.name
    if path.suffix not in (".jsonl", ".csv", ".tsv"):
        header, samples = _package_table(Path(site_dir), rel)
        return _table(rel, header, samples)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()][:SAMPLE]
        if rows and all({"t", "source", "topic", "payload"} <= set(r) for r in rows):
            return _feed(rows)
        columns = list(dict.fromkeys(key for row in rows for key in _flatten(row)))
        samples = [{k: str(v) for k, v in _flatten(row).items()} for row in rows]
        return _table(rel, columns, samples)
    dialect = "excel-tab" if path.suffix == ".tsv" else "excel"
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    header = next(reader, [])
    samples = [
        dict(zip(header, row, strict=False)) for _, row in zip(range(SAMPLE), reader, strict=False)
    ]
    return _table(rel, header, samples)


def _package_table(site_dir: Path, rel: str) -> tuple[list[str], list[dict[str, str]]]:
    """The header and first rows of the largest table the compiler read from ``static/<rel>``
    (an XLSX workbook's data sheet, not its workbook properties). Needs a ``build`` first."""
    from neptune.model.knowledge import Known
    from neptune.model.source import LocalPath, SourceRevision
    from neptune.model.world import StructuredRecord
    from neptune.sdk import read_package

    package_dir = site_dir / ".neptune" / "package"
    if not package_dir.exists():
        raise SystemExit(f"no package in {package_dir}: run `build` first, then draft {rel}")
    package = read_package(package_dir)
    ids = {
        r.content_id
        for r in package.records
        if isinstance(r, SourceRevision)
        and isinstance(r.location, LocalPath)
        and r.location.path == rel
    }
    tables: dict[str, dict[int, list[str]]] = {}
    for r in package.records:
        if isinstance(r, StructuredRecord) and str(r.provenance.evidence.source) in ids:
            cells = [str(c.value) if isinstance(c, Known) else "" for c in r.cells]
            tables.setdefault(r.table, {})[r.row] = cells
    if not tables:
        raise SystemExit(f"the compiler read no table from static/{rel}")
    rows = max(tables.values(), key=len)
    header = rows.get(0, [])
    samples = [dict(zip(header, rows[i], strict=False)) for i in sorted(rows)[1 : SAMPLE + 1]]
    return [h for h in header if h], samples


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]


def _guess(name: str, table: tuple[tuple[tuple[str, ...], str], ...]) -> str | None:
    words = _words(name)
    joined = "_".join(words)
    for keys, guess in table:
        if any(key in words or (len(key) > 3 and key in joined) for key in keys):
            return guess
    return None


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}{key}."))
        return out
    return {prefix.rstrip("."): value}


def _table(rel: str, columns: list[str], samples: list[dict[str, str]]) -> str:
    def values(column: str) -> list[str]:
        return [row.get(column, "") for row in samples if row.get(column, "")]

    timed = [c for c in columns if values(c) and all(_TIME.match(v) for v in values(c))]
    ids = [c for c in columns if _words(c)[-1:] in (["id"], ["no"], ["tag"], ["code"])]
    key = (ids or [c for c in columns if c not in timed] or columns or ["id"])[0]
    entity = _guess(PurePosixPath(rel).stem, _TYPES) or "machine"
    source = PurePosixPath(rel).parts[0] if "/" in rel else PurePosixPath(rel).stem
    lines = [
        f"# Draft for static/{rel}: check every line, then save it under `tables:`.",
        f"- file: {_q(rel)}",
        f"  source: {_q(source)}   # an id from layout.yaml `sources`",
        "  kind: stated              # observed, if a machine wrote these rows",
        f"  subject: {_q(f'{entity}:{{{key}}}')}",
    ]
    if timed:
        lines.append(f"  time: {_q(timed[0])}")
    lines.append("  facts:")
    for column in columns:
        if column == key or (timed and column == timed[0]):
            continue
        predicate = _guess(column, _PREDICATES)
        sample = (values(column) or [""])[0]
        if predicate is None:
            lines.append(f"    # - {{predicate: ?, column: {_q(column)}}}   # e.g. {_q(sample)}")
            continue
        ref = _REF_TYPES.get(predicate)
        extra = f", ref: {_q(f'{ref}:{{{column}}}')}" if ref else ""
        lines.append(
            f"    - {{predicate: {predicate}, column: {_q(column)}{extra}}}   # e.g. {_q(sample)}"
        )
    return "\n".join(lines) + "\n"


def _feed(rows: list[dict[str, Any]]) -> str:
    first = rows[0]
    payload, topic, source = first["payload"], str(first["topic"]), str(first["source"])
    if isinstance(payload.get("metrics"), list):
        decoder = "sparkplug"
        names = list(
            dict.fromkeys(m["name"] for r in rows for m in r["payload"].get("metrics", []))
        )
        samples = {m["name"]: m.get("value") for r in rows for m in r["payload"].get("metrics", [])}
    else:
        decoder = "controller_log" if {"event", "text"} <= set(payload) else "json"
        if topic.startswith("uagv/"):
            decoder = "vda5050"
        flat = [_flatten(r["payload"]) for r in rows]
        names = list(dict.fromkeys(k for f in flat for k in f))
        samples = {k: v for f in flat for k, v in f.items()}
    entity = _guess(f"{source} {topic}", _TYPES) or "machine"
    segments = [str(r["topic"]).split("/") for r in rows]
    varying = [
        i - len(segments[0])
        for i in range(len(segments[0]))
        if len({tuple(s)[i] if i < len(s) else "" for s in segments}) > 1
    ]
    slugged = re.sub(r"[^a-z0-9]+", "-", source.lower())
    device = f"{{topic[{varying[0]}]}}" if varying else slugged
    lines = [
        f"# Draft for the {source!r} feed: check every line, then save it under `feeds:`.",
        f"- source: {_q(source)}",
        f"  decoder: {decoder}",
        f"  subject: {_q(f'{entity}:{device}')}",
        "  fields:",
    ]
    for name in names:
        if name.split(".")[0] in ("seq", "headerId", "timestamp", "text", "event", "user"):
            continue
        predicate = _guess(name, _PREDICATES)
        sample = _q(str(samples.get(name)))
        if predicate is None:
            lines.append(f"    # {_q(name)}: ?   # e.g. {sample}")
        elif predicate in _REF_TYPES and predicate != "current_task":
            spec = f"{{predicate: {predicate}, ref: {_REF_TYPES[predicate]}}}"
            lines.append(f"    {_q(name)}: {spec}   # e.g. {sample}")
        else:
            lines.append(f"    {_q(name)}: {predicate}   # e.g. {sample}")
    return "\n".join(lines) + "\n"
