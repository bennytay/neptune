"""What a Rerun source is given: options for reading the objects the catalog names (ADR 0009 §5).

``storage`` declares, per provider (``s3``, ``gcs``, ``azure``), the object-store options of ADR
0006 §7 for the buckets the catalog's storage URLs name (a declared ``endpoint`` with its
``store``, a ``region``, ``anonymous``, a ``timeout``). A provider with no entry has the public
endpoint. The connector sets ``max_objects`` and ``page_size`` of each probe itself. Credentials
are the object-store connectors': declared names (``s3_access_key_id``, ``gcs_access_token``, ...)
or ``NEPTUNE_*``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.clients import Provider
from neptune_deploy.sources.object_store.config import (
    DEFAULT_MAX_LISTING_BYTES,
    ObjectStoreConfigError,
    Options,
)
from neptune_deploy.sources.stated_records import DeclaredClock, parse_clock

CONNECTOR_ID: Final = "deploy_rerun"
DEFAULT_MAX_OBJECTS: Final = 100_000  # one exact-key listing request each
_PROVIDERS: Final = {"s3": Provider.S3, "gcs": Provider.GCS, "azure": Provider.AZURE}
_FIXED: Final = {"max_objects", "page_size", "max_listing_bytes"}


@dataclass(frozen=True)
class RerunOptions:
    storage: Mapping[Provider, Options] = field(default_factory=dict)
    timeline_clocks: Mapping[str, DeclaredClock] = field(default_factory=dict)
    max_objects: int = DEFAULT_MAX_OBJECTS
    max_listing_bytes: int = DEFAULT_MAX_LISTING_BYTES
    max_export_bytes: int = 64 * 1024 * 1024

    def storage_options(self, provider: Provider) -> Options:
        return self.storage.get(provider) or Options()

    @classmethod
    def parse(cls, options: Mapping[str, JsonValue] | None) -> "RerunOptions":
        given = dict(options or {})
        known = {"storage", "timeline_clocks", "max_objects", "max_export_bytes"}
        if unknown := sorted(set(given) - known):
            raise ObjectStoreConfigError(f"unknown options for {CONNECTOR_ID}: {unknown}")
        storage: dict[Provider, Options] = {}
        declared = given.get("storage", {})
        if not isinstance(declared, Mapping) or set(declared) - set(_PROVIDERS):
            raise ObjectStoreConfigError("storage is an object keyed by s3, gcs or azure")
        for name, value in declared.items():
            if not isinstance(value, Mapping) or _FIXED & set(value):
                raise ObjectStoreConfigError(
                    f"storage.{name} is an object of object-store options (not {sorted(_FIXED)})"
                )
            storage[_PROVIDERS[name]] = Options.parse(value, _PROVIDERS[name])
        clocks: dict[str, DeclaredClock] = {}
        declared_clocks = given.get("timeline_clocks", {})
        if not isinstance(declared_clocks, Mapping):
            raise ObjectStoreConfigError("timeline_clocks is an object keyed by index name")
        for name, value in declared_clocks.items():
            try:
                clocks[name] = parse_clock(value)
            except ValueError as exc:
                raise ObjectStoreConfigError(f"timeline_clocks.{name}: {exc}") from exc
        counts = {}
        for name, default, low, high in (
            ("max_objects", cls.max_objects, 1, 10**8),
            ("max_export_bytes", cls.max_export_bytes, 1024, 2**32),
        ):
            value = given.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ObjectStoreConfigError(f"{name} is an integer from {low} to {high}")
            counts[name] = value
        return cls(
            storage=storage,
            timeline_clocks=clocks,
            max_objects=counts["max_objects"],
            max_export_bytes=counts["max_export_bytes"],
        )
