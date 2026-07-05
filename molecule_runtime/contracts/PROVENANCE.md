# Vendored plugin-manifest JSON-Schema (SSOT mirror)

`plugin-manifest.schema.json` is a **byte-for-byte copy** of the SSOT
plugin-manifest contract schema that lives in the `molecule-ai-sdk` repository:

    molecule-ai-sdk/contracts/plugin-manifest/plugin-manifest.schema.json

Source repo:          https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:          contracts/plugin-manifest/plugin-manifest.schema.json
Source commit:        `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` (last commit touching the schema — ai-sdk#53 dropped langgraph/autogen/gemini-cli/deepagents from the runtime enum)
Vendored at sdk HEAD: `56f7248455ee1a1b6a5e9f7885800d03f8f2493b` (main)

## Why vendored (and not fetched)

`molecule_runtime/manifest_ssot.py` validates plugin manifests against this
schema at plugin **load** (`plugins.load_plugin_manifest`) and at **install**
(`plugin_sources.install_declared_plugins`) — the ADVISORY phase of
molecule-core#3383 (install-time plugin-manifest SSOT validation). Validation
runs inside workspace containers at boot, so the schema must resolve fully
offline: no cross-repo clone, no token, no network. It ships in the wheel via
`[tool.setuptools.package-data]` and is loaded with `importlib.resources`.

This copy is the SSOT mirror, not a fork. It MUST stay byte-identical to the
sdk original — `scripts/check-schemas-in-sync.sh` (wired into
`.gitea/workflows/schema-sync.yml`) re-fetches the sdk original and diffs,
failing the build on drift (mirrors the molecule-ci vendored-schema drift gate
and tests/fixtures/workspace_comms/PROVENANCE.md).

## Re-vendoring

To update, re-fetch the original at the new sdk main and bump the SHAs above:

    curl -fsS -A "curl/8.4.0" \
      https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/plugin-manifest/plugin-manifest.schema.json \
      -o molecule_runtime/contracts/plugin-manifest.schema.json
