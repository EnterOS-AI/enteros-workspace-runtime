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
RANGE="$(_read MANAGEMENT_MCP_COMPATIBLE_RANGE)"
REG="$(_read MANAGEMENT_MCP_REGISTRY)"
SCOPE="$(_read MANAGEMENT_MCP_REGISTRY_SCOPE)"
TOOL="$(_read REQUIRED_TOOL)"
SPEC="${PKG}@${VER}"
# The agent npm cache the bake warms + the launch reads. Under `USER agent` the
# build HOME is the agent home, so ${HOME}/.npm is /home/agent/.npm — the same
# path the plugin fragment injects as npm_config_cache at LAUNCH.
CACHE="${HOME}/.npm"
# The per-user npmrc that supplies the scoped @molecule-ai registry. Written here
# under the agent HOME; the LAUNCH reads it HOME-independently by pointing npx at
# it with NPM_CONFIG_USERCONFIG (plugin fragment env). This FILE — not an env var
# — is how the scoped registry travels: `npm_config_<scope>:registry` is not a
# valid shell identifier (@/: ) AND npx ignores it as an env var.
AGENT_NPMRC="${HOME}/.npmrc"

command -v npm >/dev/null 2>&1 || { echo "prebake-mgmt-mcp: npm not reachable (set MOLECULE_PREBAKE_NODE_BIN=<node bin dir>)" >&2; exit 1; }
command -v npx >/dev/null 2>&1 || { echo "prebake-mgmt-mcp: npx not reachable (set MOLECULE_PREBAKE_NODE_BIN=<node bin dir>)" >&2; exit 1; }

echo "prebake-mgmt-mcp: baking ${SPEC} (launch range ${RANGE}) into ${CACHE}"
mkdir -p "${CACHE}"
# NPM config WITHOUT `npm config set --global`. The --global write targets the
# node-prefix etc/npmrc, which is ROOT-owned on any template that installs node
# system-wide (apt -> /usr/local or /etc/npmrc). The prebake runs as the NON-ROOT
# agent user, so --global EACCES-fails the image build there (it worked only on
# templates whose node prefix is agent-writable, e.g. hermes ~/.local — the
# regression this replaces). Instead:
#  - npm cache location: `npm_config_cache` env (a VALID shell identifier),
#    honored by npm/npx under any $HOME with no file write.
#  - scoped @molecule-ai registry: the per-user npmrc FILE (agent-writable). It
#    can NOT be an env var — `npm_config_@molecule-ai:registry` is an invalid
#    shell identifier (breaks `export`) and npx ignores it as an env var anyway.
# The LAUNCH points npx at this exact file via NPM_CONFIG_USERCONFIG (plugin
# fragment env) so the scoped registry + cache resolve regardless of the $HOME
# the runtime spawns the mgmt-MCP under (observed /root). SSOT values; anonymous.
export npm_config_cache="${CACHE}"
printf '%s:registry=%s\n' "${SCOPE}" "${REG}" > "${AGENT_NPMRC}"

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
# broken — so a broken bake can never ship. Takes the spec to resolve, so we can
# assert BOTH the exact baked version AND the launch RANGE.
_prebake_self_check() {
  printf '%s\n%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    | MOLECULE_MCP_MODE=management timeout 60 npx -y --offline "$1" 2>/dev/null | grep -q "${TOOL}"
}
# (1) under the build HOME + build env: the exact baked version resolves.
_prebake_self_check "${SPEC}" \
  || { echo "prebake-mgmt-mcp: ERROR — exact ${SPEC} did not resolve OFFLINE or ${TOOL} missing (build HOME); the bake is broken" >&2; exit 1; }
# (2) the LAUNCH RANGE resolves offline to the baked version — this is what the
# concierge boot actually runs (`npx @${PKG}@${RANGE}`), so it must resolve to a
# baked in-range version with zero network.
_prebake_self_check "${PKG}@${RANGE}" \
  || { echo "prebake-mgmt-mcp: ERROR — launch range ${PKG}@${RANGE} did not resolve OFFLINE (no baked version satisfies it); the concierge boot would ETARGET" >&2; exit 1; }
# (3) the RANGE resolves under a FOREIGN HOME *with the exact launch env the
# plugin fragment injects*: npm_config_cache (the baked cache) + NPM_CONFIG_USERCONFIG
# (the baked scoped-registry npmrc). REGRESSION GUARD for the exact fail-close
# class this fixes: the runtime spawns the mgmt-MCP under a non-agent HOME
# (observed /root); the config travels via these two env vars (NOT --global, NOT
# a HOME-local ~/.npmrc, NOT the npx-ignored scoped-registry env var), so this
# proves the LAUNCH resolves regardless of $HOME without any root-owned write.
_foreign_home="$(mktemp -d)"
(
  export HOME="${_foreign_home}"
  export npm_config_cache="${CACHE}"
  export NPM_CONFIG_USERCONFIG="${AGENT_NPMRC}"
  _prebake_self_check "${PKG}@${RANGE}"
) || { rm -rf "${_foreign_home}"; echo "prebake-mgmt-mcp: ERROR — ${PKG}@${RANGE} did not resolve OFFLINE under a foreign HOME with the launch env (npm_config_cache + NPM_CONFIG_USERCONFIG); the runtime would ETARGET (#1027) when it spawns the mgmt-MCP under a different HOME" >&2; exit 1; }
rm -rf "${_foreign_home}"

echo "prebake-mgmt-mcp: OK — ${SPEC} baked; ${PKG}@${RANGE} resolves offline HOME-independently (npm_config_cache + NPM_CONFIG_USERCONFIG; no --global)"
