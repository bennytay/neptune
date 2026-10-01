# 0040 — Software identity: read as each file declares it, bound to runs later

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-27

## Context

MVL-27 asks for the code, build, firmware, model checkpoint and package identities behind a run:
git commits, build and release ids, firmware versions, checkpoint ids and hashes, package and
container image identities, discovered automatically. Its acceptance: every run can carry an
immutable `SoftwareConfiguration`, and missing software identity is a finding, never silence.

What already holds, and constrains this:

- The model is frozen (ADR 0023). `SoftwareConfiguration` (ADR 0019 §5) has one item per software
  unit with `name`, `device`, `commit`, `release`, `build` and `digest`, each `Knowledge`-wrapped,
  holding ADR 0014's kinds. Nothing points at a run: binding is MVL-38's, and it is `inferred`.
- An adapter sees one artifact's bytes and never its location (ADR 0024 §1). A `.git` directory
  is many artifacts; which ref a 41-byte file is, is its path.
- The sandbox (ADR 0030) forbids processes, writes and the network: no `git`, no build tool, no
  Python execution, even if they were wanted.
- ADR 0014 §6: a checkpoint hash a source states is a claim, compared with Neptune's tier-1
  content id by validation, never assumed equal to it.

What goes wrong otherwise: a checksum file read as a commit, a firmware `1.2.3` coerced to SemVer,
a package archive's hash stored as a checkpoint's, `setup.py` executed, a pickle loaded, a
gigabyte checkpoint hashed twice, a URDF-style guess turned into a binding.

## Decision

1. **One adapter, `software`** (`neptune.adapters.software`), one module per family (git,
   manifests, lockfiles, firmware, checkpoints, SBOMs) and the shared rules in `_common`. It emits
   only `software_configuration` records: no model change. Named `software`, not `identity`,
   which is Neptune's own hashing package. One adapter, not six: every family applies the same
   rules for states, kinds and findings, and the runtime sees one line in `builtin.py`.
2. **One record per declaring file.** An item per software unit the file declares, in file order;
   `machine` is `NotCovered` (none of these formats names a machine). The record cites the whole
   file. Each value cites its own exact bytes, or, decoded from TOML or JSON, `[ByteRange of the
   document, JsonPointer]`, plus a `Span` for part of a string (a commit in a URL's fragment).
   `observed` where the bytes describe themselves (git refs, firmware and checkpoint headers),
   `stated` where a file declares other software (manifests, lockfiles, SBOMs, image indexes).
3. **The identifier contract** (what each format fills; everything else per §4):

   | Format | `name` | `device` | `commit` | `release` | `build` | `digest` |
   |---|---|---|---|---|---|---|
   | git ref file (`HEAD`, `refs/*`) | | | object name | | | |
   | `packed-refs` | refname | | object, or its `^` peel | | | |
   | `package.xml` | `<name>` | | | `<version>`, SemVer (REP 127) | | |
   | `pyproject.toml` | `[project]` / `[tool.poetry]` | | | `version`, declared | | |
   | `Cargo.toml` | `[package] name` | | | `version`, SemVer | | |
   | `CMakeLists.txt` | `project(<name>)` | | | `VERSION`, declared | | |
   | `setup.py` | `name=` literal | | | `version=` literal | | |
   | `uv.lock`, `poetry.lock`, `Cargo.lock`, `package-lock.json` | each package | | git source's commit | locked version | | |
   | ELF | FDO package note | | | FDO note `version` | GNU build-id, hex | |
   | ESP-IDF image | `project_name` | | | `version`, firmware | `app_elf_sha256`, hex | |
   | MCUboot image | | | | `ih_ver`, firmware, `M.m.r+b` | | |
   | PX4 / ArduPilot file | | `summary` (board) | `git_hash`, else describe's | `git_identity` (describe) | | |
   | safetensors, ONNX, PyTorch | | | | ONNX `model_version` | | none stated |
   | CycloneDX, SPDX | each component / package | | | `version` / `versionInfo` | | model hash; image purl digest |
   | OCI image index | `image.ref.name` | | | `image.version` | | manifest digest |

4. **States.** A value the file gives is `Known`. A field the format has a place for and the file
   leaves out or blank is `Unknown`, citing where the adapter looked. A field the format has no
   place for is `NotCovered`. `digest` holds only checkpoint and container-image digests (its two
   kinds), so for any other software it is `NotApplicable`; a package archive's hash stays cited
   in the item's bytes. Two places in one file giving one item different values is `Ambiguous`
   plus `conflicting_identity`.
5. **Missing identity is a finding per item.** An item with no `Known` or `Ambiguous` commit,
   release, build or digest, and at least one of them `Unknown` for no reason another finding
   gives, is `software.software_identity_missing` (missing, warning). Gaps already explained are
   not reported twice: `invalid_value` (text that is not a valid member of its kind), `unevaluated`
   (a CMake variable, a dynamic or workspace-inherited version, Python code) and
   `git_unpeeled_tag`. An item whose fields are all `NotCovered` (a checkpoint) has no gap.
6. **Kinds follow the format, never the text** (ADR 0014 §5). SemVer where the format mandates it
   (`package.xml`, Cargo); text there that is not SemVer is kept as a `DeclaredVersion` with
   `version_not_semver` (inconsistent, info), because the text is still what the file declares
   and only the format's claim failed. `FirmwareVersion` for firmware headers and for SBOM entries
   typed firmware; `DeclaredVersion` everywhere else (PEP 440, CMake, npm, describe output).
   Binary ids are rendered as their tools print them: build ids as lowercase hex, MCUboot as
   `imgtool`'s `major.minor.revision+build`, ONNX's int64 in decimal.
7. **Git is read file by file, never run, its object store never read.** An object name in a ref
   file is the item's `commit`. A symbolic ref (`ref: refs/heads/main`) names no commit: no record,
   and `git_symbolic_ref` (missing, info) cites and names its target. Resolving `HEAD` combines
   files by their locations and is binding's (MVL-38). Loose objects, packs and the index are
   history, not identity: no adapter claims them, so the probe engine reports them and reads no
   more than their head, whatever their size. Dirty state needs the working tree as well as the
   index and is not claimed. Only the name can tell a one-line hex checksum file from a ref, so
   a name ending `.sha256`, `.sha1`, `.md5` (and the like) vetoes the ref claim.
8. **A checkpoint's identity is its content id.** Its sha256 is computed when it is fingerprinted
   and is the record's source; the adapter never rehashes gigabytes in its sandbox, and never
   puts its own hash in `digest`, which holds what a source states. safetensors headers, ONNX
   top-level protobuf fields (the graph skipped by its length) and PyTorch zip directories are
   read; weights are never loaded and pickles never unpickled; data past the end is `truncated`.
9. **Containers and SBOMs.** An `oci` or `docker` package URL whose version is a digest is a
   container image's `ContainerImageDigest`; a CycloneDX `machine-learning-model`'s hash is its
   `ModelCheckpointHash`, SHA-256 when listed (what content ids are). OCI's
   `org.opencontainers.image.revision` is VCS-agnostic, so it is not a `GitCommit` by definition
   and stays in the bytes.
10. **Probing is by bytes, calibrated to win against generic readers.** A probe that parses and
    checks its document claims `VERIFIED`, so a generic JSON, TOML or XML reader (`STRUCTURE`)
    never ties with it; heads too long to parse claim `SIGNATURE` on their format's signature.
    Every source is one chunk.
11. **Nothing is executed or expanded, everything is bounded.** `setup.py` becomes a syntax tree
    only; XML entity declarations are refused; JSON repeating a key or holding `NaN` is
    malformed, never a silent last value. `max_document_bytes` (32 MiB), `max_header_bytes`
    (16 MiB) and `max_items` (20,000) bound every read; past one is a `limit` finding.
12. **Not here.** Binding these records to runs, including a run-level "this run has no software
    identity", is MVL-38's; manifest overrides are MVL-14's; release notes and changelogs are
    documents (MVL-28): a source has one adapter, and a version in prose is interpretation.
    Docker image ids (a config digest, not a manifest digest), package archive digests and
    repository remotes have no field and stay in the bytes until a record kind is added for them.

## Alternatives considered

- **Six adapters, one per family.** Separate lineage per family, but the rules would be copied six
  times (adapters never import each other) and drift; a version bump re-lineages records of one
  kind only, which costs little.
- **A `.git` directory as one source, resolving `HEAD` in the adapter.** An adapter reads one
  artifact; combining several is a derivation, and identical refs in two repositories are one
  artifact with two locations.
- **Reading packs to confirm a ref's commit.** Delta chains, gigabyte packs, and no identity gained
  that the refs do not already state.
- **Hashing a checkpoint into `digest` as `observed`.** It duplicates the content id, makes the
  claim-versus-id check of ADR 0014 §6 trivially true, and reads the file again.
- **An archive hash as a `ModelCheckpointHash`.** Kinds would meet: a validator would compare a
  wheel's hash with a policy's.
- **Non-SemVer text in a SemVer field as `Unknown`** (ADR 0014 §1 read literally). It loses the
  declared text, which is a valid `DeclaredVersion`.
- **New record kinds (a git ref, a lockfile entry).** A schema version for shapes
  `SoftwareConfiguration` already holds.
- **Names to recognise loose refs.** Names are advisory and branch names arbitrary; the name only
  vetoes a checksum file.

## Consequences

- MVL-38 binds runs to these records by content id (checkpoints, firmware), by location (git
  files: `HEAD`'s `git_symbolic_ref` target to the ref file or `packed-refs` item), and by equal
  declared values (a run manifest's digest or commit), each binding cited and `inferred`.
- A loose tag ref that names an annotated tag object is recorded as a `GitCommit`; binding must
  peel it (`packed-refs`' `^` line) before relying on it.
- Adding a format is a module, an entry in `FORMATS` and a version bump, which re-lineages every
  `software` record (ADR 0003).
- Output relies on the standard library's TOML, JSON, XML and Python parsers, which are not
  listed as libraries; a Python upgrade that changes what parses (new `setup.py` syntax) changes
  output. Revisit if that bites.
- `CMakeLists.txt` read as software draws the probe engine's `name_mismatch` (`.txt` is text's
  extension); an info finding, accepted.
- Revisit if binding needs refs as records of their own, dirty state, a checkpoint metadata
  convention worth a field, or a format whose identity spans files.
