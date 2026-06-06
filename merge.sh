#!/usr/bin/env bash

set -euo pipefail

DOMAINSET_DIR="prebuild/sing-box/domainset"
NON_IP_DIR="prebuild/sing-box/non_ip"
OUTPUT_DIR="dist"

mkdir -p "$OUTPUT_DIR"

DEEP_MERGE='
. as $all |
def deep_merge(a; b):
  if (a | type) == "object" and (b | type) == "object" then
    reduce ((a + b) | keys_unsorted[]) as $k (
      {};
      .[$k] = deep_merge(a[$k]; b[$k])
    )
  elif (a | type) == "array" and (b | type) == "array" then
    (a + b) | unique
  elif b != null then
    b
  else
    a
  end;

{
  version: ($all[0].version // $all[1].version),
  rules: [
    range([$all[0].rules | length, $all[1].rules | length] | max) as $i |
    if   ($all[0].rules[$i] != null) and ($all[1].rules[$i] != null) then
      deep_merge($all[0].rules[$i]; $all[1].rules[$i])
    elif $all[0].rules[$i] != null then
      $all[0].rules[$i]
    else
      $all[1].rules[$i]
    end
  ]
}
'

for file in "$DOMAINSET_DIR"/*.json; do
  [[ -f "$file" ]] || continue
  name=$(basename "$file")
  other="$NON_IP_DIR/$name"

  if [[ -f "$other" ]]; then
    echo "[INFO] merge $name"
    jq -s "$DEEP_MERGE" "$file" "$other" > "$OUTPUT_DIR/$name"
  else
    echo "[INFO] copy $name (only in domainset)"
    cp "$file" "$OUTPUT_DIR/$name"
  fi
done

for file in "$NON_IP_DIR"/*.json; do
  [[ -f "$file" ]] || continue
  name=$(basename "$file")
  if [[ ! -f "$DOMAINSET_DIR/$name" ]]; then
    echo "[INFO] copy $name (only in non_ip)"
    cp "$file" "$OUTPUT_DIR/$name"
  fi
done

IP_DIR="prebuild/sing-box/ip"
IPV4_FILE="$IP_DIR/china_ip.json"
IPV6_FILE="$IP_DIR/china_ip_ipv6.json"
IP_OUTPUT="$OUTPUT_DIR/china-ip.json"

IP_MERGE='
{
  version: (.[0].version // .[1].version // 2),
  rules: [
    {
      ip_cidr: ([.[].rules[0].ip_cidr[]] | unique)
    }
  ]
}
'

if [[ -f "$IPV4_FILE" ]] && [[ -f "$IPV6_FILE" ]]; then
  echo "[INFO] merge china_ip and china_ip_ipv6"
  jq -s "$IP_MERGE" "$IPV4_FILE" "$IPV6_FILE" > "$IP_OUTPUT"
elif [[ -f "$IPV4_FILE" ]]; then
  echo "[INFO] extract ip_cidr from china_ip.json -> china-ip.json"
  jq '{version, rules: [{ip_cidr: .rules[0].ip_cidr}]}' "$IPV4_FILE" > "$IP_OUTPUT"
elif [[ -f "$IPV6_FILE" ]]; then
  echo "[INFO] extract ip_cidr from china_ip_ipv6.json -> china-ip.json"
  jq '{version, rules: [{ip_cidr: .rules[0].ip_cidr}]}' "$IPV6_FILE" > "$IP_OUTPUT"
fi
