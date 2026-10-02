"""What a record source is given: a URL, options and read-only credentials, declared (ADR 0008).

- The URL names the system and the part of it to read: ``jira://<site>/<PROJECT>``,
  ``servicenow://<instance host>/<table>``, ``confluence://<site>/<space id>``,
  ``gdrive://<drive id or my-drive>``, ``rest://<host>`` (the profile names the rest). No user
  information, query or fragment: credentials never ride in a URL.
- Options are declared, closed and checked: an unknown option is refused, never ignored.
- Credentials are the ones declared to the factory or, if none are, the ``NEPTUNE_*`` variables of
  ``environ``. Ambient variables, files and instance metadata are never read.
- Identity says whose records they are: the host the URL names, or the declared ``instance`` name.
  A loopback host or a declared endpoint (a tunnel, an emulator, an on-premises gateway) is not a
  name anyone else shares, so it requires a declared ``instance``.
- Nothing here touches the network.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import DEFAULT_TIMEOUT, MAX_TIMEOUT, Endpoint

DEFAULT_MAX_RECORDS: Final = 100_000
DEFAULT_MAX_ATTACHMENT_BYTES: Final = 64 * 1024 * 1024
DEFAULT_MAX_SNAPSHOT_BYTES: Final = 256 * 1024 * 1024
DEFAULT_MAX_LISTING_BYTES: Final = 64 * 1024 * 1024  # id, token and name bytes a listing may hold

_INSTANCE: Final = re.compile(r"[a-z0-9][a-z0-9\-]{0,62}")
_HOST: Final = re.compile(r"[a-z0-9][a-z0-9.\-]{0,252}")


class RecordConfigError(ValueError):
    """The URL, options or credentials are not ones a record connector accepts."""


@dataclass(frozen=True)
class Location:
    """Which system, which instance of it, and which part. ``scope`` starts every object id."""

    connector_id: str
    instance: str  # the host (with a non-default port), or ``@<declared name>``
    what: str  # a project key, table, space id, drive id or profile id

    @property
    def scope(self) -> str:
        return f"{self.instance}/{self.what}/"


@dataclass(frozen=True)
class Options:
    """Declared options common to every record connector; ``extra`` holds the system's own."""

    instance: str | None = None
    scheme: str = "https"
    since: str | None = None
    max_records: int = DEFAULT_MAX_RECORDS
    page_size: int | None = None
    timeout: float = DEFAULT_TIMEOUT
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES
    max_listing_bytes: int = DEFAULT_MAX_LISTING_BYTES
    extra: Mapping[str, Any] = field(default_factory=dict)
    named: str | None = None  # a name the URL gives, where the API host is shared (Linear)

    @classmethod
    def parse(
        cls,
        options: Mapping[str, JsonValue] | None,
        extras: Mapping[str, Callable[[JsonValue], Any]],
        *,
        max_page_size: int,
    ) -> "Options":
        given = dict(options or {})
        common = {
            "instance",
            "scheme",
            "since",
            "max_records",
            "page_size",
            "timeout",
            "max_attachment_bytes",
            "max_snapshot_bytes",
            "max_listing_bytes",
        }
        unknown = sorted(set(given) - common - set(extras))
        if unknown:
            raise RecordConfigError(f"unknown options: {unknown}")
        instance = given.get("instance")
        if instance is not None and (
            not isinstance(instance, str) or not _INSTANCE.fullmatch(instance)
        ):
            raise RecordConfigError("not an instance name")
        scheme = given.get("scheme", "https")
        if scheme not in ("https", "http"):
            raise RecordConfigError("scheme is https, or http to a loopback host")
        since = given.get("since")
        if since is not None and (not isinstance(since, str) or not since.isprintable()):
            raise RecordConfigError("since is a cursor, as an earlier run returned it")
        counts: dict[str, int] = {}
        for name, low, high, default in (
            ("max_records", 1, 10**8, DEFAULT_MAX_RECORDS),
            ("page_size", 1, max_page_size, max_page_size),
            ("max_attachment_bytes", 1, 2**34, DEFAULT_MAX_ATTACHMENT_BYTES),
            ("max_snapshot_bytes", 1, 2**36, DEFAULT_MAX_SNAPSHOT_BYTES),
            ("max_listing_bytes", 1024, 2**40, DEFAULT_MAX_LISTING_BYTES),
        ):
            value = given.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise RecordConfigError(f"{name} is an integer from {low} to {high}")
            counts[name] = value
        timeout = given.get("timeout", DEFAULT_TIMEOUT)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0 < timeout <= MAX_TIMEOUT
        ):
            raise RecordConfigError(f"timeout is from 0 to {MAX_TIMEOUT} seconds")
        return cls(
            instance=instance,
            scheme=scheme,
            since=since,
            max_records=counts["max_records"],
            page_size=counts["page_size"],
            timeout=float(timeout),
            max_attachment_bytes=counts["max_attachment_bytes"],
            max_snapshot_bytes=counts["max_snapshot_bytes"],
            max_listing_bytes=counts["max_listing_bytes"],
            extra={name: check(given[name]) for name, check in extras.items() if name in given},
        )


def split_url(url: str, scheme: str) -> tuple[str, str]:
    """``(authority, path)`` of ``<scheme>://<authority>/<path>``; nothing else rides in a URL."""
    if not isinstance(url, str) or not url.isprintable() or " " in url:
        raise RecordConfigError("not a source URL")
    head, sep, rest = url.partition("://")
    if not sep or head.lower() != scheme:
        raise RecordConfigError(f"this connector reads {scheme}:// URLs")
    if "@" in rest.partition("/")[0]:
        raise RecordConfigError(
            "a source URL holds no user information; declare credentials instead"
        )
    if any(ch in rest for ch in "?#@\\"):
        raise RecordConfigError("a source URL has no query or fragment")
    authority, _, path = rest.partition("/")
    if not authority:
        raise RecordConfigError("a source URL names its system")
    return authority, path.strip("/")


def endpoint_for(authority: str, options: Options) -> Endpoint:
    try:
        return Endpoint.parse(f"{options.scheme}://{authority}")
    except ValueError as exc:
        raise RecordConfigError(str(exc)) from exc


def is_loopback(endpoint: Endpoint) -> bool:
    return (
        endpoint.host == "localhost" or endpoint.host.startswith("127.") or endpoint.host == "::1"
    )


def instance_name(endpoint: Endpoint, options: Options, *, declared_endpoint: bool = False) -> str:
    """The identity of the instance: its host, or the name the operator declared (required for a
    loopback host or a declared endpoint, which no one else shares). ``named`` is a name the
    URL gives (``options.named``: a Linear workspace shares one API host with every other)."""
    if options.instance is not None:
        return f"@{options.instance}"
    if options.named is not None:
        return f"@{options.named}"
    if declared_endpoint or is_loopback(endpoint):
        raise RecordConfigError(
            "a loopback host or declared endpoint needs a declared instance name: it is part of"
            " every object's identity"
        )
    default = 443 if endpoint.scheme == "https" else 80
    name = endpoint.host if endpoint.port == default else f"{endpoint.host}:{endpoint.port}"
    if not _HOST.fullmatch(endpoint.host):
        raise RecordConfigError("not a host name")
    return name


def credentials(
    names: Mapping[str, str],
    declared: Mapping[str, str] | None,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """The credentials to use: ``declared`` if given, else the ``NEPTUNE_*`` variables ``names``
    maps them to. Values are non-empty printable text with no line break (a header value)."""
    if declared is not None:
        unknown = sorted(set(declared) - set(names))
        if unknown:
            raise RecordConfigError(f"unknown credentials: {unknown}")
        source: Mapping[str, str | None] = declared
    else:
        source = {name: environ.get(env) for name, env in names.items()}
    found: dict[str, str] = {}
    for name in names:
        value = source.get(name)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.isprintable() or value != value.strip():
            raise RecordConfigError(f"credential {name} is printable text with no edge spaces")
        found[name] = value
    return found


def need(found: Mapping[str, str], *names: str) -> None:
    """Refuse unless every credential in ``names`` is present."""
    missing = [name for name in names if name not in found]
    if missing:
        raise RecordConfigError(f"missing credentials: {missing}")


def cursor_text(connector_id: str, payload: str) -> str:
    """A cursor as a later run is given it: connector id, ``/1:`` (the format), then the payload."""
    return f"{connector_id}/1:{payload}"


def cursor_payload(connector_id: str, text: str | None, shape: "re.Pattern[str]") -> str | None:
    """The payload of ``text`` if it is this connector's cursor of the right shape; ``None`` for no
    cursor. Another connector's cursor, or a payload that is not ``shape``, is refused."""
    if text is None:
        return None
    prefix = f"{connector_id}/1:"
    payload = text.removeprefix(prefix)
    if payload == text or not shape.fullmatch(payload):
        raise RecordConfigError(f"since is not a {connector_id} cursor")
    return payload
