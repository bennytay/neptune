"""One run: a package's tables through mapping files, and its documents through templates, into
the records of a new package (ADR 0002 §7, ADR 0003 §6)."""

from collections.abc import Sequence
from typing import Any

from neptune.store.package import IngestPackage
from neptune_deploy.lifecycle.documents import map_documents
from neptune_deploy.lifecycle.mapper import carried, map_tables
from neptune_deploy.lifecycle.mapping import LifecycleMapping, MappingError
from neptune_deploy.lifecycle.templates import DocumentTemplate


def map_records(
    base: IngestPackage,
    mappings: Sequence[LifecycleMapping] = (),
    templates: Sequence[DocumentTemplate] = (),
) -> list[Any]:
    """Every record of the mapped package: the base's source ledger and the transforms its
    records name, then each mapping's and each template's transform, lifecycle records, clocks
    and findings."""
    hashes = [template.sha256 for template in templates]
    if len(set(hashes)) != len(hashes):
        raise MappingError("the same template file is given twice")
    documents, claimed = map_documents(base, templates)
    tables = map_tables(base, mappings, claimed) if mappings else []
    out = [*documents, *tables]
    return [*carried(base, out), *out]
