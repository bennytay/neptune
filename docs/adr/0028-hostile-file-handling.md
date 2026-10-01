# 0028 — Hostile file handling: walk findings, archive limits, source verification, scratch space

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-75 (sub-issue of MVL-10)
- Extends: ADR 0009 §5 and ADR 0010 §2 (walk policy), ADR 0017 §9 (findings), ADR 0024 §2
  (`read_pieces`), ADR 0026 (workspace; MVL-73, in review)

## Context

Non-negotiable 9 says all input is hostile, and ADRs 0009/0010 already make the local walk safe:
locations cannot spell `..`, directories are opened with `O_NOFOLLOW` component by component,
symlinks are recorded and never followed, special files are never opened. What the walk saw and
did not read was still only a `SkippedEntry` or `SymlinkEntry` in memory: not a record, so a
receipt could not say it. Nothing inspected archives, whose members declare sizes and names of
their own. A source that changed after it was hashed raised an exception. And nothing said where
Neptune may write while handling untrusted bytes. MVL-75 asks for each of these as findings with
provenance, with fixtures that cannot read outside the root or exhaust disk or memory.

## Decision

1. **Discovery is a producer.** Its transform is `neptune.discovery` 1.0.0 with config `{}`
   (`neptune.discovery.policy.DISCOVERY_TRANSFORM`). `scan()` returns it with its findings, in walk
   order, so the store keeps both. A policy change is a new version and a new lineage.

   | Code | Severity · category | Subject | When |
   |---|---|---|---|
   | `neptune.discovery.symlink_not_followed` | info · skipped | the link | every symlink the walk sees, and an `open()` refused for one |
   | `neptune.discovery.special_file` | info · skipped | the entry | a FIFO, socket or device |
   | `neptune.discovery.vanished` | warning · skipped | the entry | gone between the walk and the open |
   | `neptune.discovery.unreadable` | error · skipped | the entry | a directory or file the walk could not read |
   | `neptune.discovery.size_changed` | warning · inconsistent | the file | the digest read a different number of bytes than the walk's `stat` |
   | `neptune.discovery.truncated` | error · corrupt | the missing range | `verify_artifact`: fewer bytes than the artifact declares |
   | `neptune.discovery.grown` | warning · inconsistent | the extra range | more bytes than declared; the declared prefix is intact |
   | `neptune.discovery.chunk_changed` | error · inconsistent | the chunk run | a chunk whose hash differs; consecutive chunks are one finding |
   | `neptune.discovery.short_read` | error · corrupt | the unserved range | `short_read_finding`: a reader served an adapter no bytes inside the declared size |

   Symlinks stay **never followed**, inside or outside the root. The issue's "followed only inside
   the root" is met by not following at all: the bytes behind an in-root link are reached through
   their real path, and following would add loop handling and a TOCTOU window for no evidence gain
   (ADR 0010). The finding records the target exactly as stored (it is the link's content and so
   evidence, not host state), whether it is absolute, and whether it lexically stays inside the root.
   Lexical means `..` is collapsed and nothing is resolved on disk. The root's own skip has no
   location to name and is not a finding; a root the walk cannot read is a job-level condition the
   runtime (MVL-6) reports.
2. **Archive inspection within limits** (`neptune.discovery.archive`). Producer `neptune.archive`
   1.0.0 with the limits as its config, so a different limit is a different lineage. Zip, tar and
   gzip/bzip2/xz streams (a tar inside is one archive; anything else is one unnamed member) are
   inspected without extracting anything.

   | Limit | Default | Checked |
   |---|---|---|
   | `max_members` | 10,000 per archive | zip: from the end-of-central-directory record before the directory is parsed, then the parsed count; tar: as headers are read |
   | `max_member_size` | 8 GiB | declared size before inflating; actual bytes while inflating |
   | `max_total_size` | 64 GiB across the whole tree, nested archives included | declared sum (zip), declared per member (tar), actual bytes |
   | `max_compression_ratio` | 100:1 | zip: declared per member and whole; tar: inflated position against the compressed size, from the first header; single stream: actual bytes |
   | `max_depth` | 3 (a zip holding a tar.gz holding a zip) | by sniffing each member's first 512 bytes |

   Rules that make the limits hold rather than merely report:
   - Declared sizes are checked before a byte is inflated; actual bytes are counted while
     inflating, because declared sizes can lie. `zipfile` itself stops at a member's declared size.
   - A member of a compressed tar cannot be skipped without inflating it, so any limit stops the
     inspection of that tar (`complete = False`). A zip skips the member and goes on.
   - Compressed tars are inflated by this module's bounded reader, not by `tarfile`'s stream layer,
     which inflates each 10 KiB input block whole (bzip2 turns such a block into gigabytes).
   - Pax and GNU long-name headers above 1 MiB are refused (`header_too_large`): `tarfile` reads
     them whole into memory. A zip symlink's target is member data read whole to record it, so
     it is capped the same way; a tar link name lives in its header and is already bounded.
   - Nested archives are read through a spool (memory to 1 MiB, then a file in scratch space) and
     inspected recursively against the same total budget. The spool is deleted when the level ends.
   - Member names are recorded exactly as declared (zip: before the NUL `zipfile` cuts at). A name
     that is empty, absolute, holds NUL or a `..` component is `member_path_unsafe` and the member
     is not read. `.` and empty components are tolerated; backslashes are characters.
   - Symlink and hard-link members are `member_link` (target recorded, never followed); FIFOs and
     devices `member_special`; encrypted members `member_encrypted`; unknown compression methods
     `member_unsupported`; defects `truncated`, `corrupt`, `member_truncated`, `member_corrupt`;
     unrecognised bytes `unrecognised`. Every limit is `error · limit`.
   - Subjects are `EvidenceRef`s: the member's byte range in the archive, under the outer member's
     range when nested, and under the compressed stream's range for a compressed tar (ADR 0016).

   One inspection inflates every member once. An archive adapter (M4+) may fuse this check with its
   own read; today the inspector is the policy and is called by whoever opens an archive.
3. **Verification against the artifact** (`neptune.discovery.verify.verify_artifact`). A re-read
   source is compared chunk by chunk with its `SourceArtifact`; the differences are the three
   findings in the table above, citing the affected range of the declared artifact. One chunk digest
   is held at a time. `LocalReader` (ADR 0026) keeps raising `SourceChangedError` to stop an
   adapter mid-read; the runtime turns that into these findings by calling `verify_artifact`.
   Inside an adapter, `neptune.adapters.contract.read_pieces` raises
   `ShortReadError(source, offset, length)` when a reader serves no bytes inside the size it
   declares, instead of a bare `ValueError`. It is not the adapter's finding (its codes are
   declared per adapter, and the fault is the source's), so the adapter lets it propagate and
   the runtime records `short_read_finding(source, offset, length)` for the unserved range,
   goes on to the next source, and calls `verify_artifact` for the full account.
4. **Scratch space** (`neptune.discovery.scratch`). The caller names a **private root**; the
   workspace's is `<workspace>/scratch` (wired when MVL-73 lands). It is created `0700`, must be a
   real directory owned by this user (tightened to `0700` if looser), and must not overlap the
   ingest root in either direction. `scratch_space(private_root)` yields a fresh `0700` directory
   holding an `flock`ed lock file and removes it on exit, success or crash: that is "cleaned on
   commit". `clear_scratch(private_root)` runs on resume and removes every entry whose lock nobody
   holds, skipping a live process's; symlinks are unlinked, never followed. Scratch names are
   random and never reach a record.

## Alternatives considered

- **Following symlinks that resolve inside the root.** Rejected again for ADR 0010's reasons; the
  lexical classification gives a receipt the fact without the risk.
- **A `symlink` record kind instead of a finding.** It would decide the record shape MVL-13 owns.
  A finding is a record already, carries provenance, and can be superseded by a kind later.
- **Extract to scratch, then measure.** Simple, and exactly how archive bombs work. Counting while
  inflating costs nothing extra and never writes a member to disk.
- **`tarfile`'s own stream decompression.** One call, but its `_Stream` inflates whole input blocks;
  with bzip2 one 10 KiB block can be gigabytes. The bounded readers (`gzip.GzipFile`, `bz2.BZ2File`,
  `lzma.LZMAFile`) honour `read(n)`.
- **Trusting the end-of-central-directory count.** It is used only to refuse early; the parsed
  directory is checked again, because the record can lie low.
- **Flagging backslashes in member names.** On POSIX they are characters; the names are recorded,
  and a consumer extracting on Windows applies its own rules.
- **PID-named scratch directories.** PIDs recycle and processes on other hosts share a workspace
  over a network mount; an `flock` held for the life of the work is exact on one host and is the
  limit of what a local-first store needs (ADR 0026).
- **`tempfile.gettempdir()` for scratch.** A shared, world-writable directory is the classic
  symlink-planting target, and it may sit on a small tmpfs. A private root under the workspace is
  neither.

## Consequences

- Every symlink, special file and unreadable entry now appears in receipts with discovery's
  transform; corpora with many links gain many `info` findings, which is what `info` is for.
- `ScanResult` gains `findings` and `transform`; `LocalSource` gains `root`. Existing callers are
  unaffected.
- `inspect_archive` is a full pass over an archive. Until an archive adapter fuses it with its read,
  an archive is inflated once to inspect and once to ingest.
- Memory during inspection is bounded by one 1 MiB block plus the zip central directory, which is
  itself bounded by the archive on disk; the hostile suite peaks under 2 MiB (tracemalloc).
- The workspace wiring (`Workspace.scratch_root`, `clear_scratch` at start-up) is a one-line
  follow-up after ADR 0026's PR; MVL-6 catches `ShortReadError` and `SourceChangedError` per
  source and records `short_read_finding` and `verify_artifact`'s findings.
- MVL-8's probe engine (ADR 0027, in review) lists container members under its own bounded
  policy to select adapters; `inspect_archive` is the ingest-time limit. Whether the two fuse
  into one pass is decided once both are merged.
- Revisit if a format needs its own limits (video containers, bags of bags), if `tarfile` or
  `zipfile` change the private hooks relied on (`TarInfo._proc_member`), or if inspection time on
  compressed corpora is measured to dominate ingest.
