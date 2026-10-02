"""What a Roboto source is given: a URL, options and a read-only token (ADR 0009 §1, §3).

- The URL is ``roboto://<org id>/<dataset id>/<path prefix>``: one dataset of one organisation, and
  the files whose ``relative_path`` starts with the prefix. The prefix is taken verbatim.
- Options are declared, closed and checked: an unknown option is refused, never ignored.
- The credential is the API token the caller declares, or else ``NEPTUNE_ROBOTO_API_TOKEN``. Nothing
  ambient is read: not ``ROBOTO_*``, not the Roboto SDK's config file under the home directory.
- Nothing here touches the network.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.config import (
    DEFAULT_MAX_LISTING_BYTES,
    DEFAULT_MAX_OBJECTS,
    DEFAULT_PAGE_SIZE,
    MAX_KEY_BYTES,
    ObjectStoreConfigError,
)
from neptune_deploy.sources.object_store.transport import DEFAULT_TIMEOUT, Endpoint
from neptune_deploy.sources.stated_records import DeclaredClock, parse_clock

CONNECTOR_ID: Final = "deploy_roboto"
DEFAULT_ENDPOINT: Final = "https://api.roboto.ai"
DEFAULT_API_VERSION: Final = "2026-08-27"  # the newest X-Roboto-Api-Version roboto 0.58.0 names
DEFAULT_MAX_RECORDS: Final = 100_000  # events (or comments) read per dataset
TOKEN_ENV: Final = "NEPTUNE_ROBOTO_API_TOKEN"

# Roboto's ids are a short prefix and random characters (``og_``, ``ds_``, ``fl_``). The pattern is
# wider than that, and has no ``/`` or ``:``, so the parts of an object id never run together.
ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}")
_API_VERSION: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HOST: Final = re.compile(r"[a-z0-9][a-z0-9.\-]{0,252}(:[0-9]{1,5})?")


@dataclass(frozen=True)
class RobotoLocation:
    """One dataset of one organisation, and the path prefix inside it."""

    org: str
    dataset: str
    prefix: str

    @property
    def scope(self) -> str:
        """What every object id starts with: ``<org>/<dataset>/``."""
        return f"{self.org}/{self.dataset}/"

    def object_id(self, key: str) -> str:
        return self.scope + key


def parse_url(url: str) -> RobotoLocation:
    """The location a ``roboto://<org>/<dataset>/<prefix>`` URL names."""
    if not isinstance(url, str):
        raise ObjectStoreConfigError(f"a source URL is text, got {type(url).__name__}")
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() != "roboto":
        raise ObjectStoreConfigError(f"{CONNECTOR_ID} reads roboto:// URLs")
    org, _, rest = rest.partition("/")
    dataset, _, prefix = rest.partition("/")
    if not ID.fullmatch(org) or not ID.fullmatch(dataset):
        raise ObjectStoreConfigError(
            "roboto://<org id>/<dataset id>/<prefix>: ids are letters, digits, _ and - (a URL"
            " holds no user information)"
        )
    try:
        encoded = prefix.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ObjectStoreConfigError("the prefix is not valid Unicode") from exc
    if len(encoded) > MAX_KEY_BYTES:
        raise ObjectStoreConfigError(f"the prefix is longer than {MAX_KEY_BYTES} bytes")
    return RobotoLocation(org, dataset, prefix)


@dataclass(frozen=True)
class RobotoOptions:
    """Declared options.

    ``endpoint`` is the API (https, or http to a loopback host). ``content_hosts`` are the hosts, as
    ``host`` or ``host:port``, that a signed download URL may name besides the API's own: Roboto
    answers a file request with a time-limited URL on its object store, and a response must not be
    able to send the connector to a host the operator did not name. ``api_version`` is sent as
    ``X-Roboto-Api-Version``, so the shape of what Roboto answers does not move under a stored
    document. ``events`` and ``comments`` read the dataset's annotations; ``max_records`` bounds
    each. ``event_clock`` declares what an event's ``start_time`` and ``end_time`` count (none
    assumed).
    """

    endpoint: str = DEFAULT_ENDPOINT
    content_hosts: tuple[str, ...] = ()
    api_version: str = DEFAULT_API_VERSION
    events: bool = True
    comments: bool = True
    max_records: int = DEFAULT_MAX_RECORDS
    event_clock: DeclaredClock = field(default_factory=DeclaredClock)
    max_objects: int = DEFAULT_MAX_OBJECTS
    max_listing_bytes: int = DEFAULT_MAX_LISTING_BYTES
    page_size: int = DEFAULT_PAGE_SIZE
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def parse(cls, options: Mapping[str, JsonValue] | None) -> "RobotoOptions":
        given = dict(options or {})
        unknown = sorted(set(given) - set(cls.__dataclass_fields__))
        if unknown:
            raise ObjectStoreConfigError(f"unknown options for {CONNECTOR_ID}: {unknown}")
        base = cls()
        endpoint = given.get("endpoint", base.endpoint)
        if not isinstance(endpoint, str):
            raise ObjectStoreConfigError("endpoint is a URL")
        try:
            Endpoint.parse(endpoint)
        except ValueError as exc:
            raise ObjectStoreConfigError(str(exc)) from exc
        hosts = given.get("content_hosts", [])
        if not isinstance(hosts, list | tuple) or not all(
            isinstance(host, str) and _HOST.fullmatch(host.lower()) for host in hosts
        ):
            raise ObjectStoreConfigError("content_hosts is a list of host or host:port names")
        version = given.get("api_version", base.api_version)
        if not isinstance(version, str) or not _API_VERSION.fullmatch(version):
            raise ObjectStoreConfigError("api_version is a date such as 2026-08-27")
        flags = {}
        for name in ("events", "comments"):
            value = given.get(name, getattr(base, name))
            if not isinstance(value, bool):
                raise ObjectStoreConfigError(f"{name} is true or false")
            flags[name] = value
        counts = {}
        for name, low, high in (
            ("max_records", 1, 10**8),
            ("max_objects", 1, 10**8),
            ("max_listing_bytes", 1024, 2**40),
            ("page_size", 1, 1000),
        ):
            value = given.get(name, getattr(base, name))
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ObjectStoreConfigError(f"{name} is an integer from {low} to {high}")
            counts[name] = value
        timeout = given.get("timeout", base.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not timeout > 0:
            raise ObjectStoreConfigError("timeout is a positive number of seconds")
        try:
            clock = parse_clock(given.get("event_clock"))
        except ValueError as exc:
            raise ObjectStoreConfigError(f"event_clock: {exc}") from exc
        return cls(
            endpoint=endpoint,
            content_hosts=tuple(sorted({host.lower() for host in hosts})),
            api_version=version,
            events=flags["events"],
            comments=flags["comments"],
            max_records=counts["max_records"],
            event_clock=clock,
            max_objects=counts["max_objects"],
            max_listing_bytes=counts["max_listing_bytes"],
            page_size=counts["page_size"],
            timeout=float(timeout),
        )


def api_token(declared: Mapping[str, str] | None, environ: Mapping[str, str]) -> str:
    """The token to use: the declared ``roboto_api_token``, else ``NEPTUNE_ROBOTO_API_TOKEN``.

    Roboto has no anonymous access, so none is refused. The token is printable text with no space,
    because it goes into a header; it is never printed.
    """
    if declared is not None:
        unknown = sorted(set(declared) - {"roboto_api_token"})
        if unknown:
            raise ObjectStoreConfigError(f"unknown credentials for {CONNECTOR_ID}: {unknown}")
        token = declared.get("roboto_api_token")
    else:
        token = environ.get(TOKEN_ENV) or None
    if token is None:
        raise ObjectStoreConfigError(
            f"no Roboto API token: declare roboto_api_token or set {TOKEN_ENV}"
        )
    if not isinstance(token, str) or not token or not token.isascii() or not token.isprintable():
        raise ObjectStoreConfigError("the Roboto API token is non-empty printable ASCII text")
    if " " in token:
        raise ObjectStoreConfigError("the Roboto API token holds no space")
    return token


__all__ = [
    "CONNECTOR_ID",
    "RobotoLocation",
    "RobotoOptions",
    "api_token",
    "parse_url",
]
