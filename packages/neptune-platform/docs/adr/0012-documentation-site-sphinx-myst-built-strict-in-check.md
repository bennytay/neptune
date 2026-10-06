# 0012 — Documentation site: Sphinx + MyST + Furo over the repository's own docs, built strict and offline in `check`

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191

## Context

Demo v1 (MVL-191) needs a product documentation site: the layers' existing docs, every published contract
version's JSON Schema, an API reference for the public Python surfaces, and short concept pages a robotics
founder reads first. Forces:

- **The docs already exist and are owned elsewhere.** Root `docs/` is the compiler's; each package owns its
  `docs/`. The site must not fork them: no hand-copied duplicates, no edits to make a generator happy.
- **They are written for GitHub.** Relative links between documents, links to source files, tests and
  directories, a few HTML anchors. A site that rewrote paths would break every one of them; a site that
  ignored links outside itself would hide real breakage.
- **A docs site rots silently.** A renamed heading, a moved file or a docstring that no longer parses must
  fail a check, not a reader.
- **The repository's rules apply.** Deterministic output, no network at build time, no analytics or remote
  fonts in the output (the site must open offline), every robot type in the examples.
- **The tool must last.** MkDocs core has had no release since 1.6.1 (2024-08); Material for MkDocs is in
  maintenance while its authors build a successor (Zensical, still 0.0.x). Sphinx is actively released (9.1),
  has a strict mode, and its autodoc reads this codebase's reStructuredText-style docstrings (double-backtick
  literals, `::` blocks) as they are written.

## Decision

1. **Generator.** Sphinx with MyST-Parser (Markdown) and the Furo theme, in a `docs` dependency group in the
   root `pyproject.toml` (`make setup` installs every group). The API reference is `sphinx.ext.autodoc`.
   No other extension: no intersphinx (network), no analytics, no Mermaid JavaScript (diagrams stay code
   blocks).
2. **Layout: `docsite/`, owned by Platform.** A root directory like `harness/`: `MEMBER_DIRS` in the
   Makefile and `.github/scripts/ci_plan.py` route it to `neptune-platform`, whose job lints it and whose
   tests (`tests/test_docsite_*.py`) cover it. `docsite/sources.py` declares the sections and API modules,
   `docsite/pages/` holds the hand-written pages (home, concepts, quickstart), `docsite/conf.py` is the
   Sphinx configuration.
3. **The site mirrors the repository's paths.** `python -m docsite` assembles a source tree in
   `build/docs/src` (gitignored) where `docs/architecture.md` is the page `docs/architecture` and
   `packages/neptune-ledger/docs/guarantees.md` is `packages/neptune-ledger/docs/guarantees`, so relative
   links between included documents work unchanged. Each section takes **every** Markdown file under its
   `docs/` root (new documents appear without a config change); ADRs and gate reviews are listed apart.
   A link to any other repository path is rewritten, in the assembled copy only, to its GitHub URL on
   `main`; an image is copied in; a link to a path that does not exist, or that leaves the repository, is a
   problem that fails the build. Sources are read, never written.
4. **Generated pages.** One page per layer (`layers/<slug>`), one per published contract version
   (`contracts/<id>/<version>/schema`: every definition, nested schema resources included, as a labelled
   section with its type, its properties and whether each is required, every other keyword verbatim,
   `$ref`s as cross-references, and the schema file attached for download), and one per API surface
   (`api/<module>`: a facade's `__all__`, imported names included; Memory's schema modules by what they
   define). The quickstart page embeds the README's first level-2 section whose heading contains
   "quickstart" (case-insensitive), links rewritten; until the README has one it points at the README.
5. **Strict.** Sphinx runs with `-W --keep-going -n -E` from an empty output directory: every warning
   fails, every document cross-reference and heading anchor must resolve, and no cached page hides a
   warning. Two narrow exemptions, both about Python annotations in the API reference rather than links:
   unresolved `py:` references (types from undocumented modules and the standard library) and `ref.python`
   (a short type name two documented modules both define). One docstring shim: a ``literal``s glued to a
   following word gets reStructuredText's escaped space, so Markdown-ish prose parses without editing
   another package's source.
6. **Offline and deterministic.** No clock (no `last updated`, a copyright without a year), no network, and
   after Sphinx a scan fails the build if any page, stylesheet or script would load a remote resource.
   Two builds under different `PYTHONHASHSEED` and `TZ`, and parallel or serial, are byte-identical.
7. **The build is part of `check`.** `make docs` builds into `build/docs/html`. A whole-workspace `make check`
   runs it after lint, type and test; `make check PKG=<name>` does not, mirroring CI, where `ci.yml`'s
   `docs` job runs `make docs` on **every** event, unfiltered (any repository path can be a link target and
   every public module is a page), and `check` requires it. `scripts/merge_freshness.py` does not compare it,
   as it does not compare the platform job: every PR would overlap. A break between two independently green
   PRs (one renames a file, the other links to it) surfaces in the `push` run on `main` (ADR 0005 §3, §4).

## Alternatives considered

- **MkDocs + Material + mkdocstrings.** The nicest out-of-the-box output, but MkDocs core is unmaintained,
  Material is in maintenance mode, Material fetches Google Fonts unless configured off, and mkdocstrings
  renders docstrings as Markdown, which mangles the codebase's double-backtick literals.
- **Zensical.** Material's successor; 0.0.x, not yet mature enough to build a gate on.
- **Symlink the docs into a docs root.** No rewriting step, but links to source files and directories would
  either break or be unchecked, and symlinks are fragile on some hosts. The assembled copy is as faithful
  and checks every link.
- **Only the latest version of each schema.** Smaller output, but consumers pin older versions
  (`contracts/lock.toml`) and need theirs; every published version is rendered.
- **Route the docs build through ci_plan.py paths.** Every path is a potential link target, so a filter would
  be either everything or wrong. The job is cheap (about 15 s of build on a 20-core host, under a minute on a
  CI runner, plus setup).

## Consequences

- A PR that renames a document, a heading or a linked source file, or writes a docstring Sphinx cannot
  parse, fails `docs`; the message names the assembled path, which is the repository path.
- A layer's new document joins its section automatically; a new section, API surface or concept page is a
  change to `docsite/sources.py` or `docsite/pages/`.
- The site is published nowhere yet; `build/docs/html` opens from disk. Hosting is a later decision.
- If the README's quickstart heading changes so it no longer contains "quickstart", the page falls back
  to pointing at the README; `tests/test_docsite_assemble.py` pins both behaviours.
- Sphinx's sources use Python 3.12 syntax, so the platform's mypy does not follow into `sphinx`
  (`follow_imports = "skip"`) while it targets 3.11.
