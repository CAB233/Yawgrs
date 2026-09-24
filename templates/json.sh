#!/usr/bin/env bash
set -euo pipefail

# select produces a stream of values; transform is applied to each value.
: "${RULE_PARAM_SOURCE:?json template requires build.source}"
: "${RULE_PARAM_SELECT:?json template requires build.select}"
: "${RULE_PARAM_TARGET:?json template requires build.target}"
source_id="$RULE_PARAM_SOURCE"
source_path=$(jq -er --arg id "$source_id" '.[$id] // error("unknown source ID: " + $id)' <<< "$RULE_SOURCES_JSON")
target="$RULE_PARAM_TARGET"
output="${RULE_PARAM_OUTPUT:-$RULE_NAME}"
version="${RULE_PARAM_VERSION:-3}"
transform="${RULE_PARAM_TRANSFORM:-.}"

[[ "$source_id" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo '[ERROR] Invalid source ID' >&2; exit 1; }
[[ "$output" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo '[ERROR] Invalid output basename' >&2; exit 1; }
[[ "$target" =~ ^[a-z][a-z0-9_]*$ ]] || { echo '[ERROR] Invalid target field' >&2; exit 1; }
[[ "$version" =~ ^[1-9][0-9]*$ ]] || { echo '[ERROR] Invalid rule-set version' >&2; exit 1; }

# Expressions are jq programs from trusted recipes, never shell-evaluated.
filter='if length != 1 then error("expected one JSON document") else .[0] end
  | [('"$RULE_PARAM_SELECT"') | ('"$transform"')]
  | if length == 0 then error("select/transform produced no values")
    elif any(.[]; (type != "string" and type != "number")) then
      error("mapping must produce strings or numbers; check select/transform")
    else {version: $version, rules: [{($target): (unique)}]} end'

jq -e -s --arg target "$target" --argjson version "$version" "$filter" \
  "$source_path" > "$PKGDIR/$output.json"
sing-box rule-set compile -o "$PKGDIR/$output.srs" "$PKGDIR/$output.json"
