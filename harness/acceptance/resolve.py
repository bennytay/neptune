"""Resolve the gold answers' evidence against a compiled package (Platform ADR 0007 section 5).

A gold evidence item names a corpus path and a selector; this finds the records of the package
that hold it. The result is what a consumer scores against: an answer cites an evidence item when
it cites one of its records (or, for ``message``, one of its rows). The selectors read the
package as package-schema publishes it (``records/*.jsonl``, ``derived/*.jsonl``,
``series/*.parquet``), never the corpus's raw bytes.

Selectors (``select.kind``):

- ``source`` ``{path}``: the path's ``source_revision``.
- ``document_text`` ``{path, contains, page?}``: ``document_block`` records whose text contains
  ``contains`` (``page`` counts from 1).
- ``table_row`` ``{path, key}``: ``structured_record`` rows with a cell equal to ``key``.
- ``no_table_row`` ``{path, key}``: the path's ``structured_table`` records, only when no row of
  the path has a cell equal to ``key`` (a cited absence).
- ``config_value`` ``{path, pointer, equals?}``: ``configuration_value`` records at the JSON
  pointer, whose declared text equals ``equals`` when given.
- ``calibration`` ``{path, parameter, equals?}``: ``calibration`` records with that parameter.
- ``stream`` ``{path, topic}``: ``stream`` records of the topic recorded in the path.
- ``message`` ``{path, topic, contains}``: rows of that stream with a text value containing
  ``contains``; the stream's record and each row's ``seq`` and first clock reading.
- ``finding`` ``{path, code}``: ``ingest_finding`` records with the code whose subject is the path.
- ``clock_mapping`` ``{path, offset_s}``: derived ``clock_mapping`` lines evidenced by the path
  whose anchor offset, target minus source in seconds by each clock's stated resolution, lies in
  ``[low, high]``; a clock without a known resolution is never compared.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from pathlib import Path

Json = dict[str, Any]
SELECTORS: Final = (
    "source",
    "document_text",
    "table_row",
    "no_table_row",
    "config_value",
    "calibration",
    "stream",
    "message",
    "finding",
    "clock_mapping",
)


class GoldError(ValueError):
    """The gold document is malformed: an unknown selector, a missing field, a bad reference."""


@dataclass
class Package:
    """A committed package directory, read lazily by record kind."""

    root: Path
    _kinds: dict[str, list[Json]] = field(default_factory=dict)

    def kind(self, name: str, *, derived: bool = False) -> list[Json]:
        key = f"{'derived' if derived else 'records'}/{name}"
        if key not in self._kinds:
            path = self.root / f"{key}.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
            self._kinds[key] = [json.loads(line) for line in lines if line]
        return self._kinds[key]

    def tick_seconds(self) -> dict[str, Fraction]:
        """Each clock's tick length in seconds, where its ``resolution`` is known."""
        out: dict[str, Fraction] = {}
        for derived in (False, True):
            for domain in self.kind("timestamp_domain", derived=derived):
                value = _known(domain.get("resolution"))
                if isinstance(value, dict) and value.get("denominator"):
                    out[str(domain["id"])] = Fraction(value["numerator"], value["denominator"])
        return out

    def content(self, relative: str) -> str | None:
        """The content id of the source at ``relative`` (a corpus path), if the package has it."""
        for revision in self.kind("source_revision"):
            if revision["location"].get("path") == relative:
                return str(revision["content_id"])
        return None


def _source(record: Json) -> str | None:
    evidence = record.get("provenance", {}).get("evidence")
    return evidence.get("source") if isinstance(evidence, dict) else None


def _known(state: Any) -> Any:
    return (
        state.get("value")
        if isinstance(state, dict) and state.get("knowledge") == "known"
        else None
    )


def _cells(record: Json) -> list[str]:
    return [str(_known(cell)) for cell in record.get("cells", []) if _known(cell) is not None]


def _need(select: Json, *names: str) -> list[Any]:
    missing = [name for name in names if name not in select]
    if missing:
        raise GoldError(f"selector {select.get('kind')!r} needs {', '.join(missing)}")
    return [select[name] for name in names]


def _pointer(path: list[Any]) -> str:
    """A configuration value's path as an RFC 6901 JSON pointer (``~`` and ``/`` escaped)."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _ids(records: list[Json]) -> list[str]:
    return sorted({str(record["id"]) for record in records})


def _series_rows(package: Package, stream_id: str, contains: str) -> list[Json]:
    # The compiler's own dependency (untyped); only ``message`` needs it.
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    path = package.root / "series" / f"{stream_id.removeprefix('rec:sha256:')}.parquet"
    if not path.is_file():
        return []
    rows = []
    for row in pq.read_table(path).to_pylist():
        texts = [v for k, v in row.items() if k.startswith("value/") and isinstance(v, str)]
        texts += [
            str(item)
            for k, v in row.items()
            if k.startswith("value/") and isinstance(v, list)
            for item in v
            if isinstance(item, str)
        ]
        if any(contains in value for value in texts):
            rows.append({"seq": row["seq"], "time/0": row.get("time/0")})
    return rows


def resolve_one(package: Package, select: Json) -> Json:
    """``{"kind", "records"[, "rows"]}`` for one selector; ``records`` is empty when nothing holds
    the evidence."""
    kind = select.get("kind")
    if kind not in SELECTORS:
        raise GoldError(f"unknown selector kind {kind!r}")
    (path,) = _need(select, "path")
    content = package.content(path)
    if content is None:
        return {"kind": kind, "records": [], "problem": f"the package holds no source at {path}"}
    found: list[Json] = []
    out: Json = {"kind": kind}
    if kind == "source":
        found = [r for r in package.kind("source_revision") if r["location"].get("path") == path]
    elif kind == "document_text":
        (contains,) = _need(select, "contains")
        page = select.get("page")
        for block in package.kind("document_block"):
            if _source(block) != content or contains not in str(_known(block.get("text")) or ""):
                continue
            locator = block["provenance"]["evidence"]["locator"]
            index = next((part["index"] for part in locator if part.get("kind") == "page"), None)
            if page is None or index == page - 1:
                found.append(block)
    elif kind in ("table_row", "no_table_row"):
        (key,) = _need(select, "key")
        rows = [
            row
            for row in package.kind("structured_record")
            if _source(row) == content and key in _cells(row)
        ]
        if kind == "table_row":
            found = rows
        elif not rows:
            found = [t for t in package.kind("structured_table") if _source(t) == content]
    elif kind == "config_value":
        (pointer,) = _need(select, "pointer")
        for value in package.kind("configuration_value"):
            if _source(value) != content or _pointer(value["path"]) != pointer:
                continue
            if "equals" not in select or _known(value.get("text")) == str(select["equals"]):
                found.append(value)
    elif kind == "calibration":
        (parameter,) = _need(select, "parameter")
        for record in package.kind("calibration"):
            for item in record.get("parameters", []):
                if item.get("name") != parameter:
                    continue
                value = _known(item.get("value"))
                where = item["value"].get("provenance", {}).get("evidence", {}).get("source")
                expected = select.get("equals")
                if where == content and ("equals" not in select or value in (expected, [expected])):
                    found.append(record)
    elif kind in ("stream", "message"):
        (topic,) = _need(select, "topic")
        streams = [
            s
            for s in package.kind("stream")
            if _source(s) == content and _known(s.get("topic")) == topic
        ]
        if kind == "stream":
            found = streams
        else:
            (contains,) = _need(select, "contains")
            hits: list[Json] = []
            for stream in streams:
                matched = _series_rows(package, str(stream["id"]), contains)
                if matched:
                    found.append(stream)
                    hits += [{"stream": stream["id"], **row} for row in matched]
            out["rows"] = sorted(hits, key=lambda r: (r["stream"], r["seq"]))
    elif kind == "finding":
        (code,) = _need(select, "code")
        found = [
            f
            for f in package.kind("ingest_finding")
            if f["code"] == code and f.get("subject", {}).get("ref", {}).get("source") == content
        ]
    elif kind == "clock_mapping":
        (bounds,) = _need(select, "offset_s")
        low, high = Fraction(str(bounds[0])), Fraction(str(bounds[1]))
        seconds = package.tick_seconds()
        for mapping in package.kind("clock_mapping", derived=True):
            if all(e.get("source") != content for e in mapping.get("evidence", [])):
                continue
            anchor = _known(mapping.get("anchor"))
            if anchor is None:
                continue
            source, target = anchor["source"], anchor["target"]
            source_tick = seconds.get(source["domain_id"])
            target_tick = seconds.get(target["domain_id"])
            if source_tick is None or target_tick is None:
                continue  # a clock whose tick length is not known is not compared
            offset = target["ticks"] * target_tick - source["ticks"] * source_tick
            if low <= offset <= high:
                found.append(mapping)
    out["records"] = _ids(found)
    return out


def check_gold(gold: Json) -> list[str]:
    """Structural problems of a gold document (references, selectors, ids); empty when sound."""
    problems: list[str] = []
    evidence = gold.get("evidence", {})
    for key, item in sorted(evidence.items()):
        select = item.get("select", {})
        if select.get("kind") not in SELECTORS:
            problems.append(f"{key}: unknown selector {select.get('kind')!r}")
        if "path" not in item.get("select", {}):
            problems.append(f"{key}: no path")
        if not item.get("says"):
            problems.append(f"{key}: no 'says'")
    cited: set[str] = set()
    seen: set[str] = set()
    for question in gold.get("questions", []):
        qid = question.get("id", "?")
        if qid in seen:
            problems.append(f"{qid}: repeated question id")
        seen.add(qid)
        for claim in question.get("claims", []):
            if claim.get("id", "").split(".")[0] != qid:
                problems.append(f"{qid}: claim {claim.get('id')!r} is not numbered under it")
            if claim.get("knowledge") not in ("known", "unknown"):
                problems.append(f"{claim.get('id')}: knowledge must be known or unknown")
            refs = claim.get("evidence", [])
            if not refs:
                problems.append(f"{claim.get('id')}: cites no evidence")
            for ref in refs:
                cited.add(ref)
                if ref not in evidence:
                    problems.append(f"{claim.get('id')}: cites unknown evidence {ref}")
        for ref in question.get("must_not_cite", []):
            if ref not in evidence:
                problems.append(f"{qid}: must_not_cite names unknown evidence {ref}")
    for trap in gold.get("traps", []):
        for ref in trap.get("evidence", []):
            cited.add(ref)
            if ref not in evidence:
                problems.append(f"trap {trap.get('id')}: names unknown evidence {ref}")
    problems += [f"{key}: never cited" for key in sorted(set(evidence) - cited)]
    return problems


def resolve(package_root: Path, gold: Json) -> Json:
    """Every evidence item of ``gold`` resolved against the package: ``{evidence id: result}``."""
    package = Package(package_root)
    return {
        key: resolve_one(package, item["select"]) for key, item in sorted(gold["evidence"].items())
    }


def summary(resolved: Json) -> Json:
    """What the harness report carries: counts and the evidence that resolved to nothing."""
    by_kind: dict[str, int] = defaultdict(int)
    for item in resolved.values():
        by_kind[item["kind"]] += 1
    missing = sorted(key for key, item in resolved.items() if not item["records"])
    return {
        "evidence": len(resolved),
        "resolved": len(resolved) - len(missing),
        "missing": missing,
        # why an item resolved to nothing, when the reason is not the selector (a source absent)
        "reasons": {key: resolved[key]["problem"] for key in missing if "problem" in resolved[key]},
        "selectors": dict(sorted(by_kind.items())),
    }
