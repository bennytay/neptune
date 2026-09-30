# 0014 — Version primitives: one type per kind, stored verbatim

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-62 (sub-issue of MVL-4)

## Context

Physical evidence is only useful if it stays bound to the software that produced it: the commit, the release,
the firmware on each device, the policy checkpoint, the container image. `SoftwareConfiguration` (MVL-1) and
the run bindings in MVL-27 hold these values. Every adapter that reads a `package.xml`, a PX4 `ver` banner, a
model card or a deployment manifest produces them.

The danger is coercion. `1.2.3` might be a SemVer, a firmware string or a build number. `abc1234` might be a
commit, a hash prefix or a tag. Sorting free-form strings puts `10` before `9`. A validator that treats a
checkpoint's SHA-1 as a git commit, or a firmware `1.2.3` as the package's `1.2.3`, joins things that are
unrelated.

## Decision

1. **One frozen dataclass per kind** in `neptune.model.versions`, with union `VersionPrimitive`:

   | Type | Holds | Validation |
   |---|---|---|
   | `GitCommit` | `sha` | 4–64 hex digits. `abbreviated` is true unless the length is 40 or 64 |
   | `SemanticVersion` | `value` | SemVer 2.0.0 exactly. ASCII digits only. No leading `v` |
   | `DeclaredVersion` | `value` | any text: a version under no scheme the source names |
   | `BuildId` | `value` | any text: a CI or build identifier |
   | `FirmwareVersion` | `value` | any text: a device firmware version as reported |
   | `ModelCheckpointHash` | `algorithm`, `digest` | full-length hex for md5 / sha1 / sha256 / sha384 / sha512 |
   | `ContainerImageDigest` | `digest` | OCI grammar: `sha256:` + 64 or `sha512:` + 128 lowercase hex |

   All text must be non-empty, valid Unicode, and at most 256 characters. Invalid input raises `ValueError`.
   The adapter turns that into a finding plus `Unknown`, as `from_text` does (ADR 0011 §6).
2. **Kinds never meet.** Values of different kinds are never `==`, even with identical text, and they hash as
   distinct set members. Ordering across kinds raises `TypeError`.
3. **Only `SemanticVersion` is ordered**, by SemVer §11 precedence. Free-form kinds, commits and digests have no
   order, so `sorted` over them raises. Any order Neptune could invent for them (lexical, "natural", date-like)
   would be interpretation.
4. **Stored verbatim.** Nothing is trimmed, case-folded, prefixed or completed. `SemanticVersion` keeps the whole
   declared text. `major`, `minor`, `patch`, `prerelease` and `build` are parsed views of that text, so
   prerelease and build metadata cannot be dropped. `==` is record equality: `1.0.0+a ≠ 1.0.0+b`, although
   neither precedes the other. Hex case is kept as written, except that the OCI grammar requires lowercase.
   Matching digests across sources case-insensitively is a validation concern (`validate/`, MVL-27).
5. **Grounds for a kind.** A value takes a kind because the source or its format says so, never because the
   text looks like one:
   - a field the format defines (`package.xml` `<version>` is `MAJOR.MINOR.PATCH` by REP 127; an OCI `@sha256:`
     reference; a `git_sha` key in a build-info file);
   - the source's own label for the value;
   - a manifest entry (MVL-14).

   Anything else is a `DeclaredVersion`. The `Knowledge` wrapper's provenance cites the grounding. Extracting a
   part (the `abc1234` in a `git describe` string `v1.2-3-gabc1234`) is the adapter's documented syntax, and
   the whole string stays available as a `DeclaredVersion`.
6. **A checkpoint hash is a claim**, not a content id. Whether Neptune's own tier-1 id for the checkpoint bytes
   matches it is a validation check. It is never assumed.
7. **JSON** is a tagged object with `"kind"` as the discriminator, for example
   `{"kind":"semver","value":"1.2.3-rc.1+b5"}`, `{"kind":"git_commit","sha":"…"}` or
   `{"algorithm":"sha256","digest":"…","kind":"model_checkpoint_hash"}`. `version_from_json` is strict: an
   unknown kind or a missing or extra key raises. Fields hold `Knowledge[<kind>]` or, where the source might
   declare several kinds, `Knowledge[VersionPrimitive]`.

## Alternatives considered

- **One `Version(kind, text)` class.** Fewer types, but equality and ordering would depend on a runtime tag,
  and the type checker could not stop a `FirmwareVersion` being passed where a `GitCommit` is expected.
- **Accept `v1.2.3` as SemVer.** The prefix is common in tags, but the spec excludes it. Stripping it is a
  silent normalisation. It is a `DeclaredVersion`; a derived step may read it as SemVer.
- **Lowercase hex on construction.** Case carries no meaning for hex, but lowercasing is still rewriting a
  declared value, and nothing in the evidence layer needs it yet.
- **Natural-sort order for free-form versions.** Firmware and distro strings have no common scheme
  (`0x010E`, `humble`, `2024.03`). Any order would be a guess presented as a fact.
- **Allow abbreviated checkpoint digests** (for example torchvision's 8-hex filename prefix). A prefix is not a
  digest of the stated algorithm, so it stays a `DeclaredVersion` or a finding until a real source needs a type
  for it.
- **Use the `packaging` or `semver` libraries.** `packaging` implements PEP 440, not SemVer, and normalises on
  parse. A dependency's parser changing between releases would change adapter output (ADR 0013 made the same
  call for units). The SemVer grammar is one regex.

## Consequences

- `SoftwareConfiguration` (MVL-1) can hold every identity MVL-27 lists without inventing string conventions.
- Validators that relate kinds (checkpoint hash vs content id, commit prefix vs full SHA, case-insensitive
  digest match) must do so explicitly. The types refuse to do it implicitly.
- Adding a kind (a PEP 440 version, a ROS distro name, a Debian version) is additive. It needs an ADR only if it
  introduces an ordering.
- Changing a kind's validation or JSON shape changes adapter output, and needs a new ADR.
