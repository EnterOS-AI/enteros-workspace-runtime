#!/usr/bin/env bash
# Verify that the runtime's vendored channel client is an exact SDK copy.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDORED="$REPO_ROOT/molecule_runtime/channel_sdk.py"
SOURCE="${1:-${MOLECULE_CHANNEL_SDK_SOURCE:-}}"

if [[ -z "$SOURCE" ]]; then
  echo "usage: $0 <molecule-ai-sdk checkout or molecule_plugin/channel.py>" >&2
  exit 2
fi
if [[ -d "$SOURCE" ]]; then
  SOURCE="$SOURCE/molecule_plugin/channel.py"
fi
if [[ ! -f "$SOURCE" ]]; then
  echo "channel SDK source not found: $SOURCE" >&2
  exit 2
fi
if [[ ! -f "$VENDORED" ]]; then
  echo "vendored channel SDK missing: $VENDORED" >&2
  exit 1
fi

if cmp -s "$SOURCE" "$VENDORED"; then
  echo "channel SDK vendor is byte-identical: $SOURCE"
  exit 0
fi

echo "::error::molecule_runtime/channel_sdk.py has drifted from $SOURCE" >&2
diff -u "$SOURCE" "$VENDORED" || true
exit 1
