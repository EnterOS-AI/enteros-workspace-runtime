# Vendored contract schemas (SSOT mirrors)

The files in this directory are **byte-for-byte copies** of contract SSOT files
that live in the `molecule-ai-sdk` repository. They are vendored (not fetched at
runtime) so validation/consumption resolves fully offline inside workspace
containers — no cross-repo clone, no token, no network. They ship in the wheel
via `[tool.setuptools.package-data]` (`contracts/*.json` + `contracts/*.md`) and
are loaded with `importlib.resources`.

Each is an SSOT **mirror**, not a fork: it MUST stay byte-identical to the sdk
original. `scripts/check-schemas-in-sync.sh` (wired into
`.gitea/workflows/schema-sync.yml`) re-fetches each sdk original and diffs,
failing the build on drift (mirrors the molecule-ci vendored-schema drift gate
and tests/fixtures/workspace_comms/PROVENANCE.md). To update any file, re-fetch
it at the new sdk main and bump its SHAs below.

---

## `plugin-manifest.schema.json`

Byte-for-byte copy of:

    molecule-ai-sdk/contracts/plugin-manifest/plugin-manifest.schema.json

Source repo:          https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:          contracts/plugin-manifest/plugin-manifest.schema.json
Source commit:        `92f8a88a60b0d4c337278b3bc52f549de270f6bf` (sdk#119 — plugin MCP audience contract: contract-version 0.5.0 adds the scalar `audience` (self|org) to an mcpServers contribution, generalizing `privileged` into a declared audience→credential mapping; self-schedule v1 foundation)
Vendored at sdk HEAD: `92f8a88a60b0d4c337278b3bc52f549de270f6bf` (branch `feat/mcp-audience-contract`, PENDING MERGE)

GATED (sdk#119 audience contract): the SDK commit above is a HELD draft and not
yet on sdk `main`. Re-vendored byte-for-byte from that branch (content sha256
`e4031f178b124babbb80af39827eccde7b07df44caaf45fdfb4977b0bf327267`). Until
sdk#119 merges, `scripts/check-schemas-in-sync.sh` REPORTS DRIFT for this file
(it diffs against sdk `main`, which still carries contract-version 0.4.0) — that
is expected and correct for this HELD runtime PR, which DEPENDS ON sdk#119. When
the SDK contract merges, reconcile the two SHAs above to the sdk `main` merge
commit; the file content is unchanged, so the drift gate goes green the moment
sdk#119 lands.

Why vendored: `molecule_runtime/manifest_ssot.py` validates plugin manifests
against this schema at plugin **load** (`plugins.load_plugin_manifest`) and at
**install** (`plugin_sources.install_declared_plugins`) — the ADVISORY phase of
molecule-core#3383. Validation runs inside workspace containers at boot.

Re-vendoring (from the sdk#119 branch while it is HELD; drop `-b …` once merged):

    curl -fsS -A "curl/8.4.0" \
      https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/feat/mcp-audience-contract/contracts/plugin-manifest/plugin-manifest.schema.json \
      -o molecule_runtime/contracts/plugin-manifest.schema.json

---

## `idle-prompt.schema.json` + `idle-prompt.contract.json`

Byte-for-byte copies of the idle-prompt digest contract layer (task #219):

    molecule-ai-sdk/contracts/idle-prompt/idle-prompt.schema.json
    molecule-ai-sdk/contracts/idle-prompt/idle-prompt.contract.json

Source repo:            https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path (schema):   contracts/idle-prompt/idle-prompt.schema.json
Source path (instance): contracts/idle-prompt/idle-prompt.contract.json
Source commit:          `f2bc47ec04279fe98f46447a07eee03ba28d1a7a` (ai-sdk#57 — the layer's introducing commit; both files landed together)
Vendored at sdk HEAD:   `f2bc47ec04279fe98f46447a07eee03ba28d1a7a` (main)

Why vendored: `molecule_runtime/idle_digest/contract.py` `Policy.default()` loads
the operator-ruled production policy values (idle-fire threshold, size limits,
provider timeout, stale thresholds, third-party tier) from the vendored
**instance** (`idle-prompt.contract.json`) via `importlib.resources`, offline —
the contract is the SSOT for those values; the `MOLECULE_IDLE_*` env vars remain
the test/staging retune knobs. The schema is vendored alongside so the drift gate
can prove both stay byte-identical to sdk main.

Both files are on the drift gate (`check-schemas-in-sync.sh`), so the vendored
**instance is a pure mirror** — its values cannot diverge from sdk main; a
runtime that needs different values uses the env overrides, never an edit here.

Re-vendoring (schema + instance):

    for f in schema contract; do
      curl -fsS -A "curl/8.4.0" \
        "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/idle-prompt/idle-prompt.$f.json" \
        -o "molecule_runtime/contracts/idle-prompt.$f.json"
    done

---

## `workspace-data.contract.json`

Byte-for-byte copy of:

    molecule-ai-sdk/contracts/workspace-data/workspace-data.contract.json

Source repo:          https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:          contracts/workspace-data/workspace-data.contract.json
Source commit:        `aac88bd0227f690d9b0e37c6a3f7a338aa4845b9` (ai-sdk#61 — verify per-merge not nightly; supersedes #60)
Vendored at sdk HEAD: `aac88bd0227f690d9b0e37c6a3f7a338aa4845b9` (main)

Why vendored: `molecule_runtime/mailbox_dir.py` `verify_durability` reads the
provider-agnostic snapshot-durability signals from the **instance** —
`persisted_paths` (does the mailbox base live under an archived path?),
`box_env.snapshot_uri` (the in-container env var proving CP wired R2 snapshot for
this workspace), and `durability_signal.disabled_marker` — to credit a third
`snapshot-durable` state, so the guard does not false-warn EPHEMERAL on a
boot-disk provider (Hetzner/GCP) whose `/workspace` is durable via R2
snapshot/restore rather than a live block mount. Loaded offline via
`importlib.resources`; the contract is the SSOT for these constants.

Instance only (no schema mirror): the runtime reads constants, it does not
JSON-Schema-validate this contract at boot. On the drift gate
(`check-schemas-in-sync.sh`), so the mirror stays byte-identical to sdk main.

Re-vendoring (instance):

    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/workspace-data/workspace-data.contract.json" \
      -o "molecule_runtime/contracts/workspace-data.contract.json"

## credentials.contract.json

Source: molecule-ai-sdk `contracts/credentials/credentials.contract.json`.
Source commit: `d464fc36f599757b4597bb14ef4e53ad25acc11e` (sdk#97 — declares
shown-once credential disclosure and fail-closed persistence rules).
Vendored at SDK main: `0ff6e1bf09c2be6d08b56a53e88cffd7354ef9b0`.
This is the root-level credential/privilege SSOT.

Why vendored: `tests/test_privileged_mcp_env.py` asserts `privileged_mcp_env`
forwards EVERY env key in `management_mcp_env.required` — the enforcement that
would have caught the concierge AUTH_ERROR (the forward-allowlist carried the
unprefixed `ORG_API_KEY` and stripped the canonical `MOLECULE_ORG_API_KEY` the
mcp-server reads). Drift-gated byte-identical to sdk main via
`check-schemas-in-sync.sh`.

Re-vendoring:

    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/credentials/credentials.contract.json" \
      -o "molecule_runtime/contracts/credentials.contract.json"

## cron.fixtures.json

Source: molecule-ai-sdk `contracts/cron/fixtures.json` (the `cron` contract's
executable behavioural SSOT — a list of `{expr, tz, after, expect}` rows).
Generated from `github.com/robfig/cron/v3` v3.0.1 (`NewParser(Minute|Hour|Dom|
Month|Dow)`), the shipping Go scheduler, so the contract cannot silently change
the fire time every existing schedule depends on.

Why vendored: `tests/test_cronspec_contract.py` asserts
`molecule_runtime.cronspec.compute_next_run` reproduces every row exactly — the
Python end of the cross-language equivalence gate with core
`internal/cronspec/cronspec_conformance_test.go` (which asserts the same
fixtures against robfig). A drift here is a real fire-time bug. Drift-gated
byte-identical to sdk main via `check-schemas-in-sync.sh`.

Re-vendoring:

    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/cron/fixtures.json" \
      -o "molecule_runtime/contracts/cron.fixtures.json"

## schedule.schema.json, schedule.fixtures.json

Source: molecule-ai-sdk `contracts/schedule/{schedule.schema.json, fixtures.json}`
— the `schedule` contract, SSOT for the volume-authoritative schedule grid a
`kind: trigger` scheduler plugin owns under Option A. Definition-only entries
(name, cron, timezone, prompt, enabled, source); engine bookkeeping is
daemon-owned in a separate state file, not in the grid.

Why vendored: `molecule_runtime/schedule_store.py` validates every write against
`$defs/scheduleEntry` / `$defs/scheduleGrid` and `tests/test_schedule_store.py`
asserts the fixtures' valid/invalid partition holds. Drift-gated byte-identical
to sdk main via `check-schemas-in-sync.sh`.

Re-vendoring:

    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/schedule/schedule.schema.json" \
      -o "molecule_runtime/contracts/schedule.schema.json"
    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/schedule/fixtures.json" \
      -o "molecule_runtime/contracts/schedule.fixtures.json"

## agent-trace.schema.json, agent-trace.contract.json

Source: molecule-ai-sdk `contracts/workspace-comms/agent-trace.{schema,contract}.json`
— the `AgentTrace` contract, SSOT for the per-turn agent trace this runtime
PRODUCES (`molecule_runtime/tracing.py`) and molecule-core's `/traces` proxy
reads back. Vendor-neutral trace DATA + the load-bearing tagging convention
(`tags=[workspace_id]`); Langfuse is the current concrete sink, unnamed by the
shape.

Why vendored: `tests/test_tracing.py` asserts `tracing.build_agent_trace(...)`
— the canonical record the producer assembles before mapping it onto the
Langfuse backend — validates against `$` (the vendored schema), and that the
golden `agent-trace.contract.json` example validates too. This closes the drift
loop: the producer's emitted shape cannot diverge from the SDK contract without
failing the test, and the vendored copy cannot diverge from sdk main without
failing `check-schemas-in-sync.sh`.

Source repo:          https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:          contracts/workspace-comms/agent-trace.{schema,contract}.json
Source commit:        `65ad524b6faeb8b45575a0dea715881730dfbd7e` (ai-sdk#65 — AgentTrace SSOT trace shape, SDK-first)
Vendored at sdk HEAD: `65ad524b6faeb8b45575a0dea715881730dfbd7e` (main)

Re-vendoring (schema + golden instance):

    for f in schema contract; do
      curl -fsS -A "curl/8.4.0" \
        "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/workspace-comms/agent-trace.$f.json" \
        -o "molecule_runtime/contracts/agent-trace.$f.json"
    done

## native-plugins.registry.json

Source: molecule-ai-sdk `contracts/plugin/native-plugins.registry.json` — the
SSOT for the set of platform-delivered first-party ("native") plugins and how
each installs (`install: default | concierge`). Core reads its generated
molcontracts binding to declare each plugin (molecule-core#4413); the runtime
reads the same registry to know which plugins are NATIVE.

Source repo:          https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:          contracts/plugin/native-plugins.registry.json
Source commit:        `40d58387951f74084a3f2c420a81a988ccf67c87` (sdk d3/registry-digest-v0.2.0 — bump digest plugins #v0.1.0 → #v0.2.0, D3 source-move)
Vendored at sdk HEAD: `40d58387951f74084a3f2c420a81a988ccf67c87` (branch d3/registry-digest-v0.2.0, PENDING MERGE)

GATED (D3 source-move): the SDK bump above is a HELD draft and not yet on
sdk `main`. Re-vendored byte-for-byte from that branch (content sha256
`bffb835fd9c4afc9390db8c24dbace453ec3cab148f571a945596bce8585a4e3`). When
the SDK bump merges, reconcile the two SHAs above to the sdk `main` merge
commit; the file content is unchanged so `check-schemas-in-sync.sh` goes
green against sdk main the moment the SDK bump lands.

Why vendored: `molecule_runtime/idle_digest/plugin_loader.py` sources the
D1 load-time TRUST allow-list (which plugin names may load an `official`/reserved
digest provider in-process) from this registry's plugin names, offline via
`importlib.resources` — replacing the interim `MOLECULE_NATIVE_PLUGIN_NAMES` env
knob. The registry is the SSOT for "which plugins are native"; a non-native
plugin shipping an official/reserved provider is refused at load. Drift-gated
byte-identical to sdk main via `check-schemas-in-sync.sh`.

Re-vendoring:

    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/plugin/native-plugins.registry.json" \
      -o "molecule_runtime/contracts/native-plugins.registry.json"

## mcp-plugin-delivery.contract.json  (vendored at repo-root `contracts/`)

Source: molecule-ai-sdk `contracts/mcp/mcp-plugin-delivery.contract.json` — the
cross-repo SSOT for the MCP-plugin delivery seam (`settings_path`, `mcpservers_key`,
`entry_shape`, the management-MCP server name/version/registry, and — as of
sdk#119 — the `audiences` map: each declared `audience` → its delivery
(`mcp_mode` + either an env-VALUE `credential_env` or a re-read FILE
`credential_file`/`token_file_env`) the runtime honors). NOTE: this file is vendored at the REPO-ROOT `contracts/`
dir (not `molecule_runtime/contracts/`) and is mapped under that repo-relative
key in `scripts/check-schemas-in-sync.sh`; it is the copy
`platform_agent_identity` + `tests/test_mcp_plugin_delivery_contract.py` pin
every management-MCP literal against.

Source repo:          https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:          contracts/mcp/mcp-plugin-delivery.contract.json
Source commit:        `69dbe781ed0ab0b65367844ed5a5aef7d759a491` (sdk#119 — adds the `audiences` delivery map: self→{mcp_mode:self, credential_file:/configs/.auth_token, token_file_env:MOLECULE_WORKSPACE_TOKEN_FILE} (FILE-delivered, rotation-safe), org→{mcp_mode:management, credential_env:MOLECULE_ORG_API_KEY})
Vendored at sdk HEAD: `69dbe781ed0ab0b65367844ed5a5aef7d759a491` (branch `feat/mcp-audience-contract`, PENDING MERGE)

GATED (sdk#119 audience contract): the SDK commit above is a HELD draft and not
yet on sdk `main`. Re-vendored byte-for-byte from that branch (content sha256
`060e5c3818aec81779f11e4cdb2aa234ed2405f61cb112c46c713e857552a57f`). Until
sdk#119 merges, `scripts/check-schemas-in-sync.sh` REPORTS DRIFT for this file
(it diffs against sdk `main`, which lacks the `audiences` block) — expected and
correct for this HELD runtime PR, which DEPENDS ON sdk#119. When the SDK contract
merges, reconcile the two SHAs above to the sdk `main` merge commit; the content
is unchanged, so the drift gate goes green the moment sdk#119 lands.

NOTE — contract ⇄ runtime delivery (RECONCILED in sdk#119): the contract now
declares `self` as FILE-delivered — `credential_file: /configs/.auth_token` +
`token_file_env: MOLECULE_WORKSPACE_TOKEN_FILE` — matching EXACTLY what the
runtime injects. `privileged_mcp_env` sets
`MOLECULE_WORKSPACE_TOKEN_FILE=/configs/.auth_token` (the restart-ROTATED
in-container SSOT, `platform_auth.py`) and the mcp-server child re-reads the
current token per call. It does NOT inject the token VALUE
(`MOLECULE_WORKSPACE_TOKEN`) — an env snapshot would 401 after the next rotation
(RFC review BLOCKER). Earlier revisions of this contract mis-declared
`credential_env: MOLECULE_WORKSPACE_TOKEN` for `self` (documentation only —
nothing machine-reads the audiences map); sdk#119 corrects that to the file
fields so a future consumer cannot be misled into injecting the value.

Re-vendoring (from the sdk#119 branch while it is HELD; drop `-b …` once merged):

    curl -fsS -A "curl/8.4.0" \
      "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/feat/mcp-audience-contract/contracts/mcp/mcp-plugin-delivery.contract.json" \
      -o "contracts/mcp-plugin-delivery.contract.json"
