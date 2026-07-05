"""Config + plugins ride ONE transient R2 relay object (the uniform-channel win).

cf-r2-relay-config-secret-delivery — the control plane stages a workspace's
``config.yaml + prompts/* + secrets`` AND its RESOLVED plugin trees as ONE
transient R2 bundle (``{path: base64}``). The box's config-relay boot prelude
(:mod:`molecule_runtime.config_relay`) fetches + sha256-verifies + unpacks it,
and a ``presign://<name>`` declared source then resolves the delivered plugin
tree through the SAME install path as a ``gitea://`` plugin — no network, no
box-held plugin credential.

This ties the two modules together end-to-end (unpack -> install) so a
regression in either the bundle wire-shape (``.relay-plugins/<name>/``) or the
presign resolver is caught. It is the in-process anchor for the containerized
STEP-4 e2e.
"""
from __future__ import annotations

import base64
import hashlib
import json

import molecule_runtime.config_relay as cr
import molecule_runtime.plugin_sources as ps


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _bundle_body(entries: dict[str, bytes]) -> bytes:
    """Marshal a {path: base64} bundle exactly like the CP relay Stage does."""
    return json.dumps({k: _b64(v) for k, v in entries.items()}, sort_keys=True).encode("utf-8")


def test_one_bundle_delivers_config_and_plugin(tmp_path):
    config_path = tmp_path / "configs"
    plugin_name = "seo-mcp"
    mcp_desc = b'{"mcpServers":{"seo":{"command":"seo-bin","args":["--stdio"]}}}'

    # ONE bundle: config files + a resolved plugin tree under .relay-plugins/<name>/.
    entries = {
        "config.yaml": b"name: acme-concierge\nruntime: claude_code\n",
        "prompts/system.md": b"# You are the org concierge",
        f"{ps.RELAY_PLUGIN_DROP_SUBDIR}/{plugin_name}/mcp-servers.json": mcp_desc,
        f"{ps.RELAY_PLUGIN_DROP_SUBDIR}/{plugin_name}/SKILL.md": b"# seo skill",
    }
    body = _bundle_body(entries)
    sha = hashlib.sha256(body).hexdigest()

    # --- consumer step 1: verify integrity (fail-closed contract) ---
    assert hashlib.sha256(body).hexdigest() == sha

    # --- consumer step 2: unpack the whole bundle into /configs ---
    written = cr.unpack_bundle(body, config_path)
    assert (config_path / "config.yaml").read_bytes().startswith(b"name: acme-concierge")
    assert (config_path / "prompts" / "system.md").exists()
    # The plugin tree landed in the relay DROP (a sibling of plugins/, not plugins/ itself).
    drop = config_path / ps.RELAY_PLUGIN_DROP_SUBDIR / plugin_name
    assert (drop / "mcp-servers.json").read_bytes() == mcp_desc
    assert len(written) == len(entries)

    # --- consumer step 3: presign source installs the delivered plugin ---
    report = ps.install_declared_plugins(
        plugins_dir=config_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": f"presign://{plugin_name}",
            "WORKSPACE_CONFIG_PATH": str(config_path),
        },
    )
    assert report.installed == [f"presign://{plugin_name}"]
    assert report.swapped is True

    # Canonical install location, MCP descriptor present -> resolvable to MCPServerAdaptor.
    installed = config_path / "plugins" / plugin_name
    assert (installed / "mcp-servers.json").read_bytes() == mcp_desc
    assert (installed / "SKILL.md").read_text() == "# seo skill"


def test_bundle_plugin_path_rejects_traversal(tmp_path):
    # A hostile bundle path in the plugin namespace must be rejected by the
    # consumer's path guard (defense-in-depth; the CP validates on write too).
    import pytest

    config_path = tmp_path / "configs"
    # Escapes config_path: normpath(".relay-plugins/../../escape/x") == "../escape/x".
    body = _bundle_body({f"{ps.RELAY_PLUGIN_DROP_SUBDIR}/../../escape/x": b"nope"})
    with pytest.raises(cr.RelayConfigError):
        cr.unpack_bundle(body, config_path)
