#!/usr/bin/env bash
set -euo pipefail

source_path=$(jq -er '.filter // error("missing source: filter")' <<< "$RULE_SOURCES_JSON")
cp "$source_path" "$PKGDIR/adguard-dns-filter.txt"
sing-box rule-set convert -t adguard -o "$PKGDIR/adguard-dns-filter.srs" "$PKGDIR/adguard-dns-filter.txt"
