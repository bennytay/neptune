"""Shared helpers for the record-system connector tests (ADR 0008 §9)."""

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deploy_records_fake import FIXTURES, FakeServer
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import SourceArtifact
from neptune.store.workspace import Workspace
from neptune_deploy.sources.records import (
    RecordEntry,
    RecordSource,
    confluence_source,
    gdrive_source,
    jira_source,
    onedrive_source,
    rest_source,
    servicenow_source,
)

JIRA_CREDENTIALS = {"email": "ops@example.com", "api_token": "jira-secret"}
SERVICENOW_CREDENTIALS = {"username": "integration", "password": "sn-secret"}
DRIVE_CREDENTIALS = {"access_token": "drive-token-never-printed"}
ONEDRIVE_CREDENTIALS = {"access_token": "onedrive-token-never-printed"}
CONFLUENCE_CREDENTIALS = {"email": "wiki@example.com", "api_token": "wiki-secret"}
REST_CREDENTIALS = {"api_key": "cmms-session-token-never-printed"}
SECRETS = (
    "jira-secret",
    "sn-secret",
    "drive-token-never-printed",
    "onedrive-token-never-printed",
    "wiki-secret",
    "cmms-session-token-never-printed",
)


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


def cmms_profile() -> dict[str, Any]:
    profile: dict[str, Any] = json.loads(
        (FIXTURES / "cmms_profile.json").read_text(encoding="utf-8")
    )
    return profile


def _options(**options: Any) -> dict[str, Any]:
    return {"scheme": "http", "instance": "site-a", **options}


@contextmanager
def jira(
    server: FakeServer, tmp_path: Path, *, ledger: SourceLedger | None = None, **options: Any
) -> Iterator[RecordSource]:
    with server.serve() as host:
        yield jira_source(
            f"jira://{host}/OPS",
            network=online(tmp_path),
            ledger=ledger,
            options=_options(**options),
            credentials=JIRA_CREDENTIALS,
        )


@contextmanager
def servicenow(
    server: FakeServer, tmp_path: Path, *, ledger: SourceLedger | None = None, **options: Any
) -> Iterator[RecordSource]:
    with server.serve() as host:
        yield servicenow_source(
            f"servicenow://{host}/change_request",
            network=online(tmp_path),
            ledger=ledger,
            options=_options(**options),
            credentials=SERVICENOW_CREDENTIALS,
        )


@contextmanager
def gdrive(
    server: FakeServer, tmp_path: Path, *, ledger: SourceLedger | None = None, **options: Any
) -> Iterator[RecordSource]:
    with server.serve() as host:
        yield gdrive_source(
            "gdrive://my-drive",
            network=online(tmp_path),
            ledger=ledger,
            options=_options(endpoint=f"http://{host}", **options),
            credentials=DRIVE_CREDENTIALS,
        )


@contextmanager
def onedrive(
    server: FakeServer, tmp_path: Path, *, ledger: SourceLedger | None = None, **options: Any
) -> Iterator[RecordSource]:
    with server.serve() as host:
        yield onedrive_source(
            "onedrive://b!siteA_lib01",
            network=online(tmp_path),
            ledger=ledger,
            options=_options(
                endpoint=f"http://{host}", **{"download_hosts": ["127.0.0.1"], **options}
            ),
            credentials=ONEDRIVE_CREDENTIALS,
        )


@contextmanager
def confluence(
    server: FakeServer, tmp_path: Path, *, ledger: SourceLedger | None = None, **options: Any
) -> Iterator[RecordSource]:
    with server.serve() as host:
        yield confluence_source(
            f"confluence://{host}/5001",
            network=online(tmp_path),
            ledger=ledger,
            options=_options(**options),
            credentials=CONFLUENCE_CREDENTIALS,
        )


@contextmanager
def rest(
    server: FakeServer, tmp_path: Path, *, ledger: SourceLedger | None = None, **options: Any
) -> Iterator[RecordSource]:
    with server.serve() as host:
        yield rest_source(
            f"rest://{host}",
            network=online(tmp_path),
            ledger=ledger,
            options=_options(profile=cmms_profile(), **options),
            credentials=REST_CREDENTIALS,
        )


def fingerprint(
    source: RecordSource, ledger: SourceLedger, entries: Iterable[Any]
) -> dict[str, SourceArtifact]:
    """What the compiler's scan does with a walk: digest each item, observe it in the ledger."""
    artifacts = {}
    for entry in entries:
        if isinstance(entry, RecordEntry):
            with source.open(entry.location) as stream:
                artifact = digest_stream(stream, chunk_size=1024 * 1024)
            ledger.observe(entry.location, artifact)
            artifacts[entry.id] = artifact
    return artifacts


def codes(source: RecordSource) -> list[str]:
    return sorted(finding.code for finding in source.findings())


def snapshot_json(source: RecordSource, item_id: str) -> Any:
    entry = next(e for e in source.listing().entries if e.id == item_id)
    with source.open(entry.location) as stream:
        return json.loads(stream.read())
