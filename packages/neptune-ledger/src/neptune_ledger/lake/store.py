"""Where a package's bytes are, and how an engine reaches them: the ``ObjectStore`` (ADR 0013 §3).

A store holds objects under keys: package-relative POSIX paths such as ``manifest.json`` or
``series/<64 hex>.parquet``. The Ledger only reads through it. It asks for a small object's bytes
(a manifest), an object's size, and a ``Location`` that a query engine can scan in place. Nothing
here copies, caches or writes an object.

Two stores exist: ``LocalObjectStore`` (a package directory, opened without following links) and
``S3ObjectStore`` (a bucket and prefix on any S3-compatible service). Platform X3 will own this
interface; it is kept to what the Ledger's reads need today: ``read_range`` serves the media
store's lazy, chunk-verified source reads (ADR 0014 §3).
"""

import functools
import os
import re
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from neptune_ledger.catalog.check import open_below, open_root

# A key is a package-relative path: non-empty parts, no "." or "..", no NUL, no backslash.
_KEY: Final = re.compile(r"[^/\\\x00]+(/[^/\\\x00]+)*")
# Characters both engines read as a glob in a path they are handed (ADR 0013 §5).
GLOB_CHARACTERS: Final = frozenset("*?[]{}")
_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
# What an engine parsing ``s3://bucket/key`` as a URL would read as a fragment, query or escape.
_URL_CHARACTERS: Final = frozenset("#%?")


class StoreError(ValueError):
    """A store or key that breaks the store contract: a caller error, never a package's fault."""


def check_key(key: str) -> str:
    """``key`` if it is a package-relative path, else ``StoreError``."""
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise StoreError(f"not a package-relative key: {key!r}")
    if any(part in (".", "..") for part in key.split("/")):
        raise StoreError(f"not a package-relative key: {key!r}")
    return key


@dataclass(frozen=True)
class S3Settings:
    """How to reach an S3-compatible service. Credentials never appear in ``repr`` or findings.

    ``endpoint`` is a URL (``http://127.0.0.1:9000`` for MinIO) or None for AWS itself;
    ``allow_http`` must be set for a plain-HTTP endpoint.
    """

    region: str
    endpoint: str | None = None
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    allow_http: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.region, str) or not self.region:
            raise StoreError("an S3 region is a non-empty string")
        if (self.access_key is None) != (self.secret_key is None):
            raise StoreError("an S3 access key and secret key are given together or not at all")
        if self.endpoint is not None:
            scheme = self.endpoint.split("://", 1)[0]
            if scheme not in ("http", "https") or "://" not in self.endpoint:
                raise StoreError(f"an S3 endpoint is an http(s) URL: {self.endpoint!r}")
            if scheme == "http" and not self.allow_http:
                raise StoreError("a plain-HTTP endpoint needs allow_http")


@dataclass(frozen=True)
class Location:
    """Where an engine scans one object: an absolute local path, or ``s3://bucket/key`` plus the
    settings that reach it (``s3`` is None exactly for a local path)."""

    url: str
    s3: S3Settings | None = None

    @property
    def bucket(self) -> str | None:
        return self.url.removeprefix("s3://").split("/", 1)[0] if self.s3 is not None else None

    @property
    def path(self) -> str:
        """The path an Arrow filesystem opens: the local path, or ``bucket/key``."""
        return self.url.removeprefix("s3://") if self.s3 is not None else self.url


class ObjectStore(Protocol):
    """Read-only access to one package's objects (ADR 0013 §3)."""

    def describe(self) -> str:
        """Where the store is, for findings: no credentials."""
        ...

    def location(self, key: str) -> Location:
        """Where an engine scans ``key`` in place."""
        ...

    def size(self, key: str) -> int | None:
        """The object's size, or None if it is missing, unreadable or not a plain object."""
        ...

    def read(self, key: str, limit: int) -> bytes | None:
        """The object's bytes, or None if it is missing, unreadable or larger than ``limit``."""
        ...

    def read_range(self, key: str, offset: int, length: int) -> bytes | None:
        """``length`` bytes from ``offset`` (ADR 0014 §3), or None if the object is missing,
        unreadable or shorter than ``offset + length``."""
        ...


class LocalObjectStore:
    """A package directory on a local filesystem.

    Every read opens each path component with ``O_NOFOLLOW`` (``catalog.check``), so a link
    anywhere under the root, or on the root itself, reads as missing. An engine handed a
    ``Location`` opens the path itself; the lake checks the object here first (ADR 0013 §5).
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        text = os.fspath(root)
        if not text.startswith("/") or "\x00" in text:
            raise StoreError(f"a local store's root is an absolute path: {text!r}")
        self._root = text.rstrip("/") or "/"

    def describe(self) -> str:
        return self._root

    def location(self, key: str) -> Location:
        return Location(f"{self._root.rstrip('/')}/{check_key(key)}")

    def _open(self, key: str) -> int | None:
        root = open_root(self._root)
        if root is None:
            return None
        try:
            return open_below(root, check_key(key))
        except OSError:
            return None
        finally:
            os.close(root)

    def size(self, key: str) -> int | None:
        fd = self._open(key)
        if fd is None:
            return None
        try:
            return os.fstat(fd).st_size
        finally:
            os.close(fd)

    def read(self, key: str, limit: int) -> bytes | None:
        fd = self._open(key)
        if fd is None:
            return None
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(limit + 1)
        return None if len(data) > limit else data

    def read_range(self, key: str, offset: int, length: int) -> bytes | None:
        _check_range(offset, length)
        fd = self._open(key)
        if fd is None:
            return None
        try:
            data = bytearray()
            while len(data) < length:
                got = os.pread(fd, length - len(data), offset + len(data))
                if not got:
                    return None
                data += got
            return bytes(data)
        except OSError:
            return None
        finally:
            os.close(fd)


class S3ObjectStore:
    """Objects under ``prefix`` in ``bucket`` on an S3-compatible service (MinIO, AWS S3).

    Reads go through pyarrow's S3 filesystem, which the compiler's dependency already ships, so
    the Ledger needs no S3 client of its own.
    """

    def __init__(self, settings: S3Settings, bucket: str, prefix: str = "") -> None:
        if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket) or ".." in bucket:
            raise StoreError(f"not an S3 bucket name: {bucket!r}")
        prefix = prefix.strip("/")
        if prefix:
            check_key(prefix)
            if _URL_CHARACTERS & set(prefix):
                raise StoreError(f"an S3 key holds none of # % ?: {prefix!r}")
        self._settings = settings
        self._bucket = bucket
        self._prefix = prefix

    @property
    def settings(self) -> S3Settings:
        return self._settings

    def describe(self) -> str:
        endpoint = self._settings.endpoint or f"aws:{self._settings.region}"
        where = f"{self._bucket}/{self._prefix}" if self._prefix else self._bucket
        return f"s3://{where} at {endpoint}"

    def _key(self, key: str) -> str:
        key = check_key(key)
        full = f"{self._prefix}/{key}" if self._prefix else key
        if _URL_CHARACTERS & set(full):
            raise StoreError(f"an S3 key holds none of # % ?: {full!r}")
        return full

    def location(self, key: str) -> Location:
        return Location(f"s3://{self._bucket}/{self._key(key)}", self._settings)

    def filesystem(self) -> Any:
        return arrow_s3(self._settings)

    def size(self, key: str) -> int | None:
        import pyarrow.fs as pafs

        try:
            info = self.filesystem().get_file_info(f"{self._bucket}/{self._key(key)}")
        except (OSError, ValueError):
            return None
        return int(info.size) if info.type == pafs.FileType.File else None

    def read(self, key: str, limit: int) -> bytes | None:
        size = self.size(key)
        if size is None or size > limit:
            return None
        try:
            with self.filesystem().open_input_stream(f"{self._bucket}/{self._key(key)}") as f:
                data = f.read(limit + 1)
        except (OSError, ValueError):
            return None
        return None if len(data) > limit else bytes(data)

    def read_range(self, key: str, offset: int, length: int) -> bytes | None:
        _check_range(offset, length)
        try:
            with self.filesystem().open_input_file(f"{self._bucket}/{self._key(key)}") as f:
                data = f.read_at(length, offset) if length else b""
        except (OSError, ValueError):
            return None
        return bytes(data) if len(data) == length else None


def _check_range(offset: int, length: int) -> None:
    for value in (offset, length):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StoreError(f"a byte range is two non-negative ints: {offset!r}, {length!r}")


@functools.lru_cache(maxsize=32)
def arrow_s3(settings: S3Settings) -> Any:
    """A pyarrow S3 filesystem for ``settings``, one per settings: its client and connection
    pool are reused by every store, footer read and DuckDB scan that names them."""
    import pyarrow.fs as pafs

    endpoint = settings.endpoint
    return pafs.S3FileSystem(
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        session_token=settings.session_token,
        region=settings.region,
        endpoint_override=endpoint.split("://", 1)[1] if endpoint else None,
        scheme=endpoint.split("://", 1)[0] if endpoint else "https",
        allow_bucket_creation=False,
        allow_bucket_deletion=False,
    )


def local_store(package_id: str, root_locator: str) -> ObjectStore:
    """The default package location (ADR 0013 §2): the directory the package was registered from."""
    del package_id  # the root locator alone names a local package
    return LocalObjectStore(root_locator)
