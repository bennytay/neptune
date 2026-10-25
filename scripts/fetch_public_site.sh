#!/usr/bin/env bash
# Fetch real public robot data into a site folder (issue #211, docs/site-model/real-data.md).
#
#   scripts/fetch_public_site.sh <site_dir>
#
# Dataset: "UR3 CobotOps", UCI Machine Learning Repository id 963, CC BY 4.0
# (Tyrovolas, Aliev, Antonelli, Stylios; https://archive.ics.uci.edu/dataset/963/ur3+cobotops).
# The workbook is copied byte for byte into <site_dir>/static/ur3/; the site's declared layout and
# mapping file come from scripts/public_site/. The download is cached in $TMPDIR (or /tmp) and
# checked by sha256; nothing is written into the repository. Running it again is a no-op.
set -euo pipefail

out=${1:?usage: scripts/fetch_public_site.sh <site_dir>}
url="https://archive.ics.uci.edu/static/public/963/ur3+cobotops.zip"
zip_sha=233334eeb5370e594ea621796ca2d693bd4980cc2bb24fa749b1b633a4993c8d
member=dataset_02052023.xlsx
member_sha=f0f10917de9056908a82cec1aa459ddf9da2d2db70d269b128f99241c8796091
here=$(cd "$(dirname "$0")" && pwd)
cache="${TMPDIR:-/tmp}/neptune-public-site"

sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

mkdir -p "$cache" "$out/static/ur3"
zip="$cache/ur3-cobotops.zip"
if [ ! -f "$zip" ] || [ "$(sha "$zip")" != "$zip_sha" ]; then
  echo "downloading $url"
  curl -fsSL -o "$zip.part" "$url"
  mv "$zip.part" "$zip"
fi
[ "$(sha "$zip")" = "$zip_sha" ] || { echo "sha256 mismatch for $zip" >&2; exit 1; }

xlsx="$out/static/ur3/$member"
if [ ! -f "$xlsx" ] || [ "$(sha "$xlsx")" != "$member_sha" ]; then
  unzip -p "$zip" "$member" > "$xlsx"
fi
[ "$(sha "$xlsx")" = "$member_sha" ] || { echo "sha256 mismatch for $xlsx" >&2; exit 1; }

cp "$here/public_site/layout.yaml" "$out/static/layout.yaml"
if [ -f "$here/public_site/neptune.mappings.yaml" ]; then
  cp "$here/public_site/neptune.mappings.yaml" "$out/static/neptune.mappings.yaml"
fi
echo "public site ready in $out (UCI 963 UR3 CobotOps, CC BY 4.0, sha256 $member_sha)"
