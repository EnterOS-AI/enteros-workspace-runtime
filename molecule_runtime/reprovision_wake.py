"""Self-reprovision wake note — proactive wake after a plugin-install restart.

Design §5.2 (consolidated-idle-prompt-design, operator ruling 2026-07-05,
model (a) reprovision): an agent that self-installs a plugin triggers a
workspace RESTART; boot-install (``plugin_sources.install_declared_plugins``)
re-establishes ``<config_path>/plugins`` from the desired-set on the fresh
boot. On WAKE the agent must NOT come back silent — it proactively tells the
user what it installed, then resumes prior work.

Detection — durable plugin-set diff, consumed once
==================================================
The runtime records the post-boot-install plugin set in a small JSON state
file on the durable ``/configs`` volume (the same durability class as the
``.initial_prompt_done`` marker — survives a local-docker reprovision, gone
on a wiped-disk reprovision, which safely degrades to "record silently").
On every boot, AFTER boot-install has swapped the plugins tree, we diff the
current set against the recorded one:

  * no state file (first boot / upgraded runtime / wiped disk) → record the
    current set SILENTLY — first contact is the greeting's job, not ours;
  * additions present → rewrite the state FIRST (consume-once, the same
    up-front-marker pattern as initial_prompt #71: a crash after the state
    write never replays the announcement on the next boot), then return the
    added names so ``main`` schedules the wake self-message;
  * removals / no change → state refreshed, nothing announced.

Diffing the DURABLE RESULT of boot-install (rather than trusting a marker a
specific installer left) means every path that grows the plugin set across a
reprovision — mgmt-MCP ``install_plugin`` self-install, a user-driven canvas
install, a template update — produces exactly one proactive announcement,
and none of them can double-fire.

Kept as a standalone module (no heavy imports at module scope) so the state
logic is unit-testable without standing up the workspace runtime, mirroring
``initial_prompt.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# State file recording last boot's plugin set. Lives NEXT TO the plugins dir
# (i.e. on the same durable volume boot-install writes), so state and plugins
# always share one lifetime: a disk wipe drops both together and the diff
# can never announce from stale state against a fresh tree.
STATE_FILENAME = ".molecule_plugins_boot_state.json"


def _resolve_config_path(env=None) -> Path:
    """The pre-``load_config`` config base — the SAME resolution
    ``plugin_sources._resolve_plugins_dir`` uses, so the state file sits
    beside the exact plugins tree boot-install just built."""
    if env is None:
        env = os.environ
    return Path(env.get("WORKSPACE_CONFIG_PATH") or "/configs")


def resolve_state_path(env=None) -> Path:
    return _resolve_config_path(env) / STATE_FILENAME


def _current_plugin_set(plugins_dir: Path) -> list[str]:
    """Sorted plugin names = non-hidden subdirectories of ``plugins_dir``.

    Hidden entries are skipped: boot-install's staging/backup siblings
    (``.plugins.staging-*`` live one level up, but ``.complete`` markers and
    relay drops are dot-prefixed) must never read as installed plugins.
    """
    try:
        if not plugins_dir.is_dir():
            return []
        return sorted(
            p.name
            for p in plugins_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    except OSError as exc:
        log.warning("[wake] cannot list plugins dir %s (%s)", plugins_dir, exc)
        return []


def _read_previous_plugin_set(state_path: Path) -> list[str] | None:
    """Previous boot's plugin set, or None when absent/corrupt (= first boot).

    Corrupt state is treated exactly like a missing file: record silently.
    Announcing from unparseable state risks a wrong or repeated announcement;
    staying silent for one boot is the safe failure.
    """
    try:
        raw = state_path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("[wake] cannot read state %s (%s) — treating as first boot", state_path, exc)
        return None
    try:
        data = json.loads(raw)
        plugins = data.get("plugins")
        if isinstance(plugins, list) and all(isinstance(x, str) for x in plugins):
            return plugins
    except (ValueError, AttributeError):
        pass
    log.warning("[wake] corrupt state %s — treating as first boot", state_path)
    return None


def _write_plugin_set(state_path: Path, plugins: list[str]) -> bool:
    """Atomically (tmp + ``os.replace``) persist the current plugin set."""
    tmp = state_path.with_name(f"{state_path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        tmp.write_text(json.dumps({"plugins": plugins, "recorded_at": time.time()}))
        os.replace(tmp, state_path)
        return True
    except OSError as exc:
        log.warning("[wake] cannot write state %s (%s)", state_path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def record_and_diff(
    plugins_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    env=None,
) -> list[str]:
    """Record the current plugin set; return names NEWLY ADDED since last boot.

    Call ONCE per boot, after ``install_declared_plugins`` has (re)built the
    plugins tree. Consume-once: the state is rewritten BEFORE the additions
    are returned, so the announcement can never replay on a later boot even
    if the send crashes (the initial_prompt #71 up-front-marker trade-off,
    deliberately mirrored). First boot (no/corrupt state) records silently
    and returns ``[]``. Never raises — a wake note is never worth blocking
    boot over.
    """
    if env is None:
        env = os.environ
    try:
        p_dir = (
            Path(plugins_dir)
            if plugins_dir is not None
            else _resolve_config_path(env) / "plugins"
        )
        s_path = Path(state_path) if state_path is not None else resolve_state_path(env)

        current = _current_plugin_set(p_dir)
        previous = _read_previous_plugin_set(s_path)

        if not _write_plugin_set(s_path, current):
            # State didn't persist → the same diff would fire again next
            # boot. Fail SILENT (no announcement) rather than risk a
            # groundhog-day announcement loop on a read-only volume.
            return []

        if previous is None:
            log.info("[wake] first boot state recorded (%d plugin(s)) — no announcement", len(current))
            return []

        additions = sorted(set(current) - set(previous))
        if additions:
            log.info("[wake] newly installed since last boot: %s", ", ".join(additions))
        return additions
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warning("[wake] record_and_diff failed (non-fatal): %s", exc)
        return []


def build_wake_note(additions: list[str]) -> str:
    """The post-reprovision self-message (design §5.2 step 3 — never silent)."""
    names = ", ".join(additions)
    return (
        f"You just self-reprovisioned: your workspace restarted and boot-install "
        f"activated newly installed plugin(s): {names}. "
        f"Proactively tell the user what you installed and what you can now do "
        f"with it (new tools, skills, or channels — check your tool list), "
        f"then resume your prior work."
    )


async def send_wake_note_when_ready(
    note: str,
    *,
    port: int,
    platform_url: str,
    workspace_id: str,
) -> bool:
    """Send the wake note as a self-message once the A2A server is up.

    Same transport contract as main's ``_send_initial_prompt``: probe the
    local agent-card route until ready, then POST through the platform A2A
    proxy with ``self_source_headers`` (tags the row source=agent) and
    retry with backoff. Returns True when the send completed.
    """
    import httpx  # local import — keep module import light for unit tests

    from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

    ready = False
    for _attempt in range(30):
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"http://127.0.0.1:{port}{AGENT_CARD_WELL_KNOWN_PATH}"
                )
                if resp.status_code == 200:
                    ready = True
                    break
        except Exception:  # noqa: BLE001 — probe until ready
            continue

    if not ready:
        print("Reprovision wake: server not ready after 30s, skipping", flush=True)
        return False

    import uuid as _uuid

    from molecule_runtime.a2a_client import build_message_send_params
    from molecule_runtime.platform_auth import self_source_headers

    def _do_send_sync() -> bool:
        import time as _time
        import urllib.request

        payload = json.dumps(
            {
                "method": "message/send",
                "params": build_message_send_params(
                    note,
                    message_id=f"reprovision-wake-{_uuid.uuid4().hex[:8]}",
                ),
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            **self_source_headers(workspace_id),
        }
        max_retries = 5
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(
                    f"{platform_url}/workspaces/{workspace_id}/a2a",
                    data=payload,
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=600) as resp:
                    resp.read()
                print(f"Reprovision wake: completed (status={resp.status})", flush=True)
                return True
            except Exception as e:  # noqa: BLE001 — retry with backoff
                if attempt < max_retries - 1:
                    delay = 2**attempt
                    print(
                        f"Reprovision wake: attempt {attempt + 1} failed ({e}), retrying in {delay}s...",
                        flush=True,
                    )
                    _time.sleep(delay)
        print(f"Reprovision wake: failed after {max_retries} attempts", flush=True)
        return False

    print("Reprovision wake: sending via platform proxy...", flush=True)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_send_sync)
