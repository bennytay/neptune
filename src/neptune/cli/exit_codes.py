"""The ``neptune`` command's exit codes: one per SDK error code, fixed forever (ADR 0043).

A code is never reused or renumbered; a new SDK error code gets the next free number. The SDK's
``NeptuneError.code`` (ADR 0035 §6) is the only input: the CLI never reads an error's message.
"""

from typing import Final

OK: Final = 0  # committed, or a dry run planned
INTERNAL: Final = 1  # a bug: an exception that is not a NeptuneError, or the base code ``error``
USAGE: Final = 2  # the command line itself is wrong (argparse); also ``invalid_request``
CANCELLED: Final = 130  # interrupted (Ctrl-C) and stopped at a checkpoint: resume with --resume

# SDK error code -> exit code. Every code in ``neptune.sdk.ERRORS`` is here (a test checks).
BY_CODE: Final[dict[str, int]] = {
    "error": INTERNAL,
    "invalid_request": USAGE,
    "invalid_source": 3,
    "invalid_destination": 4,
    "destination_exists": 5,
    "invalid_configuration": 6,
    "nothing_to_resume": 7,
    "unsupported": 8,
    "network_refused": 9,
    "sandbox_unavailable": 10,
    "workspace_unusable": 11,
    "package_invalid": 12,
    "job_failed": 13,
    "publish_incomplete": 14,
}


def for_code(code: str) -> int:
    """The exit code for the SDK error code ``code``; an unknown code is a bug (``INTERNAL``)."""
    return BY_CODE.get(code, INTERNAL)


def table() -> str:
    """The exit-code table, for ``--help`` and ``docs/cli.md``."""
    rows = [
        (OK, "ok: committed, or a dry run planned"),
        (INTERNAL, "internal error: a bug, please report it"),
        (USAGE, "usage: the command line is wrong (invalid_request)"),
        *((number, code) for code, number in BY_CODE.items() if number > USAGE),
        (CANCELLED, "cancelled at a checkpoint: rerun with --resume"),
    ]
    return "\n".join(f"  {number:>3}  {text}" for number, text in rows)
