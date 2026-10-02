"""The record systems: each turns one API's pages into ``Page`` s (ADR 0008).

A system knows its wire format and nothing else: how to ask for a page, how to read the page the
system answered, which identity and revision token a record has, and how to download an attachment.
It holds no policy (limits, ordering, findings, discovery), so the systems cannot differ in it.
"""

from collections.abc import Generator
from typing import Protocol

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.records.http import Api
from neptune_deploy.sources.records.model import Fetch, Page


class System(Protocol):
    """What the source needs of a record system."""

    api: Api
    declared_options: dict[str, dict[str, JsonValue]]
    # Compiler adapter options the snapshots need declared at ingest (``{adapter: {option: v}}``):
    # a CSV snapshot's first row is its header, and the compiler never guesses one (root ADR 0042).

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        """The feed's pages in order: every record if ``since`` is ``None``, else what changed
        since that cursor. A failed request raises a ``TransportError``; the source says so."""
        ...

    def download(self, fetch: Fetch) -> bytes:
        """The bytes ``fetch`` names, exactly ``fetch.size`` of them; raises otherwise."""
        ...
