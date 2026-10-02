"""What a record system declares so one factory can build it (ADR 0008)."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import Endpoint
from neptune_deploy.sources.records.config import Options
from neptune_deploy.sources.records.http import Api, Auth
from neptune_deploy.sources.records.systems import System


@dataclass(frozen=True)
class Plan:
    """Where a URL points: the API endpoint, the part of it to read, and whether the endpoint was
    declared (a declared endpoint is not a name others share, so it needs a declared instance)."""

    endpoint: Endpoint
    what: str
    declared_endpoint: bool = False


@dataclass(frozen=True)
class Spec:
    """One system: its connector id, URL scheme, closed options, credentials and builders."""

    connector_id: str
    scheme: str
    max_page_size: int
    extras: Mapping[str, Callable[[JsonValue], Any]]
    env: Mapping[str, str]  # credential name -> NEPTUNE_* variable
    since_shape: re.Pattern[str]  # what a cursor of this system looks like
    plan: Callable[[str, str, Options], Plan]
    auth: Callable[[Mapping[str, str], Options], Auth]
    build: Callable[[Api, str, Options], tuple[System, dict[str, JsonValue]]]
