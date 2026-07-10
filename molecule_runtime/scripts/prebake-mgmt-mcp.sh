#!/usr/bin/env bash
# prebake-mgmt-mcp.sh — base-runtime pre-bake of the management-MCP npm server.
#
# THE SSOT PRE-BAKE (ADR-004 base-runtime default; task #54). Ships in the
# molecules-workspace-runtime wheel (pyproject package-data `scripts/*.sh`) so
# EVERY runtime template DELEGATES to it in ONE Dockerfile line instead of
# hand-copying a ~40-line bake block (the per-template fork this retires):
#
#   RUN bash "$(python3 -c 'import molecule_runtime,os;print(os.path.dirname(molecule_runtime.__file__))')/scripts/prebake-mgmt-mcp.sh"
#
# WHAT: warm the agent npm cache with @molecule-ai/mcp-server@<PIN> so the
# concierge boot `npx --prefer-offline @molecule-ai/mcp-server@<PIN>` resolves
# ENTIRELY OFFLINE. A missing/stale bake cold-pulls -> ETARGET / CF-WAF throttle
# -> #1027 "management MCP FAILED TO LAUNCH" fail-close (launch-side of RCA #2970;
# the fetch-side is the git-native plugin source).
#
# SSOT: the package, pinned version, and registry come from the runtime's
# contract-pinned constants (molecule_runtime.platform_agent_identity), which are
# Guard-D-locked to contracts/mcp-plugin-delivery.contract.json
# (management_mcp_server). NO version is hand-typed here — bump the SDK contract,
# re-vendor, re-release the runtime, rebuild the templates.
#
# RUN AS the agent user (uid 1000) so the cache lands in the /home/agent/.npm the
# gosu-dropped runtime reads at boot. Node must be reachable; a template whose
# node lives off-PATH (e.g. hermes ~/.hermes/node/bin) exports
# MOLECULE_PREBAKE_NODE_BIN=<dir> before calling — the ONLY override a template
# should ever need ("usually should not need to").
set -eu

# The one sanctioned per-template override: a node bin dir not already on PATH.
if [ -n "${MOLECULE_PREBAKE_NODE_BIN:-}" ]; then
  PATH="${MOLECULE_PREBAKE_NODE_BIN}:${PATH}"
  export PATH
fi

_py="${MOLECULE_RUNTIME_PYTHON:-python3}"
_read() { "$_py" -c "from molecule_runtime.platform_agent_identity import $1 as v; print(v)"; }
PKG="$(_read MANAGEMENT_MCP_NPM_PACKAGE)"
VER="$(_read MANAGEMENT_MCP_PINNED_VERSION)"
REG="$(_read MANAGEMENT_MCP_REGISTRY)"
SCOPE="$(_read MANAGEMENT_MCP_REGISTRY_SCOPE)"
TOOL="$(_read REQUIRED_TOOL)"
SPEC="${PKG}@${VER}"

command -v npm >/dev/null 2>&1 || { echo "prebake-mgmt-mcp: npm not reachable (set MOLECULE_PREBAKE_NODE_BIN=<node bin dir>)" >&2; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "prebake-mgmt-mcp: npx not reachable (set MOLECULE_PREBAKE_NODE_BIN=<node bin dir>)" >&2; exit 1; }

echo "prebake-mgmt-mcp: baking ${SPEC} from ${REG}"
mkdir -p "${HOME}/.npm"
# Scoped @molecule-ai registry (anonymous read of the public org package). No
# token — the fetch is anonymous, same posture as the git-native plugin source.
printf '%s:registry=%s\n' "${SCOPE}" "${REG}" > "${HOME}/.npmrc"

# Warm-install into a throwaway dir, then DISCARD it BEFORE the seeding npx runs
# from a clean cwd (openclaw #210 ordering: a warm node_modules in cwd poisons
# the _npx cache entry that a later --offline resolve reads from).
warm="$(mktemp -d)"; cd "$warm"; npm init -y >/dev/null 2>&1
npm install --no-audit --no-fund --loglevel=error "${SPEC}"
cd "${HOME}"; rm -rf "$warm"

# Seed the _npx cache (best-effort; network is allowed at build time).
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"prebake","version":"1"}}}' \
  | MOLECULE_MCP_MODE=management timeout 60 npx -y --prefer-offline "${SPEC}" >/dev/null 2>&1 || true

# HARD offline self-check: the OFFLINE launch must expose the degrade-gate verb
# (REQUIRED_TOOL == provision_workspace). Fails the image build if the bake is
# broken — so a broken bake can never ship.
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | MOLECULE_MCP_MODE=management timeout 60 npx -y --offline "${SPEC}" 2>/dev/null | grep -q "${TOOL}" \
  || { echo "prebake-mgmt-mcp: ERROR — ${SPEC} did not resolve OFFLINE or ${TOOL} missing; the concierge warm-up bake is broken" >&2; exit 1; }

echo "prebake-mgmt-mcp: OK — ${SPEC} resolves offline with ${TOOL}"
