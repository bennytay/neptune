"""``ledger``: register, verify, rebuild and dump the catalog from the command line.

A thin front end over ``PostgresCatalog`` and ``catalog.rebuild`` until ``access/`` (MVL-99)
lands. Every command but ``dump`` prints its response as canonical JSON on stdout and exits 0 when
the call succeeded (``registered``, ``already_registered``, ``intact``, every source ``present``,
``rebuilt``), 1 when it answered with a refusal or a problem, and 2 for a usage error, an
unreachable store or a manifest that cannot be read or written.

- ``migrate``: create or update the tenant's catalog schema.
- ``register ROOT``, ``verify ID``: MVL-90.
- ``manifest``: write the registry manifest (to ``--manifest``, else stdout; ADR 0012 §1).
- ``dump``: the catalog as canonical JSON Lines (to ``--out``, else stdout; ADR 0012 §3).
- ``rebuild --from MANIFEST``: drop the tenant's catalog and replay the manifest's packages into
  a fresh one, in one transaction (ADR 0012 §2). ``--prune`` allows dropping packages the
  manifest does not list.

Configuration, by flag or environment:

- ``--dsn`` / ``NEPTUNE_LEDGER_DSN``: the catalog database (libpq connection string or URI).
- ``--tenant`` / ``NEPTUNE_LEDGER_TENANT``: the tenant whose schema is used (ADR 0002 §2).
- ``--package-root`` (repeatable) / ``NEPTUNE_LEDGER_PACKAGE_ROOTS`` (``os.pathsep``-separated):
  the tenant's package roots (ADR 0006 §3). ``register`` and ``rebuild`` refuse to run without one.
- ``--manifest`` / ``NEPTUNE_LEDGER_MANIFEST``: the registry manifest, rewritten after every
  registration that adds a package and after a rebuild.
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import psycopg

from neptune.identity import canonical_json
from neptune_ledger.api import CatalogUnavailable, codec
from neptune_ledger.catalog.manifest import Manifest, ManifestError, read_manifest, write_manifest
from neptune_ledger.catalog.migrate import MigrationError, apply_migrations
from neptune_ledger.catalog.rebuild import dump, rebuild
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.catalog.sources import LocalSourceStore, SourceStore

OK: Final = 0
PROBLEM: Final = 1
USAGE: Final = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("NEPTUNE_LEDGER_DSN"))
    parser.add_argument("--tenant", default=os.environ.get("NEPTUNE_LEDGER_TENANT"))
    parser.add_argument(
        "--manifest",
        default=os.environ.get("NEPTUNE_LEDGER_MANIFEST"),
        help="the registry manifest to keep current; default $NEPTUNE_LEDGER_MANIFEST",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="create or update the tenant's catalog schema")
    register = commands.add_parser("register", help="verify a package and catalogue it")
    register.add_argument("root", help="the package directory")
    _package_roots(register)
    commands.add_parser("manifest", help="write the registry manifest from the registration log")
    dumped = commands.add_parser("dump", help="the catalog as canonical, sorted JSON Lines")
    dumped.add_argument("--out", help="write here instead of stdout")
    rebuilt = commands.add_parser(
        "rebuild", help="drop the tenant's catalog and re-register the manifest's packages"
    )
    rebuilt.add_argument("--from", dest="source", required=True, help="the registry manifest")
    rebuilt.add_argument(
        "--prune", action="store_true", help="allow dropping packages the manifest does not list"
    )
    _package_roots(rebuilt)
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


def _package_roots(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--package-root",
        action="append",
        default=None,
        help="a package root of the tenant (repeatable); default $NEPTUNE_LEDGER_PACKAGE_ROOTS",
    )


def _roots(args: argparse.Namespace) -> list[str]:
    configured = os.environ.get("NEPTUNE_LEDGER_PACKAGE_ROOTS", "")
    return list(args.package_root or [r for r in configured.split(os.pathsep) if r])


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
            roots = _roots(args)
            if not roots:
                _error("configure the tenant's package roots (--package-root)")
                return USAGE
            with PostgresCatalog(
                args.dsn, args.tenant, package_roots=roots, manifest=args.manifest
            ) as catalog:
                registration = catalog.register(args.root)
            _print(codec.dumps(registration))
            return OK if registration.outcome != "refused" else PROBLEM
        if args.command == "manifest":
            with psycopg.connect(args.dsn, autocommit=True) as conn:
                if args.manifest:
                    write_manifest(conn, args.tenant, args.manifest)
                else:
                    sys.stdout.write(read_manifest(conn, args.tenant).to_bytes().decode())
            return OK
        if args.command == "dump":
            if args.out:
                with Path(args.out).open("wb") as out:
                    dump(args.dsn, args.tenant, out)
            else:
                dump(args.dsn, args.tenant, sys.stdout.buffer)
                sys.stdout.flush()
            return OK
        if args.command == "rebuild":
            return _rebuild(args)
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
    except (CatalogUnavailable, MigrationError, psycopg.Error, ValueError, OSError) as exc:
        _error(str(exc))
        return USAGE


def _rebuild(args: argparse.Namespace) -> int:
    """``ledger rebuild``: the tenant must be the manifest's, and package roots are required."""
    roots = _roots(args)
    if not roots:
        _error("configure the tenant's package roots (--package-root)")
        return USAGE
    manifest = Manifest.from_bytes(Path(args.source).read_bytes())
    if manifest.tenant_id != args.tenant:
        raise ManifestError(
            f"{args.source} is tenant {manifest.tenant_id!r}'s manifest, not {args.tenant!r}'s"
        )
    report = rebuild(args.dsn, manifest, package_roots=roots, prune=args.prune)
    if report.outcome == "rebuilt" and args.manifest:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            write_manifest(conn, args.tenant, args.manifest)
    _print(canonical_json.dumps(report.to_json()))
    return OK if report.outcome == "rebuilt" else PROBLEM


def _print(document: bytes) -> None:
    sys.stdout.write(document.decode("utf-8") + "\n")


def _error(message: str) -> None:
    sys.stderr.write(f"ledger: {message}\n")
