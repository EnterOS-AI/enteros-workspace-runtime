"""Self-reprovision wake note — proactive wake after a plugin-install restart.

Design §5.2 (consolidated-idle-prompt-design, operator ruling 2026-07-05,
model (a) reprovision): an agent that self-installs a plugin triggers a
workspace RESTART; boot-install (``plugin_sources.install_declared_plugins``)
re-establishes ``<config_path>/plugins`` from the desired-set on the fresh
boot. On WAKE the agent must NOT come back silent — it proactively tells the
user what it installed, then resumes prior work.

Detection — durable plugin-set diff
===================================
The runtime records the post-boot-install plugin set in a small JSON state
file on the durable ``/configs`` volume (the same durability class as the
``.initial_prompt_done`` marker). On every boot, AFTER boot-install has
swapped the plugins tree, ``prepare_wake_plan`` diffs the current set
against the recorded one. Diffing the DURABLE RESULT of boot-install
(rather than trusting a marker a specific installer left) means every path
that grows the plugin set across a reprovision — mgmt-MCP ``install_plugin``
self-install, a user-driven canvas install, a template update — produces an
announcement, and none of them can double-fire.

Consumption contract — never silently lost, never a groundhog-day loop
======================================================================
The failure modes pull in opposite directions:

  * consume UP FRONT (the plain ``initial_prompt`` #71 pattern) and any
    boot that cannot send — adapter not ready (misconfigured creds), the
    A2A server slow past a probe window — eats the announcement FOREVER,
    violating the feature's own "never wake silent" contract;
  * consume only on success with NO bound and a note whose send crashes
    the box replays every boot — the exact #71 crash-loop.

So consumption is staged, and every stage is bounded:

  1. ``prepare_wake_plan`` (boot step 0.2d) is a READ + refresh: when there
     is nothing to announce (first boot, no additions, removals only) it
     refreshes the state and returns an empty plan. When there ARE
     additions it does NOT touch the pending announcement — a boot that
     never reaches the scheduling step (adapter not ready, crash) leaves
     the diff intact for the next boot.
  2. ``mark_wake_attempt`` (called only once the adapter is READY, right
     before scheduling the send) persists the pending announcement with an
     incremented attempt counter — the #71 up-front marker, but per-attempt
     instead of forever. If this persist FAILS (read-only volume), the
     caller must NOT send: an unpersisted attempt cannot be bounded.
  3. ``send_wake_note_when_ready`` probes the local A2A server (generous
     window — a trip DEFERS to the next boot, it no longer loses the note),
     sends through the platform proxy, and CONSUMES the pending state only
     on send success.
  4. A pending announcement that has burned ``MAX_WAKE_ATTEMPTS`` boots is
     dropped LOUDLY by the next ``prepare_wake_plan`` — bounded replay.

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
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# State file recording last boot's plugin set + any pending announcement.
# Lives NEXT TO the plugins dir (same durable volume boot-install writes),
# so state and plugins share one lifetime: a disk wipe drops both together
# and the diff can never announce from stale state against a fresh tree.
STATE_FILENAME = ".molecule_plugins_boot_state.json"

# An announcement that could not be delivered re-arms on the next boot, at
# most this many times, then is dropped loudly. Bounds the #71-style replay
# (a note whose send crashes the box) while never silently eating the first
# failure.
MAX_WAKE_ATTEMPTS = 3

# A2A-server readiness probe window for the send (1s per attempt). This is
# a SAFETY NET, not a deadline the success path waits out — the probe
# proceeds the moment the server answers. Generous (vs initial_prompt's 30)
# because tripping it defers the announcement to the next boot; overridable
# for tests / constrained boxes.
_READY_PROBE_ATTEMPTS_ENV = "MOLECULE_WAKE_READY_PROBE_ATTEMPTS"
_DEFAULT_READY_PROBE_ATTEMPTS = 300


@dataclass
class WakePlan:
    """One boot's announcement plan, produced by ``prepare_wake_plan``.

    ``additions`` empty ⇒ nothing to announce this boot. ``attempts`` is how
    many boots have already tried to deliver this same announcement.
    """

    additions: list[str] = field(default_factory=list)
    attempts: int = 0
    current: list[str] = field(default_factory=list)
    state_path: str = ""


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

    Hidden entries are skipped: relay drops and marker files are
    dot-prefixed and must never read as installed plugins.
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


def _read_state(state_path: Path) -> dict | None:
    """Parsed state dict, or None when absent/corrupt (= first boot).

    Corrupt state is treated exactly like a missing file: record silently.
    Announcing from unparseable state risks a wrong or repeated
    announcement; staying silent for one boot is the safe failure.
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
            return data
    except (ValueError, AttributeError):
        pass
    log.warning("[wake] corrupt state %s — treating as first boot", state_path)
    return None


def _write_state(state_path: Path, plugins: list[str], pending: dict | None = None) -> bool:
    """Atomically (tmp + ``os.replace``) persist the state. True on success."""
    doc: dict = {"plugins": plugins, "recorded_at": time.time()}
    if pending:
        doc["pending"] = pending
    tmp = state_path.with_name(f"{state_path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, state_path)
        return True
    except OSError as exc:
        log.warning("[wake] cannot write state %s (%s)", state_path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def prepare_wake_plan(
    plugins_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    env=None,
) -> WakePlan:
    """Compute this boot's announcement plan. Call ONCE per boot, after
    ``install_declared_plugins`` has (re)built the plugins tree.

    Returns an empty plan (and refreshes the durable state) when there is
    nothing to announce: first boot / corrupt state (record silently —
    first contact is the greeting's job), no additions, removals only, or a
    pending announcement that has exhausted ``MAX_WAKE_ATTEMPTS`` (dropped
    LOUDLY). When there ARE additions the state is left UNTOUCHED — a boot
    that cannot deliver (adapter not ready, crash before scheduling)
    re-detects the same diff next boot. Never raises.
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
        state = _read_state(s_path)

        if state is None:
            _write_state(s_path, current)
            log.info("[wake] first boot state recorded (%d plugin(s)) — no announcement", len(current))
            return WakePlan(current=current, state_path=str(s_path))

        previous = state.get("plugins") or []
        pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
        pending_additions: list[str] = []
        attempts = 0
        if pending:
            raw_adds = pending.get("additions")
            if isinstance(raw_adds, list):
                # Only re-announce pending plugins that are STILL present.
                pending_additions = [a for a in raw_adds if isinstance(a, str) and a in current]
            try:
                attempts = int(pending.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0

        additions = sorted(set(current) - set(previous) | set(pending_additions))

        # The attempts budget belongs to the PENDING announcement, not to the
        # batch as a whole. Any genuinely-new addition (not part of the
        # carried-over pending set) resets the counter — otherwise a fresh
        # install could inherit an exhausted/partial budget and be dropped
        # with ZERO delivery attempts (e.g. pending plugin-x at attempts=3
        # gets removed while plugin-y lands: y must get its full budget).
        # A surviving pending plugin riding along with a new one gets extra
        # budget — bounded per composition change, and strictly better than
        # silently losing the new plugin's announcement.
        if set(additions) - set(pending_additions):
            attempts = 0

        if not additions:
            _write_state(s_path, current)  # refresh; clears stale pending
            return WakePlan(current=current, state_path=str(s_path))

        if attempts >= MAX_WAKE_ATTEMPTS:
            log.warning(
                "[wake] DROPPING announcement for %s after %d failed delivery attempt(s) — "
                "bounded replay (see MAX_WAKE_ATTEMPTS)",
                ", ".join(additions), attempts,
            )
            _write_state(s_path, current)
            return WakePlan(current=current, state_path=str(s_path))

        log.info("[wake] newly installed since last boot: %s (delivery attempts so far: %d)",
                 ", ".join(additions), attempts)
        return WakePlan(additions=additions, attempts=attempts, current=current, state_path=str(s_path))
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warning("[wake] prepare_wake_plan failed (non-fatal): %s", exc)
        return WakePlan()


def mark_wake_attempt(plan: WakePlan) -> bool:
    """Persist the pending announcement with an incremented attempt counter.

    Call ONLY once the adapter is ready and the send is about to be
    scheduled. Returns False when the persist failed — the caller MUST NOT
    send in that case (an unpersisted attempt cannot be bounded, and an
    unbounded announce-every-boot loop is worse than one deferred note).
    """
    if not plan.additions or not plan.state_path:
        return False
    return _write_state(
        Path(plan.state_path),
        plan.current,
        pending={"additions": plan.additions, "attempts": plan.attempts + 1},
    )


def consume_wake_note(plan: WakePlan) -> bool:
    """Clear the pending announcement (send succeeded). True on success."""
    if not plan.state_path:
        return False
    return _write_state(Path(plan.state_path), plan.current)


def build_wake_note(additions: list[str]) -> str:
    """The post-reprovision self-message (design §5.2 step 3 — never silent).

    Delivery contract (core#4587 follow-up, 2026-07-24). This wake is stamped
    ``self-lifecycle`` so the replay guard governs restart loops (see the
    A2A_SOURCE_SELF_LIFECYCLE stamp in send_wake_note_when_ready). But that
    same membership in ``_ROUTINE_SELF_SOURCE_TYPES`` routes the agent's TURN
    REPLY through the silent drop-path built for cron/harvester nudges — so a
    plain reply here never reaches the user (confirmed on a real lark-channel
    install: the agent replied 769 chars, none of it reached canvas, then hung
    83s on a consent-gated terminal command trying to "do" the work instead).
    The reliable channel is the ``send_message_to_user`` tool
    (a2a_tools_messaging → platform ``/notify`` → canvas): every runtime
    exposes it, and it is INDEPENDENT of the suppressed turn reply. So this
    note instructs the agent to (1) reach the user via that tool explicitly,
    not via the turn reply, and (2) PRESENT interactive next steps rather than
    run blocking / consent-gated commands unattended — an interactive
    device-flow (e.g. a lark scan) cannot complete without the user, and an
    unprompted terminal call just hangs on the consent gate. Cross-runtime by
    construction: it lives in the shared runtime, so hermes / claude-code /
    codex / openclaw all get the same behavior."""
    names = ", ".join(additions)
    return (
        f"You just self-reprovisioned: your workspace restarted and boot-install "
        f"activated newly installed plugin(s): {names}. "
        f"Use the send_message_to_user tool to tell the user — in one clear "
        f"message — what you installed and what they can now do with it (new "
        f"tools, skills, or channels — check your tool list). Do NOT rely on "
        f"replying in this turn: this is a self-message, so your turn reply is "
        f"NOT shown to the user — send_message_to_user is the channel that "
        f"reaches them. If activating it needs an interactive step (for example "
        f"a device-flow scan or an approval), PRESENT that next step in your "
        f"message and wait for the user; do NOT run blocking or consent-gated "
        f"terminal commands unprompted. Then resume your prior work."
    )


def _ready_probe_attempts(env=None) -> int:
    if env is None:
        env = os.environ
    try:
        v = int(env.get(_READY_PROBE_ATTEMPTS_ENV) or _DEFAULT_READY_PROBE_ATTEMPTS)
        return v if v > 0 else _DEFAULT_READY_PROBE_ATTEMPTS
    except (TypeError, ValueError):
        return _DEFAULT_READY_PROBE_ATTEMPTS


async def send_wake_note_when_ready(
    plan: WakePlan,
    *,
    port: int,
    platform_url: str,
    workspace_id: str,
) -> bool:
    """Send the wake note as a self-message once the A2A server is up, and
    CONSUME the pending announcement on success.

    Same transport contract as main's ``_send_initial_prompt``: probe the
    local agent-card route until ready (the success path proceeds the
    moment the server answers; the window is a generous safety net whose
    trip DEFERS the note to the next boot rather than losing it), then POST
    through the platform A2A proxy with ``self_source_headers`` (tags the
    row source=agent), retrying with backoff. Returns True when the send
    completed and the state was consumed.
    """
    import httpx  # local import — keep module import light for unit tests

    from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

    note = build_wake_note(plan.additions)

    ready = False
    for _attempt in range(_ready_probe_attempts()):
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
        print(
            "Reprovision wake: server not ready within the probe window — "
            "deferring announcement to the next boot (pending state kept)",
            flush=True,
        )
        return False

    import uuid as _uuid

    from molecule_runtime.a2a_client import build_message_send_params
    from molecule_runtime.a2a_executor import (
        A2A_MESSAGE_SOURCE_TYPE,
        A2A_SOURCE_SELF_LIFECYCLE,
    )
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
                    # task #219 §5: stamp the lifecycle-wake source so the
                    # reprovision announcement is guard-governed (a restart loop
                    # re-announcing trips the replay breaker) instead of the
                    # unstamped guard-bypass it was before.
                    metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_LIFECYCLE},
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
        print(
            f"Reprovision wake: failed after {max_retries} attempts — "
            "pending state kept, will re-announce next boot",
            flush=True,
        )
        return False

    print("Reprovision wake: sending via platform proxy...", flush=True)
    loop = asyncio.get_event_loop()
    sent = await loop.run_in_executor(None, _do_send_sync)
    if sent:
        if consume_wake_note(plan):
            print("Reprovision wake: pending state consumed", flush=True)
        else:
            print(
                "Reprovision wake: WARNING — sent but could not consume pending state; "
                "a bounded re-announcement may occur next boot",
                flush=True,
            )
    return sent
