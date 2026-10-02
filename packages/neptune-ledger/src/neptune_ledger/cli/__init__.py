"""``ledger``: register and verify packages from the command line (MVL-90).

A thin front end over ``PostgresCatalog`` until ``access/`` (MVL-99) lands. Every command prints
its response as canonical JSON on stdout and exits 0 when the call succeeded (``registered``,
``already_registered``, ``intact``, every source ``present``), 1 when it answered with a refusal
or a problem, and 2 for a usage error or an unreachable store.

Configuration, by flag or environment:

- ``--dsn`` / ``NEPTUNE_LEDGER_DSN``: the catalog database (libpq connection string or URI).
- ``--tenant`` / ``NEPTUNE_LEDGER_TENANT``: the tenant whose schema is used (ADR 0002 §2).
- ``--package-root`` (repeatable) / ``NEPTUNE_LEDGER_PACKAGE_ROOTS`` (``os.pathsep``-separated):
  the tenant's package roots (ADR 0006 §3). ``register`` refuses to run without one.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Final

import psycopg

from neptune.identity import canonical_json
from neptune_ledger.api import CatalogUnavailable, codec
from neptune_ledger.catalog.migrate import MigrationError, apply_migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.catalog.sources import LocalSourceStore, SourceStore

OK: Final = 0
PROBLEM: Final = 1
USAGE: Final = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("NEPTUNE_LEDGER_DSN"))
    parser.add_argument("--tenant", default=os.environ.get("NEPTUNE_LEDGER_TENANT"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="create or update the tenant's catalog schema")
    register = commands.add_parser("register", help="verify a package and catalogue it")
    register.add_argument("root", help="the package directory")
    register.add_argument(
        "--package-root",
        action="append",
        default=None,
        help="a package root of the tenant (repeatable); default $NEPTUNE_LEDGER_PACKAGE_ROOTS",
    )
    verify = commands.add_parser("verify", help="re-hash a registered package at its stored root")
    verify.add_argument("package_id")
    verify.add_argument("--as-of", type=int, default=None, help="a catalog point (tx_seq)")
    verify.add_argument(
        "--source-root",
        action="append",
        default=[],
        help="also re-hash referenced sources under this directory (repeatable)",
    )
    return parser


def _stores(roots: Sequence[str]) -> list[SourceStore]:
    stores: list[SourceStore] = []
    for root in roots:
        if "://" in root:
            raise ValueError(
                f"{root}: this build reads local source roots; an S3-compatible root needs a"
                " SourceStore backed by an S3 client (Ledger ADR 0007)"
            )
        stores.append(LocalSourceStore(root))
    return stores


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.dsn or not args.tenant:
        _error("set --dsn and --tenant (or NEPTUNE_LEDGER_DSN, NEPTUNE_LEDGER_TENANT)")
        return USAGE
    try:
        if args.command == "migrate":
            with psycopg.connect(args.dsn, autocommit=True) as conn:
                applied = apply_migrations(conn, args.tenant)
            sys.stdout.write(canonical_json.dumps({"applied": applied}).decode() + "\n")
            return OK
        if args.command == "register":
            configured = os.environ.get("NEPTUNE_LEDGER_PACKAGE_ROOTS", "")
            roots = args.package_root or [r for r in configured.split(os.pathsep) if r]
            if not roots:
                _error("configure the tenant's package roots (--package-root)")
                return USAGE
            with PostgresCatalog(args.dsn, args.tenant, package_roots=roots) as catalog:
                registration = catalog.register(args.root)
            _print(codec.dumps(registration))
            return OK if registration.outcome != "refused" else PROBLEM
        stores = _stores(args.source_root)
        with PostgresCatalog(args.dsn, args.tenant, package_roots=None) as catalog:
            report = catalog.verify(args.package_id, as_of=args.as_of)
            _print(codec.dumps(report))
            good = report.verdict == "intact"
            if stores and good:
                sources = catalog.verify_sources(args.package_id, stores, as_of=args.as_of)
                _print(canonical_json.dumps(sources.to_json()))
                good = all(check.state == "present" for check in sources.checks)
        return OK if good else PROBLEM
    except (CatalogUnavailable, MigrationError, psycopg.Error, ValueError) as exc:
        _error(str(exc))
        return USAGE


def _print(document: bytes) -> None:
    sys.stdout.write(document.decode("utf-8") + "\n")


def _error(message: str) -> None:
    sys.stderr.write(f"ledger: {message}\n")
