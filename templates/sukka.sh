#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$BUILDDIR/upstream"
source_path=$(jq -er '.archive // error("missing source: archive")' <<< "$RULE_SOURCES_JSON")
tar -xzf "$source_path" --strip-components=1 -C "$BUILDDIR/upstream"
base="$BUILDDIR/upstream/sing-box"
test -d "$base/domainset"
test -d "$base/non_ip"

shopt -s nullglob
for file in "$base/domainset"/*.json "$base/non_ip"/*.json; do
  name=$(basename "$file")
  if [[ -e "$PKGDIR/$name" ]]; then
    continue
  fi
  inputs=()
  for directory in domainset non_ip; do
    if [[ -f "$base/$directory/$name" ]]; then
      inputs+=( -c "$base/$directory/$name" )
    fi
  done
  sing-box rule-set merge "$PKGDIR/$name" "${inputs[@]}"
  sing-box rule-set compile -o "$PKGDIR/${name%.json}.srs" "$PKGDIR/$name"
done

ip_inputs=()
for name in china_ip china_ip_ipv6; do
  if [[ -f "$base/ip/$name.json" ]]; then
    ip_inputs+=( -c "$base/ip/$name.json" )
  fi
done
if (( ${#ip_inputs[@]} )); then
  sing-box rule-set merge "$PKGDIR/china-ip.json" "${ip_inputs[@]}"
  sing-box rule-set compile -o "$PKGDIR/china-ip.srs" "$PKGDIR/china-ip.json"
fi
