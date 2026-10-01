# 0007 — Registration: the indexing boundary, local package roots, and on-request source checks behind a read-only store

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-90

## Context

MVL-90 implements `register` and `verify` (ADR 0004 §4, ADR 0006 §1–§5). Four questions are left
open by ADRs 0002–0006:

- **Which rows does registration write?** MVL-91 owns "index every record kind", but ADR 0004 §4
  requires the log, package and every index row in one transaction, and ADR 0005 §2's
  `conflicting_id` for records needs every record's `body_digest` at registration.
- **Where can roots live?** The issue asks for local directories and S3-compatible storage.
  ADR 0006 §1 and §3 define package roots on resolved local paths walked without following links.
- **How are referenced sources re-hashed?** The issue asks `verify` to re-hash referenced sources on
  request and report moved, changed or absent ones as findings. catalog-api 1.1.0 has no finding
  code or verdict for a source location: `file_missing` and `file_digest_mismatch` name
  package-relative files, and `damaged` means the package's own bytes changed. A source that moved
  is not a damaged package (the same reasoning as ADR 0006 §2). Contract types may not change in
  this issue.
- **What does "identical table dumps" mean** when `tx_time` is the host clock (ADR 0002 §4)?

## Decision

1. **Indexing boundary.** Registration writes every row migrations 0001 and 0002 define: the
   log, `package`, `source`, `package_source`, `source_location`, `location_absence`,
   `transform`, `transform_upstream`, `clock`, `record` (all ADR 0002 §5 columns plus
   `body_digest`) and `record_logical_id`. The mapping is the pure function
   `neptune_ledger.catalog.index.package_rows(package_id, manifest, lines)`, over the record lines
   registration hashed. A test checks that it writes exactly the rows of the walkthrough's ADR
   0002 §5 harness. MVL-91 owns everything beyond 0001/0002: kind-specific projection columns,
   generated migrations, any stored body, and pointer lists beyond `Ambiguous`. It extends
   `package_rows` and writes inside the same transaction.
2. **Package roots are local directories.** `register(package_root)` and `verify` work on a local
   directory, opened and walked with `O_NOFOLLOW` descriptors relative to the root's descriptor
   (`catalog.check`). A path swapped for a link after the walk is never followed. Large files
   (series, blobs) go to the compiler's checks as `/proc/self/fd/N/<path>` of the checked root.
   `verify` treats a stored root that no longer resolves to itself as `unreachable`: a link was
   put on the path.
3. **Referenced sources are checked by `PostgresCatalog.verify_sources(package_id, stores, *,
   as_of)`, outside catalog-api.** For each referenced source, it checks each location the package
   currently states (ADR 0006 §5). It looks in the stores in order and reports `present`,
   `changed` (another size or digest), `absent`, `moved` or `unsupported` (an external object).
   `moved` means absent at this location but intact at a current location that another package,
   registered by `as_of`, states for the same content id. Request problems use catalog-api codes
   (`unknown_package`, `as_of_out_of_range`, `invalid_request`). It only reads and records
   nothing, so the catalog stays a function of packages and the log. The contract `verify`
   checks only the package's own bytes. The missing finding codes are a catalog-api gap. A minor
   version that adds source codes, or a `sources` option on `verify`, would fold this in.
4. **Source roots sit behind a read-only `SourceStore`:** `describe()` and `open(path) -> BinaryIO
   | None`, with `path` the location's root-relative bytes. `LocalSourceStore` serves a directory
   and never reads outside it once paths are resolved. An S3-compatible store is the same two
   methods over `GetObject(prefix + path)`. The Ledger ships no S3 client. The interface is
   tested with an in-memory object store, and the CLI refuses an `s3://` root with a message
   naming this ADR.
5. **Determinism.** Registering the same packages in the same order into two empty Ledgers gives
   identical rows in every table, except `tx_time` and `tx_clock.last_time`, which are host clock
   readings (ADR 0002 §4). The registration log replays them on a rebuild.
6. **Verify details.** A manifest that no longer hashes to the id is `manifest_digest_mismatch`.
   No file is then compared, because the listing is untrusted. An `as_of` below 1 is
   `invalid_request`. A catalog with no registration has no point: `as_of` is `NotCovered`.
7. **CLI.** `ledger migrate | register <root> | verify <id> [--as-of N] [--source-root DIR]`
   prints canonical JSON. It exits 0 on success, 1 on a refusal or a problem, and 2 on a usage
   error or an unreachable store. `--dsn`, `--tenant` and `--package-root` also come from
   `NEPTUNE_LEDGER_DSN`, `NEPTUNE_LEDGER_TENANT` and `NEPTUNE_LEDGER_PACKAGE_ROOTS`. `register`
   refuses to run without package roots. It calls `PostgresCatalog` directly until `access/`
   (MVL-99) exists.

## Alternatives considered

- **Leave `record` rows to MVL-91.** Rejected. ADR 0004 §4 needs one transaction, and ADR 0005
  §2's record conflicts need every body digest before anything is written. A second pass would
  make a registration visible half-indexed.
- **Report sources inside `VerifyReport` under `file_missing` / `file_digest_mismatch`, or as
  `damaged`.** Rejected. That changes what the codes mean, and it calls a moved source tampering
  (ADR 0006 §2's reasoning). Contract changes need a catalog-api version.
- **Record found locations in the catalog.** Rejected. A sighting is a registration fact no
  package states. It would break the rebuild guarantee (ADR 0002 §4), as a second root would
  (ADR 0006 §2).
- **Add boto3 or obstore now.** Rejected. That is a heavy dependency with no deployment that needs
  it yet. The two-method interface is where it plugs in.
- **Packages in S3.** Rejected for now. ADR 0006 §1 and §3 are defined on local resolved paths
  and link-free walks. An object store needs its own containment rule.
- **Inject a fixed clock to make `tx_time` equal.** Rejected. `next_tx()` reads the host clock by
  ADR 0002 §4. Replay already reproduces logged times.

## Consequences

- MVL-91 starts from `package_rows` and the registration transaction, not from a blank.
- The catalog-api owner should add source finding codes in a minor version. Until then,
  `verify_sources` is a Ledger-local surface, used by the CLI and by tests.
- A deployment with S3 sources adds one `SourceStore` implementation and a dependency.
- Revisit 2 if packages must be registered straight from object storage.
