"""``FormantSource``: one Formant organisation as a read-only fleet-ops source (ADR 0010 §2 to §4).

URL: ``formant://<organization id>``. Parts: ``devices``, ``events``, ``annotations``,
``interventions`` and ``recordings`` (Formant's file records, referenced and never fetched). Each
non-empty part is one document (``base.FleetOpsSource``) with a ``stated`` table over it, and
``interventions`` also becomes ``Intervention`` lifecycle records (``interventions.py``).

Identity: ``ExternalObjectRef("deploy_formant", "<instance>/<organization>/<part>",
"records:<sha256>")``. The instance is the endpoint's host (and a non-default port), or ``@<name>``
when the operator declares ``instance``, which a declared endpoint requires (as ADR 0006 §3
requires ``store``): two sites that each run a Formant-compatible endpoint on ``localhost`` never
share an identity.

A part that cannot be read, stops at a limit or is not strict JSON is a finding and is absent or
partial; nothing is invented to fill it. The workspace is asked before the source is built and
before every request.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, Severity
from neptune.model.jsonvalue import JsonValue
from neptune.model.reference import TimestampDomain
from neptune_deploy.lifecycle.times import check_format
from neptune_deploy.sources.fleet_ops import options as opt
from neptune_deploy.sources.fleet_ops.base import FleetOpsSource, Part
from neptune_deploy.sources.fleet_ops.documents import (
    Document,
    StatedTable,
    parse_clock,
)
from neptune_deploy.sources.fleet_ops.formant_api import (
    DEFAULT_ENDPOINT,
    ROUTES,
    FormantApi,
    filters_for,
    valid_token,
)
from neptune_deploy.sources.fleet_ops.interventions import (
    DEFAULT_TIME_FORMATS,
    build_interventions,
)
from neptune_deploy.sources.object_store.transport import Endpoint, NetworkGate, redact

CONNECTOR_ID: Final = "deploy_formant"
TOKEN_VARIABLE: Final = "NEPTUNE_FORMANT_ACCESS_TOKEN"
TOKEN_CREDENTIAL: Final = "formant_access_token"
_ORGANIZATION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}")
_INSTANCE: Final = re.compile(r"@[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
_CLOCK_FIELDS: Final = ("endTime", "startTime", "time")
_OPTIONS: Final = frozenset(
    {
        "annotations",
        "clock",
        "devices",
        "device_ids",
        "endpoint",
        "events",
        "from",
        "instance",
        "interventions",
        "max_listing_bytes",
        "max_records",
        "page_size",
        "recordings",
        "time_formats",
        "timeout",
        "to",
    }
)


@dataclass(frozen=True)
class FormantOptions:
    """Declared options, checked. Defaults are the ones the transform records."""

    endpoint: str
    instance: str
    parts: tuple[str, ...]
    since: str | None
    until: str | None
    devices: tuple[str, ...]
    page_size: int
    max_records: int
    max_listing_bytes: int
    timeout: float
    time_formats: tuple[str, ...]
    clock: JsonValue | None

    @classmethod
    def parse(cls, options: Mapping[str, JsonValue] | None) -> "FormantOptions":
        given = opt.closed(options, _OPTIONS)
        endpoint = opt.text(given, "endpoint", DEFAULT_ENDPOINT)
        assert endpoint is not None
        try:
            parsed = Endpoint.parse(endpoint)
        except ValueError as exc:
            raise opt.FleetOpsConfigError(str(exc)) from exc
        declared = opt.text(given, "instance", None)
        if declared is not None and not _INSTANCE.fullmatch(declared):
            raise opt.FleetOpsConfigError("instance is '@' and a name: letters, digits, _ . -")
        if declared is None and "endpoint" in given:
            raise opt.FleetOpsConfigError(
                "a declared endpoint has its own organisations: declare an instance name"
                f" ('@name') for {redact(endpoint)}"
            )
        parts = tuple(
            name
            for name in ROUTES
            if opt.flag(given, name, True)  # fixed order, not option order
        )
        formats = opt.texts(given, "time_formats") or DEFAULT_TIME_FORMATS
        for pattern in formats:
            try:
                check_format(pattern)
            except ValueError as exc:
                raise opt.FleetOpsConfigError(f"time format {pattern!r}: {exc}") from exc
        try:
            parse_clock(given.get("clock"))
        except ValueError as exc:
            raise opt.FleetOpsConfigError(str(exc)) from exc
        return cls(
            endpoint=endpoint,
            instance=declared or parsed.authority,
            parts=parts,
            since=opt.text(given, "from", None),
            until=opt.text(given, "to", None),
            devices=opt.texts(given, "device_ids"),
            page_size=opt.integer(given, "page_size", 100, 1, 1000),
            max_records=opt.integer(given, "max_records", 100_000, 1, 10_000_000),
            max_listing_bytes=opt.integer(
                given, "max_listing_bytes", 256 * 1024 * 1024, 1024, 4 * 1024**3
            ),
            timeout=opt.seconds(given, "timeout", 60.0),
            time_formats=formats,
            clock=given.get("clock"),
        )


def parse_url(url: str) -> str:
    """The organisation id of ``formant://<organization id>``."""
    scheme, sep, rest = url.partition("://") if isinstance(url, str) else ("", "", "")
    organization = rest.strip("/")
    if scheme != "formant" or not sep or not _ORGANIZATION.fullmatch(organization):
        raise opt.FleetOpsConfigError("a Formant URL is formant://<organization id>")
    return organization


def token_for(credentials: Mapping[str, str] | None, environ: Mapping[str, str]) -> str:
    """The declared token, else ``NEPTUNE_FORMANT_ACCESS_TOKEN``. Nothing ambient is read."""
    if credentials is not None and credentials.keys() - {TOKEN_CREDENTIAL}:
        raise opt.FleetOpsConfigError(f"credentials are {TOKEN_CREDENTIAL!r} only")
    token = (credentials or {}).get(TOKEN_CREDENTIAL) or environ.get(TOKEN_VARIABLE)
    if token is None:
        raise opt.FleetOpsConfigError(
            f"no Formant token: declare {TOKEN_CREDENTIAL!r} or set {TOKEN_VARIABLE}"
        )
    if not valid_token(token):
        raise opt.FleetOpsConfigError("the Formant token is not printable ASCII without a space")
    return token


class FormantSource(FleetOpsSource):
    """A Formant organisation's devices, events, annotations, interventions and recordings."""

    connector_id = CONNECTOR_ID
    EXTRA_CODES: Final = {
        "recording_not_fetched": (
            FindingCategory.SKIPPED,
            Severity.INFO,
            "recordings are referenced by their file records; their bytes are never fetched",
        ),
    }

    def __init__(
        self,
        organization: str,
        options: FormantOptions,
        network: NetworkGate,
        token: str,
        *,
        ledger: SourceLedger | None = None,
    ) -> None:
        super().__init__(ledger)
        self.organization = organization
        self.options = options
        self.scope = f"{options.instance}/{organization}/"
        self.api = FormantApi(
            organization,
            Endpoint.parse(options.endpoint),
            network,
            token=token,
            timeout=options.timeout,
        )
        self._clock = parse_clock(options.clock)

    def config(self) -> dict[str, JsonValue]:
        o = self.options
        config: dict[str, JsonValue] = {
            "clock": self._clock.config(),
            "device_ids": list(o.devices),
            "instance": o.instance,
            "max_listing_bytes": o.max_listing_bytes,
            "max_records": o.max_records,
            "organization": self.organization,
            "page_size": o.page_size,
            "parts": list(o.parts),
            "time_formats": list(o.time_formats),
        }
        if o.since is not None:
            config["from"] = o.since
        if o.until is not None:
            config["to"] = o.until
        return config

    def collect(self) -> Sequence[Part]:
        parts: list[Part] = []
        for name in self.options.parts:
            found = self.api.query(
                name,
                filters_for(name, self.options.since, self.options.until, self.options.devices),
                page_size=self.options.page_size,
                limit=self.options.max_records,
                budget=self.options.max_listing_bytes,
            )
            part = Part(name, found.items, _CLOCK_FIELDS if name != "devices" else (), self._clock)
            if not found.complete:
                part.stopped, part.status = found.stopped or "unknown", found.status
            parts.append(part)
        return parts

    def extend(
        self,
        part: Part,
        document: Document,
        table: StatedTable,
        clocks: Mapping[str, TimestampDomain],
    ) -> Sequence[Any]:
        if part.name == "recordings":
            self.report(
                "recording_not_fetched",
                self.part_subject("recordings"),
                {"records": document.item_count},
            )
        if part.name != "interventions":
            return ()
        domains, records = build_interventions(
            document, self.transform, self.options.time_formats, self.report
        )
        return [*domains, *records]


def formant_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> FormantSource:
    """``deploy_formant``: a Formant organisation (``formant://<organization id>``), read only.

    The workspace is asked first, so a local-only workspace refuses it (root ADR 0026 §6).
    """
    network.require_network("reading deploy_formant sources")
    organization = parse_url(url)
    parsed = FormantOptions.parse(options)
    token = token_for(credentials, os.environ if environ is None else environ)
    return FormantSource(organization, parsed, network, token, ledger=ledger)
