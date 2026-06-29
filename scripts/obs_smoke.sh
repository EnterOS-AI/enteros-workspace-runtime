#!/usr/bin/env bash
# obs_smoke.sh — LIVE Langfuse trace smoke (local stack / dispatch lane).
#
# Emits a real trace through the runtime's Langfuse OTLP bridge and asserts it
# lands in Langfuse, keyed off a unique run-id. This is the live counterpart to
# the per-PR hermetic emission test (tests/test_langfuse_emission.py): together
# they make a broken/disconnected Langfuse fail LOUD instead of silently
# dropping every agent trace.
#
# SSOT, NO HARDCODING: the Langfuse host + keys are derived from Infisical
# (/shared/observability) when an INFISICAL_* identity is present, else from the
# already-exported LANGFUSE_* env. Nothing is baked in.
#
# GATED WHEN OFF: if the keys can't be resolved OR Langfuse is unreachable, the
# smoke SKIPs (exit 0) — so a stack with Langfuse intentionally off (the
# fail-open posture) does NOT turn the lane red. It only FAILS (exit 1) when
# Langfuse IS reachable+configured but the emitted trace never shows up.
#
# Usage:
#   bash scripts/obs_smoke.sh
# Env (all optional — resolved from Infisical when absent):
#   LANGFUSE_HOST_LOCAL    host-side URL to QUERY Langfuse (e.g. http://localhost:3001)
#   LANGFUSE_HOST          in-network URL the EMITTER pushes to (e.g. http://langfuse-web:3000)
#   LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
#   INFISICAL_API / INFISICAL_TOKEN / INFISICAL_WORKSPACE_ID / INFISICAL_OBS_ENV(=prod)
#   OBS_SMOKE_RUNTIME_IMAGE (default workspace-template-claude-code:latest)
#   OBS_SMOKE_NETWORK       (default molecule-obs-net)
#   OBS_SMOKE_TIMEOUT_S     (default 60)
set -euo pipefail

skip() { echo "obs-smoke: SKIP — $*"; exit 0; }
fail() { echo "obs-smoke: FAIL — $*" >&2; exit 1; }

INFISICAL_OBS_ENV="${INFISICAL_OBS_ENV:-prod}"
OBS_SMOKE_RUNTIME_IMAGE="${OBS_SMOKE_RUNTIME_IMAGE:-workspace-template-claude-code:latest}"
OBS_SMOKE_NETWORK="${OBS_SMOKE_NETWORK:-molecule-obs-net}"
OBS_SMOKE_TIMEOUT_S="${OBS_SMOKE_TIMEOUT_S:-60}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Resolve keys from Infisical SSOT when not already exported -------------
inf_get() { # $1=secretName -> prints value or empty
  [ -n "${INFISICAL_API:-}" ] && [ -n "${INFISICAL_TOKEN:-}" ] && [ -n "${INFISICAL_WORKSPACE_ID:-}" ] || return 0
  curl -fsS "$INFISICAL_API/v3/secrets/raw/$1?workspaceId=$INFISICAL_WORKSPACE_ID&environment=$INFISICAL_OBS_ENV&secretPath=%2Fshared%2Fobservability" \
    -H "Authorization: Bearer $INFISICAL_TOKEN" 2>/dev/null \
    | sed -nE 's/.*"secretValue":"([^"]*)".*/\1/p'
}
: "${LANGFUSE_PUBLIC_KEY:=$(inf_get LANGFUSE_PUBLIC_KEY)}"
: "${LANGFUSE_SECRET_KEY:=$(inf_get LANGFUSE_SECRET_KEY)}"
: "${LANGFUSE_HOST:=$(inf_get LANGFUSE_HOST)}"
: "${LANGFUSE_HOST_LOCAL:=$(inf_get LANGFUSE_HOST_LOCAL)}"
LANGFUSE_HOST="${LANGFUSE_HOST:-http://langfuse-web:3000}"
LANGFUSE_HOST_LOCAL="${LANGFUSE_HOST_LOCAL:-http://localhost:3001}"

[ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ] \
  || skip "no LANGFUSE_* keys (Infisical /shared/observability empty and env unset) — Langfuse off"

# --- Gate on Langfuse reachability ------------------------------------------
health=$(curl -s -o /dev/null -w '%{http_code}' "$LANGFUSE_HOST_LOCAL/api/public/health" 2>/dev/null || true)
[ "$health" = "200" ] || skip "Langfuse not reachable at $LANGFUSE_HOST_LOCAL (health=$health) — off"

command -v docker >/dev/null 2>&1 || skip "docker unavailable — cannot drive the runtime emitter"

# --- Emit a real trace through the runtime telemetry path -------------------
RUNID="smk-$(date +%s)-${RANDOM:-0}$$"
echo "obs-smoke: emitting trace obs-smoke-$RUNID via $OBS_SMOKE_RUNTIME_IMAGE (host=$LANGFUSE_HOST)"
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --network "$OBS_SMOKE_NETWORK" \
  -e RUNID="$RUNID" \
  -e LANGFUSE_HOST="$LANGFUSE_HOST" \
  -e LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY" \
  -e LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY" \
  -e WORKSPACE_ID="obs-smoke" \
  -v "$SCRIPT_DIR/_obs_emit_trace.py:/tmp/_obs_emit_trace.py:ro" \
  --entrypoint python "$OBS_SMOKE_RUNTIME_IMAGE" /tmp/_obs_emit_trace.py >/dev/null 2>&1 \
  || fail "emitter container failed (image=$OBS_SMOKE_RUNTIME_IMAGE, net=$OBS_SMOKE_NETWORK)"

# --- Assert the trace shows up in Langfuse for this run-id -------------------
deadline=$(( $(date +%s) + OBS_SMOKE_TIMEOUT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
       "$LANGFUSE_HOST_LOCAL/api/public/traces?limit=25&orderBy=timestamp.desc" 2>/dev/null \
       | grep -q "obs-smoke-$RUNID"; then
    echo "obs-smoke: PASS — trace obs-smoke-$RUNID visible in Langfuse"
    exit 0
  fi
  sleep 3
done
fail "trace obs-smoke-$RUNID never appeared in Langfuse within ${OBS_SMOKE_TIMEOUT_S}s (emission path broken/disconnected)"
