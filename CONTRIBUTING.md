# Contributing

**This repo is the source of truth for the Molecule workspace runtime.**

Runtime code lives here under `molecule_runtime/`. The old
[`molecule-core`](https://git.moleculesai.app/molecule-ai/molecule-core)
`workspace/` tree was retired during the standalone runtime SSOT cutover.

## Where to send your change

| Want to … | Open PR against … |
|---|---|
| Add a new shared tool | `molecule-ai-workspace-runtime` → `molecule_runtime/builtin_tools/` |
| Fix a bug in the runtime or MCP server | `molecule-ai-workspace-runtime` |
| Add a new adapter | `molecule-ai-workspace-template-<runtime>` |
| Update this README or CONTRIBUTING | `molecule-ai-workspace-runtime` |

## What if you really need to edit this repo

Do it through a normal branch and PR in this repo. Do not reintroduce
runtime source under `molecule-core/workspace/`; core consumes the wheel
published from this repo.

## External MCP multi-workspace rule

For local external agents using `molecule-mcp`, `MOLECULE_WORKSPACES` is the
only multi-workspace config surface. Each entry is:

```json
{"id": "<workspace-id>", "token": "<tenant-issued-token>", "platform_url": "https://<tenant>.moleculesai.app"}
```

Do not add `org_id` to this config. `platform_url` selects the tenant and the
token is tenant-scoped. Do not assume the same local agent has the same
workspace ID in every org; each tenant can issue a different workspace ID.
