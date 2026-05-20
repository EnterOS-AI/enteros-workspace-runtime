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
