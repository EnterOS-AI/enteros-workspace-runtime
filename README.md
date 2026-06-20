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

## MCP SSOT public surface (issue #38)

Adapters (a2a_mcp_server, langchain integrations, future SDKs) consume
the universal Molecule tool + target-resolution contract via the SSOT
modules in `molecule_runtime`. **Adapters are shims; base
MCP/runtime is the source of truth.** The drift is one of the failure
modes the SSOT was created to prevent (a previous refactor split the
universal Molecule contract across multiple modules, which made it
easy for a future adapter to silently fork it).

* `molecule_runtime.mcp_schemas` — `MOLECULE_MCP_TOOLS`,
  `openai_function_tools()`, `PERMISSION_MAP`, `get_tool_schema(name)`,
  `validate_adapter_schemas(adapter_tools)`. Adapters import tool
  lists and per-tool schemas from here, NOT from
  `molecule_runtime.mcp_tools` or `platform_tools.registry` directly.
* `molecule_runtime.mcp_target_resolution` — `resolve_workspaces()`,
  `read_token_file()`, `print_missing_env_help()`,
  `resolve_target_for_adapter()`. Adapters parse workspace env vars via
  this, NOT directly from `os.environ`.

`tests/test_mcp_ssot.py` pins the SSOT public surface (drift tests):
the in-tree `a2a_mcp_server` adapter's `TOOLS` list is asserted to be
the same object as the SSOT, and the env-driven workspace resolution
contract is tested across the legacy single-workspace,
single-workspace-token-file, and multi-workspace-JSON shapes.

## Multiple External Workspaces

`molecule-mcp` can serve more than one external workspace from the same local
process. Set `MOLECULE_WORKSPACES` to a JSON array of workspace credentials:

```json
[
  {
    "id": "workspace-id-local-to-hongming-org",
    "token": "...",
    "platform_url": "https://hongming.moleculesai.app"
  },
  {
    "id": "different-workspace-id-local-to-agents-team-org",
    "token": "...",
    "platform_url": "https://agents-team.moleculesai.app"
  }
]
```

Each entry is independently registered and heartbeated against its own
`platform_url`; inbox polling and outbound A2A calls also route by the
workspace ID that initiated the call.

`org_id` is intentionally not part of this local MCP bridge config. The
tenant is selected by `platform_url`, and the workspace token is scoped by the
tenant that issued it. Workspace IDs do not need to match across orgs; use the
ID and token returned by each tenant.

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

Releases are **automatic on a green merge to `main`** (CTO standing directive,
2026-06-10) — no manual tag or approval gate:

1. Land changes via reviewed PR (2 non-author approvals + CI green).
2. On merge to `main`, `auto-release.yml` re-runs the merge-blocking gates
   (`unit-tests` + `responsiveness-e2e`) inline. Gitea has no `workflow_run`
   trigger, so the release workflow re-runs the gate itself rather than
   subscribing to the `ci` workflow's success.
3. On green it computes the **next patch** version from the latest `runtime-v*`
   tag, bumps `[project].version` in `pyproject.toml` to match (preserving the
   `pyproject == tag` invariant), commits that bump to `main` and cuts tag
   `runtime-vX.Y.Z` — all via the Gitea API as the `molecule-runtime-release-bot`
   identity (`RELEASE_BOT_TOKEN`; no token on disk).
4. The tag trips `publish-runtime.yml` → builds wheel + sdist → publishes to the
   Gitea package registry → its `propagate` job opens `.runtime-version` bump PRs
   on each consumer template. Merging a template bump trips that template's
   `publish-image.yml`, which bakes the pinned wheel into a fresh image, pushes
   ECR `:latest` + `:sha-<7>`, and auto-promotes the digest into the
   control-plane `runtime_image_pins` (prod + staging). Agents boot from the
   promoted pinned image (runtime baked at build, not pip-installed at boot).

**Loop guard:** the bump commit is authored by the release bot and its message
carries `[skip-bump]`; `auto-release.yml`'s `guard` job skips when `github.actor`
is the bot OR the HEAD message contains `[skip-bump]`. The tag push does not match
`on: push: branches:[main]`, so cutting the tag never re-enters `auto-release.yml`.

**Manual bump (escape hatch):** edit `version =` in `pyproject.toml` in a PR and
tag `runtime-vX.Y.Z` on `main` post-merge; `publish-runtime.yml` still fires on
any `runtime-v*` tag.

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
