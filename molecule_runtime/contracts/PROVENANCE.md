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
Source commit:        `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` (last commit touching the schema — ai-sdk#53 dropped langgraph/autogen/gemini-cli/deepagents from the runtime enum)
Vendored at sdk HEAD: `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` (main)

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
