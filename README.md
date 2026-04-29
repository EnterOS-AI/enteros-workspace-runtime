# molecule-ai-workspace-runtime

> **⚠️ This repo is a publish artifact, not the source of truth.**
>
> Runtime code lives in **[`Molecule-AI/molecule-core` → `workspace/`](https://github.com/Molecule-AI/molecule-core/tree/main/workspace)**. This repo is regenerated and republished from there by the [`publish-runtime`](https://github.com/Molecule-AI/molecule-core/blob/main/.github/workflows/publish-runtime.yml) workflow on every `runtime-v*` tag.
>
> **Don't edit files here directly.** PRs against this repo will not be merged. Open them against `molecule-core` instead.

---

Shared Python runtime infrastructure for all Molecule AI agent adapters.

This package provides the core machinery every Molecule AI workspace container needs:

- **A2A server** — registers with the platform, heartbeats, serves A2A JSON-RPC
- **Adapter interface** — `BaseAdapter` / `AdapterConfig` / `SetupResult`
- **Built-in tools** — delegation, memory, approvals, sandbox, telemetry
- **Skill loader** — loads and hot-reloads skill modules from `/configs/skills/`
- **Plugin system** — per-workspace + shared plugin discovery and install
- **Config / preflight** — YAML config loading with validation

## Installation

```bash
pip install molecule-ai-workspace-runtime
```

## Adapter discovery

The runtime discovers adapters in two ways:

1. **`ADAPTER_MODULE` env var** (standalone adapter repos):
   ```bash
   ADAPTER_MODULE=adapter molecule-runtime
   ```
   The runtime imports `adapter` and calls `adapter.Adapter`.

2. **Subdirectory scan** (monorepo local dev): falls back to scanning
   `molecule_runtime/adapters/<runtime>/` and importing the matching
   subdir's `Adapter` class.

## Contributing

**Don't open PRs here.** Send your change to
[`Molecule-AI/molecule-core`](https://github.com/Molecule-AI/molecule-core)
under the `workspace/` directory. After your PR merges to main and a
`runtime-v*` tag is pushed, the [`publish-runtime`](https://github.com/Molecule-AI/molecule-core/blob/main/.github/workflows/publish-runtime.yml)
workflow rebuilds this mirror + uploads the new wheel to PyPI.

See [`docs/workspace-runtime-package.md`](https://github.com/Molecule-AI/molecule-core/blob/main/docs/workspace-runtime-package.md)
for the full publishing flow.

## Why this split

The runtime needs to ship as a PyPI artifact (so the 8 workspace template
images can `pip install` it), but it also needs to evolve in lock-step
with the platform's wire protocol (queue shape, A2A metadata, event
payloads). A monorepo edit + auto-publish pipeline gives both: atomic
cross-cutting changes, plus a clean PyPI release on every tag.

For the back-history of why this repo previously was the source of truth
and the drift that caused: see issue [`Molecule-AI/molecule-core#2103`](https://github.com/Molecule-AI/molecule-core/pull/2103).
