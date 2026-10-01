#!/usr/bin/env bash
# Create a workspace member from packages/_template.
#
# Usage: scripts/new-package.sh <name>
#
# <name> is the distribution name (lowercase letters, digits and single hyphens, e.g. neptune-ledger);
# the import package is the same name with hyphens as underscores. Copies packages/_template to
# packages/<name>, substituting __PKG_NAME__ / __PKG_MODULE__ in paths and contents, then re-locks the
# workspace so uv.lock registers the member (the root `packages/*` glob already includes it). CI needs
# no edit: its plan step discovers members from packages/*/pyproject.toml. Run `make setup` next.
set -euo pipefail

usage() {
  echo "usage: $0 <name>   (e.g. neptune-ledger)" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
name=$1
[[ $name =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ ]] || usage
[[ $name != neptune ]] || { echo "'neptune' is the compiler (the root project)" >&2; exit 1; }
# `check`, `plan` and `template` are CI job names; a package of that name would collide with them.
case $name in
  check | plan | template) echo "'$name' is reserved" >&2; exit 1 ;;
esac
module=${name//-/_}

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
template=$root/packages/_template
target=$root/packages/$name
[[ -d $template ]] || { echo "missing $template" >&2; exit 1; }
[[ ! -e $target ]] || { echo "$target already exists" >&2; exit 1; }

# Build in a scratch directory and move into place, so a failure never leaves a half-made package.
scratch=$(mktemp -d "$root/packages/.new-package.XXXXXX")
chmod 755 "$scratch"
trap 'rm -rf "$scratch"' EXIT
(cd "$template" && find . -type f -print0) | while IFS= read -r -d '' rel; do
  rel=${rel#./}
  out=$scratch/${rel//__PKG_MODULE__/$module}
  mkdir -p "$(dirname "$out")"
  sed -e "s/__PKG_NAME__/$name/g" -e "s/__PKG_MODULE__/$module/g" "$template/$rel" >"$out"
done
mv "$scratch" "$target"
trap - EXIT

(cd "$root" && ${UV:-uv} lock --quiet)
# Register with the contracts registry (empty lock section) so `make contracts-check PKG=<name>` is green.
(cd "$root" && ${UV:-uv} run --no-project --quiet python scripts/contracts.py register "$name")
echo "created packages/$name (import $module); next: make setup && make check PKG=$name" >&2
