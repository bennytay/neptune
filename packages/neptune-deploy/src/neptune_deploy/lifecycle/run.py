"""One run: a package's tables through mapping files, and its documents through templates, into
the records of a new package (ADR 0002 §7, ADR 0003 §6, ADR 0012 §3)."""

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from neptune.store.package import IngestPackage
from neptune_deploy.lifecycle.documents import map_documents
from neptune_deploy.lifecycle.mapper import carried, plan_tables, tables_of
from neptune_deploy.lifecycle.mapping import LifecycleMapping, MappingError
from neptune_deploy.lifecycle.templates import DocumentTemplate

if TYPE_CHECKING:
    from neptune_deploy.eventlogs.mapping import EventLogMapping


def check_declared(
    mappings: Sequence[LifecycleMapping],
    templates: Sequence[DocumentTemplate],
    event_logs: Sequence["EventLogMapping"] = (),
) -> None:
    """Refuse a run that names a file twice, before anything is written."""
    log_hashes = [log.sha256 for log in event_logs]
    if len(set(log_hashes)) != len(log_hashes):
        raise MappingError("the same event-log mapping file is given twice")
    hashes = [template.sha256 for template in templates]
    if len(set(hashes)) != len(hashes):
        raise MappingError("the same template file is given twice")
    mapping_hashes = [mapping.sha256 for mapping in mappings]
    if len(set(mapping_hashes)) != len(mapping_hashes):
        raise MappingError("the same mapping file is given twice")
    ids = [mapping.id for mapping in mappings]
    if len(set(ids)) != len(ids):
        raise MappingError(f"two mapping files share an id: {ids}")


def iter_records(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
    event_logs: Sequence["EventLogMapping"] = (),
) -> Iterator[Any]:
    """Every record of the mapped package, one at a time: the base's source ledger and the
    transforms its records name, then each template's and each mapping's transform, lifecycle
    records, clocks and findings. A table's rows are mapped as the iterator is read, and none is
    kept, so the compiler's streaming writer bounds the memory of the whole run."""
    check_declared(mappings, templates, event_logs)
    documents, claimed = map_documents(base, templates)
    taken = set(claimed)
    logs = []
    if event_logs:
        from neptune_deploy.eventlogs.mapper import plan_event_logs

        # A log a mapping writes as a typed table is that mapping's: no lifecycle rule reads it
        # again, as a document's tables are its template's (ADR 0017 §1).
        tables = [t for t in tables_of(base.records)[0] if t.record.id not in taken]
        logs = [run for run in plan_event_logs(base, event_logs, tables) if run.tables]
        for run in logs:
            taken |= run.claimed
    plan = plan_tables(base, mappings, taken) if mappings else None
    transforms = [r for r in documents if r.kind == "transform_record"]
    transforms += [run.transform for run in logs]
    if plan is not None:
        transforms += plan.transforms
    yield from carried(base, transforms)
    yield from documents
    for run in logs:
        yield from run.records()
    if plan is not None:
        yield from plan.records()


def map_records(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
    event_logs: Sequence["EventLogMapping"] = (),
) -> list[Any]:
    """``iter_records`` held as a list, for a package that fits in memory."""
    return list(iter_records(base, mappings, templates, event_logs))
