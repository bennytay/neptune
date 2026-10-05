"""One run: a package's tables through mapping files, and its documents through templates, into
the records of a new package (ADR 0002 §7, ADR 0003 §6, ADR 0012 §3)."""

from collections.abc import Iterator, Sequence
from typing import Any

from neptune.store.package import IngestPackage
from neptune_deploy.lifecycle.documents import map_documents
from neptune_deploy.lifecycle.mapper import carried, plan_tables
from neptune_deploy.lifecycle.mapping import LifecycleMapping, MappingError
from neptune_deploy.lifecycle.templates import DocumentTemplate


def iter_records(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
) -> Iterator[Any]:
    """Every record of the mapped package, one at a time: the base's source ledger and the
    transforms its records name, then each template's and each mapping's transform, lifecycle
    records, clocks and findings. A table's rows are mapped as the iterator is read, and none is
    kept, so the compiler's streaming writer bounds the memory of the whole run."""
    hashes = [template.sha256 for template in templates]
    if len(set(hashes)) != len(hashes):
        raise MappingError("the same template file is given twice")
    documents, claimed = map_documents(base, templates)
    plan = plan_tables(base, mappings, claimed) if mappings else None
    transforms = [r for r in documents if r.kind == "transform_record"]
    if plan is not None:
        transforms += plan.transforms
    yield from carried(base, transforms)
    yield from documents
    if plan is not None:
        yield from plan.records()


def map_records(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
) -> list[Any]:
    """``iter_records`` held as a list, for a package that fits in memory."""
    return list(iter_records(base, mappings, templates))
