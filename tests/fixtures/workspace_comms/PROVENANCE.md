# Vendored workspace-comms JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the `molecule-ai-sdk` repository (the contracts SSOT moved
there when `molecule-contracts` was archived — the schemas were folded in
byte-for-byte, so each schema's `$id` still carries the historical
`molecule-contracts/workspace-comms/...` URL, which is intentional and must NOT
be rewritten here):

    molecule-ai-sdk/contracts/workspace-comms/{register,heartbeat,a2a-envelope,agent-card}.schema.json

Source commit: `48670cf26706f9a3a547f188e8ef91d438748954` (EV2 — added mcp_tools_ready + first_ready_at to heartbeat.schema.json request; the SDK-owned positive tools-loaded readiness signal, runtime#273 landed the negative half)
Source repo:   https://git.moleculesai.app/molecule-ai/molecule-ai-sdk
Source path:   contracts/workspace-comms/

## Why vendored (and not fetched)

`tests/test_workspace_comms_conformance.py` validates the REAL register /
heartbeat / A2A payloads this package builds against these schemas (JSON-Schema
draft 2020-12). The gate must run **offline** in CI with no cross-repo clone and
no network — so the schemas are vendored here rather than fetched from the
contracts repo or an installed package.

These copies are the SSOT mirror, not a fork. They MUST stay byte-identical to
the `molecule-ai-sdk` originals. The conformance test asserts each schema's `$id`
matches the canonical (historical `molecule-contracts`) URL as a tripwire against
silent edits; to update them, re-fetch the files from `molecule-ai-sdk` at a new
commit and bump the SHA above:

    for s in register heartbeat a2a-envelope agent-card; do
      curl -fsS -A "curl/8.4.0" \
        "https://git.moleculesai.app/molecule-ai/molecule-ai-sdk/raw/branch/main/contracts/workspace-comms/$s.schema.json" \
        -o "tests/fixtures/workspace_comms/$s.schema.json"
    done

This vendored conformance gate REPLACES the old AST drift-checker
(`scripts/check_platform_comm_contract.py`): instead of statically pattern-matching
code structure across repos, it validates the actual wire payloads the builders
emit against the SSOT schema — strictly stronger.
