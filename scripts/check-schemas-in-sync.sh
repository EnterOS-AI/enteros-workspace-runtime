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
# Exit 0 = in sync. Exit 1 = drift (vendored copy != sdk main, OR the mapped SDK
# path does not resolve). Exit 2 = could not fetch (transport/infra) — a soft
# skip so a transient git.* TLS stall doesn't paint every PR red.
#
# HTTP STATUS IS NOT A TRANSPORT ERROR
# ------------------------------------
# This used to be `curl -fsS`, whose exit status is non-zero for an HTTP 404 in
# exactly the same way it is non-zero for a TLS stall — so both took the soft-skip
# arm. That made the gate blind to the one failure it is here to catch: if a
# mapped SDK path is renamed, moved, or simply typo'd in MAP, the fetch 404s, the
# gate prints "could not fetch … skipping" and goes green, and the vendored copy
# silently becomes a fork. Which is the exact thing contracts/PROVENANCE.md says
# the map exists to prevent: "a vendored file absent from that map is a mirror
# nothing checks, which is how a mirror silently becomes a fork" — and a mapped
# file whose fetch always 404s is that same mirror wearing a map entry.
#
# So the fetch drops -f, captures %{http_code} separately from curl's own exit
# status, and classifies:
#   curl exit != 0  -> transport (DNS/TLS/timeout/reset)   -> soft skip (exit 2)
#   HTTP 2xx        -> fetched                             -> diff it
#   HTTP 4xx        -> the mapped SDK path does not resolve -> HARD FAIL (exit 1)
#   HTTP 5xx/other  -> forge-side infra                    -> soft skip (exit 2)
# -L is new and deliberate: it resolves a redirect to its real terminal status so
# a 3xx cannot land in the "other" bucket and soft-skip.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Overridable ONLY so the gate's own status classifier can be exercised against a
# local server (tests/test_schema_sync_gate_http_status.py) — a test that had to
# edit this script to run would not be testing this script. CI never sets it.
BASE="${SCHEMA_SYNC_SDK_BASE:-https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main}"
UA="curl/8.4.0"

# MAP is an associative array, which is bash >= 4. On bash 3.2 (what /bin/bash
# still is on macOS) `declare -A` fails, the loop then iterates nothing, and
# WITHOUT this guard the script falls straight through to `exit 0` — green,
# having compared not one byte. Same failure class as the 404 soft-skip above:
# a gate that checked nothing must never report success.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  echo "::error::this gate needs bash >= 4 for associative arrays; found bash ${BASH_VERSION:-unknown}. NOTHING was checked — do not read this as in sync."
  exit 1
fi

# repo-relative vendored-copy path  ->  path within molecule-ai-sdk (contracts/).
# Keys are REPO-RELATIVE (not bare basenames) so the gate can span both vendored
# roots: molecule_runtime/contracts/ (the packaged schemas) AND the top-level
# contracts/ (mcp-plugin-delivery, mirrored from the SDK's contracts/mcp/).
declare -A MAP=(
  [molecule_runtime/contracts/plugin-manifest.schema.json]="contracts/plugin-manifest/plugin-manifest.schema.json"
  [molecule_runtime/contracts/plugin-state.schema.json]="contracts/plugin-state/plugin-state.schema.json"
  [molecule_runtime/contracts/plugin-state.contract.json]="contracts/plugin-state/plugin-state.contract.json"
  [molecule_runtime/contracts/idle-prompt.schema.json]="contracts/idle-prompt/idle-prompt.schema.json"
  [molecule_runtime/contracts/idle-prompt.contract.json]="contracts/idle-prompt/idle-prompt.contract.json"
  [molecule_runtime/contracts/workspace-data.contract.json]="contracts/workspace-data/workspace-data.contract.json"
  [molecule_runtime/contracts/credentials.contract.json]="contracts/credentials/credentials.contract.json"
  [molecule_runtime/contracts/cron.fixtures.json]="contracts/cron/fixtures.json"
  [molecule_runtime/contracts/schedule.schema.json]="contracts/schedule/schedule.schema.json"
  [molecule_runtime/contracts/schedule.fixtures.json]="contracts/schedule/fixtures.json"
  [molecule_runtime/contracts/agent-trace.schema.json]="contracts/workspace-comms/agent-trace.schema.json"
  [molecule_runtime/contracts/agent-trace.contract.json]="contracts/workspace-comms/agent-trace.contract.json"
  [molecule_runtime/contracts/native-plugins.registry.json]="contracts/plugin/native-plugins.registry.json"
  [molecule_runtime/contracts/branding.contract.json]="contracts/branding/branding.contract.json"
  [molecule_runtime/contracts/plugin-install-report.contract.json]="contracts/plugin-install-report/plugin-install-report.contract.json"
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
  # NOTE: no -f. -f collapses "HTTP 404" and "the TLS handshake never completed"
  # into the same non-zero exit, and those two need opposite verdicts.
  http_code="$(curl -sS -L --max-redirs 5 --max-time 30 -A "$UA" \
    -w '%{http_code}' -o "$tmp/$slug" "$BASE/$remote_path" 2>"$tmp/$slug.curlerr")"
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    # Genuine transport failure: DNS, TLS, connection reset, timeout. Nothing was
    # learned about the mirror, so assert nothing about it.
    echo "::warning::could not reach molecule-ai-sdk for $remote_path (curl exit $curl_rc: $(tr -d '\n' < "$tmp/$slug.curlerr")) — skipping drift check for $local_rel"
    fetch_fail=1
    continue
  fi
  case "$http_code" in
    2??) : ;;  # fetched — fall through to the diff
    4??)
      # The forge answered, and its answer was "that path is not there". That is
      # a MAP bug or an un-followed SDK rename, not infra, and it is the failure
      # mode that turns a mirror into an unwatched fork. Loud.
      echo "::error::HTTP $http_code for $BASE/$remote_path — the SDK path mapped for $local_rel does not resolve."
      echo "::error::  The mirror is therefore UNCHECKED, not in sync. Fix the MAP entry (renamed/moved in molecule-ai-sdk?) or re-vendor per molecule_runtime/contracts/PROVENANCE.md."
      drift=1
      continue
      ;;
    *)
      # 5xx / 000 / anything else: the forge is unwell, which is infra.
      echo "::warning::HTTP $http_code fetching $remote_path from molecule-ai-sdk main (forge-side) — skipping drift check for $local_rel"
      fetch_fail=1
      continue
      ;;
  esac
  if diff -u "$local_file" "$tmp/$slug" > "$tmp/$slug.diff"; then
    echo "OK   $local_rel == molecule-ai-sdk:$remote_path"
  else
    echo "::error::DRIFT $local_rel has drifted from molecule-ai-sdk:$remote_path"
    cat "$tmp/$slug.diff"
    drift=1
  fi
done

if [ "$drift" -ne 0 ]; then
  echo "::error::Vendored schemas are out of sync with molecule-ai-sdk (contracts/) main, or a mapped SDK path no longer resolves."
  echo "Re-vendor per molecule_runtime/contracts/PROVENANCE.md and bump the source-commit SHAs; if the SDK moved a file, fix the MAP entry in this script too."
  exit 1
fi
if [ "$fetch_fail" -ne 0 ]; then
  echo "::warning::Some schemas could not be fetched; drift check was partial (soft skip)."
  exit 2
fi
echo "All vendored schemas are in sync with molecule-ai-sdk (contracts/) main."
exit 0
