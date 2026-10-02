"""Finding codes of the Foxglove connector, and how a failed call becomes one (ADR 0007 §7).

Each code is prefixed with the connector id in a finding (``deploy_foxglove.<code>``); its category
and severity are fixed per code. Findings carry codes, counts, statuses and ids (as hex), never
error text, URLs, link hosts or the API key.
"""

from typing import Final

from neptune.model.finding import FindingCategory, Severity
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.foxglove.client import LinkRefused, ResponseInvalid
from neptune_deploy.sources.object_store.transport import (
    HttpStatusError,
    RedirectRefused,
    TransportError,
)

CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "listing_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a listing request failed; the listing is incomplete and nothing is asserted gone",
    ),
    "not_authorised": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "the API refused the key (401 or 403); check its capabilities",
    ),
    "rate_limited": (
        FindingCategory.LIMIT,
        Severity.ERROR,
        "the API rate-limited the request (429); it was not retried",
    ),
    "redirect_refused": (
        FindingCategory.SKIPPED,
        Severity.ERROR,
        "the API or a download link answered with a redirect, which is never followed",
    ),
    "response_invalid": (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "a response is not the strict JSON the API documents; the read stopped there",
    ),
    "link_refused": (
        FindingCategory.SKIPPED,
        Severity.ERROR,
        "the API named a download link that is not https at the API's host or a declared link host",
    ),
    "listing_limit": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "the listing stopped at the recording or page limit; it is incomplete",
    ),
    "pagination_loop": (
        FindingCategory.INCONSISTENT,
        Severity.ERROR,
        "a page held only recordings already listed; the listing stopped there",
    ),
    "record_invalid": (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "entries are not the objects the API documents; they are not used",
    ),
    "recording_id_invalid": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "recordings whose id is not a usable Foxglove id; they are not used",
    ),
    "recording_duplicated": (
        FindingCategory.AMBIGUOUS,
        Severity.WARNING,
        "one recording id was listed with two different descriptions; neither is used",
    ),
    "import_incomplete": (
        FindingCategory.SKIPPED,
        Severity.WARNING,
        "recordings whose import status is not complete have no data to stream; not read",
    ),
    "stream_unavailable": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "the recording's MCAP stream could not be opened to measure it; not read",
    ),
    "size_unknown": (
        FindingCategory.MISSING,
        Severity.ERROR,
        "the stream states no total length, so it cannot be read in ranges; not read",
    ),
    "stream_empty": (
        FindingCategory.MISSING,
        Severity.WARNING,
        "the recording's stream holds no bytes; not read",
    ),
    "stream_too_large": (
        FindingCategory.LIMIT,
        Severity.ERROR,
        "the stream claims more bytes than the connector's limit; not read",
    ),
    "short_read": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a ranged read ended before the bytes it promised",
    ),
    "object_changed": (
        FindingCategory.INCONSISTENT,
        Severity.ERROR,
        "the stream no longer holds the listed revision's bytes (size or chunk hash)",
    ),
    "object_gone": (
        FindingCategory.MISSING,
        Severity.ERROR,
        "the recording no longer exists",
    ),
    "read_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a ranged read failed, or was answered with other bytes than were asked for",
    ),
    "devices_failed": (
        FindingCategory.FAILED,
        Severity.WARNING,
        "the device list could not be read; declared device data is only what recordings state",
    ),
    "topics_failed": (
        FindingCategory.FAILED,
        Severity.WARNING,
        "a recording's topic list could not be read; its topics are Unknown",
    ),
    "gone_unverified": (
        FindingCategory.FAILED,
        Severity.WARNING,
        "recordings missing from the listing could not be checked one by one; not asserted gone",
    ),
    "device_name_differs": (
        FindingCategory.INCONSISTENT,
        Severity.WARNING,
        "a recording and the device list give one device id two names; both are kept, Ambiguous",
    ),
    "identifier_property_unusable": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "a device property declared as an identifier holds no text value; it is not an identifier",
    ),
}
SKIP_REASONS: Final = (
    "import_incomplete",
    "record_invalid",
    "recording_duplicated",
    "recording_id_invalid",
)


def failure(exc: TransportError, default: str) -> tuple[str, dict[str, JsonValue]]:
    """The finding code for a failed call (``default`` unless the failure has its own), and its
    details: a status and a cause, never text."""
    details: dict[str, JsonValue] = {}
    if exc.status is not None:
        details["status"] = exc.status
    if isinstance(exc, RedirectRefused | LinkRefused | ResponseInvalid):
        return exc.code, details
    if isinstance(exc, HttpStatusError):
        if exc.status in (401, 403):
            return "not_authorised", details
        if exc.status == 429:
            return "rate_limited", details
    details["cause"] = exc.code
    return default, details
