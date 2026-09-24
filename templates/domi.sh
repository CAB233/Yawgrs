#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f "$BUILDDIR/domi.toml" ]]; then
  echo "[ERROR] domi: missing $BUILDDIR/domi.toml; prepare this file in [prepare]" >&2
  exit 1
fi
cd "$BUILDDIR"
echo "[INFO] domi: exporting JSON rule-sets"
domi-cli --config "$BUILDDIR/domi.toml"
shopt -s nullglob
outputs=("$BUILDDIR"/*.json)
if (( ${#outputs[@]} == 0 )); then
  echo "[ERROR] domi: no JSON rule-sets generated in $BUILDDIR" >&2
  exit 1
fi
for file in "${outputs[@]}"; do
  name=$(basename "$file")
  echo "[INFO] domi: compiling $name"
  cp "$file" "$PKGDIR/$name"
  sing-box rule-set compile -o "$PKGDIR/${name%.json}.srs" "$PKGDIR/$name"
done
