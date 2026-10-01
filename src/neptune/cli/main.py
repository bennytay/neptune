"""``neptune ingest``: one command from a messy run folder to a package (ADR 0043).

A thin layer over the SDK (ADR 0035): the command line becomes a ``Neptune`` client and one call,
``ingest`` or ``dry_run``; the SDK's events become progress, its ``NeptuneError.code`` the exit
code (``exit_codes``), and its ``IngestResult`` the result. Nothing here decides what is ingested.

Output:

- human (default): quiet. A committed job prints where the package and its receipt are and a
  one-line summary on stdout; errors go to stderr as ``neptune: <code>: <message>``. ``-v``
  adds one progress line per job event on stderr.
- ``--json``: JSON Lines on stdout (sorted keys, no whitespace). One
  ``{"type": "event", ...}`` line per job event as it happens, then exactly one
  ``{"type": "result", ...}`` line, always last, always with the same keys. No wall-clock time,
  job token, host or workspace path appears, so the same input gives byte-identical lines.

``--explain`` (a dry run) adds the plan's ``Explanation`` (ADR 0044): rendered after the planned
lines, or, with ``--json``, one ``{"explanation": ..., "type": "explanation"}`` line (its canonical
``dumps()``) just before the result line.

Ctrl-C stops the job at its next checkpoint (exit 130); the workspace keeps the work, and
``--resume`` continues it. A second Ctrl-C aborts at once.
"""

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Sequence
from types import FrameType
from typing import Final, TextIO

import neptune
from neptune.cli import exit_codes
from neptune.model.finding import IngestFinding
from neptune.model.package import ReceiptFinding
from neptune.sdk import (
    ConfigurationError,
    IgnorePolicy,
    IngestResult,
    Isolation,
    JobError,
    JobEvent,
    JobOptions,
    Neptune,
    NeptuneError,
    PublishIncompleteError,
    committed_result,
)
from neptune.store.package import RECEIPT

RESULT_FORMAT: Final = 1  # the result line's shape; bumped only by a breaking change

_DESCRIPTION: Final = """\
Ingest a run folder, or one file, into a Neptune package: every file is discovered, hashed,
probed and parsed by the adapter that claims it, and everything Neptune could not read is a
finding in the package's receipt, not a failure. No manifest is needed.

Nothing ignored is silent: version-control internals and OS metadata (.git/, .DS_Store, ...)
are skipped by default, plus --ignore patterns and the source's own .neptune-ignore; each
skipped entry is a finding naming its rule."""

_EPILOG: Final = """\
examples:
  neptune ingest runs/2026-09-30 --out packages/2026-09-30
  neptune ingest runs/2026-09-30 --dry-run
  neptune ingest runs/2026-09-30 --explain --json > plan.jsonl
  neptune ingest arm-cell/episode-7.mcap --out packages/episode-7 --json

exit codes:
{table}

docs/cli.md describes the JSON output and every option."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neptune", description="Neptune: robotics-native ingestion fabric."
    )
    parser.add_argument("--version", action="version", version=f"neptune {neptune.__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    ingest = commands.add_parser(
        "ingest",
        help="ingest a folder or a file into a package",
        description=_DESCRIPTION,
        epilog=_EPILOG.format(table=exit_codes.table()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ingest.add_argument("source", help="a folder or a file: a path, or a file: URI")
    ingest.add_argument(
        "-o",
        "--out",
        metavar="DEST",
        help="where to write the package; must not exist yet and must not be inside the source "
        "(required unless --dry-run)",
    )
    ingest.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="discover, fingerprint, probe and plan only: say what would be ingested, write no "
        "package",
    )
    ingest.add_argument(
        "--explain",
        action="store_true",
        help="a dry run that also explains the plan: every file, each adapter's verdict and why, "
        "the proposed sessions, the work left and what would be left out (implies --dry-run)",
    )
    ingest.add_argument(
        "--resume",
        action="store_true",
        help="continue earlier work on this source in the workspace (an interrupted ingest or a "
        "dry run); exit 7 if there is none. Without it, earlier work is reused anyway",
    )
    ingest.add_argument(
        "--attempts",
        type=_positive,
        default=JobOptions.attempts,
        metavar="N",
        help="tries per adapter call before its source is quarantined (default %(default)s)",
    )
    ingest.add_argument(
        "-w",
        "--workspace",
        metavar="DIR",
        help="the workspace (cache, checkpoints); default $NEPTUNE_HOME, else "
        "$XDG_CACHE_HOME/neptune, else ~/.cache/neptune",
    )
    ingest.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="PATTERN",
        help="leave entries matching PATTERN unread (gitignore subset; repeatable)",
    )
    ingest.add_argument(
        "--no-default-ignores",
        action="store_true",
        help="read version-control internals and OS metadata too",
    )
    ingest.add_argument(
        "--no-ignore-file",
        action="store_true",
        help="do not apply the source's .neptune-ignore",
    )
    ingest.add_argument(
        "--isolation",
        choices=[str(i) for i in Isolation],
        default=str(Isolation.SUBPROCESS),
        help="where adapter code runs: a confined child process per call (default), or this "
        "process, unconfined (trusted adapters only)",
    )
    ingest.add_argument(
        "--allow-degraded-sandbox",
        action="store_true",
        help="run on a host that cannot apply every sandbox guarantee; the receipt says which "
        "were lost",
    )
    ingest.add_argument("--job", metavar="NAME", help="name the job in the package's envelope")
    ingest.add_argument(
        "--json",
        action="store_true",
        help="JSON Lines on stdout: one line per job event, then one result line",
    )
    ingest.add_argument(
        "-v", "--verbose", action="store_true", help="one progress line per job event on stderr"
    )
    return parser


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not at least 1")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """The ``neptune`` console script. Ctrl-C asks the job to stop at its next checkpoint."""
    cancel = threading.Event()

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        sys.stderr.write("neptune: stopping at the next checkpoint (Ctrl-C again to abort)\n")

    previous = signal.signal(signal.SIGINT, interrupt)
    try:
        return run(argv, stdout=sys.stdout, stderr=sys.stderr, cancel=cancel)
    finally:
        signal.signal(signal.SIGINT, previous)


def run(
    argv: Sequence[str] | None,
    *,
    stdout: TextIO,
    stderr: TextIO,
    cancel: threading.Event | None = None,
) -> int:
    """Parse ``argv`` and run the command, writing to ``stdout`` and ``stderr``; the exit code."""
    parser = build_parser()
    try:  # argparse writes help, its version and usage errors to sys.stdout and sys.stderr
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, --version (0) or a usage error (2)
        return exc.code if isinstance(exc.code, int) else exit_codes.USAGE
    if args.command is None:
        parser.print_usage(stderr)
        return exit_codes.USAGE
    if args.explain and args.out is not None:
        return _usage(stderr, "--explain is a dry run and writes no package; drop --out")
    args.dry_run = args.dry_run or args.explain
    if args.dry_run and args.out is not None:
        return _usage(stderr, "--dry-run writes no package; drop --out")
    if not args.dry_run and args.out is None:
        return _usage(stderr, "--out DEST is required (or --dry-run)")
    return _Ingest(args, stdout, stderr, cancel).run()


def _usage(stderr: TextIO, message: str) -> int:
    stderr.write(f"neptune ingest: error: {message}\n")
    return exit_codes.USAGE


class _Ingest:
    """One ``neptune ingest`` invocation: the SDK call, its events, and what it prints."""

    def __init__(
        self,
        args: argparse.Namespace,
        stdout: TextIO,
        stderr: TextIO,
        cancel: threading.Event | None,
    ) -> None:
        self.args = args
        self.stdout = stdout
        self.stderr = stderr
        self.cancel = cancel

    def run(self) -> int:
        args = self.args
        try:
            client = Neptune(args.workspace, options=self._options())
            if args.dry_run:
                result = client.dry_run(
                    args.source, on_event=self._event, cancel=self.cancel, resume=args.resume
                )
            else:
                result = client.ingest(
                    args.source,
                    args.out,
                    on_event=self._event,
                    cancel=self.cancel,
                    resume=args.resume,
                )
            return self._done(result)
        except NeptuneError as exc:
            return self._failed(exc.code, str(exc), exc)
        except KeyboardInterrupt as exc:  # a second Ctrl-C: aborted, unless it already published
            if (published := committed_result(exc)) is not None:
                return self._done(published)
            return self._end("cancelled", exit_codes.CANCELLED, None, {}, None)
        except Exception as exc:  # a bug: say so, with the traceback, and a stable code
            traceback.print_exception(exc, file=self.stderr)
            return self._failed("internal", f"{type(exc).__name__}: {exc}", None)

    def _options(self) -> JobOptions:
        args = self.args
        try:
            ignore = IgnorePolicy(
                defaults=not args.no_default_ignores,
                patterns=tuple(args.ignore),
                file=not args.no_ignore_file,
            )
        except ValueError as exc:  # IgnoreError: a refused --ignore pattern
            raise ConfigurationError(f"--ignore: {exc}") from exc
        named = {"job": args.job} if args.job is not None else {}
        try:
            return JobOptions(
                attempts=args.attempts,
                isolation=Isolation(args.isolation),
                allow_degraded_sandbox=args.allow_degraded_sandbox,
                ignore=ignore,
                **named,
            )
        except JobError as exc:  # options that contradict each other, or an empty --job
            raise ConfigurationError(str(exc)) from exc

    # --- events --------------------------------------------------------------------------------

    def _event(self, event: JobEvent) -> None:
        if self.args.json:
            self._line({"event": event.to_json(), "type": "event"})
        elif self.args.verbose:
            details = _json(event.details) if event.details else ""
            self.stderr.write(f"neptune: {event.phase}: {event.kind} {details}".rstrip() + "\n")
            self.stderr.flush()

    def _line(self, value: dict[str, object]) -> None:
        self.stdout.write(_json(value) + "\n")
        self.stdout.flush()

    # --- the end -------------------------------------------------------------------------------

    def _done(self, result: IngestResult) -> int:
        if result.cancelled:
            summary = _summary(result, records={})
            return self._end("cancelled", exit_codes.CANCELLED, result, summary, None)
        records: dict[str, int] = {}
        findings: list[IngestFinding | ReceiptFinding] = list(result.findings)
        if result.committed:
            receipt = result.read_receipt()  # PackageInvalidError if it is not the job's
            records = dict(receipt.records)
            findings = list(receipt.findings)
        summary = _summary(result, records=records, findings=findings)
        explanation = result.explanation if self.args.explain else None
        if explanation is not None and self.args.json:  # its canonical bytes, before the result
            self.stdout.write(
                f'{{"explanation":{explanation.dumps().decode()},"type":"explanation"}}\n'
            )
        status = self._end(str(result.state), exit_codes.OK, result, summary, None)
        if explanation is not None and not self.args.json:
            self.stdout.write("\n" + explanation.render())
            self.stdout.flush()
        return status

    def _failed(self, code: str, message: str, error: NeptuneError | None) -> int:
        status = exit_codes.for_code(code) if error is not None else exit_codes.INTERNAL
        destination = error.destination if isinstance(error, PublishIncompleteError) else None
        summary: dict[str, object] = {}
        if destination is not None:  # whole at the destination, not yet durable
            summary["destination"] = _text(os.fspath(destination))
        return self._end("failed", status, None, summary, {"code": code, "message": message})

    def _end(
        self,
        state: str,
        status: int,
        result: IngestResult | None,
        summary: dict[str, object],
        error: dict[str, object] | None,
    ) -> int:
        out: dict[str, object] = {
            "cache": None,
            "destination": None,
            "error": error,
            "exit_code": status,
            "findings": None,
            "format": RESULT_FORMAT,
            "package": None,
            "receipt": None,
            "receipt_path": None,
            "records": None,
            "source": _text(self.args.source),
            "sources": None,
            "state": state,
            "type": "result",
            **summary,
        }
        if self.args.json:
            self._line(out)
        else:
            self._human(out)
        return status

    def _human(self, out: dict[str, object]) -> None:
        state, error = out["state"], out["error"]
        if isinstance(error, dict):
            self.stderr.write(f"neptune: {error['code']}: {error['message']}\n")
            if out["destination"] is not None:
                self.stderr.write(f"neptune: the package is at {out['destination']}\n")
            return
        if state == "cancelled":
            self.stderr.write(
                "neptune: cancelled at a checkpoint; the workspace keeps the work. "
                "Rerun with --resume to continue.\n"
            )
            return
        findings = out["findings"]
        counts = findings.get("by_severity") if isinstance(findings, dict) else None
        noted = "none"
        if isinstance(counts, dict) and counts:
            noted = ", ".join(f"{n} {severity}" for severity, n in sorted(counts.items()))
        if state == "committed":
            self.stdout.write(
                f"committed {out['destination']}\n"
                f"  package  {out['package']}\n"
                f"  receipt  {out['receipt_path']}\n"
                f"  sources  {out['sources']} ingested; findings: {noted}\n"
            )
        else:
            self.stdout.write(
                f"planned {out['source']}: {out['sources']} sources to ingest; nothing written\n"
                f"  findings: {noted}\n"
            )
        self.stdout.flush()


def _summary(
    result: IngestResult,
    *,
    records: dict[str, int],
    findings: Sequence[IngestFinding | ReceiptFinding] | None = None,
) -> dict[str, object]:
    """The result line's fields an outcome fills in."""
    listed = result.findings if findings is None else findings
    severities = Counter(str(f.severity) for f in listed)
    codes = Counter(f.code for f in listed)
    destination = result.destination
    summary: dict[str, object] = {
        "cache": {key: dict(value) for key, value in sorted(result.cache.totals().items())},
        "findings": {
            "by_code": dict(sorted(codes.items())),
            "by_severity": dict(sorted(severities.items())),
            "total": len(listed),
        },
        "sources": len(result.ingested) if result.committed else len(result.cache.sources),
    }
    if destination is not None:
        summary["destination"] = _text(os.fspath(destination))
    if result.committed and destination is not None:
        summary["package"] = result.package
        summary["receipt"] = result.receipt
        summary["receipt_path"] = _text(os.fspath(destination / RECEIPT))
        summary["records"] = dict(sorted(records.items()))
    return summary


def _json(value: object) -> str:
    """One line of JSON: sorted keys, no whitespace, so the same value is the same bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value: str) -> str:
    """A command-line or path string as printable text: undecodable bytes as ``\\x..`` escapes."""
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
