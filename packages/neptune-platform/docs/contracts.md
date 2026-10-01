# neptune-platform contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.

CI runs `make contracts-check PKG=neptune-platform`. That target applies the owner rule to any schema this package
exports, then runs `scripts/contracts.py check --package neptune-platform` against `contracts/lock.toml`. The policy is
neptune-platform ADR 0002. A new package needs a `[neptune-platform]` section in `contracts/lock.toml`, left empty if it
consumes nothing, and an entry in `contracts/packages.toml`.

## Publishes

_None yet._

## Consumes

_None yet._
