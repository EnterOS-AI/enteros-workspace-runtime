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
Source commit:        `a3d70972ee082a8d862fd083ec6f92bbea133185` (SDK RuntimeId became an open bounded/path-safe slug; official runtime support moved to the adapter registry)
Vendored at sdk HEAD: `656eb86eb53d00718fa5a8f60bf98cc01ccf3353` (`sdk-v0.5.2`, main)

Why vendored: `molecule_runtime/manifest_ssot.py` validates plugin manifests
against this schema at plugin **load** (`plugins.load_plugin_manifest`) and at
**install** (`plugin_sources.install_declared_plugins`) — the ADVISORY phase of
molecule-core#3383. Validation runs inside workspace containers at boot.

Re-vendoring:

    curl -fsS -A "curl/8.4.0" \
      https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/plugin-manifest/plugin-manifest.schema.json \
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
