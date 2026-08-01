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
# PRIVATE-INDEX ONLY (#393). This bake used to point ONLY the @molecule-ai SCOPE
# at our registry, so `@molecule-ai/mcp-server` came from us but its TRANSITIVE
# tree (@modelcontextprotocol/sdk, pino, zod, express, ...) was pulled LIVE from
# registry.npmjs.org on every template image build. That took CI down on
# 2026-08-01 (ETIMEDOUT on registry.npmjs.org/@modelcontextprotocol%2fsdk), and
# an npmjs.org outage blocks producing a workspace image AT ALL — including an
# urgent security fix. Two changes close it, mirroring how MOLECULE_RUNTIME_INDEX
# already points pip at our own PyPI index:
#
#   (a) `npm_config_registry` makes OUR registry the DEFAULT for this bake, not
#       just the scope. npm has no upstream fallback, so a package missing from
#       the mirror is a hard 404 that FAILS THE BUILD — it can never silently
#       resolve from npmjs.org (the dependency-confusion surface we already
#       closed on the PyPI side). BUILD-TIME ENV ONLY, deliberately: writing a
#       default registry into the agent's ~/.npmrc would also redirect the
#       AGENT's own `npm install` in the workspace at our sparse mirror and
#       break ordinary user projects. The agent npmrc stays SCOPED-ONLY.
#   (b) `npm ci` from a VENDORED LOCKFILE (scripts/mgmt-mcp-lock/) instead of
#       `npm install <spec>`. `npm install` re-resolved every transitive RANGE at
#       build time — `@modelcontextprotocol/sdk` floats on `^1.12.0` — so two
#       builds of the same template commit were NOT guaranteed to contain the
#       same MCP server. The lockfile pins all ~120 packages by exact version +
#       sha512, and every `resolved` URL in it points at our registry (a lock
#       `resolved` URL OVERRIDES the configured registry, so this — not the
#       .npmrc — is what actually forecloses the upstream fetch).
#
# The lockfile is regenerated ONLY by scripts/mirror_mgmt_mcp_npm.py, which also
# mirrors the tree into our registry. Re-run it whenever the pinned version moves.
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
UPSTREAM_HOST="$(_read MANAGEMENT_MCP_UPSTREAM_REGISTRY_HOST)"
TOOL="$(_read REQUIRED_TOOL)"
SPEC="${PKG}@${VER}"
# The vendored PIN that ships in the wheel (package-data scripts/mgmt-mcp-lock/*.json).
LOCK_DIR="$("$_py" -c "import molecule_runtime, os; from molecule_runtime.platform_agent_identity import MANAGEMENT_MCP_LOCK_DIR as d; print(os.path.join(os.path.dirname(molecule_runtime.__file__), d))")"
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
command -v node >/dev/null 2>&1 || { echo "prebake-mgmt-mcp: node not reachable (set MOLECULE_PREBAKE_NODE_BIN=<node bin dir>)" >&2; exit 1; }

# --- the vendored pin must be present and must BE the pin ---------------------
# A wheel built without the package-data entry, or a version bump that forgot to
# re-run the mirror, must fail HERE with a named cause — not 200 lines later as
# an opaque resolve error, and never by falling back to a live upstream fetch.
[ -f "${LOCK_DIR}/package.json" ] && [ -f "${LOCK_DIR}/package-lock.json" ] \
  || { echo "prebake-mgmt-mcp: ERROR — vendored npm pin missing at ${LOCK_DIR} (package.json + package-lock.json); the wheel did not ship scripts/mgmt-mcp-lock/*.json" >&2; exit 1; }
_locked_version="$(node -e 'const p=require(process.argv[1]);process.stdout.write(String(p.dependencies&&p.dependencies[process.argv[2]]||""))' "${LOCK_DIR}/package.json" "${PKG}")"
[ "${_locked_version}" = "${VER}" ] \
  || { echo "prebake-mgmt-mcp: ERROR — pin drift: ${LOCK_DIR}/package.json pins ${PKG}@${_locked_version:-<none>} but MANAGEMENT_MCP_PINNED_VERSION is ${VER}; re-run scripts/mirror_mgmt_mcp_npm.py" >&2; exit 1; }
# Every `resolved` URL must be OUR registry. This is the assertion that actually
# forecloses npmjs.org: a lock `resolved` URL overrides the configured registry,
# so one upstream URL left in the lock silently reopens the live dependency.
grep -q "${UPSTREAM_HOST}" "${LOCK_DIR}/package-lock.json" \
  && { echo "prebake-mgmt-mcp: ERROR — ${LOCK_DIR}/package-lock.json still references ${UPSTREAM_HOST}; a lock 'resolved' URL overrides the registry, so the build would fetch from upstream. Re-run scripts/mirror_mgmt_mcp_npm.py" >&2; exit 1; } || true

echo "prebake-mgmt-mcp: baking ${SPEC} (launch range ${RANGE}) into ${CACHE} from ${REG} (mirror-only, no npmjs.org)"
mkdir -p "${CACHE}"
# NPM config WITHOUT `npm config set --global`. The --global write targets the
# node-prefix etc/npmrc, which is ROOT-owned on any template that installs node
# system-wide (apt -> /usr/local or /etc/npmrc). The prebake runs as the NON-ROOT
# agent user, so --global EACCES-fails the image build there (it worked only on
# templates whose node prefix is agent-writable, e.g. hermes ~/.local — the
# regression this replaces). Instead:
#  - npm cache location: `npm_config_cache` env (a VALID shell identifier),
#    honored by npm/npx under any $HOME with no file write.
#  - DEFAULT registry for THIS BAKE: `npm_config_registry` env (also a valid
#    shell identifier). Build-time only — see the PRIVATE-INDEX ONLY note above.
#  - scoped @molecule-ai registry: the per-user npmrc FILE (agent-writable). It
#    can NOT be an env var — `npm_config_@molecule-ai:registry` is an invalid
#    shell identifier (breaks `export`) and npx ignores it as an env var anyway.
# The LAUNCH points npx at this exact file via NPM_CONFIG_USERCONFIG (plugin
# fragment env) so the scoped registry + cache resolve regardless of the $HOME
# the runtime spawns the mgmt-MCP under (observed /root). SSOT values; anonymous.
export npm_config_cache="${CACHE}"
export npm_config_registry="${REG}"
printf '%s:registry=%s\n' "${SCOPE}" "${REG}" > "${AGENT_NPMRC}"

# Warm-install into a throwaway dir, then DISCARD it BEFORE the seeding npx runs
# from a clean cwd (openclaw #210 ordering: a warm node_modules in cwd poisons
# the _npx cache entry that a later --offline resolve reads from).
#
# `npm ci` (NOT `npm install <spec>`): it installs EXACTLY the vendored lock —
# every version and sha512 fixed — instead of re-resolving ~120 transitive
# semver ranges against whatever upstream published today. That is the
# reproducibility property #393 asks for: same runtime release => same tree.
warm="$(mktemp -d)"; cp "${LOCK_DIR}/package.json" "${LOCK_DIR}/package-lock.json" "$warm/"; cd "$warm"
npm ci --no-audit --no-fund --loglevel=error
# `npm ci` populates TARBALLS but not PACKUMENTS — it resolves straight from the
# lock's `resolved` URLs and never asks the registry "what versions exist?". The
# offline launch DOES ask (it resolves `<PKG>@<RANGE>`), so without this warm the
# self-check below dies ENOTCACHED. Bounded BY THE LOCK: only names+versions the
# pin already fixed are fetched, so this cannot widen what the bake pulls.
node -e '
const lock = require(process.argv[1]).packages;
const specs = new Set();
for (const [p, e] of Object.entries(lock)) {
  if (!p || !e.resolved) continue;
  specs.add((e.name || p.split("node_modules/").pop()) + "@" + e.version);
}
process.stdout.write([...specs].join("\n") + "\n");
' "${LOCK_DIR}/package-lock.json" | xargs -n 20 npm cache add --loglevel=error
cd "${HOME}"; rm -rf "$warm"

# Seed the _npx cache for BOTH the exact spec and the LAUNCH RANGE. The launch
# runs `npx @PKG@RANGE`, whose _npx entry is keyed by the RANGE string — a
# different key from the exact spec — so seeding only the exact spec would leave
# the launch to re-resolve. Best-effort: the hard gate is the self-check below.
for _seed in "${SPEC}" "${PKG}@${RANGE}"; do
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"prebake","version":"1"}}}' \
    | MOLECULE_MCP_MODE=management timeout 60 npx -y --prefer-offline "${_seed}" >/dev/null 2>&1 || true
done

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
# (the baked scoped-registry npmrc) — and WITHOUT this bake's npm_config_registry,
# which is build-time-only and must therefore not be load-bearing for the launch.
# REGRESSION GUARD for the exact fail-close class this fixes: the runtime spawns
# the mgmt-MCP under a non-agent HOME (observed /root); the config travels via
# these two env vars (NOT --global, NOT a HOME-local ~/.npmrc, NOT the
# npx-ignored scoped-registry env var), so this proves the LAUNCH resolves
# regardless of $HOME without any root-owned write.
_foreign_home="$(mktemp -d)"
(
  unset npm_config_registry
  export HOME="${_foreign_home}"
  export npm_config_cache="${CACHE}"
  export NPM_CONFIG_USERCONFIG="${AGENT_NPMRC}"
  _prebake_self_check "${PKG}@${RANGE}"
) || { rm -rf "${_foreign_home}"; echo "prebake-mgmt-mcp: ERROR — ${PKG}@${RANGE} did not resolve OFFLINE under a foreign HOME with the launch env (npm_config_cache + NPM_CONFIG_USERCONFIG); the runtime would ETARGET (#1027) when it spawns the mgmt-MCP under a different HOME" >&2; exit 1; }
rm -rf "${_foreign_home}"

echo "prebake-mgmt-mcp: OK — ${SPEC} baked from the ${REG} mirror against the vendored lock; ${PKG}@${RANGE} resolves offline HOME-independently (npm_config_cache + NPM_CONFIG_USERCONFIG; no --global)"
