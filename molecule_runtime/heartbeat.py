"""Heartbeat loop — alive signal + delegation status checker.

Every 30 seconds:
1. Send heartbeat to platform (alive signal with current_task, error_rate)
2. Check pending delegations — any results back?
3. Store completed delegation results for the agent to pick up

Resilient: recreates HTTP client on failure, auto-restarts on crash.
"""

import asyncio
import json
import logging
import os
import threading
import time
import httpx

from molecule_runtime.a2a_client import build_message_send_params
from molecule_runtime.platform_auth import auth_headers, refresh_cache, self_source_headers


def _runtime_state_payload() -> dict:
    """Build the {runtime_state, sample_error} portion of the heartbeat
    body when SOME adapter executor has marked itself wedged. Returns
    an empty dict when the runtime is healthy so the heartbeat payload
    doesn't grow fields the platform doesn't need.

    Source of truth is runtime_wedge (lives in molecule-runtime,
    independent of any specific adapter). Pre task #87 this imported
    from claude_sdk_executor — that worked because the executor was
    bundled into molecule-runtime, but blocked moving it to the
    claude-code template repo. The runtime_wedge module is now the
    cross-cutting wedge-state holder; adapters mark/clear via it,
    heartbeat reads it.

    Imported lazily so a workspace whose runtime image somehow ships
    without runtime_wedge (corrupt install, mid-rolling-deploy state)
    keeps heartbeating — a missing import means "no wedge info; assume
    healthy."
    """
    try:
        from molecule_runtime.runtime_wedge import is_wedged, wedge_reason
    except Exception:
        return {}
    if not is_wedged():
        return {}
    return {
        "runtime_state": "wedged",
        # sample_error doubles as the human-readable banner text on the
        # canvas's degraded card — keep it short and actionable.
        "sample_error": wedge_reason(),
    }


def _runtime_metadata_payload() -> dict:
    """Build the {runtime_metadata} portion of the heartbeat body —
    adapter-declared capabilities + per-capability override values
    (idle timeout, etc.). The platform reads this to route capabilities
    to the right owner: native (adapter) vs fallback (platform).

    Returns an empty dict if the adapter can't be loaded or introspected.
    Heartbeat must NEVER fail because of capability discovery — observability
    is more important than capability accuracy. The platform falls through
    to its own defaults when fields are missing.

    See project memory `project_runtime_native_pluggable.md` and
    workspace/adapter_base.py:RuntimeCapabilities.
    """
    try:
        from molecule_runtime.adapters import get_adapter
        # ADAPTER_MODULE wins over the runtime arg in get_adapter — pass
        # an empty string to force the env-var path.
        adapter_cls = get_adapter("")
        adapter = adapter_cls()
        caps = adapter.capabilities()
        meta: dict = {"capabilities": caps.to_dict()}
        idle = adapter.idle_timeout_override()
        # Only include the override when it's a positive integer. None /
        # zero / negative falls through to the platform's global default
        # (env A2A_IDLE_TIMEOUT_SECONDS, default 5min) — that "absent
        # field = use default" contract is what keeps the wire small.
        if isinstance(idle, int) and idle > 0:
            meta["idle_timeout_seconds"] = idle
        return {"runtime_metadata": meta}
    except Exception as e:
        # debug-level: missing ADAPTER_MODULE in dev / test envs is normal
        logger.debug("runtime_metadata: failed to read adapter caps: %s", e)
        return {}


logger = logging.getLogger(__name__)


def _persist_inbound_secret_from_heartbeat(resp) -> None:
    """Persist ``platform_inbound_secret`` from a heartbeat response, if any.

    The platform's heartbeat handler (workspace-server PR #2421) returns
    the secret on every beat — mirrors /registry/register so a workspace
    whose secret was lazy-healed on the platform side picks it up within
    one heartbeat tick instead of requiring a runtime restart.

    Without this delivery path the chat-upload code path's "secret was
    just minted, will pick up on next heartbeat" 503 message is a lie
    and the workspace stays 401-forever until the operator restarts the
    runtime. Caught 2026-04-30 on the hongmingwang tenant — the
    standalone wrapper (mcp_cli.py) got the same change in #2421 but
    the in-container heartbeat (this file) was missed in the first
    pass.

    Failure is non-fatal: if the body isn't JSON, doesn't carry the
    field, or the disk write fails, the next heartbeat retries. This
    matches the cold-start register flow in main.py:319-323.
    """
    try:
        body = resp.json()
    except Exception:
        return
    if not isinstance(body, dict):
        return
    secret = body.get("platform_inbound_secret")
    if not secret:
        return
    try:
        from molecule_runtime.platform_inbound_auth import save_inbound_secret

        save_inbound_secret(secret)
    except Exception as exc:
        logger.warning(
            "heartbeat: persist inbound secret failed: %s", exc
        )


HEARTBEAT_INTERVAL = 30  # seconds — fallback default when no per-instance value is passed
MAX_CONSECUTIVE_FAILURES = 10
MAX_SEEN_DELEGATION_IDS = 200
SELF_MESSAGE_COOLDOWN = 60  # seconds — minimum between self-messages to prevent loops
# Statuses that warrant a user-visible canvas chat push. The agent self-
# message path (above each /notify loop) always fires so the agent can
# synthesize its own NL summary via send_message_to_user. The /notify
# chat push exists only to surface failures the agent might otherwise
# swallow — completed delegations are noise on the canvas (chloe-dong
# task #384). Keep this list tiny + explicit so adding a new status
# requires deliberate review.
NOTIFY_STATUSES = frozenset({"failed", "error", "timeout", "cancelled"})
_DELEGATION_PREFIX = "Delegation completed:"
# Shared path — adapter executors (in their template repos) read this
# same file via executor_helpers.read_delegation_results so heartbeat-
# delivered async delegation results land in the next agent turn.
DELEGATION_RESULTS_FILE = os.environ.get("DELEGATION_RESULTS_FILE", "/tmp/delegation_results.jsonl")
# Cursor file for tracking activity_log IDs processed from the a2a_receive path
# (delegations fired via tool_delegate_task → POST /workspaces/:id/a2a proxy, not
# POST /workspaces/:id/delegate). Persisted to disk so heartbeat restarts
# don't re-process the same rows.
_ACTIVITY_DELEGATION_CURSOR_FILE = os.environ.get(
    "DELEGATION_ACTIVITY_CURSOR_FILE",
    "/tmp/delegation_activity_cursor",
)


class HeartbeatLoop:
    def __init__(
        self,
        platform_url: str,
        workspace_id: str,
        interval_seconds: int = HEARTBEAT_INTERVAL,
        agent_card: dict | None = None,
    ):
        self.platform_url = platform_url
        self.workspace_id = workspace_id
        self.agent_card = agent_card
        # Per-instance interval — main.py threads ObservabilityConfig.
        # heartbeat_interval_seconds (clamped to [5, 300] at parse time)
        # in here so operators can tune cadence per-workspace via the
        # `observability:` block in config.yaml. Defaults to the
        # legacy module constant so callers that haven't been updated
        # yet (and tests that construct HeartbeatLoop directly with the
        # 2-arg signature) keep their existing 30s behavior.
        self._interval_seconds = interval_seconds
        self.start_time = time.time()
        self.error_count = 0
        self.request_count = 0
        self.active_tasks = 0
        self.current_task = ""
        self.sample_error = ""
        self._task = None
        self._consecutive_failures = 0
        # #2723 (300s tool-chain-lost): the alive-signal heartbeat POST runs
        # on a DEDICATED OS THREAD, not the agent's shared asyncio event
        # loop. A long synchronous/CPU-bound tool step (bulk file
        # download/upload via a sync client, big inline subprocess, image
        # processing) blocks the event loop and starves an asyncio-task
        # heartbeat → no /heartbeat for minutes → the platform's canvas
        # idle watchdog (workspace-server applyIdleTimeout) sees broadcaster
        # silence and kills the turn. A real OS thread keeps POSTing
        # regardless of what the event loop is doing.
        #
        # Only the alive signal moves to the thread. Delegation/activity
        # polling (which itself fires async A2A self-message POSTs and disk
        # writes) stays on the async _loop — it is not what the idle
        # watchdog keys on, and keeping it async avoids cross-thread sharing
        # of the httpx.AsyncClient / asyncio primitives those POSTs use.
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()
        self._seen_delegation_ids: set[str] = set()
        self._last_self_message_time = 0.0
        self._parent_name: str | None = None  # Cached after first lookup
        # Seen activity IDs for a2a_receive polling (delegations via POST /a2a proxy path).
        # Loaded lazily from cursor file on first poll to avoid blocking startup.
        self._seen_activity_ids: set[str] = set()
        self._activity_cursor_loaded = False

    @property
    def error_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.error_count / self.request_count

    def record_error(self, error: str) -> None:
        self.error_count += 1
        self.request_count += 1
        self.sample_error = error

    def record_success(self) -> None:
        self.request_count += 1

    def start(self) -> None:
        # #2723: alive-signal heartbeat on a dedicated daemon OS thread so it
        # survives a blocked asyncio event loop. The async _loop continues to
        # own delegation/activity polling.
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_thread_loop,
            name="heartbeat-alive",
            daemon=True,
        )
        self._hb_thread.start()
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception():
            logger.error("Heartbeat loop died: %s — restarting", task.exception())
            self._task = asyncio.create_task(self._loop())
            self._task.add_done_callback(self._on_done)

    def _send_heartbeat(self, client: httpx.Client) -> None:
        """Send one alive-signal heartbeat POST (sync). Runs on the dedicated
        OS thread. Mirrors the original async POST: same /registry/heartbeat
        URL, same payload, same 401-refresh-and-retry-once, same
        platform_inbound_secret persistence. Raises on transport/HTTP error
        so the caller can track consecutive failures."""
        body: dict = {
            "workspace_id": self.workspace_id,
            "error_rate": self.error_rate,
            "sample_error": self.sample_error,
            "active_tasks": self.active_tasks,
            "current_task": self.current_task,
            "uptime_seconds": int(time.time() - self.start_time),
        }
        # #2421: backfill agent_card when the initial register failed. Only
        # sent when we have a card — the platform writes it only if the DB
        # row's agent_card is NULL.
        if self.agent_card is not None:
            body["agent_card"] = self.agent_card
        # Layer the runtime-wedge + metadata fields on top (status→degraded,
        # capability routing). Identical to the original async path.
        body.update(_runtime_state_payload())
        body.update(_runtime_metadata_payload())
        try:
            resp = client.post(
                f"{self.platform_url}/registry/heartbeat",
                json=body,
                headers=auth_headers(),
            )
            self.error_count = 0
            self.request_count = 0
            self._consecutive_failures = 0
            _persist_inbound_secret_from_heartbeat(resp)
            return
        except Exception as e:
            # Issue #1877: on 401, re-read the token from disk and retry once.
            is_401 = (
                isinstance(e, httpx.HTTPStatusError)
                and e.response.status_code == 401
            )
            if not is_401:
                raise
            logger.warning(
                "Heartbeat 401 for %s — refreshing token cache and retrying once",
                self.workspace_id,
            )
            refresh_cache()
            retry_body: dict = {
                "workspace_id": self.workspace_id,
                "error_rate": self.error_rate,
                "sample_error": self.sample_error,
                "active_tasks": self.active_tasks,
                "current_task": self.current_task,
                "uptime_seconds": int(time.time() - self.start_time),
            }
            if self.agent_card is not None:
                retry_body["agent_card"] = self.agent_card
            retry_body.update(_runtime_state_payload())
            retry_resp = client.post(
                f"{self.platform_url}/registry/heartbeat",
                json=retry_body,
                headers=auth_headers(),
            )
            self._consecutive_failures = 0
            self.request_count += 1
            _persist_inbound_secret_from_heartbeat(retry_resp)

    def _heartbeat_thread_loop(self) -> None:
        """Dedicated-OS-thread alive-signal loop (#2723). Keeps POSTing
        /registry/heartbeat on a plain time.sleep cadence using a SYNC
        httpx.Client, fully independent of the agent's asyncio event loop —
        so it does not stop when a long synchronous tool step blocks that
        loop. Recreates the client after a run of consecutive failures,
        mirroring the async loop's resilience."""
        while not self._hb_stop.is_set():
            client = None
            try:
                client = httpx.Client(timeout=10.0)
                while not self._hb_stop.is_set():
                    try:
                        self._send_heartbeat(client)
                    except Exception as e:
                        self._consecutive_failures += 1
                        if (
                            self._consecutive_failures <= 3
                            or self._consecutive_failures % MAX_CONSECUTIVE_FAILURES == 0
                        ):
                            logger.warning(
                                "Heartbeat failed (%d consecutive): %s",
                                self._consecutive_failures,
                                e,
                            )
                        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            logger.info(
                                "Heartbeat: recreating HTTP client after %d failures",
                                self._consecutive_failures,
                            )
                            break  # drop to outer loop → recreate client
                    # Interruptible sleep: wake immediately on stop() so the
                    # thread joins promptly during shutdown.
                    self._hb_stop.wait(self._interval_seconds)
            except Exception as e:
                logger.error(
                    "Heartbeat thread error: %s — retrying in %ds",
                    e,
                    self._interval_seconds,
                )
                self._hb_stop.wait(self._interval_seconds)
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    async def stop(self):
        # Stop the dedicated heartbeat thread first (signal + join), then
        # cancel the async delegation loop.
        self._hb_stop.set()
        if self._hb_thread is not None:
            # Join off the event loop so a blocked/slow thread shutdown does
            # not stall the async caller. Interval is small (<=300s) and the
            # stop Event interrupts the sleep, so this returns quickly.
            await asyncio.to_thread(self._hb_thread.join, 10.0)
            self._hb_thread = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # #2723: the alive-signal /registry/heartbeat POST moved to the
        # dedicated OS thread (_heartbeat_thread_loop) so it survives a
        # blocked event loop. This async loop now owns ONLY delegation /
        # activity polling — work that itself fires async A2A self-message
        # POSTs and is, by nature, event-loop-bound.
        while True:
            client = None
            try:
                client = httpx.AsyncClient(timeout=10.0)
                while True:
                    # 1. Check delegation status
                    try:
                        await self._check_delegations(client)
                    except Exception as e:
                        logger.debug("Delegation check failed: %s", e)

                    # 2. Check activity_logs for delegation results that arrived via
                    # the POST /a2a proxy path (tool_delegate_task → send_a2a_message).
                    # These are NOT written to the delegations table, so
                    # _check_delegations misses them. See issue #354.
                    try:
                        await self._check_activity_delegations(client)
                    except Exception as e:
                        logger.debug("Activity delegation check failed: %s", e)

                    await asyncio.sleep(self._interval_seconds)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "Heartbeat loop error: %s — retrying in %ds", e, self._interval_seconds
                )
                await asyncio.sleep(self._interval_seconds)
            finally:
                if client:
                    try:
                        await client.aclose()
                    except Exception:
                        pass

    async def _check_delegations(self, client: httpx.AsyncClient):
        """Check for completed delegations and store results for the agent."""
        try:
            resp = await client.get(
                f"{self.platform_url}/workspaces/{self.workspace_id}/delegations",
                headers=auth_headers(),
            )
            if resp.status_code != 200:
                return

            delegations = resp.json()
            if not isinstance(delegations, list):
                return

            new_results = []
            for d in delegations:
                did = d.get("delegation_id", "")
                status = d.get("status", "")

                if not did or did in self._seen_delegation_ids:
                    continue

                if status in ("completed", "failed"):
                    # Fix B (Cycle 5): validate source_id before accepting delegation
                    # results. Only process delegations that THIS workspace created
                    # (source_id == self.workspace_id). Attacker-crafted delegation
                    # records with a foreign source_id cannot inject instructions.
                    source_id = d.get("source_id", "")
                    if source_id != self.workspace_id:
                        logger.warning(
                            "Heartbeat: skipping delegation %s — source_id %r does not "
                            "match this workspace %r; possible injection attempt",
                            did, source_id, self.workspace_id,
                        )
                        self._seen_delegation_ids.add(did)  # mark seen so we don't warn again
                        continue

                    self._seen_delegation_ids.add(did)
                    new_results.append({
                        "delegation_id": did,
                        "target_id": d.get("target_id", ""),
                        "source_id": source_id,
                        "status": status,
                        "summary": d.get("summary", ""),
                        "response_preview": d.get("response_preview", ""),
                        "error": d.get("error", ""),
                        "timestamp": time.time(),
                    })

            # Evict old seen IDs if over limit
            if len(self._seen_delegation_ids) > MAX_SEEN_DELEGATION_IDS:
                # Keep most recent half
                self._seen_delegation_ids = set(list(self._seen_delegation_ids)[MAX_SEEN_DELEGATION_IDS // 2:])

            if new_results:
                # Append to results file for context injection on next message
                with open(DELEGATION_RESULTS_FILE, "a") as f:
                    for r in new_results:
                        f.write(json.dumps(r) + "\n")
                logger.info("Heartbeat: %d new delegation results — triggering self-message", len(new_results))

                # Build a summary message for the agent.
                # Fix B (Cycle 5): do NOT embed raw response_preview text in
                # user-role A2A messages — that is the prompt-injection vector.
                # Instead reference only the delegation ID and status; the agent
                # reads full content from DELEGATION_RESULTS_FILE which was
                # written above from trusted platform data.
                summary_lines = []
                for r in new_results:
                    line = f"- [{r['status']}] Delegation {r['delegation_id'][:8]}: {r['summary'][:80]}"
                    if r.get("error"):
                        line += f"\n  Error: {r['error'][:100]}"
                    summary_lines.append(line)

                # Look up parent workspace (cached after first call)
                if self._parent_name is None:
                    try:
                        parent_resp = await client.get(
                            f"{self.platform_url}/workspaces/{self.workspace_id}",
                            headers=auth_headers(),
                        )
                        if parent_resp.status_code == 200:
                            parent_id = parent_resp.json().get("parent_id", "")
                            if parent_id:
                                parent_info = await client.get(
                                    f"{self.platform_url}/workspaces/{parent_id}",
                                    headers=auth_headers(),
                                )
                                if parent_info.status_code == 200:
                                    self._parent_name = parent_info.json().get("name", "")
                        if self._parent_name is None:
                            self._parent_name = ""  # No parent — cache empty
                    except Exception:
                        pass  # Will retry next cycle
                parent_name = self._parent_name or ""

                report_instruction = ""
                if parent_name:
                    report_instruction = (
                        f"\n\nIMPORTANT: Report these results back to your parent '{parent_name}' "
                        f"by delegating a summary to them. Use delegate_task or delegate_task_async "
                        f"with a concise status report. Also use send_message_to_user to notify the user."
                    )
                else:
                    report_instruction = (
                        "\n\nReport results using send_message_to_user to notify the user."
                    )

                trigger_msg = (
                    "Delegation results are ready. Review them and take appropriate action:\n"
                    + "\n".join(summary_lines)
                    + report_instruction
                )

                # Send A2A self-message to wake the agent.
                # Minimum 60s between self-messages to avoid spam, but always send
                # when there are genuinely NEW results to process.
                now = time.time()
                if now - self._last_self_message_time < SELF_MESSAGE_COOLDOWN:
                    logger.debug("Heartbeat: self-message cooldown (60s), will retry next cycle")
                else:
                    self._last_self_message_time = now
                    try:
                        # self_source_headers() adds X-Workspace-ID so the
                        # platform tags this row source=agent, not canvas
                        # — see platform_auth.py for the full rationale.
                        await client.post(
                            f"{self.platform_url}/workspaces/{self.workspace_id}/a2a",
                            json={
                                "method": "message/send",
                                # #2251: single model-based builder — params
                                # generated FROM the receiver's a2a-sdk v0.3
                                # SendMessageRequest schema.
                                "params": build_message_send_params(trigger_msg),
                            },
                            headers=self_source_headers(self.workspace_id),
                            timeout=120.0,
                        )
                        logger.info("Heartbeat: self-message sent to process delegation results")
                    except Exception as e:
                        logger.warning("Heartbeat: failed to send self-message: %s", e)

                # Also push notification to user via canvas, but ONLY for
                # failure-class statuses. Success rows are agent-only — the
                # self-message above wakes the agent who synthesizes its
                # own NL via send_message_to_user. See task #384 / CTO
                # decision 2026-05-20 (canvas pollution from raw envelopes).
                for r in new_results:
                    if r.get("status") not in NOTIFY_STATUSES:
                        continue
                    try:
                        reason = r.get("error") or r.get("summary", "")
                        msg = f"Delegation {r['status']}: {reason[:200]}"
                        await client.post(
                            f"{self.platform_url}/workspaces/{self.workspace_id}/notify",
                            json={"message": msg, "type": "delegation_result"},
                            headers=auth_headers(),
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.debug("Delegation check error: %s", e)

    async def _check_activity_delegations(self, client: httpx.AsyncClient):
        """Poll activity_logs for delegation results that arrived via the POST /a2a proxy path.

        tool_delegate_task → send_a2a_message → POST /workspaces/:id/a2a (proxy)
        logs to activity_logs but NOT the delegations table. _check_delegations
        only checks the delegations table, so these results are invisible to the
        heartbeat — the agent never wakes up to consume them (issue #354).

        This method closes that gap: polls GET /workspaces/:id/activity?type=a2a_receive,
        filters for rows from peer workspaces (source_id != "" and != self.workspace_id),
        tracks seen IDs with a cursor file, and sends a self-message to wake the agent.
        """
        try:
            # Load cursor lazily on first call so startup is not blocked by disk I/O.
            if not self._activity_cursor_loaded:
                self._activity_cursor_loaded = True
                try:
                    if os.path.exists(_ACTIVITY_DELEGATION_CURSOR_FILE):
                        cursor = open(_ACTIVITY_DELEGATION_CURSOR_FILE).read().strip()
                        if cursor:
                            self._seen_activity_ids = set(cursor.split(","))
                except Exception:
                    pass  # Corrupt cursor — start fresh

            params: dict[str, str] = {"type": "a2a_receive"}
            resp = await client.get(
                f"{self.platform_url}/workspaces/{self.workspace_id}/activity",
                params=params,
                headers=auth_headers(),
            )
            if resp.status_code != 200:
                return

            rows = resp.json()
            if not isinstance(rows, list):
                return

            # Activity API returns newest-first; process in reverse order so
            # we advance the cursor monotonically (oldest → newest).
            rows = list(reversed(rows))

            new_results: list[dict] = []
            last_id: str | None = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                activity_id = str(row.get("id", ""))
                if not activity_id:
                    continue
                last_id = activity_id

                if activity_id in self._seen_activity_ids:
                    continue

                # Filter: must have a non-empty source_id that is NOT this workspace
                # (peer agent messages only; skip canvas-user messages and self-notify).
                source_id = row.get("source_id") or ""
                if not source_id or source_id == self.workspace_id:
                    continue

                # Skip non-result rows. A peer send that merely got QUEUED
                # because we were busy ("queued: target busy"), or a receive
                # that FAILED (status present and != "ok" — e.g. the 300s
                # "timeout awaiting response headers"), is not a peer response
                # with content. Harvesting it as a "completed" result wakes
                # the agent to "process" transient backpressure, generating
                # more sends and more backpressure: a self-amplifying replay
                # storm (observed: one notification wrapping 12x identical
                # "queued: target busy" echoes). Mark seen so the cursor still
                # advances; never surface it as a result.
                row_status = row.get("status") or ""
                if (row_status and row_status != "ok") or "queued: target busy" in (row.get("summary") or ""):
                    self._seen_activity_ids.add(activity_id)
                    continue

                self._seen_activity_ids.add(activity_id)
                summary = row.get("summary") or ""
                # Extract response text from request_body if available.
                # Shape mirrors inbox._extract_text: walk parts for "text" field.
                response_text = summary
                request_body = row.get("request_body")
                if isinstance(request_body, dict):
                    params_obj = request_body.get("params")
                    if isinstance(params_obj, dict):
                        msg = params_obj.get("message")
                        if isinstance(msg, dict):
                            parts = msg.get("parts") or []
                            texts = []
                            for p in (parts if isinstance(parts, list) else []):
                                if isinstance(p, dict) and p.get("kind") == "text" or p.get("type") == "text":
                                    t = p.get("text", "")
                                    if t:
                                        texts.append(t)
                            if texts:
                                response_text = " ".join(texts)

                new_results.append({
                    "delegation_id": activity_id,  # Use activity ID as pseudo-delegation ID
                    "target_id": source_id,
                    "source_id": self.workspace_id,
                    "status": "completed",
                    "summary": summary,
                    "response_preview": response_text[:4096],
                    "error": "",
                    "timestamp": time.time(),
                })

            if not new_results:
                return

            # Persist cursor so restarts don't re-process these rows.
            if last_id:
                try:
                    with open(_ACTIVITY_DELEGATION_CURSOR_FILE, "w") as f:
                        # Keep cursor as comma-joined IDs; truncate if over 100KB.
                        cursor_str = ",".join(sorted(self._seen_activity_ids))
                        if len(cursor_str) > 102_400:
                            # Evict oldest half when cursor file grows too large.
                            sorted_ids = sorted(self._seen_activity_ids)
                            self._seen_activity_ids = set(sorted_ids[len(sorted_ids) // 2:])
                            cursor_str = ",".join(sorted(self._seen_activity_ids))
                        f.write(cursor_str)
                except Exception:
                    pass  # Non-fatal; next cycle will retry

            # Append to results file and trigger self-message (mirrors _check_delegations).
            with open(DELEGATION_RESULTS_FILE, "a") as f:
                for r in new_results:
                    f.write(json.dumps(r) + "\n")
            logger.info(
                "Heartbeat: %d new a2a_receive delegation results from activity_logs — "
                "triggering self-message",
                len(new_results),
            )

            # Build and send self-message to wake the agent.
            summary_lines = []
            for r in new_results:
                line = f"- [completed] Peer response from {r['target_id'][:8]}: {r['summary'][:80] or '(no summary)'}"
                if r.get("error"):
                    line += f"\n  Error: {r['error'][:100]}"
                summary_lines.append(line)

            # Look up parent name (reuse cached value from _check_delegations if set).
            if self._parent_name is None:
                try:
                    parent_resp = await client.get(
                        f"{self.platform_url}/workspaces/{self.workspace_id}",
                        headers=auth_headers(),
                    )
                    if parent_resp.status_code == 200:
                        parent_id = parent_resp.json().get("parent_id", "")
                        if parent_id:
                            parent_info = await client.get(
                                f"{self.platform_url}/workspaces/{parent_id}",
                                headers=auth_headers(),
                            )
                            if parent_info.status_code == 200:
                                self._parent_name = parent_info.json().get("name", "")
                    if self._parent_name is None:
                        self._parent_name = ""
                except Exception:
                    self._parent_name = ""
            parent_name = self._parent_name or ""

            report_instruction = ""
            if parent_name:
                report_instruction = (
                    f"\n\nIMPORTANT: Delegate a summary of these results to your parent "
                    f"'{parent_name}' using delegate_task. Also use send_message_to_user "
                    f"to notify the user."
                )
            else:
                report_instruction = (
                    "\n\nReport results using send_message_to_user to notify the user."
                )

            trigger_msg = (
                "Delegation results are ready (from a2a_receive via activity_logs). "
                "Review them and take appropriate action:\n"
                + "\n".join(summary_lines)
                + report_instruction
            )

            now = time.time()
            if now - self._last_self_message_time < SELF_MESSAGE_COOLDOWN:
                logger.debug(
                    "Heartbeat: self-message cooldown active; "
                    "a2a_receive results will be retried next cycle"
                )
            else:
                self._last_self_message_time = now
                try:
                    await client.post(
                        f"{self.platform_url}/workspaces/{self.workspace_id}/a2a",
                        json={
                            "method": "message/send",
                            # #2251: single model-based builder — params
                            # generated FROM the receiver's a2a-sdk v0.3
                            # SendMessageRequest schema.
                            "params": build_message_send_params(trigger_msg),
                        },
                        headers=self_source_headers(self.workspace_id),
                        timeout=120.0,
                    )
                    logger.info("Heartbeat: a2a_receive self-message sent")
                except Exception as e:
                    logger.warning("Heartbeat: failed to send a2a_receive self-message: %s", e)

            # Also notify the user via canvas — failure-class only. The
            # a2a_receive path currently hardcodes status="completed"
            # (peer responses are by definition successful deliveries),
            # so this loop is normally a no-op now — that is the fix
            # for task #384 canvas pollution. If a future change emits
            # failure rows here, the gate + double-prefix dedupe below
            # keep them clean.
            for r in new_results:
                if r.get("status") not in NOTIFY_STATUSES:
                    continue
                try:
                    summary = (r.get("summary") or "").lstrip()
                    # Strip any pre-existing "Delegation completed:" prefix
                    # on the upstream summary so we don't double up.
                    if summary.startswith(_DELEGATION_PREFIX):
                        summary = summary[len(_DELEGATION_PREFIX):].lstrip()
                    reason = r.get("error") or summary or "(no detail)"
                    msg = f"Delegation {r['status']}: {reason[:200]}"
                    await client.post(
                        f"{self.platform_url}/workspaces/{self.workspace_id}/notify",
                        json={"message": msg, "type": "delegation_result"},
                        headers=auth_headers(),
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.debug("Activity delegation check error: %s", e)
