"""What an object-store source is given: a URL, options and read-only credentials (ADR 0006).

- The URL names the store and the prefix: ``s3://<bucket>/<prefix>``, ``gs://<bucket>/<prefix>`` or
  ``az://<account>/<container>/<prefix>``. The prefix is taken verbatim: no percent-decoding, no
  Unicode normalisation, no collapsing of ``//`` or ``..``.
- Options are declared, closed and checked: an unknown option is refused, never ignored.
- Credentials are declared by the caller, or else read from ``NEPTUNE_*`` environment variables.
  The ambient ``AWS_*``, ``GOOGLE_*`` and ``AZURE_*`` variables (and credential files) are never
  read, so an operator's own, usually writable, credentials are never picked up by accident. An
  Azure SAS token that grants anything beyond read and list is refused.
- Nothing here touches the network.
"""

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.clients import Addressing, Provider
from neptune_deploy.sources.object_store.sigv4 import AwsCredentials
from neptune_deploy.sources.object_store.transport import DEFAULT_TIMEOUT, Endpoint

# The connector ids: the ``neptune.sources`` entry-point names, and every ExternalObjectRef's.
CONNECTOR_IDS: Final = {
    Provider.S3: "deploy_s3",
    Provider.GCS: "deploy_gcs",
    Provider.AZURE: "deploy_azure_blob",
}
SCHEMES: Final = {"s3": Provider.S3, "gs": Provider.GCS, "az": Provider.AZURE}

MAX_KEY_BYTES: Final = 1024  # S3's and GCS's limit on a key; Azure's names are shorter still
DEFAULT_MAX_OBJECTS: Final = 1_000_000
DEFAULT_PAGE_SIZE: Final = 1000
DEFAULT_MAX_LISTING_BYTES: Final = 256 * 1024 * 1024  # key and token bytes a listing may hold

ENV: Final = {
    "s3_access_key_id": "NEPTUNE_S3_ACCESS_KEY_ID",
    "s3_secret_access_key": "NEPTUNE_S3_SECRET_ACCESS_KEY",
    "s3_session_token": "NEPTUNE_S3_SESSION_TOKEN",
    "gcs_access_token": "NEPTUNE_GCS_ACCESS_TOKEN",
    "azure_sas_token": "NEPTUNE_AZURE_SAS_TOKEN",
}

_S3_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]")
_GCS_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9._\-]{1,220}[a-z0-9]")
_AZURE_ACCOUNT: Final = re.compile(r"[a-z0-9]{3,24}")
_AZURE_CONTAINER: Final = re.compile(r"[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]")
_REGION: Final = re.compile(r"[a-z0-9][a-z0-9\-]{0,31}")
_STORE: Final = re.compile(r"[a-z0-9][a-z0-9\-]{0,62}")
# What a SAS token may grant: read and list. Anything else (write, delete, add, create, tag, ...)
# makes the credential writable, and the source refuses it.
_SAS_READ_ONLY: Final = frozenset("rl")
# Query parameters the client sets itself; a SAS token naming one is refused rather than merged.
_SAS_RESERVED: Final = frozenset({"comp", "marker", "maxresults", "prefix", "restype", "versionid"})


class ObjectStoreConfigError(ValueError):
    """The URL, options or credentials are not ones this connector accepts."""


@dataclass(frozen=True)
class StoreLocation:
    """Which bucket (or account and container) and which prefix in it."""

    provider: Provider
    bucket: str  # the S3 or GCS bucket, or the Azure container
    prefix: str
    account: str | None = None  # the Azure storage account
    store: str | None = None  # the declared name of a store at a declared endpoint

    @property
    def scope(self) -> str:
        """What every object id here starts with: ``<bucket>/`` or ``<account>/<container>/``,
        after ``<store>:`` for a declared endpoint, whose bucket names are its own (ADR 0006 §3).

        No bucket, account or container name holds ``/`` or ``:``, so the parts never run together.
        """
        store = f"{self.store}:" if self.store else ""
        names = f"{self.account}/{self.bucket}" if self.account else self.bucket
        return f"{store}{names}/"

    def object_id(self, key: str) -> str:
        """The ``ExternalObjectRef.object_id`` of ``key``: the scope, then the key verbatim."""
        return self.scope + key


def parse_url(url: str, provider: Provider) -> StoreLocation:
    """The location a source URL names, for the connector of ``provider``."""
    if not isinstance(url, str):
        raise ObjectStoreConfigError(f"a source URL is text, got {type(url).__name__}")
    scheme, sep, rest = url.partition("://")
    if not sep or SCHEMES.get(scheme.lower()) is not provider:
        expected = next(name for name, value in SCHEMES.items() if value is provider)
        raise ObjectStoreConfigError(f"{CONNECTOR_IDS[provider]} reads {expected}:// URLs")
    account = None
    if provider is Provider.AZURE:
        account, _, rest = rest.partition("/")
        if not _AZURE_ACCOUNT.fullmatch(account):
            raise ObjectStoreConfigError("not an Azure storage account name")
    bucket, _, prefix = rest.partition("/")
    pattern = {
        Provider.S3: _S3_BUCKET,
        Provider.GCS: _GCS_BUCKET,
        Provider.AZURE: _AZURE_CONTAINER,
    }[provider]
    if not pattern.fullmatch(bucket) or ".." in bucket:
        raise ObjectStoreConfigError(
            "not a bucket or container name (a URL holds no user information)"
        )
    try:
        encoded = prefix.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ObjectStoreConfigError("the prefix is not valid Unicode") from exc
    if len(encoded) > MAX_KEY_BYTES:
        raise ObjectStoreConfigError(f"the prefix is longer than {MAX_KEY_BYTES} bytes")
    return StoreLocation(provider, bucket, prefix, account)


@dataclass(frozen=True)
class Options:
    """Declared options. ``endpoint`` replaces the provider's public endpoint (MinIO, an emulator),
    and ``store`` then names that store: its bucket names are its own, so the name is part of each
    object's identity.

    ``addressing`` (S3 only) defaults to virtual-hosted for the public endpoint and path-style for
    a declared one. ``versions`` (S3 only) lists with ``ListObjectVersions`` so a versioned bucket's
    objects carry their version id; off, ``ListObjectsV2`` and etags, for stores without it.
    """

    endpoint: str | None = None
    store: str | None = None  # required with ``endpoint``: whose bucket namespace it is
    region: str = "us-east-1"
    addressing: Addressing | None = None
    versions: bool = True
    anonymous: bool = False
    max_objects: int = DEFAULT_MAX_OBJECTS
    max_listing_bytes: int = DEFAULT_MAX_LISTING_BYTES
    page_size: int = DEFAULT_PAGE_SIZE
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def parse(cls, options: Mapping[str, JsonValue] | None, provider: Provider) -> "Options":
        given = dict(options or {})
        s3_only = {"region", "addressing", "versions"}
        known = {
            "endpoint",
            "store",
            "anonymous",
            "max_objects",
            "max_listing_bytes",
            "page_size",
            "timeout",
        }
        allowed = known | s3_only if provider is Provider.S3 else known
        unknown = sorted(set(given) - allowed)
        if unknown:
            raise ObjectStoreConfigError(f"unknown options for {provider.value}: {unknown}")
        parsed = cls()
        endpoint = given.get("endpoint")
        if endpoint is not None and not isinstance(endpoint, str):
            raise ObjectStoreConfigError("endpoint is a URL")
        store = given.get("store")
        if (endpoint is None) != (store is None):
            raise ObjectStoreConfigError(
                "a declared endpoint needs a declared store name, and only it: bucket names are"
                " unique per store, so the store is part of every object's identity"
            )
        if store is not None and (not isinstance(store, str) or not _STORE.fullmatch(store)):
            raise ObjectStoreConfigError("not a store name")
        region = given.get("region", parsed.region)
        if not isinstance(region, str) or not _REGION.fullmatch(region):
            raise ObjectStoreConfigError("not a region")
        addressing = given.get("addressing")
        if addressing is not None and addressing not in tuple(Addressing):
            raise ObjectStoreConfigError(f"addressing is one of {[a.value for a in Addressing]}")
        flags = {}
        for name in ("versions", "anonymous"):
            value = given.get(name, getattr(parsed, name))
            if not isinstance(value, bool):
                raise ObjectStoreConfigError(f"{name} is true or false")
            flags[name] = value
        counts = {}
        for name, low, high in (
            ("max_objects", 1, 10**8),
            ("max_listing_bytes", 1024, 2**40),
            ("page_size", 1, 1000),
        ):
            value = given.get(name, getattr(parsed, name))
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ObjectStoreConfigError(f"{name} is an integer from {low} to {high}")
            counts[name] = value
        timeout = given.get("timeout", parsed.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not timeout > 0:
            raise ObjectStoreConfigError("timeout is a positive number of seconds")
        return cls(
            endpoint=endpoint,
            store=store,
            region=region,
            addressing=Addressing(addressing) if addressing is not None else None,
            versions=flags["versions"],
            anonymous=flags["anonymous"],
            max_objects=counts["max_objects"],
            max_listing_bytes=counts["max_listing_bytes"],
            page_size=counts["page_size"],
            timeout=float(timeout),
        )


def endpoint_for(location: StoreLocation, options: Options) -> tuple[Endpoint, Addressing]:
    """Where requests for ``location`` go, and (for S3) how the bucket is addressed."""
    try:
        if location.provider is Provider.GCS:
            return Endpoint.parse(
                options.endpoint or "https://storage.googleapis.com"
            ), Addressing.PATH
        if location.provider is Provider.AZURE:
            default = f"https://{location.account}.blob.core.windows.net"
            return Endpoint.parse(options.endpoint or default), Addressing.PATH
        addressing = options.addressing or (
            Addressing.PATH if options.endpoint else Addressing.VIRTUAL
        )
        base = Endpoint.parse(options.endpoint or f"https://s3.{options.region}.amazonaws.com")
    except ValueError as exc:
        raise ObjectStoreConfigError(str(exc)) from exc
    if addressing is Addressing.PATH:
        return base, addressing
    if "." in location.bucket and base.scheme == "https":
        # A dotted bucket as a host name fails TLS validation of the wildcard certificate.
        raise ObjectStoreConfigError("a bucket with dots is addressed path-style over https")
    return Endpoint(
        base.scheme, f"{location.bucket}.{base.host}", base.port, base.base_path
    ), addressing


@dataclass(frozen=True)
class Credentials:
    """The credentials of one provider; at most one field is set. Never printed."""

    aws: AwsCredentials | None = None
    gcs_token: str | None = None
    azure_sas: tuple[tuple[str, str], ...] | None = None

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


def _declared_text(declared: Mapping[str, str], name: str) -> str | None:
    value = declared.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ObjectStoreConfigError(f"credential {name} is non-empty printable text")
    return value


def azure_sas(token: str) -> tuple[tuple[str, str], ...]:
    """A SAS token's parameters, refused unless it grants read and list only."""
    # Split by hand: ``parse_qsl`` reads ``+`` as a space, which corrupts a base64 signature
    # written unescaped. Percent-escapes are decoded; ``+`` stays ``+``.
    params: list[tuple[str, str]] = []
    for part in token.removeprefix("?").split("&"):
        name, sep, value = part.partition("=")
        if not sep or not name:
            raise ObjectStoreConfigError("the SAS token is not a query string")
        params.append((urllib.parse.unquote(name), urllib.parse.unquote(value)))
    names = [name for name, _ in params]
    if len(set(names)) != len(names):
        raise ObjectStoreConfigError("the SAS token repeats a parameter")
    fields = dict(params)
    if "sig" not in fields:
        raise ObjectStoreConfigError("the SAS token has no signature")
    permissions = fields.get("sp")
    if not permissions or not set(permissions) <= _SAS_READ_ONLY:
        raise ObjectStoreConfigError(
            "the SAS token must grant read and list only (sp of r and l); it is refused"
        )
    if reserved := sorted(set(fields) & _SAS_RESERVED):
        raise ObjectStoreConfigError(f"the SAS token sets request parameters: {reserved}")
    return tuple(params)


def credentials_for(
    provider: Provider,
    declared: Mapping[str, str] | None,
    environ: Mapping[str, str],
    *,
    anonymous: bool,
) -> Credentials:
    """The credentials to use: ``declared`` if given, else the ``NEPTUNE_*`` environment variables.

    Anonymous access is chosen explicitly; credentials found while anonymous, or none found while
    not, are refused.
    """
    names = {
        Provider.S3: ("s3_access_key_id", "s3_secret_access_key", "s3_session_token"),
        Provider.GCS: ("gcs_access_token",),
        Provider.AZURE: ("azure_sas_token",),
    }[provider]
    if declared is not None:
        unknown = sorted(set(declared) - set(names))
        if unknown:
            raise ObjectStoreConfigError(f"unknown credentials for {provider.value}: {unknown}")
        source = {name: _declared_text(declared, name) for name in names}
    else:
        source = {name: environ.get(ENV[name]) or None for name in names}
    present = {name: value for name, value in source.items() if value is not None}
    if anonymous:
        if present:
            raise ObjectStoreConfigError("anonymous access is declared, and credentials are set")
        return Credentials()
    if provider is Provider.S3:
        key, secret = present.get("s3_access_key_id"), present.get("s3_secret_access_key")
        if key is None or secret is None:
            raise ObjectStoreConfigError(
                "no S3 credentials: declare them, set NEPTUNE_S3_ACCESS_KEY_ID and"
                " NEPTUNE_S3_SECRET_ACCESS_KEY, or declare anonymous access"
            )
        try:
            return Credentials(aws=AwsCredentials(key, secret, present.get("s3_session_token")))
        except ValueError as exc:
            raise ObjectStoreConfigError(str(exc)) from exc
    if provider is Provider.GCS:
        token = present.get("gcs_access_token")
        if token is None or not token.isprintable() or " " in token:
            raise ObjectStoreConfigError(
                "no GCS access token: declare it, set NEPTUNE_GCS_ACCESS_TOKEN, or declare"
                " anonymous access"
            )
        return Credentials(gcs_token=token)
    sas = present.get("azure_sas_token")
    if sas is None:
        raise ObjectStoreConfigError(
            "no Azure SAS token: declare it, set NEPTUNE_AZURE_SAS_TOKEN, or declare anonymous"
            " access"
        )
    return Credentials(azure_sas=azure_sas(sas))
