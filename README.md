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

## Channel plugin local A2A transport

A plugin whose `plugin.yaml` declares `kind: channel` can declare a
workspace-owned daemon with `contributes.daemons`. At boot the runtime
discovers and supervises every declared daemon, but injects the local channel
capability only into daemons owned by `kind: channel` plugins:

- `MOLECULE_CHANNEL_API_VERSION` — the SDK/host contract version. Version `1`
  is required by the current client.

- `MOLECULE_CHANNEL_A2A_SOCKET` — a private Unix socket serving the
  workspace's existing A2A HTTP/JSON-RPC application.
- `MOLECULE_CHANNEL_A2A_TOKEN` — a distinct, ephemeral bearer capability for
  that plugin's socket. The helper sends it in the local-only capability header.
- `MOLECULE_CHANNEL_PLUGIN_ID` — the installed plugin identity the runtime
  stamps as channel provenance.

The socket does **not** define a second event envelope. Provider plugins import
the provider-neutral client from `molecule-ai-sdk`; they do not import the
runtime's private host implementation:

```python
from molecule_plugin.channel import (
    channel_message_response_text,
    send_channel_message,
)

response = await send_channel_message(
    text,
    metadata={
        "chat_id": chat_id,
        "user_id": sender_id,
        "username": sender_name,
        "message_id": external_message_id,
    },
)
reply_text = channel_message_response_text(response)
```

The helper sends the existing platform request shape (IDs shown explicitly):

```json
{"jsonrpc":"2.0","id":"req-1","method":"message/send","params":{"message":{"kind":"message","role":"user","messageId":"msg-1","parts":[{"kind":"text","text":"hello"}]},"metadata":{"chat_id":"C123","user_id":"U456","username":"Ada","message_id":"171.1"}}}
```

With a2a-sdk 1.x, a completed turn returns the existing JSON-RPC `Task` shape;
IDs and timestamp are generated per turn:

```json
{"jsonrpc":"2.0","id":"req-1","result":{"kind":"task","id":"task-1","contextId":"ctx-1","artifacts":[{"artifactId":"artifact-1","parts":[{"kind":"text","text":"pong"}]}],"status":{"state":"completed","timestamp":"2026-07-13T03:44:52Z","message":{"kind":"message","messageId":"reply-1","role":"agent","taskId":"task-1","contextId":"ctx-1","parts":[{"kind":"text","text":"pong"}]}}}}
```

`message/send` returns that synchronous result when the turn completes.
Clients that need an explicit start acknowledgement plus completion can post
the same envelope with `method: "message/stream"` and consume the existing A2A
working-status and terminal-message SSE events. The reusable helper intentionally
targets `message/send`; streaming clients use an UDS-aware HTTP client directly.

Channel provenance uses the existing platform fields under `params.metadata`:
`source`, `chat_id`, `user_id`, `username`, and `message_id`. The daemon
supplies channel event fields, but the runtime always overwrites only the
canonical `params.metadata.source` with `MOLECULE_CHANNEL_PLUGIN_ID`. A client
claim at `params.message.metadata.source` is rejected before dispatch rather
than mirrored into a second provenance surface. Before stamping, the listener
requires the plugin-specific
`MOLECULE_CHANNEL_A2A_TOKEN`; another same-UID daemon finding the socket path
does not receive that token through its own injected environment and cannot
select a different source merely by changing request JSON. Plugins still run
under the workspace UID and are trusted code, not mutually sandboxed
principals. The socket directory is mode 0700 and each socket is mode 0600
before the daemon starts. Paths and tokens are ephemeral per-boot capabilities
and must not be persisted.

If the socket bind fails, the runtime removes all four reserved capability
variables and still starts the daemon. `send_channel_message` raises
`ChannelCapabilityUnavailable` when the version, socket, or token is absent or
unsupported, which means this host cannot run the channel plugin. Once a local
send is attempted, a connection, timeout, or HTTP failure raises
`ChannelDeliveryUnknown`; the same external event must **not** be replayed
because the agent may already have accepted the turn.

The runtime intentionally does not depend on `molecule-ai-sdk`. Instead,
`molecule_runtime/channel_sdk.py` is a byte-for-byte copy of the SDK-owned
`molecule_plugin/channel.py`; `molecule_runtime.channel_events` hosts the socket
and retains `ChannelEvent*` aliases only for runtime compatibility. Check a
local SDK checkout before updating the vendor:

```bash
scripts/check-channel-sdk-vendor.sh ../molecule-ai-sdk
```

CI runs the same exact-copy gate against `molecule-ai-sdk` main, in addition to
the client/host conformance tests.

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
3. On green it computes the next patch from the latest `runtime-v*` tag and
   compares it with the reviewed `[project].version` floor. The higher version
   becomes `runtime-vX.Y.Z` (so an explicit `0.4.0` cutover is not flattened to
   `0.3.126`). The release bot creates only that tag through the Gitea API;
   protected `main` is never mutated and no token is written to disk.
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
