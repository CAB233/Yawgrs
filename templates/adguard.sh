#!/usr/bin/env bash
set -euo pipefail

source_path=$(jq -er '.filter // error("missing source: filter")' <<< "$RULE_SOURCES_JSON")
output="${RULE_PARAM_OUTPUT:-adguard-dns-filter}"
[[ "$output" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo '[ERROR] Invalid output basename' >&2; exit 1; }
cp "$source_path" "$PKGDIR/$output.txt"
sing-box rule-set convert -t adguard -o "$PKGDIR/$output.srs" "$PKGDIR/$output.txt"
