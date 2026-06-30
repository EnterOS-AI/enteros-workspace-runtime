# Vendored workspace-comms JSON-Schemas (SSOT mirror)

These `*.schema.json` files are **byte-for-byte copies** of the SSOT contract
schemas that live in the `molecule-contracts` repository:

    molecule-contracts/workspace-comms/{register,heartbeat,a2a-envelope,agent-card}.schema.json

Source commit: `6193798eaaccba700ec271c42acb6cfa15538427`
Source repo:   https://git.moleculesai.app/molecule-ai/molecule-contracts

## Why vendored (and not fetched)

`tests/test_workspace_comms_conformance.py` validates the REAL register /
heartbeat / A2A payloads this package builds against these schemas (JSON-Schema
draft 2020-12). The gate must run **offline** in CI with no cross-repo clone and
no network — so the schemas are vendored here rather than fetched from the
contracts repo or an installed package.

These copies are the SSOT mirror, not a fork. They MUST stay byte-identical to
the contracts repo. The conformance test asserts each schema's `$id` matches the
canonical contracts URL as a tripwire against silent edits; to update them, copy
the files again from the contracts repo at a new commit and bump the SHA above.

This vendored conformance gate REPLACES the old AST drift-checker
(`scripts/check_platform_comm_contract.py`): instead of statically pattern-matching
code structure across repos, it validates the actual wire payloads the builders
emit against the SSOT schema — strictly stronger.
