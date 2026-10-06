# neptune-platform contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

The platform maintains the `contracts/` registry and `scripts/contracts.py` (ADR 0002) but owns no contract in
it. CI runs `make contracts-check PKG=neptune-platform`: the owner rule (a no-op while this package exports no
schema), then `scripts/contracts.py check --package neptune-platform` against the empty `[neptune-platform]`
section of `contracts/lock.toml`, then the check that `contracts/compatibility.md` matches the registry
(regenerate it with `scripts/contracts.py matrix`).

Registry rules that the tool enforces beyond ADR 0002:

- **A draft binds nobody.** A minor or patch version must accept every golden of the earlier **stable**
  versions of its major. A draft's goldens bind only the draft, so the next version (for example the
  owner's first real `catalog-api` export after the platform-authored 0.0.0 draft) is free to differ.
- **The first stable version enters every lock.** `bump` to a contract's first stable version, like a major
  bump, writes the new version into the lock section of every in-repo consumer in the same PR. A package
  is in the repository once it has a `contracts/lock.toml` section (`scripts/new-package.sh` adds it).
- **An owner that fails to import is a failure.** The owner's contract tests are skipped only while the
  owner module or one of its parent packages does not exist; any other import error fails the check.

## Golden-only releases

When an owner's goldens change but its schema does not (for example a consolidator or parser version bump
that changes ids, a new lineage under root AGENTS.md non-negotiable 6), publish them as a new minor or patch
version with `scripts/contracts.py bump <contract> <x.y.z> --golden-only` (ADR 0010). It refuses a major,
a schema that is not byte-identical to the latest version's, and goldens equal to the latest version's. The
constant rule is unchanged: an integer constant stays at the major; a string constant is raised to the new
version first. The new `version.json` records `"release": "golden-only"`, `check` re-verifies it against the
version before it, `compatibility.md` shows `x.y.z (golden-only, schema of <previous>)`, and the
announcement tells consumers that only the goldens changed. Earlier version directories are never touched.

## Publishes

_None yet._

## Consumes

_None yet._
