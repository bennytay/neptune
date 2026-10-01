# 0029 — Hostile file handling: walk findings, archive limits, source verification, scratch space

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-75 (sub-issue of MVL-10)
- Extends: ADR 0009 §5 and ADR 0010 §2 (walk policy), ADR 0017 §9 (findings), ADR 0024 §2
  (`read_pieces`), ADR 0026 (workspace)

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
   Lexical means `..` is collapsed against the link's own directory and nothing is resolved on disk.
   An absolute target is never inside: whether it lands back in the root depends on where the root
   is mounted, which is host state, and the same tree must give the same finding bytes wherever it
   is mounted. The root's own skip has no location to name and is not a finding; a root the walk
   cannot read is a job-level condition the runtime (MVL-6) reports.
2. **Archive inspection within limits** (`neptune.discovery.archive`). Producer `neptune.archive`
   1.0.0 with the limits as its config, so a different limit is a different lineage. Zip, tar and
   gzip/bzip2/xz streams (a tar inside is one archive; anything else is one unnamed member) are
   inspected without extracting anything.

   | Limit | Default | Checked |
   |---|---|---|
   | `max_members` | 10,000 per archive | zip: the end-of-central-directory record's count, then the directory's entries walked unheld, both before `zipfile` parses it; tar: as headers are read |
   | `max_member_size` | 8 GiB | declared (a sparse tar member's expanded) size before inflating; actual bytes while inflating |
   | `max_total_size` | 64 GiB across the whole tree, nested archives included | declared sum (zip), declared per member (tar), actual bytes |
   | `max_compression_ratio` | 100:1 | zip: declared per member and whole; tar: each member's declared size against the bytes it stores (sparse holes are zeros tarfile never reads), plain or compressed, and a compressed tar's inflated position against its compressed size; single stream: actual bytes |
   | `max_depth` | 3 (a zip holding a tar.gz holding a zip) | by sniffing each member's first 512 bytes |

   Rules that make the limits hold rather than merely report:
   - Declared sizes are checked before a byte is inflated; actual bytes are counted while
     inflating, because declared sizes can lie. `zipfile` itself stops at a member's declared size.
   - A member of a compressed tar cannot be skipped without inflating it, so any limit stops the
     inspection of that tar (`complete = False`). A zip skips the member and goes on.
   - Compressed tars are inflated by this module's bounded reader, not by `tarfile`'s stream layer,
     which inflates each 10 KiB input block whole (bzip2 turns such a block into gigabytes).
   - Tar headers cost at most 1 MiB per member, however they are split (`header_too_large`).
     `tarfile` reads each extended header whole and recurses once per chained long name or pax
     header, holding every link until the member's last header parses. So one extended header
     above 1 MiB is refused unread; the stream `tarfile` reads is metered, serving at most 1 MiB
     (plus its 10 KiB read-ahead) past where a member's headers start; at most 32 extended headers
     chain before one member, a fixed cap so the finding never depends on the stack depth at which
     a `RecursionError` would strike; and pax global headers, kept for the archive's life, are
     capped at 1 MiB in total. `tarfile`'s own member list is not kept. A zip symlink's target is
     member data read whole to record it, so it is capped the same way; a tar link name lives in
     its header and is already bounded.
   - A zip's central directory is bounded before `zipfile` reads it whole and builds every entry:
     it is located with `zipfile`'s own end-record reader (zip64 included), must fit in the bytes
     before its end record (`corrupt`), may average at most 1 KiB per allowed member, at least
     1 MiB (`header_too_large`), and its entries are walked without being held, stopping one past
     `max_members` (`member_count_exceeded`), because the end record's count can lie low.
   - A tar member's declared size is its expanded size. A sparse member stores only its chunks and
     `tarfile` fills the holes with zeros, so the member, total and ratio limits hold the expanded
     size against the stored chunks before the member is read or skipped. A compressed tar's
     inflated position counts as far as the header's own size field reaches, which `tarfile`
     inflates to skip even where the sparse map reads less.
   - A zip member is a regular file unless its attributes positively say otherwise: the high 16
     bits are a mode only from a Unix or OS X host and only when they hold a file type (CPython's
     `writestr` stores permissions alone); otherwise a trailing `/` or the MS-DOS directory
     attribute makes a directory.
   - Nested archives are read through a spool (memory to 1 MiB, then a file in scratch space) and
     inspected recursively against the same total budget. The spool is deleted when the level ends.
   - Member names are recorded exactly as declared (zip: before the NUL `zipfile` cuts at). A name
     that is empty, absolute, holds NUL or a `..` component is `member_path_unsafe` and the member
     is not read, whatever its kind (a link named `../x` is unsafe before it is a link). `.` and empty components are tolerated; backslashes are characters.
   - Symlink and hard-link members are `member_link` (target recorded, never followed); FIFOs and
     devices `member_special`; encrypted members `member_encrypted`; unknown compression methods
     `member_unsupported`; defects `truncated`, `corrupt`, `member_truncated`, `member_corrupt`;
     unrecognised bytes `unrecognised`. Every limit is `error · limit`.
   - Whatever `zipfile`, `tarfile` or a decompressor raises while opening, listing or reading is the
     archive's or the member's finding: they raise far beyond their documented errors on hostile
     headers (`IndexError` and `ValueError` from sparse maps, `NotImplementedError` from a zip
     version). Details record a fixed `error` code mapped from the exception's class (`bad_zip`,
     `bad_tar`, `bad_deflate`, `end_of_data`, ...), never the library's message, which varies
     across Python and zlib versions.
   - Subjects are `EvidenceRef`s: the member's byte range in the archive, under the outer member's
     range when nested, and under the compressed stream's range for a compressed tar (ADR 0016).
     Ranges are cut to the archive, since a header may declare any offset or size.

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
4. **Scratch space** (`neptune.discovery.scratch`). The caller names a **private root** and the
   **ingest root**, both required, so the overlap check always runs; the workspace's private root
   is to be `<workspace>/scratch`. It is created `0700`, must be a real directory owned by this
   user (tightened to `0700` if looser), and must not overlap the ingest root in either direction.
   `scratch_space(private_root, ingest_root=...)` yields a fresh `0700` directory holding an
   `flock`ed lock file and removes it on exit, success or crash: that is "cleaned on commit".
   `clear_scratch(private_root, ingest_root=...)` runs on resume and removes every entry whose lock
   nobody holds, skipping a live process's, and debris too, which is why it checks the overlap
   first; symlinks are unlinked, never followed. The private root is
   itself `flock`ed: shared while a scratch directory is created and its lock taken, exclusive
   while `clear_scratch` sweeps, so a sweep never removes a directory still being set up. Scratch
   names are random and never reach a record.

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
- **Trusting the end-of-central-directory count.** It is used only to refuse early; the
  directory's entries are walked and counted before `zipfile` parses them, because the record can
  lie low and `zipfile` builds every entry before a caller sees one.
- **Catching only the documented exceptions.** `tarfile` and `zipfile` raise `IndexError`,
  `ValueError` and `NotImplementedError` on hostile headers; any of them would fail the job.
- **`RecursionError` as the limit on chained tar headers.** It strikes at a depth that depends on
  the caller's stack, so the same archive would give different findings from different call
  sites, and the links it holds until then can be gigabytes.
- **Judging an absolute symlink target against the root's host path.** It answers whether the
  link reaches the root on this host, which changes when the tree is mounted elsewhere; the
  target itself is recorded, so a consumer that knows the mount can still resolve it.
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
- Memory during inspection is bounded by one 1 MiB block, 1 MiB of headers per tar member, and
  a zip central directory of at most 1 KiB per allowed member (about 10 MiB by default) holding
  at most `max_members` entries; a report holds every member's declared name. The committed
  hostile fixtures peak near 1 MiB, the bombs the unit tests generate under 4 MiB (tracemalloc).
- Wiring is a follow-up: a `<workspace>/scratch` root beside ADR 0026's `staging/`, with
  `clear_scratch` at start-up; and in the job runtime (ADR 0028), which already turns
  `SourceChangedError` into a per-source finding, catching `ShortReadError` beside it and
  recording `short_read_finding` and `verify_artifact`'s findings.
- MVL-8's probe engine (ADR 0027) lists container members under its own bounded policy to select
  adapters; `inspect_archive` is the ingest-time limit. Whether the two fuse into one pass is a
  follow-up decision.
- Revisit if a format needs its own limits (video containers, bags of bags), if `tarfile` or
  `zipfile` change the private hooks relied on (`TarInfo._proc_member`, `TarFile.offset` and
  `members`, `zipfile._EndRecData`), or if inspection time on compressed corpora is measured to
  dominate ingest.
