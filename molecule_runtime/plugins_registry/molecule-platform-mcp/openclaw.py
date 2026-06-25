"""Platform-curated openclaw adaptor for the privileged management MCP.

Phase P4. ``resolve("molecule-platform-mcp", "openclaw", …)`` would already
land on :class:`MCPServerAdaptor` via the runtime-agnostic ``_resolve_mcp_default``
fallback (the plugin ships an ``mcpServers`` descriptor) — BUT that fallback only
fires when the descriptor is shaped exactly as expected. Pinning the mapping in
the platform registry (resolution path #1, which wins over every other source)
makes it EXPLICIT and hot-fixable: the privileged ``molecule-platform-mcp`` plugin
maps to :class:`MCPServerAdaptor` → ``register_mcp_server_hook`` →
``render_for_runtime("openclaw", …)``, which writes the stdio descriptor into
``~/.openclaw/openclaw.json`` ``mcp.servers.<name>`` (the file ``openclaw mcp set``
mutates) — NOT :class:`RawDropAdaptor`, which would silently copy the plugin files
to ``/configs/plugins/`` and warn "no tools wired" (a capability-less concierge,
the exact failure this phase closes).

``MCPServerAdaptor`` is runtime-AGNOSTIC: it calls ``ctx.register_mcp_server`` and
the active adapter renders the native config, so no openclaw-specific install
logic is needed here — only the registry pin.
"""

from molecule_runtime.plugins_registry.builtins import MCPServerAdaptor as Adaptor

__all__ = ["Adaptor"]
