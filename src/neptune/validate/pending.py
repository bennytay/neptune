"""Rules whose input no record kind on main carries yet: written, tested, and off (ADR 0054 §4).

Each reads a ``Protocol`` instead of a record kind. The engine runs one only when its input is
supplied (``Inputs``); otherwise the report lists it as not covered, with the reason below. When
the kind lands, its issue maps the records onto the Protocol and turns the rule on.
"""

from collections import defaultdict
from collections.abc import Iterator
from typing import Any, Final, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from neptune.identity import canonical_json
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import Knowledge, Known
from neptune.model.provenance import EvidenceRef
from neptune.model.units import Unit
from neptune.model.versions import version_to_json
from neptune.validate.engine import Context, Draft, Rule, evidence_of, plural, short


class DeclaredLimit(Protocol):
    """A bound the evidence declares for one series column (a joint's position limits in a URDF).

    ``unit`` is the limit's declared unit and ``column_unit`` the column's; the rule compares only
    when both are known and equal, so no unit is ever assumed or converted.
    """

    @property
    def stream(self) -> RecordId: ...
    @property
    def column(self) -> str: ...
    @property
    def lower(self) -> float | None: ...
    @property
    def upper(self) -> float | None: ...
    @property
    def unit(self) -> Knowledge[Unit]: ...
    @property
    def column_unit(self) -> Knowledge[Unit]: ...
    @property
    def evidence(self) -> EvidenceRef: ...


class DocumentRevision(Protocol):
    """One revision of a logical document (an SOP, a site rule) and the revisions it supersedes."""

    @property
    def record(self) -> RecordId: ...
    @property
    def document(self) -> LogicalId: ...
    @property
    def revision(self) -> str: ...
    @property
    def supersedes(self) -> tuple[str, ...]: ...
    @property
    def evidence(self) -> EvidenceRef: ...


class RunSoftware(Protocol):
    """A run bound to a software configuration that ran in it (MVL-38's binding)."""

    @property
    def run(self) -> RecordId: ...
    @property
    def configuration(self) -> RecordId: ...
    @property
    def evidence(self) -> EvidenceRef: ...


def _canonical(value: Any) -> str:
    return canonical_json.dumps(version_to_json(value)).decode()


def _known_equal(a: Knowledge[Unit], b: Knowledge[Unit]) -> bool:
    return isinstance(a, Known) and isinstance(b, Known) and a.value == b.value


def declared_limit_exceeded(context: Context) -> Iterator[Draft]:
    """Series values outside a limit the evidence declares for that column, in the same unit."""
    streams = {stream.id: stream for stream in context.records("stream")}
    for limit in sorted(context.inputs.limits, key=lambda item: (item.stream, item.column)):
        stream = streams.get(limit.stream)
        content = context.package.series.get(limit.stream)
        if stream is None or content is None or not _known_equal(limit.unit, limit.column_unit):
            continue
        parquet = pq.ParquetFile(
            pa.BufferReader(content) if isinstance(content, bytes) else content
        )
        if limit.column not in parquet.schema_arrow.names:
            continue
        below = above = 0
        for batch in parquet.iter_batches(
            batch_size=context.bounds.batch_rows, columns=[limit.column]
        ):
            values = batch.column(0)
            if limit.lower is not None:
                below += pc.sum(pc.less(values, limit.lower)).as_py() or 0
            if limit.upper is not None:
                above += pc.sum(pc.greater(values, limit.upper)).as_py() or 0
        if not below and not above:
            continue
        yield Draft(
            subject=evidence_of(stream),
            message=f"{plural(below + above, 'value')} of {limit.column} in stream"
            f" {short(stream.id)} lie outside the declared limit"[:900],
            details={
                "above": above,
                "below": below,
                "column": limit.column,
                "lower": limit.lower,
                "upper": limit.upper,
            },
            related=[limit.evidence],
            records=(stream.id,),
        )


def stale_document_revision(context: Context) -> Iterator[Draft]:
    """The package holds a revision of a document that another revision it holds supersedes."""
    by_document: dict[LogicalId, list[Any]] = defaultdict(list)
    for item in context.inputs.document_revisions:
        by_document[item.document].append(item)
    for document in sorted(by_document, key=lambda d: (d.namespace, d.value)):
        revisions = sorted(by_document[document], key=lambda item: (item.revision, item.record))
        for old in revisions:
            newer = [r for r in revisions if old.revision in r.supersedes]
            if not newer:
                continue
            yield Draft(
                subject=old.evidence,
                message=f"revision {old.revision!r} of {document.namespace}:{document.value} is"
                f" superseded by {plural(len(newer), 'revision')} in this package"[:900],
                details={
                    "document": f"{document.namespace}:{document.value}",
                    "revision": old.revision,
                    "superseded_by": sorted(r.revision for r in newer),
                },
                related=[r.evidence for r in newer],
                records=[old.record, *(r.record for r in newer)],
            )


def run_software_conflict(context: Context) -> Iterator[Draft]:
    """A run is bound to configurations that state different commits or releases for one piece
    of software on one device."""
    configs = {c.id: c for c in context.records("software_configuration")}
    bound: dict[RecordId, list[Any]] = defaultdict(list)
    for binding in context.inputs.run_software:
        if binding.configuration in configs:
            bound[binding.run].append(binding)
    for run in sorted(bound):
        stated: dict[tuple[str, str, str], dict[str, list[Any]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for binding in bound[run]:
            for item in configs[binding.configuration].software:
                if not isinstance(item.name, Known):
                    continue
                device = item.device.value if isinstance(item.device, Known) else ""
                for field in ("commit", "release"):
                    value = getattr(item, field)
                    if isinstance(value, Known):
                        stated[item.name.value, device, field][_canonical(value.value)].append(
                            binding
                        )
        for (name, device, field), values in sorted(stated.items()):
            if len(values) < 2:
                continue
            bindings = sorted(
                {b.configuration: b for v in values.values() for b in v}.values(),
                key=lambda b: b.configuration,
            )
            yield Draft(
                subject=bindings[0].evidence,
                message=f"run {short(run)} is bound to configurations stating"
                f" {len(values)} different {field} values for {name!r}"[:900],
                details={"device": device, "field": field, "name": name, "run": run},
                related=[b.evidence for b in bindings[1:]],
                records=[run, *(b.configuration for b in bindings)],
            )


_C, _W = FindingCategory, Severity.WARNING

RULES: Final = (
    Rule("declared_limit_exceeded", 1, _C.INCONSISTENT, _W,
         "series values lie outside a limit the evidence declares, in the same unit",
         declared_limit_exceeded,
         not_covered="no record kind on main declares a limit for a series column; joint and"
         " actuator limits arrive with the embodiment kinds"),
    Rule("run_software_conflict", 1, _C.INCONSISTENT, _W,
         "a run is bound to configurations that disagree on one piece of software",
         run_software_conflict,
         not_covered="no record kind on main binds software configurations to runs (MVL-38)"),
    Rule("stale_document_revision", 1, _C.INCONSISTENT, _W,
         "the package holds a document revision another revision in it supersedes",
         stale_document_revision,
         not_covered="no record kind on main states a document's logical id and revision"),
)  # fmt: skip

# The ``Inputs`` field each rule above reads: supplying it turns the rule on.
NEEDS: Final = {
    "declared_limit_exceeded": "limits",
    "run_software_conflict": "run_software",
    "stale_document_revision": "document_revisions",
}
