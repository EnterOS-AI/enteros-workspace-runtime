#!/usr/bin/env bash
# check-schemas-in-sync.sh — fail if the vendored plugin-manifest JSON-Schema
# in molecule_runtime/contracts/ has drifted from molecule-ai-sdk main.
#
# molecule_runtime/contracts/plugin-manifest.schema.json is a byte-for-byte
# SSOT mirror of the molecule-ai-sdk contracts/ original (see
# molecule_runtime/contracts/PROVENANCE.md). The advisory manifest gate
# (manifest_ssot.py, molecule-core#3383) validates plugin.yaml OFFLINE against
# the vendored copy, so this gate is what keeps the mirror honest: it
# re-fetches the schema from molecule-ai-sdk (contracts/) main and diffs.
#
# Mirrors molecule-ci's scripts/check-schemas-in-sync.sh (which in turn mirrors
# molecule-ai-workspace-runtime#196's vendored workspace-comms drift gate).
# molecule-ai-sdk is public, so the fetch is anonymous — no token needed.
#
# Exit 0 = in sync. Exit 1 = drift (vendored copy != sdk main). Exit 2 =
# could not fetch (network/infra) — treated as a soft skip so a transient
# git.* TLS stall doesn't paint every PR red; the byte-identical invariant is
# still enforced whenever the fetch succeeds.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main"
UA="curl/8.4.0"

# repo-relative vendored-copy path  ->  path within molecule-ai-sdk (contracts/).
# Keys are REPO-RELATIVE (not bare basenames) so the gate can span both vendored
# roots: molecule_runtime/contracts/ (the packaged schemas) AND the top-level
# contracts/ (mcp-plugin-delivery, mirrored from the SDK's contracts/mcp/).
declare -A MAP=(
  [molecule_runtime/contracts/plugin-manifest.schema.json]="contracts/plugin-manifest/plugin-manifest.schema.json"
  [molecule_runtime/contracts/idle-prompt.schema.json]="contracts/idle-prompt/idle-prompt.schema.json"
  [molecule_runtime/contracts/idle-prompt.contract.json]="contracts/idle-prompt/idle-prompt.contract.json"
  [molecule_runtime/contracts/workspace-data.contract.json]="contracts/workspace-data/workspace-data.contract.json"
  [molecule_runtime/contracts/credentials.contract.json]="contracts/credentials/credentials.contract.json"
  [contracts/mcp-plugin-delivery.contract.json]="contracts/mcp/mcp-plugin-delivery.contract.json"
)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

drift=0
fetch_fail=0
for local_rel in "${!MAP[@]}"; do
  remote_path="${MAP[$local_rel]}"
  local_file="$REPO_ROOT/$local_rel"
  slug="$(echo "$local_rel" | tr '/' '_')"
  if [ ! -f "$local_file" ]; then
    echo "::error::vendored schema missing: $local_rel"
    drift=1
    continue
  fi
  if ! curl -fsS -A "$UA" "$BASE/$remote_path" -o "$tmp/$slug"; then
    echo "::warning::could not fetch $remote_path from molecule-ai-sdk main (network/infra) — skipping drift check for $local_rel"
    fetch_fail=1
    continue
  fi
  if diff -u "$local_file" "$tmp/$slug" > "$tmp/$slug.diff"; then
    echo "OK   $local_rel == molecule-ai-sdk:$remote_path"
  else
    echo "::error::DRIFT $local_rel has drifted from molecule-ai-sdk:$remote_path"
    cat "$tmp/$slug.diff"
    drift=1
  fi
done

if [ "$drift" -ne 0 ]; then
  echo "::error::Vendored schemas are out of sync with molecule-ai-sdk (contracts/) main."
  echo "Re-vendor per molecule_runtime/contracts/PROVENANCE.md and bump the source-commit SHAs."
  exit 1
fi
if [ "$fetch_fail" -ne 0 ]; then
  echo "::warning::Some schemas could not be fetched; drift check was partial (soft skip)."
  exit 2
fi
echo "All vendored schemas are in sync with molecule-ai-sdk (contracts/) main."
exit 0
