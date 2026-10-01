# Hostile fixtures

Archives that lie, overflow, nest, traverse or end early. `make_hostile.py` generates every file
deterministically (no clock, randomness or host path) and `tests/integration/test_hostile_fixtures.py`
checks the committed files against it: byte for byte, or by content where deflate output may differ
between zlib builds. Each is small on disk and large, wrong or dangerous only as declared.

| File | Size | What it declares | Expected finding (`neptune.archive.*`) |
|---|---|---|---|
| `bomb.zip` | 49 KB | one member of 48 MiB zeros, deflated 1000:1 | `compression_ratio_exceeded`, before a byte is inflated |
| `bomb.tar.gz` | 49 KB | a tar of the same member, gzipped | `compression_ratio_exceeded`, from the first header |
| `many_members.zip` | 103 KB | 1,200 empty members | none with defaults; `member_count_exceeded` at `max_members=1000`, before the directory is parsed |
| `nested.zip` | 709 B | zips nested six deep | `nesting_depth_exceeded` at depth 4 |
| `mixed.zip` | 446 B | a zip holding a tar.gz holding a zip holding a file | none: depth 3 is allowed |
| `traversal.zip` | 1 KB | `../../etc/passwd`, `/etc/shadow`, `a/../../b.txt`, a NUL in a name, an empty name, a symlink, a FIFO; `a//b`, `C:\win\x` and `ok/fine.txt` are tolerated | `member_path_unsafe` ×5, `member_link`, `member_special` |
| `links.tar` | 10 KB | symlinks to `/etc/passwd` and `../outside/canary.txt`, a hard link, a FIFO, a directory, `/abs/file`, `../up.txt` | `member_link` ×3, `member_special`, `member_path_unsafe` ×2 |
| `pax_bomb.tar.gz` | 2 KB | a pax extended header of 2 MiB (tarfile reads these whole into memory) | `header_too_large` |
| `huge_member.tar` | 1 KB | a 4 GiB member followed by nothing | `member_truncated`; `member_size_exceeded` with a 1 GiB limit |
| `truncated.zip` | 2 KB | a stored zip cut at 60% | `truncated` (no end-of-central-directory record) |
| `truncated.tar` | 3 KB | a tar cut inside its second member | `member_truncated` for `b.txt`; `a.txt` is whole |
| `truncated.tar.gz` | 105 B | a gzip stream cut at 60% | `truncated` |
| `corrupt_member.zip` | 195 B | a deflated member with one byte flipped | `member_corrupt` |
| `encrypted.zip` | 181 B | a member with the encryption flag set | `member_encrypted` |

Symlinks and odd names cannot be committed portably, so `build_tree(root, outside)` creates that
part of the suite at test time: a loop (`loop -> loop`, `ping <-> pong`), escapes by absolute and
relative path and through a linked directory into a sibling `outside/canary.txt`, links inside the
root, a dangling link, a chain, names that look like traversal (`..hidden`, `..\..\etc\passwd`,
`%2e%2e%2fetc%2fpasswd`, ` ..`, `...`, a 255-character name), a FIFO, a 64-level deep tree, a benign
file that must still ingest, and a copy of every archive above. Every link is a
`neptune.discovery.symlink_not_followed` finding and the FIFO a `neptune.discovery.special_file`.

What is deliberately absent: a zip member whose declared size is smaller than its inflated size.
`zipfile` stops at the declared size, so such a member cannot inflate past what it declares; it is
not a bomb vector here. Gzip, bzip2 and xz single streams declare nothing trustworthy, so they are
bounded by actual bytes; a zip symlink whose target declares 2 MiB is refused as a header too large.
`tests/unit/discovery/test_archive_limits.py` builds both in memory, along with the headers that
crash `tarfile` or `zipfile`, chained long names and pax headers, sparse members that expand
200 MiB from 512 bytes, and zip directories that outnumber their end record.
