# 0010 — Lossless local locations: raw names, symlinks, absences

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-59
- Amends: ADR 0009 §3, §4, §5

## Context

ADR 0009's v0 walk dropped three kinds of evidence the filesystem shows plainly. It skipped files with
non-UTF-8 names. It skipped symlinks without recording them. It never recorded a location whose file
disappeared. On review of PR #4 the maintainer chose maximum accuracy. Robotics corpora contain all three:
Latin-1 filenames from old loggers, `latest -> run_2026…` links, and runs deleted or moved between syncs.

## Decision

1. **Non-UTF-8 names are ingested.** The location is `RawLocalPath`, serialised as
   `{"kind": "local_raw", "path_hex": "<lowercase hex of the exact bytes>"}`. It has the same component rules
   as `LocalPath`. A path that decodes as UTF-8 must be a `LocalPath`, so each location has exactly one
   representation (`local_location(raw)`). `LocalSource.open` accepts both. A directory with a non-UTF-8 name
   is walked, and everything below it is a `RawLocalPath`.
2. **Symlinks are recorded, never followed.** The walk yields `SymlinkEntry(location, target)`, where `target`
   is the link's contents exactly as stored (`readlink`), unresolved. Reading a link's contents does not
   traverse it. Resolving a link and deciding what it means (an alias, "latest run") is interpretation. It
   belongs to grouping (MVL-13). `SkipReason.UNDECODABLE_NAME` is removed. `SkipReason.SYMLINK` remains for
   `open()` refusals.
3. **Absence is explicit.** `SourceAbsence(id, location, supersedes)` joins the revision chain. It
   always supersedes exactly one `SourceRevision`. Its id is a record id with kind `source_absence` over
   `(location, supersedes)`. When bytes reappear, the new revision supersedes the absence. The package gains a
   third source table beside artifacts and revisions.
4. **Absence only with coverage.** `discovery.scan()` walks, digests, observes and then reconciles. A local
   location whose latest entry is a revision and which this scan did not yield is marked absent unless the
   scan was blind to it. The scan is blind when:
   - the location or an ancestor was skipped as `unreadable` or `missing`. This includes files that
     changed between walk and open, and the root itself;
   - an ancestor is now a symlink. It is not followed, so what lies behind it is unknown.

   A symlink, FIFO or directory sitting *at* the location counts as coverage: the regular file is gone.
   External locations are never reconciled by a local scan.
5. **Walk order is byte order** of sibling names. This is identical to code-point order for UTF-8 names and
   well defined for all others.

## Alternatives considered

- **Percent-encoding or `surrogateescape` strings for raw names.** One string field would serve every name.
  But percent-encoding changes legitimate names containing `%`. Lone surrogates are forbidden in canonical
  JSON (ADR 0002). A separate variant keeps UTF-8 names byte-for-byte as they were.
- **Following symlinks that resolve inside the root.** The same bytes are already reached through the real
  path, so following adds duplicates, loop handling and a TOCTOU window. Recording the target keeps the
  information and gives none of those up.
- **Marking every unseen location absent.** It is simpler, but an unreadable directory or a transient error
  would then assert that files were deleted. That is a false fact, which violates non-negotiable 3.
- **An absence flag on `SourceRevision`.** A revision without a content id would need an optional field. `None`
  in a canonical record is a bug (ADR 0004), and a second type keeps both shapes total.

## Consequences

- Every regular file a local walk can see is ingested. Every symlink is recorded. Every disappearance the scan
  could see is recorded, and nothing is asserted about what it could not see.
- Consumers must handle three location kinds and two chain-entry kinds.
- The store (MVL-5/MVL-16) persists `SourceAbsence` alongside artifacts and revisions.
- `SymlinkEntry` is a walk result, not yet a canonical record. MVL-13 decides its record shape when it first
  consumes links.
