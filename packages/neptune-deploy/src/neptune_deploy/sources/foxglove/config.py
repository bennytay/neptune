"""What a Foxglove source is given: a URL, options and one read-only API key (ADR 0007).

- The URL names the scope: ``foxglove://<project id>`` for one project, or ``foxglove://-`` for
  every project the key can read. It is never part of an object's identity (a recording id is
  global to its Foxglove deployment), only of what was asked for.
- Options are declared, closed and checked: an unknown option is refused, never ignored.
- The API key is declared by the caller, or else read from ``NEPTUNE_FOXGLOVE_API_KEY``. Nothing
  ambient is read: not ``FOXGLOVE_*``, not the Foxglove CLI's credential file.
- Nothing here touches the network.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import DEFAULT_TIMEOUT, Endpoint

CONNECTOR_ID: Final = "deploy_foxglove"
CONNECTOR_VERSION: Final = "0.1.0"
DEFAULT_ENDPOINT: Final = "https://api.foxglove.dev/v1"
API_KEY_ENV: Final = "NEPTUNE_FOXGLOVE_API_KEY"
API_KEY_NAME: Final = "foxglove_api_key"
ALL_PROJECTS: Final = "-"

MAX_ID: Final = 128  # characters in a recording, device, project or session id
DEFAULT_MAX_RECORDINGS: Final = 1_000_000
DEFAULT_MAX_LISTING_BYTES: Final = 256 * 1024 * 1024  # canonical JSON bytes a listing may hold
DEFAULT_PAGE_SIZE: Final = 1000
MAX_PAGE_SIZE: Final = 2000  # the API's own limit: more is a 400
COMPRESSIONS: Final = ("", "lz4", "zstd")

# Foxglove-generated ids look like ``rec_0dxxxx`` and ``dev_abc123``; a project id like ``prj_…``.
# The pattern is wider than that, because the API does not promise a format, and narrower than
# "anything", because an id becomes part of an object id, a URL path and a JSON pointer.
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}")
_STORE: Final = re.compile(r"[a-z0-9][a-z0-9\-]{0,62}")
_HOST: Final = re.compile(r"[a-z0-9]([a-z0-9\-.]{0,251}[a-z0-9])?")
# RFC 3339 as the API documents it: UTC, up to nine fractional digits.
_RFC3339: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z"
)
_PROPERTY_KEY: Final = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{0,63}")


class FoxgloveConfigError(ValueError):
    """The URL, options or credentials are not ones this connector accepts."""


def valid_id(value: object) -> bool:
    """Whether ``value`` can be a Foxglove id here (a recording, device, project or session id)."""
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def parse_url(url: str) -> str | None:
    """The project id ``url`` names, or ``None`` for every project (``foxglove://-``)."""
    if not isinstance(url, str):
        raise FoxgloveConfigError(f"a source URL is text, got {type(url).__name__}")
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() != "foxglove":
        raise FoxgloveConfigError(f"{CONNECTOR_ID} reads foxglove:// URLs")
    if rest == ALL_PROJECTS:
        return None
    if not valid_id(rest):
        # The URL is not echoed: nothing the operator typed is repeated in an error or a log.
        raise FoxgloveConfigError(
            f"foxglove://<project id> or foxglove://{ALL_PROJECTS} (a URL holds no user"
            " information, query or path)"
        )
    return rest


@dataclass(frozen=True)
class Options:
    """Declared options.

    ``endpoint`` (with ``store``) replaces the public API (a staging deployment, a recorded-API
    emulator): its ids are its own, so the store name is part of every object's identity.
    ``device_id``, ``device_name``, ``start`` and ``end`` narrow the index as the API's
    ``/recordings`` filters do. ``link_hosts`` names the hosts, besides the API's own, that a
    download link may point at. ``identifier_properties`` names the device properties the operator
    declares to be identifiers: only those become declared identifiers. ``topics`` reads each
    recording's declared topics (one request per recording, on demand). ``compression`` is the
    chunk compression of the MCAP the stream serves: it is part of what the bytes are.
    """

    endpoint: str = DEFAULT_ENDPOINT
    store: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    start: str | None = None
    end: str | None = None
    link_hosts: tuple[str, ...] = ()
    identifier_properties: tuple[str, ...] = ()
    topics: bool = True
    compression: str = "lz4"
    max_recordings: int = DEFAULT_MAX_RECORDINGS
    max_listing_bytes: int = DEFAULT_MAX_LISTING_BYTES
    page_size: int = DEFAULT_PAGE_SIZE
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def parse(cls, options: Mapping[str, JsonValue] | None) -> "Options":
        given = dict(options or {})
        allowed = {
            "endpoint",
            "store",
            "device_id",
            "device_name",
            "start",
            "end",
            "link_hosts",
            "identifier_properties",
            "topics",
            "compression",
            "max_recordings",
            "max_listing_bytes",
            "page_size",
            "timeout",
        }
        unknown = sorted(set(given) - allowed)
        if unknown:
            raise FoxgloveConfigError(f"unknown options for {CONNECTOR_ID}: {unknown}")
        default = cls()
        endpoint = given.get("endpoint")
        store = given.get("store")
        if (endpoint is None) != (store is None):
            raise FoxgloveConfigError(
                "a declared endpoint needs a declared store name, and only it: ids are unique per"
                " deployment, so the store is part of every recording's identity"
            )
        if endpoint is not None and not isinstance(endpoint, str):
            raise FoxgloveConfigError("endpoint is a URL")
        if store is not None and (not isinstance(store, str) or not _STORE.fullmatch(store)):
            raise FoxgloveConfigError("not a store name")
        ids: dict[str, str | None] = {}
        for name in ("device_id",):
            value = given.get(name)
            if value is not None and not valid_id(value):
                raise FoxgloveConfigError(f"{name} is a Foxglove id")
            ids[name] = value if isinstance(value, str) else None
        device_name = given.get("device_name")
        if device_name is not None and (
            not isinstance(device_name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,100}", device_name)
        ):
            raise FoxgloveConfigError("device_name is a Foxglove device name")
        times: dict[str, str | None] = {}
        for name in ("start", "end"):
            value = given.get(name)
            if value is not None and (not isinstance(value, str) or not _RFC3339.fullmatch(value)):
                raise FoxgloveConfigError(
                    f"{name} is an RFC 3339 UTC time (…Z), as the API reads it"
                )
            times[name] = value if isinstance(value, str) else None
        hosts = _text_list(given.get("link_hosts", []), "link_hosts", _HOST)
        properties = _text_list(
            given.get("identifier_properties", []), "identifier_properties", _PROPERTY_KEY
        )
        if len({name.lower() for name in properties}) != len(properties):
            raise FoxgloveConfigError("identifier_properties differ only in case")
        topics = given.get("topics", default.topics)
        if not isinstance(topics, bool):
            raise FoxgloveConfigError("topics is true or false")
        compression = given.get("compression", default.compression)
        if compression not in COMPRESSIONS:
            raise FoxgloveConfigError(f"compression is one of {list(COMPRESSIONS)}")
        counts: dict[str, int] = {}
        for name, low, high in (
            ("max_recordings", 1, 10**8),
            ("max_listing_bytes", 1024, 2**40),
            ("page_size", 1, MAX_PAGE_SIZE),
        ):
            value = given.get(name, getattr(default, name))
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise FoxgloveConfigError(f"{name} is an integer from {low} to {high}")
            counts[name] = value
        timeout = given.get("timeout", default.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not timeout > 0:
            raise FoxgloveConfigError("timeout is a positive number of seconds")
        return cls(
            endpoint=endpoint if isinstance(endpoint, str) else DEFAULT_ENDPOINT,
            store=store if isinstance(store, str) else None,
            device_id=ids["device_id"],
            device_name=device_name if isinstance(device_name, str) else None,
            start=times["start"],
            end=times["end"],
            link_hosts=hosts,
            identifier_properties=properties,
            topics=topics,
            compression=str(compression),
            max_recordings=counts["max_recordings"],
            max_listing_bytes=counts["max_listing_bytes"],
            page_size=counts["page_size"],
            timeout=float(timeout),
        )


def _text_list(value: JsonValue, name: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in value
    ):
        raise FoxgloveConfigError(f"{name} is a list of valid names")
    return tuple(sorted(set(str(item) for item in value)))


def endpoint_for(options: Options) -> Endpoint:
    """Where API requests go: ``https``, or ``http`` to a loopback host only."""
    try:
        return Endpoint.parse(options.endpoint)
    except ValueError as exc:
        raise FoxgloveConfigError(str(exc)) from exc


def api_key_for(declared: Mapping[str, str] | None, environ: Mapping[str, str]) -> str:
    """The API key to use: ``declared`` if given, else ``NEPTUNE_FOXGLOVE_API_KEY``.

    Foxglove API keys carry capabilities and cannot be inspected offline, so the operator issues one
    that holds only the read capabilities (ADR 0007 §6). It never appears in a repr or a finding.
    """
    if declared is not None:
        unknown = sorted(set(declared) - {API_KEY_NAME})
        if unknown:
            raise FoxgloveConfigError(f"unknown credentials for {CONNECTOR_ID}: {unknown}")
        key = declared.get(API_KEY_NAME)
    else:
        key = environ.get(API_KEY_ENV) or None
    if key is None:
        raise FoxgloveConfigError(
            f"no Foxglove API key: declare {API_KEY_NAME} or set {API_KEY_ENV}"
        )
    if (
        not isinstance(key, str)
        or not key
        or not key.isascii()
        or not key.isprintable()
        or " " in key
        or len(key) > 512
    ):
        raise FoxgloveConfigError("the Foxglove API key is one printable token")
    return key
