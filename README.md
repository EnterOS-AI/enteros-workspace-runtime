# molecule-ai-workspace-runtime

Shared Python runtime infrastructure for all [Molecule AI](https://git.moleculesai.app/molecule-ai/molecule-core)
agent adapters and workspace template images.

> **This repo is the canonical source of truth as of 2026-05-20.**
> Direct PRs are the editable path. The monorepo `molecule-core/workspace-server`
> pins this wheel by version (`molecule-ai-workspace-runtime==X.Y.Z`).
>
> Previously the monorepo `workspace/` directory was the source and this
> repo was a publish-time mirror. That arrangement is reversed by the
> standalone-as-SSOT migration ([CTO-GO 2026-05-20](https://git.moleculesai.app/molecule-ai/molecule-ai-workspace-runtime)).

## What lives here

This package provides the core machinery every Molecule AI workspace container needs:

- **A2A server** — registers with the platform, heartbeats, serves A2A JSON-RPC
- **Adapter interface** — `BaseAdapter` / `AdapterConfig` / `SetupResult`
- **Built-in tools** — delegation, memory, approvals, sandbox, audit, telemetry
- **Skill loader** — loads and hot-reloads skill modules from `/configs/skills/`
- **Plugin system** — per-workspace + shared plugin discovery and install
- **Config / preflight** — YAML config loading with validation
- **External-runtime MCP** (`molecule-mcp`) — universal MCP stdio server for
  external agents (Claude Code, hermes, codex, etc.) running outside the
  platform's container fleet
- **Multi-workspace support** — `MOLECULE_WORKSPACES` env var lets one MCP
  process serve N workspaces concurrently (introduced in the multi-WS PR
  series, finalised in 0.2.0)

## Installation

```bash
pip install molecule-ai-workspace-runtime
# Or, recommended for the external MCP server:
pipx install molecule-ai-workspace-runtime
```

## Contributing

This repo is the editable source. Open PRs directly here.

### Branch protection contract

- 2 non-author approvals required (typically `core-qa` + `core-devops` persona tokens)
- All CI contexts must pass: `ci / unit-tests`, `ci / lint`, `ci / build`,
  `ci / smoke-install`, `Secret scan / Scan diff for credential-shaped strings`
- No admin-bypass; no force-push to `main`
- Use the per-agent persona-token pattern (see
  [`feedback_per_agent_gitea_identity_default`](https://git.moleculesai.app/molecule-ai/molecule-core/)
  in the ops handbook) — not the founder PAT for CI

### Local development

```bash
# Run the unit tests
python -m venv .venv && source .venv/bin/activate
pip install \
  --index-url https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/ \
  -e . pytest pytest-asyncio
pytest -q
```

```bash
# Build a local wheel + smoke-install
pip install build
python -m build
pip install dist/*.whl
molecule-mcp --help
```

## Release process

1. Land changes via reviewed PR (2 non-author approvals + CI green)
2. Bump `version =` in `pyproject.toml` (semver — patch for fixes, minor for
   additive features, major for breaking API)
3. Tag `runtime-vX.Y.Z` on `main` post-merge
4. `publish-runtime.yml` (Gitea Actions) fires on the tag → builds wheel +
   sdist → publishes to the Gitea package registry → cascades the version pin
   to template repos

## Consumer pinning

Monorepo `workspace-server` (and the 8 workspace template Dockerfiles) pin
this package by exact version:

```dockerfile
RUN pip install --no-cache-dir \
    --index-url https://git.moleculesai.app/api/packages/molecule-ai/pypi/simple/ \
    molecule-ai-workspace-runtime==0.2.0
```

The version bump in this repo is the gating event; consumers pick up the
new version via the publish cascade (or by editing the Dockerfile pin
directly).

## Architecture: why a separate repo

The runtime needs to ship as a PyPI artifact (so the 8 workspace template
images can `pip install` it AND so operators can run `molecule-mcp` outside
our container fleet) while still evolving fast.

A standalone editable repo with independent CI cadence avoids two problems
the previous mirror arrangement had:

1. **CI saturation** — runtime-only changes had to go through the monorepo's
   full PR-CI lane (Go build, Docker layers, integration tests). Now Python
   unit tests + lint + wheel build + smoke install run independently in
   ~2-3 minutes.
2. **Bidirectional drift** — when standalone was a publish artifact but also
   accepted ad-hoc PRs (mirror-guard CI gave inconsistent enforcement),
   security fixes landed in standalone never reached the monorepo and
   monorepo features (multi-WS code) never reached standalone. The
   standalone-as-SSOT migration audited and reconciled this drift.

## Back-history

- [#87](https://git.moleculesai.app/molecule-ai/molecule-core/issues/87) — original
  workspace executor split (template repos host their own `executor.py`,
  runtime hosts the shared helpers)
- [#2103](https://git.moleculesai.app/molecule-ai/molecule-core/pull/2103) — first
  attempt at "standalone is the source" (predated mirror-guard CI); reverted
  because direct edits caused drift
- Standalone-as-SSOT migration (CTO-GO 2026-05-20) — this is the canonical
  flip, with the audit + drift reconciliation baked into the initial 0.2.0
  release.
