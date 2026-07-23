"""Workspace runtime entry point.

Loads config -> discovers adapter -> setup -> create executor -> wrap in A2A -> register -> heartbeat.
"""

import asyncio
import os
import socket
from collections.abc import Mapping

import httpx
import uvicorn
# KI-009 a2a-sdk v1 migration: A2AStarletteApplication removed; use Starlette route factory
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, AgentInterface
from starlette.applications import Starlette

from molecule_runtime.adapters import get_adapter, AdapterConfig
from molecule_runtime.agents_md import generate_agents_md
from molecule_runtime.config import load_config
from molecule_runtime.heartbeat import HeartbeatLoop
from molecule_runtime.preflight import run_preflight, render_preflight_report
from molecule_runtime.builtin_tools.awareness_client import get_awareness_config
import uuid as _uuid

from molecule_runtime.builtin_tools.telemetry import setup_telemetry, make_trace_middleware
from molecule_runtime.policies.namespaces import resolve_awareness_namespace

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # SSOT typed payload (molecule-contracts / RFC molecule-core#3285), published
    # as `molecule-ai-contracts` on the gitea PyPI registry. Imported under
    # TYPE_CHECKING ONLY — no hard runtime dependency (see the `[contracts]` extra
    # in pyproject.toml). Only `RegisterRequest` is pulled in here to avoid a name
    # clash with the unrelated `a2a.types.AgentCard` imported above. The boot
    # register body below is type-checked against the SSOT contract; mirrors the
    # molecule-ai-sdk #36 adoption. NOTE: `register_body` is a function-local
    # annotation, which CPython never evaluates at runtime, so this stays a pure
    # type-checker-only reference even without `from __future__ import annotations`.
    from molecule_ai_contracts.workspace_comms_gen import RegisterRequest

# Holds the background loaded_mcp_tools init-enumeration task so it is not
# garbage-collected before it completes (asyncio only keeps weak refs to tasks).
_LOADED_MCP_TOOLS_BG_TASK = None  # type: "asyncio.Task | None"


def _is_privileged_setup_failure(setup_err: BaseException) -> bool:
    """Return True when *setup_err* is a privileged-plugin install failure.

    Privileged-plugin setup failures (currently ``molecule-platform-mcp``) must
    abort the runtime boot loudly rather than degrading to
    "reachable-but-misconfigured". The dedicated ``PrivilegedPluginInstallError``
    subclass (from ``adapter_base``) is the discriminator.

    Extracted as a pure function so ``tests/test_main_privileged_plugin_failure.py``
    can exercise the exact rule ``main.py`` uses without duplicating it.
    """
    from molecule_runtime.adapter_base import PrivilegedPluginInstallError

    return isinstance(setup_err, PrivilegedPluginInstallError)


def _probe_wiring_failure_is_fatal() -> bool:
    """Return True when failing to wire the management-MCP gate probe must ABORT
    the boot (fail-closed) rather than degrade to the claude settings.json
    fallback (fail-open).

    The probe is the ONLY runtime-agnostic way the RCA#2970 online gate can tell
    whether the management MCP is wired into the file THIS runtime actually
    reads. If wiring it fails on a PLATFORM agent (concierge) and we silently
    fall back to the claude settings.json check, a non-claude concierge
    (codex/openclaw/…) is judged against a file it never reads — the #3159
    cross-runtime mis-attribution (a healthy concierge false-negatived, or a
    wrong "present" asserted). That is a fail-OPEN of the platform gate, so on a
    platform agent it is FATAL. An ordinary (non-platform) workspace does not
    gate on the management MCP, so the fallback is harmless there and a probe
    hiccup must not block its boot.

    Extracted as a pure function so ``tests/test_main_privileged_plugin_failure.py``
    (and friends) can exercise the exact rule ``main.py`` uses without
    duplicating it. The runtime-side kind=platform signal is
    ``on_platform_agent_image()`` (the MOLECULE_PLATFORM_AGENT_IMAGE_BAKED env
    marker core sets for concierge workspaces)."""
    from molecule_runtime.platform_agent_identity import on_platform_agent_image

    return on_platform_agent_image()


from molecule_runtime.initial_prompt import (
    mark_initial_prompt_attempted,
    resolve_initial_prompt_marker,
)
from molecule_runtime.a2a_client import build_message_send_params
from molecule_runtime.a2a_executor import (
    A2A_MESSAGE_SOURCE_TYPE,
    A2A_SOURCE_SELF_IDLE,
    A2A_SOURCE_SELF_LIFECYCLE,
)
from molecule_runtime.platform_agent_identity import identity_gate_payload
from molecule_runtime.platform_auth import auth_headers, self_source_headers
from molecule_runtime.transcript_auth import transcript_authorized as _transcript_authorized


def get_machine_ip() -> str:  # pragma: no cover
    """Get the machine's IP for A2A discovery.

    Uses a UDP "connection" to a public probe address to discover the local
    routable IP without sending any actual traffic.  The probe host/port are
    configurable via ``MOLECULE_NETWORK_PROBE_HOST`` and
    ``MOLECULE_NETWORK_PROBE_PORT`` so operators in air-gapped or restricted
    networks can point the probe at an internal gateway instead of a public
    DNS server.
    """
    probe_host = os.environ.get("MOLECULE_NETWORK_PROBE_HOST", "8.8.8.8")
    try:
        probe_port = int(os.environ.get("MOLECULE_NETWORK_PROBE_PORT", "80"))
    except ValueError:
        probe_port = 80
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((probe_host, probe_port))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        # Fall back to the ONE loopback token the platform's push-guard SSRF
        # allowlist accepts by name (workspace-server validateAgentURL allows
        # host=="localhost", but rejects the literal 127.0.0.1 as a loopback
        # block). Returning "127.0.0.1" here is what produced the 400
        # `url_validate_failed` on a localbuild dev box that never got
        # MOLECULE_WORKSPACE_URL injected — a probe failure would advertise an
        # un-registerable self-URL. "localhost" registers; the operator still
        # sees the loud warning from resolve_workspace_url naming the missing
        # env var for the containerized-platform case.
        return "localhost"


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


def resolve_workspace_url(
    env: Mapping[str, str], port: int, delivery_mode: str = "push"
) -> str:
    """Resolve the externally-advertised A2A URL the agent registers (runtime#95).

    Precedence:
      1. ``MOLECULE_WORKSPACE_URL`` — a platform-injected, externally-reachable
         URL (full scheme+host, e.g. a per-workspace Cloudflare tunnel
         ``https://ws-<id>.<appDomain>``). Used VERBATIM — no ``:port`` is
         appended, since the tunnel/proxy fronts the agent's port.
      2. Fallback: ``http://<HOSTNAME-or-machine-ip>:<port>``.

    Why (1) exists: the fallback advertises the host's own name, which may resolve
    only inside that host's private network. A control plane on another network
    then gets DNS failure and rejects ``/registry/register`` with 400, leaving the
    workspace undialable. A platform-injected reachable URL fixes cross-network
    push delivery while same-network workspaces keep the private-host fallback.

    Push-mode loopback guard (registration-400 fix): under push delivery the
    platform DIALS this URL, so its write-time SSRF guard (workspace-server
    ``validateAgentURL``) rejects a loopback host — it blocks the literal
    ``127.0.0.1`` but ALLOWS ``localhost`` by name. On a localbuild/dev box that
    was never handed ``MOLECULE_WORKSPACE_URL``, the fallback could resolve to
    ``127.0.0.1`` (via ``get_machine_ip``'s probe-failure branch) and register
    would 400 ``url_validate_failed``. When we are about to advertise a loopback
    host under push, we (a) emit ONE loud warning naming the missing
    ``MOLECULE_WORKSPACE_URL`` (the fix for the containerized-platform case is to
    inject a routable host there) and (b) substitute ``localhost`` — the one
    loopback token the push guard accepts by name — so the dev box registers
    instead of 400-ing. In poll mode the platform never dials the URL, so the
    fallback is left untouched.
    """
    injected = (env.get("MOLECULE_WORKSPACE_URL") or "").strip()
    if injected:
        return injected
    machine = (env.get("HOSTNAME") or "").strip() or get_machine_ip()
    if delivery_mode == "push" and machine.lower() in _LOOPBACK_HOSTS:
        # Advertising a loopback self-URL under push → the platform can't dial
        # it and validateAgentURL 400s (except host=="localhost"). Warn loud +
        # coerce to the accepted "localhost" token so a dev/localbuild box that
        # never received MOLECULE_WORKSPACE_URL still registers on first attempt.
        print(
            "WARNING: no MOLECULE_WORKSPACE_URL injected and the resolved boot "
            f"host is loopback ({machine!r}); advertising http://localhost:{port} "
            "so /registry/register isn't rejected by the platform's SSRF push "
            "guard. Set MOLECULE_WORKSPACE_URL to a platform-reachable URL "
            "(e.g. the per-workspace tunnel, or the Docker bridge gateway for a "
            "containerized platform) for cross-host push delivery.",
            flush=True,
        )
        machine = "localhost"
    return f"http://{machine}:{port}"


def _check_delegation_results_pending() -> bool:
    """Check if there are unconsumed delegation results waiting.

    Reads ``DELEGATION_RESULTS_FILE``.  Returns ``True`` if the file
    exists and contains non-whitespace content (after stripping) — meaning
    the idle loop should skip this tick.  Returns ``False`` if the file is
    absent, empty, or contains only whitespace.

    The extracted form lets unit tests call this directly rather than mirroring
    the logic (anti-pattern flagged as #401).
    """
    # RC #203: resolve the SAME queue the heartbeat writer and the executor
    # reader use — kernel-ON the durable mailbox queue, kernel-OFF legacy /tmp.
    from molecule_runtime.heartbeat import _delegation_results_file

    try:
        with open(_delegation_results_file()) as rf:
            rf.seek(0)
            return bool(rf.read().strip())
    except FileNotFoundError:
        return False


async def register_with_platform(
    client,
    *,
    platform_url: str,
    workspace_id: str,
    workspace_url: str,
    agent_card: dict,
    headers: dict,
    max_attempts: int = 8,
    delivery_mode: str = "push",
) -> bool:
    """POST the boot registration to ``/registry/register`` with bounded
    exponential backoff until it gets a 2xx.

    internal#688: the original boot-register fired EXACTLY ONCE. If the
    tenant orchestrator (workspace-server) was momentarily down — e.g. a
    workspace-recreate sweep stopped its service, so Cloudflare returned 530 /
    tunnel-error 1033 — the one POST failed, a warning was printed, and the
    workspace proceeded with ``workspaces.url`` left empty in the platform
    DB. The workspace heartbeats fine and shows ``online``, but it's
    undialable: schedule ticks throw ``workspace has no URL`` and A2A
    dispatch can't reach it. Before this retry path, recovery required restarting
    every affected workspace container.

    Both failure shapes from the incident are retried here:
      * a transport-level exception (orchestrator unreachable), and
      * a transient server-side HTTP response — 5xx, plus Cloudflare edge
        errors 520–530 (e.g. 530 while the origin is down) — the old code
        only special-cased ``== 200`` and silently treated every other
        status as "registered".

    review 7658: a *client* error (4xx other than the Cloudflare 520–530
    band) is NOT transient — it almost always means misconfiguration (bad
    platform URL / wrong or missing auth → 401/403/404). Retrying it just
    masks the real cause behind ~91s of backoff, so we short-circuit: log
    the status (code only, no secrets) and return False immediately.

    Bounded by ``max_attempts`` so a permanently-misconfigured platform URL
    can't wedge boot forever. On exhaustion we return False (the caller
    proceeds to start the heartbeat — the workspace can still serve traffic,
    and the server-side heartbeat backfill is the belt-and-suspenders net)
    rather than raising, which would crash the container.

    Returns True once a 2xx is observed (token capture done here on the
    successful response); False if every attempt failed.
    """
    last_detail = "no attempts made"
    for attempt in range(max_attempts):
        try:
            # Typed against the SSOT contract (molecule-contracts RegisterRequest)
            # for drift-prevention; the wire payload is unchanged.
            register_body: RegisterRequest = {
                "id": workspace_id,
                "url": workspace_url,
                "agent_card": agent_card,
                **identity_gate_payload(),
            }
            # E1 + sticky-poll fix (prod agents-team incident 2026-07-05):
            # ALWAYS declare the resolved delivery mode — push AND poll.
            # RegisterRequest already permits the key (mcp_heartbeat.py sets
            # exactly this for the standalone path) — no contract change, and
            # the server validates both values (registry.go IsValidDeliveryMode).
            #
            # The old shape only declared "poll" and stayed silent for push.
            # The platform's resolveDeliveryMode gives an EMPTY payload value
            # the row's existing delivery_mode — so a workspace whose row was
            # ever set to poll (an earlier poll-mode run, an external bridge
            # experiment) could NEVER heal back to push by re-registering.
            # Live consequence: the tenant staged every inbound A2A in
            # activity_logs for a poller the push-mode runtime never starts —
            # the agent was silently deaf to all chat/A2A until a manual DB
            # flip. Declaring the actual mode on every boot makes the row
            # converge to the runtime's real behavior instead of wedging on
            # a stale value. (External laptop bridges register through their
            # own path and still declare poll explicitly — unaffected.)
            if delivery_mode in ("push", "poll"):
                register_body["delivery_mode"] = delivery_mode
            resp = await client.post(
                f"{platform_url}/registry/register",
                json=register_body,
                headers=headers,
            )
            status = resp.status_code
            if 200 <= status < 300:
                print(f"Registered with platform: {status}")
                # Phase 30.1 — capture the auth token issued at first
                # register. The platform only mints one on first register
                # per workspace, so a subsequent restart gets an empty
                # auth_token and we keep using the on-disk copy.
                try:
                    body = resp.json()
                    tok = body.get("auth_token")
                    if tok:
                        from molecule_runtime.platform_auth import save_token
                        save_token(tok)
                        # CWE-532 (task #344): redacted — never log token.
                        print("Saved workspace auth token (value=[REDACTED])")
                    # RFC #2312 PR-F: persist platform_inbound_secret if the
                    # platform supplied one. Idempotent.
                    inbound = body.get("platform_inbound_secret")
                    if inbound:
                        from molecule_runtime.platform_inbound_auth import save_inbound_secret
                        save_inbound_secret(inbound)
                        # CWE-532 (task #344): redacted — never log secret.
                        print("Saved platform_inbound_secret (value=[REDACTED])")
                except Exception as parse_exc:
                    print(f"Warning: couldn't parse register response for token: {parse_exc}")
                return True
            # internal#688 review (review 7658): classify the failure.
            # Cloudflare edge errors 520–530 (e.g. 530 / tunnel 1033 while
            # the orchestrator origin is down) are TRANSIENT — keep retrying.
            if 520 <= status <= 530:
                last_detail = f"HTTP {status} (Cloudflare edge — transient)"
            elif 400 <= status < 500:
                # A real 4xx (401/403/404/…) means misconfiguration, not a
                # momentary outage: retrying just masks it behind ~91s of
                # backoff. Fail fast and log the status so the misconfig is
                # visible. Status code only — no headers/body — so no secret
                # (e.g. the bearer in `headers`) can leak (CWE-532).
                print(
                    f"Register: HTTP {status} is a client error "
                    f"(misconfiguration) — not retrying; proceeding "
                    f"(heartbeat backfill is the recovery path)"
                )
                return False
            else:
                # 5xx (and any other non-2xx) — transient server-side, retry.
                last_detail = f"HTTP {status}"
        except Exception as e:  # transport error — orchestrator unreachable
            last_detail = repr(e)

        if attempt < max_attempts - 1:
            # Exponential backoff capped at 30s: 1, 2, 4, 8, 16, 30, 30…
            # Mirrors the initial-prompt retry pattern already in this file.
            delay = min(2 ** attempt, 30)
            print(
                f"Register: attempt {attempt + 1}/{max_attempts} failed "
                f"({last_detail}), retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
        else:
            print(
                f"Warning: failed to register with platform after "
                f"{max_attempts} attempts ({last_detail}) — proceeding; "
                f"heartbeat backfill is the recovery path"
            )
    return False


# ---------------------------------------------------------------------------
# E1 — poll-mode inbound delivery (opt-in; default is push).
# ---------------------------------------------------------------------------
# Host of the LOCAL executor the poll consumer posts to. MUST be loopback: a
# post to the platform proxy (/workspaces/<id>/a2a) would write a NEW
# a2a_receive activity row that the poller re-fetches => infinite loop. A direct
# 127.0.0.1 post hits the boot_routes DefaultRequestHandler -> executor and
# creates no activity row, so the cursor advances exactly once (no echo).
LOCAL_EXECUTOR_HOST = "127.0.0.1"

# Pre-bind retry budget for the local self-POST. The inbox poller fires its
# first poll the instant its daemon thread starts; if uvicorn hasn't finished
# binding 127.0.0.1:{port} yet, the consumer's self-POST raises ConnectError
# (connection refused). We retry with a bounded backoff so a message staged in
# that pre-bind window is held in-flight until the local executor is listening
# — never dropped while the inbox cursor advances past it (the E1 startup-race
# fix). Bounded so a genuinely dead executor can't wedge the poller callback
# forever: 40 * 0.25s ≈ 10s, comfortably longer than a normal uvicorn bind.
_LOCAL_POST_CONNECT_RETRIES = 40
_LOCAL_POST_CONNECT_BACKOFF_SECONDS = 0.25


def resolve_delivery_mode(env: Mapping[str, str], config_delivery_mode: str | None) -> str:
    """Resolve the effective inbound delivery mode.

    Precedence (env override wins, mirroring the LOG_LEVEL pattern): the
    ``MOLECULE_DELIVERY_MODE`` env var, then ``config.a2a.delivery_mode``, else
    ``"push"``. Lower-cased so ``"Poll"`` / ``"PUSH"`` resolve consistently.
    """
    return (env.get("MOLECULE_DELIVERY_MODE") or config_delivery_mode or "push").strip().lower()


async def _post_polled_message_to_local_executor(port: int, workspace_id: str, text: str):
    """POST one polled inbox message to the LOCAL executor as a JSON-RPC
    ``message/send`` — NEVER the platform proxy (see LOCAL_EXECUTOR_HOST).

    Same envelope shape the direct-peer delegate path uses (a2a_client) so the
    boot_routes DefaultRequestHandler accepts it. No self-source ``source_type``
    marker is stamped: a polled peer/canvas message is a real inbound turn, not a
    routine self-ping, so it must not be dropped by the non-blocking fast-path.
    """
    url = f"http://{LOCAL_EXECUTOR_HOST}:{port}/"
    body = {
        "jsonrpc": "2.0",
        "id": str(_uuid.uuid4()),
        "method": "message/send",
        "params": build_message_send_params(text),
    }
    headers = {"Content-Type": "application/json", **self_source_headers(workspace_id)}
    # Retry on connection-refused (uvicorn not bound yet). A ConnectError here is
    # the pre-bind window, not a real delivery failure — backing off and retrying
    # keeps the staged message from being lost while the inbox cursor advances.
    # Any non-connection error (e.g. a real 4xx/5xx from the executor) returns on
    # the first attempt as before. See _LOCAL_POST_CONNECT_* for the bound.
    last_exc: Exception | None = None
    for _attempt in range(_LOCAL_POST_CONNECT_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                return resp.status_code
        except httpx.ConnectError as exc:
            last_exc = exc
            await asyncio.sleep(_LOCAL_POST_CONNECT_BACKOFF_SECONDS)
    # Budget exhausted — the executor never came up. Log + give up (best-effort;
    # the caller's run_coroutine_threadsafe must never see this crash the poller).
    print(
        f"poll-consumer: local executor {LOCAL_EXECUTOR_HOST}:{port} unreachable "
        f"after {_LOCAL_POST_CONNECT_RETRIES} attempts ({last_exc}); message dropped",
        flush=True,
    )
    return None


def make_poll_inbox_consumer(port: int, workspace_id: str, loop):
    """Build the inbox notification callback for poll mode.

    The callback runs on the poller DAEMON thread (``inbox.record`` fires it),
    so it marshals the httpx post onto the main asyncio ``loop`` via
    ``run_coroutine_threadsafe`` — the post then shares the event loop instead of
    racing it (the same marshalling rationale as the idle/initial-prompt
    self-sends). Best-effort: a scheduling failure logs and returns so the poller
    thread never crashes.
    """
    def _consume(msg: dict) -> None:
        text = (msg or {}).get("text") or ""
        if not text:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                _post_polled_message_to_local_executor(port, workspace_id, text),
                loop,
            )
        except Exception as exc:  # noqa: BLE001 — callback must never crash the poller
            print(f"poll-consumer: failed to schedule local executor post: {exc}", flush=True)

    return _consume


def maybe_start_poll_delivery(
    delivery_mode: str,
    platform_url: str,
    workspace_id: str,
    port: int,
    loop,
    *,
    start_pollers=None,
    set_callback=None,
) -> bool:
    """Start poll-mode inbound delivery when ``delivery_mode == "poll"``.

    Registers the inbox consumer (drives the local executor) then starts the
    pollers via the SAME SSOT helper the standalone path uses
    (``mcp_inbox_pollers.start_inbox_pollers``) so cursor-file semantics,
    activation idempotency and multi-workspace handling are identical. Returns
    True when poll delivery was started, False for push (default) — in push mode
    this whole branch is dead code, preserving the never-run-both invariant
    (inbox.py module docstring). ``start_pollers`` / ``set_callback`` are
    injectable for tests.
    """
    if delivery_mode != "poll":
        return False
    if start_pollers is None:
        from molecule_runtime.mcp_inbox_pollers import start_inbox_pollers
        start_pollers = start_inbox_pollers
    if set_callback is None:
        from molecule_runtime.inbox import set_notification_callback
        set_callback = set_notification_callback
    set_callback(make_poll_inbox_consumer(port, workspace_id, loop))
    start_pollers(platform_url, [workspace_id])
    print(
        f"Delivery mode: poll — inbox poller started for {workspace_id} "
        f"(consumer posts to {LOCAL_EXECUTOR_HOST}:{port})",
        flush=True,
    )
    return True


async def start_poll_delivery_when_bound(
    server,
    delivery_mode: str,
    platform_url: str,
    workspace_id: str,
    port: int,
    loop,
    *,
    poll_interval: float = 0.05,
    max_wait_seconds: float = 60.0,
    start_pollers=None,
    set_callback=None,
) -> bool:
    """Start poll-mode inbound delivery, but ONLY after uvicorn has bound.

    The E1 startup-race fix. The inbox poller fires its first poll the instant
    its daemon thread starts; if that happens before uvicorn binds
    ``127.0.0.1:{port}`` the consumer's local self-POST hits a closed socket and
    the staged message is dropped while the inbox cursor advances past it.
    ``uvicorn.Server`` flips ``started`` True once its socket is bound and
    accepting, so gating the poller on it guarantees the consumer only ever posts
    to a listening executor — no staged message is lost.

    No-op (returns False) in push mode (default), preserving the never-run-both
    invariant. ``max_wait_seconds`` is a defensive bound: if startup somehow
    stalls we still start the poller (the consumer's bounded connection-refused
    retry is the backstop) rather than never delivering. ``poll_interval`` /
    ``start_pollers`` / ``set_callback`` are injectable for tests.
    """
    if delivery_mode != "poll":
        return False
    from molecule_runtime.plugin_daemons import wait_until_server_bound
    bound = await wait_until_server_bound(
        server, max_wait=max_wait_seconds, poll_interval=poll_interval
    )
    if not bound:
        print(
            "Delivery mode: poll — uvicorn not reported bound after "
            f"{max_wait_seconds:.0f}s; starting poller anyway "
            "(consumer retries connection-refused as backstop)",
            flush=True,
        )
    return maybe_start_poll_delivery(
        delivery_mode,
        platform_url,
        workspace_id,
        port,
        loop,
        start_pollers=start_pollers,
        set_callback=set_callback,
    )


async def main(prepare_only: bool = False):  # pragma: no cover
    # This process is always inside a tenant workspace or concierge trust boundary.
    # Remove operator-only capabilities before any helper, SDK, CLI, or MCP child
    # can inherit them. Core/CP provisioning deny them too; this is the runtime's
    # independent fail-closed guard if an ambient value is ever misconfigured.
    from molecule_runtime.privileged_mcp_env import (
        scrub_tenant_forbidden_process_env,
    )
    scrub_tenant_forbidden_process_env()

    workspace_id = os.environ.get("WORKSPACE_ID", "")
    if not workspace_id:
        raise SystemExit("FATAL: WORKSPACE_ID env var is not set. Aborting.")
    config_path = os.environ.get("WORKSPACE_CONFIG_PATH", "/configs")

    # 0.0 Config-relay fetch prelude (cf-r2-relay-config-secret-delivery).
    # When the CP provisions with the R2 relay ENABLED it stages this
    # workspace's {config.yaml + prompts/* + secrets} to a transient R2 object
    # and injects MOLECULE_CONFIG_RELAY_URI (a short-TTL presigned GET) + _SHA256
    # + _ACK_TOKEN. Fetch the bundle over the presigned HTTPS URL, verify its
    # sha256, unpack it into config_path, then POST /cp/workspaces/<id>/relay-ack
    # so the CP deletes the object. Fail-CLOSED on a genuine fetch/integrity
    # failure (a mis-delivered config aborts boot rather than boot a mis-
    # configured agent); transient/cold-presign failures retry with backoff; the
    # ack is best-effort (CP reaper + bucket lifecycle are the backstops). INERT
    # unless MOLECULE_CONFIG_RELAY_URI is present — the CP injects it only when
    # its MOLECULE_CONFIG_RELAY_ENABLE flag is on, so this is a no-op until the
    # operator flips that flag. MUST run BEFORE any step reads config_path
    # (credential helper, npm auth, declared-plugins, load_config).
    from molecule_runtime.config_relay import run_config_relay_prelude
    run_config_relay_prelude(workspace_id=workspace_id, config_path=config_path)

    # Docker-aware default — host.docker.internal resolves the platform service
    # from inside the Docker network mesh; falls back to localhost for local dev.
    if os.path.exists("/.dockerenv") or os.environ.get("DOCKER_VERSION"):
        platform_url = os.environ.get("PLATFORM_URL", "http://host.docker.internal:8080")
    else:
        platform_url = os.environ.get("PLATFORM_URL", "http://localhost:8080")
    awareness_config = get_awareness_config()

    # 0. Initialise OpenTelemetry (no-op if packages not installed)
    setup_telemetry(service_name=workspace_id)

    # 0.1 Normalise LLM auth env vars based on token type.
    # Platform stores tokens as ANTHROPIC_AUTH_TOKEN, but the Claude SDK/CLI
    # expects different env vars per token kind (OAuth vs API key vs proxy).
    # Doing this early means every downstream adapter/executor sees a
    # consistent, correct env — no per-adapter detection needed.
    from molecule_runtime.llm_auth import normalise_llm_env
    # NB: the actual call is deferred to step 0.1b (after load_config) so
    # the resolved provider (SSOT) can be passed. Provider-scoped clearing
    # of an inherited CLAUDE_CODE_OAUTH_TOKEN prevents the silent Anthropic
    # drain (2026-05-28) on non-Anthropic workspaces.

    # 0.2 GitHub credential helper installer — extracts bundled .sh scripts,
    # configures git, starts refresh daemon, primes gh CLI. Eliminates the
    # per-template wiring that caused #1933 (claude-code-default template
    # shipped without the wiring; 39 workspaces lost their tokens after the
    # ~60min installation-token TTL). Fails-soft so a missing git/gh binary
    # doesn't block runtime startup. See credential_helper.py for the full
    # rationale.
    from molecule_runtime.credential_helper import install_credential_helper
    install_credential_helper()

    # 0.2b Gitea npm-registry auth — the npm companion to the git credential
    # helper above. The concierge's management MCP is `npx @molecule-ai/mcp-server`,
    # an npm fetch from the PRIVATE gitea registry; without registry auth npx only
    # saw the unauth view and ETARGET'd the current version → MCP never started →
    # fleet-wide concierge `degraded` (RCA 2026-06-24). Writes ~/.npmrc with the
    # gitea registry + _authToken from the SAME gitea read token (SSOT). No-op
    # when no gitea token is present; fail-soft. See npm_auth.py.
    from molecule_runtime.npm_auth import install_npm_gitea_auth
    install_npm_gitea_auth()

    # 0.2c Declared-plugins boot-install (RFC#2843 #32 — base-runtime hoist).
    # Port of the proven shell block (wt-claude-code/entrypoint.sh:214-284) into
    # a Python source-provider module so EVERY runtime gets the boot-install
    # uniformly — no per-template fork. Runs AFTER npm_auth (so a plugin setup.sh
    # that does an npm fetch is already authed) and BEFORE load_config /
    # adapter.setup, so the plugins land on disk before _common_setup's
    # load_plugins reads <config_path>/plugins and install_plugins_via_registry
    # wires their MCP/skills. No-op when MOLECULE_DECLARED_PLUGINS is empty;
    # fail-soft (never raises into boot). During the template cutover BOTH this
    # and the shell block run idempotently (shell first as root pre-gosu, this
    # second as the agent uid here) — the second simply rebuilds the same tree.
    from molecule_runtime.plugin_sources import install_declared_plugins
    # BOOT_STEP 1/8 (task #51) — "Install plugins". emit_boot_step is
    # concierge-gated + fire-and-forget + 404-safe, so these are pure additive
    # telemetry: no control-flow, timing, or behavior change to the boot itself.
    from molecule_runtime.boot_step_emit import emit_boot_step
    _BOOT_TOTAL = 8
    emit_boot_step("PLG", "Install plugins", "running", step=1, total=_BOOT_TOTAL)
    try:
        print(install_declared_plugins().summary(), flush=True)
        emit_boot_step("PLG", "Install plugins", "ok", step=1, total=_BOOT_TOTAL)
    except Exception as e:  # noqa: BLE001 — boot-install must never block boot
        print(f"declared-plugins boot-install failed (non-fatal): {e}", flush=True)
        emit_boot_step(
            "PLG", "Install plugins", "failed", step=1, total=_BOOT_TOTAL,
            message=f"{type(e).__name__}: {e}",
        )

    # 0.2d Self-reprovision wake detection (design §5.2 — proactive wake).
    # Diff the plugins tree boot-install just (re)built against last boot's
    # durable record. READ-ONLY when there are additions: the diff is NOT
    # consumed here — consumption is staged at 10b2 (attempt-marked once the
    # adapter is known ready, consumed on send success, replay bounded by
    # MAX_WAKE_ATTEMPTS) so a misconfigured or slow boot DEFERS the
    # announcement instead of eating it. First boot / no-additions boots
    # refresh the state silently (greeting territory, not ours).
    # MUST run right here: after install_declared_plugins (the tree is
    # final) and before load_config may reassign config_path (the state
    # file lives beside the env-resolved plugins dir it describes).
    from molecule_runtime.reprovision_wake import prepare_wake_plan
    wake_plan = prepare_wake_plan()

    # 0.3 Pre-commit hook installer — refuses commits that add internal-flavored
    # paths or known secret shapes to any git repo the workspace touches. Lifted
    # into runtime so all templates get the gate without per-Dockerfile wiring.
    # Companion to credential_helper (#1933) + defense-in-depth for #2090-class
    # credential leaks. See precommit_hook.py for the full hook contract.
    from molecule_runtime.precommit_hook import install_pre_commit_hook
    install_pre_commit_hook()

    # 0a. Fix /workspace perms before any agent code runs. Docker ships
    # named volumes as root:root 755 — without this the non-root agent
    # user can't write files the user asked it to produce, and the
    # "agent → file → user downloads" flow dead-ends at a bash "permission
    # denied". Best-effort: no-ops silently if molecule-runtime itself
    # isn't root (template's own start.sh should have handled it there).
    from molecule_runtime.executor_helpers import ensure_workspace_writable
    ensure_workspace_writable()

    # 0b. Mailbox kernel wiring (MUST-FIX 1/2/5). NATIVE default-ON (operator
    # ruling 2026-07-13): install() runs the ORDERING-CRITICAL §7.2 legacy-state
    # migration (must precede the inbox poller + heartbeat reads below), arms
    # the process-global turn lease, and probes durability. Under the
    # MOLECULE_MAILBOX_KERNEL=0 emergency opt-out it installs nothing and every
    # kernel helper stays a no-op (legacy flow byte-identical). The runaway-guard
    # should_halt() pre-check (MUST-FIX 2) is kept in the idle loop below AND is
    # centralized in kernel.should_inject_autonomous_turn for any new autonomous
    # injector; active_tasks increment/decrement is preserved throughout.
    from molecule_runtime import kernel as _mailbox_kernel
    _mailbox_kernel.install()

    # 1. Load config
    config = load_config(config_path)
    # 1.0  Adopt the resolved config base. When the asset-fetcher fails to
    # deliver /configs/config.yaml, load_config() falls back to the image-baked
    # /opt/molecule-platform-agent-template/ identity and reassigns
    # WorkspaceConfig.config_path to that directory. We MUST follow the
    # reassignment here — every downstream consumer in main() (run_preflight,
    # generate_agents_md, AdapterConfig, build_system_prompt, ExecRead,
    # load_skills, mcp_servers) resolves config-relative paths from the local
    # `config_path` variable. If we kept the pre-load value, the runtime
    # would boot with the right model but an EMPTY system prompt and missing
    # skills/plugins — silently identity-less, the exact failure mode core
    # #2919 risk-2 is meant to prevent. Researcher RC 12052 + reviewer
    # REQUEST_CHANGES 12447/12448 finding.
    config_path = config.config_path
    # 0.1b Normalise LLM auth env vars now that the resolved provider is
    # known. Platform stores tokens as ANTHROPIC_AUTH_TOKEN, but the Claude
    # SDK/CLI expects different env vars per token kind (OAuth vs API key vs
    # proxy). Passing config.provider lets a non-Anthropic workspace shed an
    # inherited CLAUDE_CODE_OAUTH_TOKEN (a shared tenant global) so it can
    # never silently bill Anthropic instead of the configured provider.
    # Runs before preflight so required-env checks see the normalised shape.
    print(normalise_llm_env(provider=getattr(config, "provider", "")).summary())
    port = config.a2a.port
    # E1: resolve inbound delivery mode once (env override wins over config;
    # default "push"). Push (default) keeps the existing uvicorn-route-only
    # inbound; "poll" tells the platform to stage A2A in activity_logs and starts
    # the inbox poller below. The proven push concierge never sets this, so the
    # entire poll branch is dead code for it.
    delivery_mode = resolve_delivery_mode(os.environ, config.a2a.delivery_mode)
    # BOOT_STEP 2/8 (task #51) — "Load identity" (preflight: config + required
    # env). ok/failed emitted below once preflight.ok is known.
    emit_boot_step("ID", "Load identity", "running", step=2, total=_BOOT_TOTAL)
    preflight = run_preflight(config, config_path)
    render_preflight_report(preflight)

    # 1a. Generate AGENTS.md so peer agents and discovery tools can see this
    # workspace's identity, role, endpoint, and capabilities immediately.
    try:
        generate_agents_md(config_path, "/workspace/AGENTS.md")
    except Exception as _agents_md_err:  # pragma: no cover
        print(f"Warning: AGENTS.md generation failed (non-fatal): {_agents_md_err}")
    if not preflight.ok:
        # BOOT_STEP 2/8 failed — preflight rejected config / required env. Emit
        # the red step before the halt so the canvas shows WHY boot stopped.
        emit_boot_step(
            "ID", "Load identity", "failed", step=2, total=_BOOT_TOTAL,
            message="preflight failed — see boot log for missing config/env",
        )
        raise SystemExit(1)
    emit_boot_step("ID", "Load identity", "ok", step=2, total=_BOOT_TOTAL)
    if awareness_config:
        awareness_namespace = resolve_awareness_namespace(
            workspace_id,
            awareness_config.get("namespace", ""),
        )
        print(f"Awareness enabled for namespace: {awareness_namespace}")

    # 1.5  Initialise governance adapter (no-op if disabled or package absent)
    from molecule_runtime.builtin_tools.governance import initialize_governance
    if config.governance.enabled:
        await initialize_governance(config.governance)
        print(f"Governance: Microsoft Agent Governance Toolkit enabled (mode={config.governance.policy_mode})")
    else:
        print("Governance: disabled (set governance.enabled: true in config.yaml to activate)")

    # 2. Create heartbeat (passed to adapter for task tracking).
    # interval is sourced from observability.heartbeat_interval_seconds
    # in config.yaml — clamped to [5, 300] at parse time. Operators
    # who want a faster crash-detection signal lower it; ones who want
    # to reduce platform write load raise it.
    heartbeat = HeartbeatLoop(
        platform_url,
        workspace_id,
        interval_seconds=config.observability.heartbeat_interval_seconds,
    )

    # 3. Get adapter for this runtime
    runtime = config.runtime or "claude-code"
    # BOOT_STEP 3/8 (task #51) — "Start runtime": resolve + instantiate the
    # runtime adapter. get_adapter raises KeyError on an unknown runtime (no
    # silent fallback), so emit failed before it propagates.
    emit_boot_step("RT", "Start runtime", "running", step=3, total=_BOOT_TOTAL)
    try:
        adapter_cls = get_adapter(runtime)  # Raises KeyError if unknown — no silent fallback
        adapter = adapter_cls()
    except Exception as _rt_err:  # noqa: BLE001 — emit then re-raise, no swallow
        emit_boot_step(
            "RT", "Start runtime", "failed", step=3, total=_BOOT_TOTAL,
            message=f"unknown or unloadable runtime {runtime!r}: {_rt_err}",
        )
        raise
    emit_boot_step("RT", "Start runtime", "ok", step=3, total=_BOOT_TOTAL)
    print(f"Runtime: {runtime} ({adapter.display_name()})")

    # 3a. Wire pluggable event-log backend from config.observability.event_log.
    # Default config.yaml sets backend=memory; operators set "disabled" to
    # opt out without removing append-call sites from adapter code.
    from molecule_runtime.event_log import create_event_log
    adapter.event_log = create_event_log(
        backend=config.observability.event_log.backend,
        ttl_seconds=config.observability.event_log.ttl_seconds,
        max_entries=config.observability.event_log.max_entries,
    )

    # 4. Build adapter config
    adapter_config = AdapterConfig(
        model=config.model,
        # system_prompt is intentionally NOT set here — it is BASE-OWNED. The
        # base fills config.system_prompt during setup() (_common_setup ->
        # build_system_prompt, honoring config.yaml prompt_files) on this same
        # instance, before any executor reads it. (Field defaults to None.)
        tools=config.skills,  # Skill names from config.yaml
        runtime_config=vars(config.runtime_config) if config.runtime_config else {},
        config_path=config_path,
        workspace_id=workspace_id,
        prompt_files=config.prompt_files,
        a2a_port=port,
        heartbeat=heartbeat,
    )

    # 4a. Wire the runtime-agnostic management-MCP gate probe (#3159). The
    # RCA#2970 online gate must ask the ACTIVE adapter whether the management
    # MCP is wired into the file IT reads (codex config.toml, hermes
    # config.yaml, …) rather than unconditionally checking
    # .claude/settings.json. main.py is the one place that holds both the
    # adapter and its config, so it registers the probe here. The baked-binary
    # path and the claude-settings fallback both still apply inside
    # mcp_server_present().
    # BOOT_STEP 4/8 (task #51) — "Management MCP". THE key UX win: on the
    # fail-closed abort below the runtime emits a RED, HALTED step with the
    # box-level diagnostic instead of leaving the canvas on an infinite spinner.
    emit_boot_step("MCP", "Management MCP", "running", step=4, total=_BOOT_TOTAL)
    try:
        from molecule_runtime.platform_agent_identity import (
            register_mcp_launch_env_provider,
            register_mcp_present_probe,
        )
        register_mcp_present_probe(
            lambda _a=adapter, _c=adapter_config: _a.management_mcp_present(_c)
        )
        # runtime#49: give the heartbeat readiness prober the SAME adapter
        # launch-env overlay the boot enumeration path uses, so its management-MCP
        # spawn resolves npx/node on a runtime whose interpreter is off the system
        # PATH (e.g. hermes). Registered here — the one place holding both the
        # adapter and its config — because the prober runs on a heartbeat daemon
        # thread that has neither in scope. Threaded opaquely (no runtime name).
        register_mcp_launch_env_provider(
            lambda _a=adapter, _c=adapter_config: _a.mcp_launch_env(_c)
        )
        emit_boot_step("MCP", "Management MCP", "ok", step=4, total=_BOOT_TOTAL)
    except Exception as probe_err:  # noqa: BLE001
        # On a PLATFORM agent (concierge), silently falling back to the claude
        # settings.json check would fail-OPEN the RCA#2970 gate for a non-claude
        # concierge (the #3159 cross-runtime mis-attribution). Fail CLOSED +
        # LOUD: abort the boot. An ordinary workspace doesn't gate on the
        # management MCP, so its boot proceeds with the harmless fallback.
        if _probe_wiring_failure_is_fatal():
            # Red, halted keycap + the box-level diagnostic (on_platform_agent_
            # image / mcp_command_resolved / binary / settings entry) so the
            # canvas shows WHY, without needing SSH to the locked-down box.
            try:
                from molecule_runtime.platform_agent_identity import (
                    management_mcp_diagnostic,
                )
                _diag = management_mcp_diagnostic()
            except Exception:  # noqa: BLE001 — diagnostic is best-effort
                _diag = {}
            emit_boot_step(
                "MCP", "Management MCP", "failed", step=4, total=_BOOT_TOTAL,
                message=(
                    f"management-MCP gate probe failed to wire "
                    f"({type(probe_err).__name__}: {probe_err}); diag={_diag}"
                ),
            )
            raise RuntimeError(
                "FATAL: failed to register management-MCP gate probe on a "
                "platform agent — refusing to boot fail-open against the claude "
                "settings.json fallback (the #3159 cross-runtime mis-attribution)"
            ) from probe_err
        # Non-fatal (ordinary workspace) — the fallback is harmless. Mark the
        # step ok so the boot screen (if any) doesn't stall on a running keycap.
        emit_boot_step("MCP", "Management MCP", "ok", step=4, total=_BOOT_TOTAL)
        print("WARNING: failed to register management-MCP gate probe; "
              "falling back to claude settings.json check")

    # 5. Build the AgentCard *before* adapter.setup() so /.well-known/agent-card.json
    # is reachable as soon as uvicorn binds, regardless of whether the adapter
    # has working LLM credentials. Decoupling readiness ("is the workspace up?")
    # from configuration ("can it actually answer?") means a workspace with a
    # missing/rotated key stays REACHABLE — canvas can render a clear
    # "agent not configured" error instead of "stuck booting forever," and
    # operators can deprovision/redeploy normally. Skills built from
    # config.skills (static names from config.yaml) up front; richer metadata
    # from the adapter's loaded_skills swaps in below if setup() succeeds.
    # Externally-advertised A2A URL — platform-injected MOLECULE_WORKSPACE_URL
    # (e.g. per-workspace Cloudflare tunnel) wins, else intra-VPC fallback.
    # See resolve_workspace_url + runtime#95. Pass the resolved delivery_mode so
    # the push-mode loopback guard only coerces/warns when the platform will
    # actually dial this URL (no-op in poll mode).
    workspace_url = resolve_workspace_url(os.environ, port, delivery_mode)

    # v1: AgentCard.url removed; put url+protocol in supported_interfaces instead.
    # v1: AgentCapabilities.inputModes/outputModes removed; move to AgentCard.default_*.
    # v1: pushNotifications → push_notifications (Pydantic field name)
    #
    # AgentCard's protocol message uses `supported_interfaces` (plural,
    # interfaces — see a2a-sdk types/a2a_pb2.pyi:189). The 0.3.x→1.0
    # migration in #1974 originally used `supported_protocols`, which
    # the protobuf doesn't expose at all — every workspace boot since
    # then crashed with `ValueError: Protocol message AgentCard has no
    # "supported_protocols" field`. The crash didn't surface in the
    # publish-runtime smoke because the smoke only IMPORTS
    # molecule_runtime.main, never CALLS the AgentCard constructor.
    # Don't rename back.
    agent_card = AgentCard(
        name=config.name,
        description=config.description or config.name,
        version=config.version,
        supported_interfaces=[
            AgentInterface(protocol_binding="https://a2a.g/v1", url=workspace_url)
        ],
        capabilities=AgentCapabilities(
            streaming=config.a2a.streaming,
            push_notifications=config.a2a.push_notifications,
            # Note: state_transition_history (a 0.x capability flag) was
            # removed in a2a-sdk 1.0. Per the SDK's own
            # a2a/compat/v0_3/conversions.py: "No longer supported in
            # v1.0". The capability is now universal — Task.history is
            # always available and tasks/get accepts historyLength via
            # apply_history_length(). Don't add this kwarg back.
        ),
        # Static skill stubs from config.yaml; replaced with rich metadata
        # below if adapter.setup() loads skills successfully.
        skills=[
            AgentSkill(id=name, name=name, description=name, tags=[], examples=[])
            for name in (config.skills or [])
        ],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
    )

    # 5b. Materialize the workspace's CANONICAL PERSONA into the ACTIVE runtime's
    # native identity file (system-prompt.md / SOUL.md / AGENTS.md),
    # so a workspace on ANY runtime boots with its intended identity — even
    # runtimes (openclaw) whose gateway reads a native file and never consumes
    # config.system_prompt. Runtime-agnostic: dispatches on adapter.name() via the
    # persona-materialization PORT. Runs BEFORE setup() so openclaw's setup-time
    # ``/configs/*.md`` -> gateway-workspace copy picks up the freshly written
    # SOUL.md (+ cleared BOOTSTRAP/AGENTS placeholders). This is the runtime half
    # of core #3418's provision half: the delivered persona
    # (config.prompt_files, e.g. prompts/concierge.md) becomes the model's actual
    # on-disk identity for the real runtime, not just claude-code. Best-effort +
    # idempotent + no-op when no persona is delivered, so nothing regresses.
    try:
        materialized_persona_path = adapter.materialize_persona(adapter_config)
        if materialized_persona_path is not None:
            print(
                f"Persona: materialized canonical identity into "
                f"{materialized_persona_path} (runtime={runtime})"
            )
    except Exception as _persona_err:  # noqa: BLE001 — persona is best-effort
        print(f"Warning: persona materialization failed (non-fatal): {_persona_err}")

    # 5c. Surface the canonical skills dir into the ACTIVE runtime's NATIVE
    # skill-discovery location (the skills-surfacing PORT — the generalization
    # of template-claude-code#224's entrypoint symlink into ONE cross-runtime
    # contract). Directory-level (symlink / config pointer), so plugin skills
    # installed into /configs/skills — at boot OR post-boot — are visible to
    # the runtime's next native scan. Runs BEFORE setup() on purpose: openclaw
    # and codex spawn their gateway/app-server during setup(), and the native
    # surface must exist before those processes take their first skill scan.
    # Fail-loud-not-fatal: an unsatisfiable runtime logs an ERROR (via the
    # adapter hook) but never bricks the boot — skills are not privileged.
    try:
        materialized_skills_target = adapter.materialize_skills(adapter_config)
        if materialized_skills_target is not None:
            print(
                f"Skills: surfaced /configs/skills natively at "
                f"{materialized_skills_target} (runtime={runtime})"
            )
    except Exception as _skills_err:  # noqa: BLE001 — never brick boot on skills
        print(f"WARNING: skills materialization failed (non-fatal): {_skills_err}")

    # 6. Setup adapter and create executor
    # On failure: log + continue. The card route stays mounted (above);
    # the JSON-RPC route below returns -32603 "agent not configured" until
    # the operator fixes credentials and redeploys. Heartbeat keeps running
    # so the platform sees the workspace as reachable-but-misconfigured
    # rather than crash-looping.
    adapter_ready = False
    adapter_error: str | None = None
    executor = None
    try:
        await adapter.setup(adapter_config)

        # Re-assert the @molecule-ai npm scope config AFTER adapter setup.
        # PLATFORM CONTRACT (hard): the management MCP is `npx
        # @molecule-ai/mcp-server`; the boot-time write (step 0.2b) can be
        # CLOBBERED by template setup steps that install their own node stacks
        # (hermes's installer rewrote the npmrc → ETARGET → the core#3082
        # loaded_mcp_tools gate fail-closed the concierge, 2026-07-09).
        # Idempotent + fail-soft: a no-op when setup touched nothing.
        install_npm_gitea_auth()

        if prepare_only:
            # PREPARE MODE (runtime#357 / core#4587): materialize config ONLY.
            # By here boot-install (step 1) has written the boot-installed
            # plugins' mcp_servers stanzas, and adapter.setup() has written
            # persona, skills, and the management/self MCP stanzas — the full
            # config block is now on disk. Return BEFORE create_executor /
            # registration / heartbeat / uvicorn so a template can run this as a
            # pre-step and launch its agent gateway against a COMPLETE config on
            # the gateway's FIRST boot.
            #
            # Motivation: hermes >= 0.19 discovers MCP servers EAGERLY at gateway
            # startup. Writing them post-launch (the historical order) forced a
            # gateway restart to pick them up — a ~90s unreachable window on
            # every fresh boot (core#4587). Pre-materializing removes the
            # restart entirely.
            #
            # SSOT: this is the SAME boot-install + adapter.setup() the real
            # serve runs, so the pre-written block is byte-identical and the
            # real serve's later rewrite is a no-op (a hermes-side reconcile
            # watcher, if present, sees no post-launch change and stays dormant).
            # The heartbeat was CREATED (not started) above; stop() defensively.
            if hasattr(heartbeat, "stop"):
                try:
                    await heartbeat.stop()
                except Exception:  # noqa: BLE001
                    pass
            print(
                "MOLECULE_PREPARE_OK: config materialized "
                "(mcp_servers + persona + skills); exiting before serve",
                flush=True,
            )
            return 0

        executor = await adapter.create_executor(adapter_config)

        # SSOT trace wrap: the single funnel every adapter's executor passes
        # through — wrapping here means ALL runtimes inherit Langfuse tracing
        # from one place. No-op when Langfuse is unset. Fail-open.
        try:
            from molecule_runtime import tracing as _tracing

            executor = _tracing.wrap_executor(executor, workspace_id, adapter_config.model)
        except Exception:
            pass

        # 6.0 loaded_mcp_tools producer (core#3082) — INIT-TIME enumeration.
        # Right after the executor is built (MCP servers are wired into the
        # runtime's native config by setup()), enumerate the connected MCP
        # servers' tool inventory and publish it via set_loaded_mcp_tools, so
        # the FIRST heartbeat carries `loaded_mcp_tools` and the platform gate
        # can flip a de-baked concierge degraded->online WITHOUT waiting for a
        # user turn (task #85). Runtime-agnostic: reads the declared servers
        # from the active runtime's native config and talks the MCP wire
        # protocol directly (mirrors the adk#34 / codex#142 prior art).
        #
        # GATED to kind=platform (the concierge): capture_loaded_mcp_tools_at_init
        # itself no-ops unless _is_platform_agent() (the same
        # MOLECULE_PLATFORM_AGENT_IMAGE_BAKED signal main.py uses for the
        # management-MCP gate probe), so tenants that declare MCP servers (e.g.
        # image-gen) don't spawn+enumerate them at every boot.
        #
        # BOOT-SAFE: the probe is fully async (asyncio.create_subprocess_exec +
        # asyncio.wait_for on every read AND a hard overall-enumeration deadline),
        # so a management MCP that answers `initialize` then stalls on
        # `tools/list` (e.g. CP slow/unreachable during an incident) CANNOT hang
        # boot. Doubly defended here with an outer asyncio.wait_for and a
        # blanket except so this hook can NEVER raise or hang into the register/
        # heartbeat path below. On any timeout/stall/failure the producer is left
        # None — the heartbeat omits the field and core's grace window applies
        # (degraded-until-first-turn, the accepted fallback). The claude-code
        # executor's per-turn `init`-message capture (separate template repo)
        # still runs as a complementary refresh.
        # Spawn as a NON-BLOCKING BACKGROUND task with retry. The management MCP
        # is frequently NOT connectable at this early-boot moment
        # (mcp_server_present hasn't flipped true yet, and/or
        # `npx @molecule-ai/mcp-server` is still cold-starting), so a one-shot
        # enumeration here misses and the concierge sits degraded until a user
        # turn (validated 2026-06-25: provisioning->online->degraded, then a turn
        # flips it online). Running with retry in the BACKGROUND lets it succeed
        # once the MCP becomes ready, so a fresh concierge reaches online WITHOUT
        # a turn — while NEVER delaying register/heartbeat below (fire-and-forget;
        # the coroutine is boot-safe + never raises). Keep a reference so the task
        # is not GC'd. The per-turn capture (template executor) remains a fallback.
        # BOOT_STEP 5/8 (task #51) — "Enumerate tools". Numbered 5 (before A2A
        # "Wire transport", step 6) so the keycaps light up in wall-clock order:
        # this init-enumeration is kicked off during adapter.setup(), which runs
        # BEFORE the A2A routes are assembled below. The enumeration itself runs
        # as a non-blocking BACKGROUND task (retry until the management MCP is
        # connectable), so boot does NOT wait for the tool list. We mark the step
        # ok once the enumeration is successfully KICKED OFF — the terminal
        # online verdict (loaded_mcp_tools populated) is the platform's, carried
        # by the heartbeat, not this presentation step.
        emit_boot_step("TOOL", "Enumerate tools", "running", step=5, total=_BOOT_TOTAL)
        try:
            from molecule_runtime.loaded_mcp_tools_probe import (
                capture_loaded_mcp_tools_with_retry,
            )
            global _LOADED_MCP_TOOLS_BG_TASK
            _LOADED_MCP_TOOLS_BG_TASK = asyncio.create_task(
                # runtime#181 / ADR-004: pass the resolved adapter so enumeration
                # goes through the runtime-owns-discovery contract
                # (adapter.enumerate_loaded_mcp_tools). Each adapter reads its OWN
                # native config and feeds the resolved specs to the generic engine
                # (enumerate_from_specs_async); the engine never resolves servers by
                # runtime name. The BaseAdapter default handles a not-yet-migrated /
                # third-party adapter via the generic JSON reader.
                capture_loaded_mcp_tools_with_retry(adapter, adapter_config)
            )
            print(
                "loaded_mcp_tools: spawned background init-enumeration (retry until "
                "the management MCP is connectable; non-blocking)",
                flush=True,
            )
            emit_boot_step("TOOL", "Enumerate tools", "ok", step=5, total=_BOOT_TOTAL)
        except Exception as mcp_probe_err:  # noqa: BLE001 — never block boot
            print(
                f"loaded_mcp_tools: failed to spawn init enumeration (non-fatal): "
                f"{type(mcp_probe_err).__name__}: {mcp_probe_err}",
                flush=True,
            )
            emit_boot_step(
                "TOOL", "Enumerate tools", "failed", step=5, total=_BOOT_TOTAL,
                message=f"{type(mcp_probe_err).__name__}: {mcp_probe_err}",
            )

        # 6a. Boot-smoke short-circuit (issue #2275): if MOLECULE_SMOKE_MODE
        # is set, exercise the executor's full import tree by calling
        # execute() once with stub deps + a short timeout. Skips platform
        # registration + uvicorn entirely. Returns process exit code.
        from molecule_runtime.smoke_mode import is_smoke_mode, run_executor_smoke
        if is_smoke_mode():
            exit_code = await run_executor_smoke(executor)
            if hasattr(heartbeat, "stop"):
                try:
                    await heartbeat.stop()
                except Exception:  # noqa: BLE001
                    pass
            raise SystemExit(exit_code)

        # 6b. Restore from pre-stop snapshot if one exists (molecule-core#1391).
        # The snapshot is scrubbed before being written, so secrets are
        # already redacted — restore_state must not re-expose them.
        from molecule_runtime.lib.pre_stop import read_snapshot
        snapshot = read_snapshot()
        if snapshot:
            try:
                adapter.restore_state(snapshot)
                print(
                    f"Pre-stop snapshot restored: task={snapshot.get('current_task', '')!r}, "
                    f"uptime={snapshot.get('uptime_seconds', 0)}s"
                )
            except Exception as restore_err:
                print(f"Warning: snapshot restore failed (continuing): {restore_err}")

        # 6c. Swap rich skill metadata into the card now that setup() loaded
        # them. In-place mutation: a2a-sdk's create_agent_card_routes serialises
        # the card on each request, so the route mounted below sees the update.
        # Isolated via card_helpers.enrich_card_skills — a malformed
        # loaded_skills shape (e.g., a future adapter that doesn't follow
        # the .metadata convention) is logged + swallowed instead of
        # propagating up to the outer except, where it would silently
        # degrade an OK boot to the not-configured state.
        from molecule_runtime.card_helpers import enrich_card_skills
        enrich_card_skills(agent_card, getattr(adapter, "loaded_skills", None))
        adapter_ready = True
    except SystemExit:
        # Smoke-mode exit signal — propagate untouched.
        raise
    except Exception as setup_err:  # noqa: BLE001
        # Privileged-plugin setup failures (currently molecule-platform-mcp)
        # must abort the runtime setup loudly — not degrade to
        # "reachable-but-misconfigured", which would leave the concierge
        # with a configured-but-missing privileged binary and no loud
        # failure signal. The dedicated PrivilegedPluginInstallError
        # subclass (from adapter_base) is the discriminator.
        if _is_privileged_setup_failure(setup_err):
            print(
                f"FATAL: privileged plugin setup failed — aborting runtime boot. "
                f"Reason: {type(setup_err).__name__}: {setup_err}",
                flush=True,
            )
            raise
        adapter_error = f"{type(setup_err).__name__}: {setup_err}"
        print(
            f"WARNING: adapter.setup() failed — workspace will serve agent-card "
            f"but JSON-RPC will return -32603 until configuration is fixed. "
            f"Reason: {adapter_error}",
            flush=True,
        )
        # Heartbeat keeps running so the platform marks the workspace as
        # reachable-but-misconfigured. Operators can then redeploy with the
        # correct env vars without having to chase a crash-loop.
        if prepare_only:
            # Prepare could NOT materialize config (setup failed). Exit
            # non-zero so the template does NOT trust a partial config and
            # falls back to its normal launch + the post-launch reconcile
            # path. Prepare is strictly a best-effort optimization; a failure
            # here must never brick boot, only forgo the optimization.
            print(
                f"MOLECULE_PREPARE_FAILED: adapter.setup() failed in prepare "
                f"mode ({adapter_error}); caller should fall back to its "
                f"normal post-launch reconcile path",
                flush=True,
            )
            return 1

    # 7. Wrap in A2A.
    #
    # Route assembly is in molecule_runtime/boot_routes.py so the contract —
    # card always mounted, JSON-RPC route swaps based on adapter state
    # (DefaultRequestHandler when executor is non-None, not_configured
    # handler returning -32603 otherwise) — is unit-testable with
    # Starlette's TestClient. main.py is `# pragma: no cover` so without
    # this extraction a future refactor that re-coupled card + setup()
    # would silently bypass PR #2756. tests/test_boot_routes.py pins
    # the four-branch contract.
    # BOOT_STEP 6/8 (task #51) — "Wire transport": assemble the A2A/JSON-RPC
    # Starlette routes (card always mounted; JSON-RPC route swaps on adapter
    # state). This is the real transport-wiring phase — the A2A app is built
    # here, then served by uvicorn below. Numbered 6 (after TOOL step 5) so the
    # keycaps light up in wall-clock order.
    emit_boot_step("A2A", "Wire transport", "running", step=6, total=_BOOT_TOTAL)
    from molecule_runtime.boot_routes import build_routes
    app = Starlette(routes=build_routes(agent_card, executor, adapter_error))
    emit_boot_step("A2A", "Wire transport", "ok", step=6, total=_BOOT_TOTAL)

    # 8. Register with platform
    # When adapter.setup() failed, advertise via configuration_status so
    # the platform/canvas can render "configured: false, reason: …" instead
    # of a confused "ready but silent" state.
    loaded_skills = getattr(adapter, "loaded_skills", None) or []
    agent_card_dict = {
        "name": config.name,
        "description": config.description,
        "version": config.version,
        "url": workspace_url,
        "skills": [
            {
                "id": s.metadata.id,
                "name": s.metadata.name,
                "description": s.metadata.description,
                "tags": s.metadata.tags,
            }
            for s in loaded_skills
        ] if adapter_ready else [
            {"id": n, "name": n, "description": n, "tags": []}
            for n in (config.skills or [])
        ],
        "capabilities": {
            "streaming": config.a2a.streaming,
            "pushNotifications": config.a2a.push_notifications,
        },
        "configuration_status": "ready" if adapter_ready else "not_configured",
        **({"configuration_error": adapter_error} if adapter_error else {}),
    }

    # internal#688: boot-register with bounded retry + backoff. A one-shot
    # POST silently leaves workspaces.url empty when the orchestrator is
    # momentarily down (Cloudflare 530 / tunnel 1033 during a recreate
    # sweep), making the workspace online-but-undialable. register_with_platform
    # retries until a 2xx and never raises, so boot continues regardless.
    # BOOT_STEP 7/8 (task #51) — "Register": boot-register with the platform.
    # register_with_platform never raises (returns True on 2xx, False on
    # bounded-retry exhaustion); we mirror that into the step's ok/failed.
    emit_boot_step("NET", "Register", "running", step=7, total=_BOOT_TOTAL)
    async with httpx.AsyncClient(timeout=10.0) as client:
        _registered = await register_with_platform(
            client,
            platform_url=platform_url,
            workspace_id=workspace_id,
            workspace_url=workspace_url,
            agent_card=agent_card_dict,
            headers=auth_headers(),
            delivery_mode=delivery_mode,
        )
    if _registered:
        emit_boot_step("NET", "Register", "ok", step=7, total=_BOOT_TOTAL)
    else:
        # Registration exhausted its retries — the workspace can still serve
        # traffic and the heartbeat backfill is the recovery net, but surface
        # the degraded register so the boot screen isn't falsely green.
        emit_boot_step(
            "NET", "Register", "failed", step=7, total=_BOOT_TOTAL,
            message="registration retries exhausted — heartbeat backfill is the recovery path",
        )

    heartbeat.agent_card = agent_card_dict

    # 9. Start heartbeat
    # BOOT_STEP 8/8 (task #51) — "Go online". `online` is ultimately a CP
    # verdict (WORKSPACE_ONLINE flips the canvas out of the boot screen), but
    # from the runtime's side the register-complete + heartbeat-started state is
    # the terminal boot step we can assert; emit ok as the heartbeat arms.
    emit_boot_step("ONLINE", "Go online", "running", step=8, total=_BOOT_TOTAL)
    heartbeat.start()
    emit_boot_step("ONLINE", "Go online", "ok", step=8, total=_BOOT_TOTAL)

    # 9a. E1 — poll-mode inbound delivery is started LATER, gated on uvicorn
    # having bound (see the start_poll_delivery_when_bound task created just
    # before server.serve() below). Starting the poller here — before the server
    # socket is listening — would race: the poller's first poll could pull a
    # staged message and self-POST to 127.0.0.1:{port} before uvicorn binds,
    # dropping it while the inbox cursor advances. No-op in push mode (default).

    # 9b. Start skills hot-reload watcher (background task)
    # When a skill file changes the watcher reloads the skill module and calls
    # back into the adapter so the next A2A request uses the updated tools.
    # Skipped on misconfigured boots — adapter has no executor / tool registry
    # to swap into, so reloading skills would NPE on the agent rebuild path.
    if adapter_ready and config.skills:
        try:
            from molecule_runtime.skill_loader.watcher import SkillsWatcher

            def _on_skill_reload(updated_skill):
                """Rebuild the runtime agent when a skill changes in-place."""
                if not hasattr(adapter, "loaded_skills"):
                    return
                # Replace the matching skill in the adapter's skill list
                adapter.loaded_skills = [
                    updated_skill if s.metadata.id == updated_skill.metadata.id else s
                    for s in adapter.loaded_skills
                ]
                # Rebuild the agent's tool list from updated skills
                if hasattr(adapter, "all_tools") and hasattr(adapter, "system_prompt"):
                    from molecule_runtime.builtin_tools.approval import request_approval
                    from molecule_runtime.builtin_tools.delegation import delegate_task, delegate_task_async, check_task_status
                    from molecule_runtime.builtin_tools.memory import commit_memory, recall_memory
                    from molecule_runtime.builtin_tools.sandbox import run_code
                    # Core platform tools mirror adapter_base.all_tools — must
                    # match the platform_tools registry names so docs and tools
                    # never drift.
                    base_tools = [
                        delegate_task, delegate_task_async, check_task_status,
                        request_approval, commit_memory, recall_memory, run_code,
                    ]
                    skill_tools = []
                    for sk in adapter.loaded_skills:
                        skill_tools.extend(sk.tools)
                    adapter.all_tools = base_tools + skill_tools
                    print(f"Skills hot-reload: '{updated_skill.metadata.id}' reloaded — "
                          f"{len(updated_skill.tools)} tool(s)")

            skills_watcher = SkillsWatcher(
                config_path=config_path,
                skill_names=config.skills,
                on_reload=_on_skill_reload,
                current_runtime=runtime,
            )
            asyncio.create_task(skills_watcher.start())
            print(f"Skills hot-reload enabled for: {config.skills}")
        except Exception as e:
            print(f"Warning: skills watcher could not start: {e}")

    # 10. Run A2A server
    print(f"Workspace {workspace_id} starting on port {port}")
    # Wrap the ASGI app with W3C TraceContext extraction middleware so incoming
    # A2A HTTP requests propagate their trace context into _incoming_trace_context.
    # v1: Starlette app is constructed directly; no build() step needed
    starlette_app = app

    # Add /transcript route — exposes the most-recent agent session log
    # (claude-code reads ~/.claude/projects/<cwd>/<session>.jsonl). Other
    # runtimes return supported:false.
    from starlette.responses import JSONResponse

    async def _transcript_handler(request):
        # Require workspace bearer token — the same token issued at registration
        # and stored in /configs/.auth_token. Any container on molecule-core-net
        # could otherwise read the full session log. Closes #287.
        #
        # #328: fail CLOSED when the token file is unavailable. get_token()
        # returns None during the bootstrap window (first register hasn't
        # completed), if /configs/.auth_token was deleted, or on OSError.
        # The old `if expected:` guard treated all three cases as "skip
        # auth" — an unauthenticated container on the same Docker network
        # could read the entire session log during that window. Deny
        # instead. The platform's TranscriptHandler acquires the token
        # during registration, so once the bootstrap completes it always
        # has a valid credential to present.
        from molecule_runtime.platform_auth import get_token
        if not _transcript_authorized(get_token(), request.headers.get("Authorization", "")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            since = int(request.query_params.get("since", "0"))
            limit = int(request.query_params.get("limit", "100"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "since and limit must be integers"}, status_code=400)
        # Isolate adapter call: misconfigured boots leave the adapter
        # partially-initialised, and a future adapter override of
        # transcript_lines might assume setup() ran. Surface a 503 with
        # a clear reason instead of letting the exception propagate to
        # Starlette's 500 handler — same pattern as the not-configured
        # JSON-RPC route (PR #2756). BaseAdapter.transcript_lines's
        # default returns {"supported": false} so today's 4 adapters
        # never trigger this branch; this is the safety net.
        try:
            result = await adapter.transcript_lines(since=since, limit=limit)
        except Exception as transcript_err:  # noqa: BLE001
            return JSONResponse(
                {
                    "error": "transcript unavailable",
                    "detail": f"{type(transcript_err).__name__}: {transcript_err}",
                },
                status_code=503,
            )
        return JSONResponse(result)

    starlette_app.add_route("/transcript", _transcript_handler, methods=["GET"])

    # /internal/* — platform→workspace forward calls (RFC #2312). Auth
    # is the per-workspace platform_inbound_secret in
    # /configs/.platform_inbound_secret, distinct from the outbound
    # workspace_auth_token used by /transcript above.
    from molecule_runtime.internal_chat_uploads import ingest_handler as _internal_chat_uploads_ingest
    starlette_app.add_route(
        "/internal/chat/uploads/ingest",
        _internal_chat_uploads_ingest,
        methods=["POST"],
    )
    from molecule_runtime.internal_file_read import file_read_handler as _internal_file_read
    starlette_app.add_route(
        "/internal/file/read",
        _internal_file_read,
        methods=["GET"],
    )
    # /internal/schedules* — the runtime schedule API (Option A). Backed by the
    # volume grid a kind:trigger scheduler plugin fires from; same forward-auth.
    from molecule_runtime.internal_schedules import add_schedule_routes as _add_schedule_routes
    _add_schedule_routes(starlette_app)

    built_app = make_trace_middleware(starlette_app)

    # uvicorn expects the level name in lowercase ("debug" / "info" /
    # "warning" / "error" / "critical"). config.observability.log_level
    # is uppercased at parse time (config.py.load_config) for the
    # Python ``logging`` module's convention; lower it here so both
    # consumers get the form they expect from one source of truth.
    # An ``LOG_LEVEL`` env var still wins as an ops-side debugging
    # override — set it on the workspace process to bypass YAML
    # without a config edit + restart cycle.
    uvicorn_log_level = os.environ.get("LOG_LEVEL", config.observability.log_level).lower()
    server_config = uvicorn.Config(
        built_app,
        host="0.0.0.0",
        port=port,
        log_level=uvicorn_log_level,
    )
    server = uvicorn.Server(server_config)

    # 10b. Schedule initial_prompt self-message after server is ready.
    # Only runs on first boot — creates a marker file to prevent re-execution on restart.
    # Skipped on misconfigured boots: the self-message would route through the
    # platform back to /, hit the -32603 not-configured handler, and consume
    # the marker for a fire that can't actually run. Wait until the operator
    # fixes credentials and the workspace redeploys with adapter_ready=True.
    initial_prompt_task = None
    initial_prompt_marker = resolve_initial_prompt_marker(config_path)
    if adapter_ready and config.initial_prompt and not os.path.exists(initial_prompt_marker):
        # Write the marker UP FRONT (#71): if the prompt later crashes or
        # times out, we do NOT replay on next boot — that created a
        # ProcessError cascade where every message kept crashing. Operators
        # can always re-send via chat. Log loudly if the marker write
        # fails so the situation is visible.
        if not mark_initial_prompt_attempted(initial_prompt_marker):
            print(
                f"Initial prompt: WARNING — could not write marker at "
                f"{initial_prompt_marker}; this boot may replay if it crashes.",
                flush=True,
            )
        async def _send_initial_prompt():
            """Wait for server to be ready, then send initial_prompt as self-message."""
            # Gate on the SAME "is uvicorn bound?" signal every other post-bind
            # action uses (poll-delivery, daemon supervisor): the in-process
            # ``server.started`` flag, via the shared fail-OPEN helper. The old
            # path here instead self-polled the agent-card over HTTP for 30s and
            # RETURNED WITHOUT SENDING on timeout — a fail-CLOSED drop of the
            # user's very first prompt. (It was doubly broken: a2a-sdk 1.x renamed
            # the well-known path constant, so the probe 404'd every attempt and
            # ALWAYS fell through to "skipping" even when the server was serving
            # fine.) Fail-OPEN like the siblings: after the wait, send anyway —
            # ``max_wait`` is a defensive bound, not a verdict, and the send path
            # below already has its own connection-refused retry/backoff as the
            # net if the server is genuinely a beat behind.
            from molecule_runtime.plugin_daemons import wait_until_server_bound
            bound = await wait_until_server_bound(server, max_wait=60)
            if not bound:
                print(
                    "Initial prompt: uvicorn not reported bound after 60s; "
                    "sending anyway (send-path retry is the backstop) — NOT dropping "
                    "the user's first prompt",
                    flush=True,
                )

            # Send initial prompt through the platform A2A proxy (not directly to self).
            # NOTE (verified against a2a_proxy_helpers.go, review #327): the
            # proxy's A2A_RESPONSE broadcast is gated on caller identity
            # (callerID == "" || isCanvasUser) — a self_source_headers call is
            # callerID == workspaceID, so THIS response is NOT auto-broadcast
            # to the canvas. Any user-visible output of the initial-prompt
            # turn must go through send_message_to_user; the response body
            # read below is otherwise discarded (same class as the digest
            # discard fixed by idle_digest/reply_forwarder — follow-up #327).
            # Uses urllib in a thread to avoid asyncio/httpx streaming hangs.
            import json as _json
            import urllib.request

            def _do_send_sync():
                import time as _time
                payload = _json.dumps({
                    "method": "message/send",
                    # #2251: single model-based builder — params generated
                    # FROM the receiver's a2a-sdk v0.3 SendMessageRequest schema.
                    "params": build_message_send_params(
                        config.initial_prompt,
                        message_id=f"initial-{_uuid.uuid4().hex[:8]}",
                        # task #219 §5: stamp the lifecycle-wake source so the
                        # first-boot greeting is guard-governed (a runaway
                        # re-greet trips the replay breaker) instead of the
                        # unstamped guard-bypass it was before.
                        metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_LIFECYCLE},
                    ),
                }).encode()

                # #220: include platform bearer token so the request isn't
                # silently rejected once any workspace has a live token on
                # file. Without this, initial_prompt 401s in multi-tenant
                # mode exactly like /registry/register did in #215.
                # X-Workspace-ID via self_source_headers() so the platform
                # tags the row source=agent — without it the canvas's
                # My Chat tab renders the initial_prompt as if the user
                # had typed it. See platform_auth.py for the full
                # explanation.
                headers = {
                    "Content-Type": "application/json",
                    **self_source_headers(workspace_id),
                }

                # Retry with backoff — the platform proxy may not be able to
                # reach us yet (container networking takes a moment to settle).
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
                        print(f"Initial prompt: completed (status={resp.status})", flush=True)
                        break
                    except Exception as e:
                        if attempt < max_retries - 1:
                            delay = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
                            print(f"Initial prompt: attempt {attempt + 1} failed ({e}), retrying in {delay}s...", flush=True)
                            _time.sleep(delay)
                        else:
                            print(f"Initial prompt: failed after {max_retries} attempts — {e}", flush=True)
                            return

                # Marker was already written up front (#71). Nothing to do here.

            print("Initial prompt: sending via platform proxy...", flush=True)
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, _do_send_sync)

        initial_prompt_task = asyncio.create_task(_send_initial_prompt())

    # 10b2. Self-reprovision wake note (design §5.2 step 3 — never silent).
    # When step 0.2d detected plugins newly added since the last boot, the
    # agent's FIRST act after this reprovision is announcing them to the
    # user, via the same platform-proxy self-message seam as initial_prompt.
    # Staged consumption (review 2026-07-05): the pending announcement is
    # attempt-marked HERE (only once the adapter is known ready — a
    # misconfigured boot leaves the state untouched, so the note re-arms on
    # the next healthy boot) and consumed inside the send task only on send
    # SUCCESS. Replay is bounded by MAX_WAKE_ATTEMPTS; a failed attempt-mark
    # persist (read-only volume) skips the send entirely so an unbounded
    # announce-every-boot loop is impossible.
    reprovision_wake_task = None
    if wake_plan.additions:
        if adapter_ready:
            from molecule_runtime.reprovision_wake import (
                mark_wake_attempt,
                send_wake_note_when_ready,
            )
            if mark_wake_attempt(wake_plan):
                reprovision_wake_task = asyncio.create_task(
                    send_wake_note_when_ready(
                        wake_plan,
                        port=port,
                        platform_url=platform_url,
                        workspace_id=workspace_id,
                    )
                )
            else:
                print(
                    "Reprovision wake: could not persist the attempt marker — "
                    f"skipping announcement for {wake_plan.additions} (unbounded-replay guard)",
                    flush=True,
                )
        else:
            print(
                "Reprovision wake: newly installed plugin(s) detected "
                f"({wake_plan.additions}) but the adapter is not ready — deferring "
                "the announcement to the next healthy boot (state not consumed)",
                flush=True,
            )

    # 10c. Idle loop — reflection-on-completion / backlog-pull pattern.
    # Fires config.idle_prompt every config.idle_interval_seconds while the
    # workspace has no active task. This turns every role from "waits for cron"
    # into "self-wakes when idle" — the Hermes/Letta shape from today's
    # multi-framework survey (see docs/ecosystem-watch.md). Cost collapses to
    # event-driven in practice: the idle check is local (no LLM call, just
    # heartbeat.active_tasks==0), and the prompt only fires when there's
    # actually nothing to do. Gated on idle_prompt being non-empty so existing
    # workspaces upgrade opt-in — set idle_prompt in org.yaml defaults or
    # per-workspace to enable.
    idle_loop_task = None

    # 10c-kernel. Contract-driven idle DIGEST (task #219). The mailbox kernel
    # is NATIVE (default ON, operator ruling 2026-07-13): the assembled provider
    # digest (identity header + task-queue + goal-state) replaces the static
    # idle_prompt self-post below. The legacy loop below survives only behind
    # the MOLECULE_MAILBOX_KERNEL=0 emergency opt-out. The whole block is
    # wrapped so a wiring fault degrades to "no idle loop", never a boot crash.
    # The controller LOGIC is unit-tested (tests/test_idle_controller.py); the
    # boot-integration path was validated live on the local stack 2026-07-13
    # (idle-digest canary) and is asserted by the ephemeral-CP gate's
    # idle-digest sub-step.
    _idle_digest_enabled = False
    if adapter_ready:
        try:
            from molecule_runtime import mailbox_dir as _mbox

            _idle_digest_enabled = _mbox.kernel_enabled()
        except Exception:
            _idle_digest_enabled = False

    if _idle_digest_enabled:
        try:
            from molecule_runtime.idle_digest import (
                IdleDigestController as _IDC,
                Policy as _IdlePolicy,
                build_default_providers as _build_idle_providers,
            )

            _idle_policy = _IdlePolicy.default()
            _idle_providers = _build_idle_providers(
                config_path=config.config_path,
                prompt_files=config.prompt_files,
                workspace_name=config.name,
                runtime_kind=config.runtime,
                # D5 mail providers (sent-folder + inbound-a2a): default
                # binding = the platform mail-summary API. Comms-plugin
                # binding replaces this via comms_source later.
                platform_url=platform_url,
                workspace_id=workspace_id,
            )
            # goal-state first-boot migration of a legacy config.idle_prompt value
            for _gp in _idle_providers:
                if _gp.provider_id == "goal-state":
                    try:
                        _gp.migrate_from_config(config.idle_prompt)
                    except Exception:
                        pass
                    try:
                        # Provision-time deterministic seed (MOLECULE_IDLE_GOAL
                        # workspace secret / CP env) — after the one-shot config
                        # migration, same source-rank rules (never clobbers an
                        # agent-set goal).
                        _gp.bootstrap_from_env()
                    except Exception:
                        pass
                    break

            _IDLE_FIRE_TIMEOUT = max(60, min(300, _idle_policy.idle_fire_after_seconds))

            # The complete self-fire round trip — <SYSTEM IDLE PROMPT> framing,
            # message/send POST, and forwarding the turn's reply text to the
            # user — lives in idle_digest/poster.py so the wire behavior is
            # conformance-tested (real builders, real HTTP) instead of sitting
            # untestable in a boot closure.
            from molecule_runtime.idle_digest.poster import make_digest_poster

            _digest_poster = make_digest_poster(
                platform_url, workspace_id, _IDLE_FIRE_TIMEOUT
            )

            _idle_controller = _IDC(
                providers=_idle_providers,
                policy=_idle_policy,
                poster=_digest_poster,
                # #381: skip the digest while unconsumed delegation results wait
                skip_check=_check_delegation_results_pending,
                # atomic-fire recheck: only fire when no turn is in flight
                pre_fire_check=lambda: heartbeat.active_tasks == 0,
            )

            async def _run_idle_digest_loop():
                await asyncio.sleep(min(_idle_policy.idle_fire_after_seconds, 60))
                while True:
                    try:
                        await asyncio.sleep(_idle_policy.idle_fire_after_seconds)
                    except asyncio.CancelledError:
                        return
                    # same circuit breaker as the static loop (runaway 2026-06-29)
                    try:
                        from molecule_runtime.autonomous_loop_guard import (
                            should_halt as _loop_should_halt,
                        )

                        if _loop_should_halt():
                            print(
                                "Idle digest: circuit breaker OPEN — halting "
                                "(runtime degraded; restart to resume)",
                                flush=True,
                            )
                            return
                    except Exception:
                        pass
                    if heartbeat.active_tasks > 0:
                        continue
                    try:
                        _outcome = await _idle_controller.tick()
                        if _outcome == "fired":
                            print("Idle digest: fired", flush=True)
                    except Exception as _e:  # pragma: no cover — never crash the loop
                        print(f"Idle digest: tick error — {_e}", flush=True)

            idle_loop_task = asyncio.create_task(_run_idle_digest_loop())
            print(
                "Idle digest: contract-driven digest loop armed (mailbox kernel on)",
                flush=True,
            )
        except Exception as _e:  # pragma: no cover — degrade to the legacy loop
            print(
                f"Idle digest: wiring failed — falling back to the legacy "
                f"static idle loop — {_e}",
                flush=True,
            )
            idle_loop_task = None
            # A workspace WITH config.idle_prompt had idle behavior before the
            # kernel; a digest wiring fault must not remove it. The digest task
            # is None here, so re-enabling the legacy loop cannot double-fire.
            _idle_digest_enabled = False

    # Skipped on misconfigured boots — the self-fire would route to the
    # -32603 handler in a tight loop and consume cycles for no useful work.
    if adapter_ready and config.idle_prompt and not _idle_digest_enabled:
        # Idle-fire HTTP timeout. Kept tight relative to the fire cadence so a
        # hung platform doesn't accumulate dangling requests — a fire that
        # takes longer than the idle interval itself is almost certainly stuck.
        IDLE_FIRE_TIMEOUT_SECONDS = max(60, min(300, config.idle_interval_seconds))
        # Initial settle delay — never longer than 60s so cold-start races
        # don't stall the first fire, and never shorter than the configured
        # interval (short intervals shouldn't fire instantly on boot either).
        IDLE_INITIAL_SETTLE_SECONDS = min(config.idle_interval_seconds, 60)

        async def _run_idle_loop():
            """Self-sends config.idle_prompt periodically when the workspace is idle."""
            await asyncio.sleep(IDLE_INITIAL_SETTLE_SECONDS)

            import json as _json
            from urllib import request as _urlreq, error as _urlerr

            while True:
                try:
                    await asyncio.sleep(config.idle_interval_seconds)
                except asyncio.CancelledError:
                    return

                # Autonomous-loop circuit breaker (runaway self-fire incident
                # 2026-06-29). Once the replay guard has tripped — N consecutive
                # identical / no-new-info self-fired outputs — STOP firing the
                # idle prompt entirely. The workspace is already marked degraded
                # via runtime_wedge; continuing to self-wake would just keep
                # re-emitting the same stale replay and burning tokens. A
                # workspace restart clears the wedge and resumes idle behavior.
                try:
                    from molecule_runtime.autonomous_loop_guard import should_halt as _loop_should_halt

                    if _loop_should_halt():
                        print(
                            "Idle loop: circuit breaker OPEN — halting self-fire "
                            "(runtime degraded by replay guard; restart to resume)",
                            flush=True,
                        )
                        return
                except Exception:
                    pass  # guard import/lookup must never crash the idle loop

                # Local idle check — no platform API call, no LLM call.
                # heartbeat.active_tasks == 0 means no in-flight work.
                if heartbeat.active_tasks > 0:
                    continue

                # Issue #381 fix: skip the idle prompt if there are unconsumed
                # delegation results waiting. The heartbeat sends a self-message
                # for every new result batch, so sending the idle prompt here would
                # race: the agent would compose a stale tick BEFORE processing the
                # results notification, producing repeated identical asks (peer sends
                # correction, we respond with stale state, peer asks again).
                # By skipping the idle prompt when results are pending, we let the
                # heartbeat's own self-message wake the agent after results are
                # written. The agent then sees the results in _prepare_prompt()
                # and processes them before composing.
                # Guard logic extracted to _check_delegation_results_pending() for
                # direct unit-testing (#401 follow-up).
                if _check_delegation_results_pending():
                    print(
                        "Idle loop: skipping — unconsumed delegation results pending "
                        "(heartbeat will notify agent)",
                        flush=True,
                    )
                    continue

                # Self-post the idle prompt via the platform A2A proxy (same
                # path as initial_prompt). The agent's own concurrency control
                # rejects if the workspace becomes busy between this check and
                # the post — that's the expected safety valve.
                payload = _json.dumps({
                    "method": "message/send",
                    # #2251: single model-based builder — params generated
                    # FROM the receiver's a2a-sdk v0.3 SendMessageRequest schema.
                    "params": build_message_send_params(
                        config.idle_prompt,
                        message_id=f"idle-{_uuid.uuid4().hex[:8]}",
                        # Stamp the typed self-ping marker so the executor (a)
                        # drops the idle fire instead of queuing it behind an
                        # in-flight turn, and (b) subjects its output to the
                        # autonomous-loop replay guard. Idle self-wake was the
                        # driver of the runaway replay incident.
                        metadata={A2A_MESSAGE_SOURCE_TYPE: A2A_SOURCE_SELF_IDLE},
                    ),
                }).encode()

                def _post_sync():
                    # Returns (status_code, error_type) so the caller logs the
                    # actual outcome instead of a bare "post failed" line.
                    # #220: include auth_headers() on every idle fire. Without
                    # this, the idle loop 401s in multi-tenant mode.
                    # self_source_headers() adds X-Workspace-ID so the
                    # platform classifies the idle fire as source=agent
                    # rather than user-typed canvas input.
                    headers = {
                        "Content-Type": "application/json",
                        **self_source_headers(workspace_id),
                    }
                    try:
                        req = _urlreq.Request(
                            f"{platform_url}/workspaces/{workspace_id}/a2a",
                            data=payload,
                            headers=headers,
                        )
                        with _urlreq.urlopen(req, timeout=IDLE_FIRE_TIMEOUT_SECONDS) as resp:
                            resp.read()
                            return resp.status, None
                    except _urlerr.HTTPError as e:
                        return e.code, type(e).__name__
                    except _urlerr.URLError as e:
                        return None, f"URLError: {e.reason}"
                    except Exception as e:  # pragma: no cover — catch-all safety net
                        return None, type(e).__name__

                print(
                    f"Idle loop: firing (active_tasks=0, interval={config.idle_interval_seconds}s, "
                    f"timeout={IDLE_FIRE_TIMEOUT_SECONDS}s)",
                    flush=True,
                )
                loop_ref = asyncio.get_running_loop()

                def _log_result(future):
                    try:
                        status, err = future.result()
                        if err:
                            print(
                                f"Idle loop: post failed — status={status} err={err}",
                                flush=True,
                            )
                        else:
                            print(f"Idle loop: post ok status={status}", flush=True)
                    except Exception as e:  # pragma: no cover
                        print(f"Idle loop: executor callback crashed — {e}", flush=True)

                # DELIBERATELY no reply forwarding here (review #327): this loop
                # is the MOLECULE_MAILBOX_KERNEL=0 emergency opt-out whose
                # contract is byte-identical pre-kernel behavior, and its raw
                # config.idle_prompt carries no (idle) silence contract — the
                # digest poster (idle_digest/poster.py) owns reply delivery.
                fut = loop_ref.run_in_executor(None, _post_sync)
                fut.add_done_callback(_log_result)

        idle_loop_task = asyncio.create_task(_run_idle_loop())

    # 9a (deferred). E1 — poll-mode inbound delivery, gated on uvicorn bind.
    # Created as a task so it can wait for server.started (flipped True once the
    # socket is bound) before starting the inbox poller; this closes the
    # startup race where a message staged in the pre-bind window would be
    # self-POSTed to a not-yet-listening 127.0.0.1:{port} and lost while the
    # inbox cursor advanced. No-op in push mode (the proven default path is
    # byte-for-byte unchanged). Captures the running loop so the poller-thread
    # consumer can marshal its post onto this event loop.
    poll_delivery_task = None
    if delivery_mode == "poll":
        poll_delivery_task = asyncio.create_task(
            start_poll_delivery_when_bound(
                server,
                delivery_mode,
                platform_url,
                workspace_id,
                port,
                asyncio.get_running_loop(),
            )
        )

    # 9c. Plugin-declared channel daemons (issue #215). Manifest-declared
    # long-running sidecars (`contributes.daemons` — e.g. a channel bridge like
    # lark-channel-molecule) are spawned only AFTER uvicorn binds (same gate as
    # the poll-delivery starter above: a bridge posting at the local A2A server
    # must never race the bind) and terminated in the finally below — daemons
    # die with the workspace. Fail-open for the agent itself: discovery/spawn
    # failures are logged, never fatal, and zero-daemon workspaces skip the
    # task entirely. PR-2 binds this SAME built A2A app on a private Unix
    # socket per plugin identity; the post-bind starter waits for those local
    # listeners and only then publishes the path + spawns the daemons.
    # Plugin-declared daemons (issue #215) + post-boot hot-install
    # (scheduler-as-trigger-plugin, per-workspace delivery). The lifecycle lives
    # in a DaemonRuntime holder so it can be ESTABLISHED at boot AND EXTENDED
    # when a trigger/channel plugin is installed after boot — via
    # POST /internal/daemons/reload — WITHOUT a workspace restart (the daemon
    # supervisor + private sockets otherwise only come up at boot). The holder's
    # ensure_daemons() does the same work the inline block used to (load plugins,
    # discover specs, set/clear NATIVE_SCHEDULER_ENV + seed the grid for a
    # trigger, cold-start the supervisor + channel-event sockets once uvicorn
    # binds). It is scheduled as a BACKGROUND task — never awaited here — because
    # its bind gate only clears once `server.serve()` (below) starts listening;
    # awaiting inline would deadlock. Fail-open for the agent: setup failures are
    # logged, never fatal, and a zero-daemon workspace is a no-op.
    daemon_runtime = None
    daemon_boot_task = None
    try:
        from molecule_runtime.daemon_runtime import DaemonRuntime, add_daemon_routes

        daemon_runtime = DaemonRuntime(
            built_app, config_path, server, log_level=uvicorn_log_level,
        )
        add_daemon_routes(starlette_app, daemon_runtime)
        daemon_boot_task = asyncio.create_task(daemon_runtime.ensure_daemons())
    except Exception as e:  # noqa: BLE001 — daemons must never block boot
        print(f"Plugin daemons: setup failed (non-fatal): {e}", flush=True)

    try:
        await server.serve()
    finally:
        # 10d. Pre-stop serialization — molecule-core#1391.
        # Capture in-memory state before the container exits so it survives
        # intentional pause and unplanned restart. All content is scrubbed
        # via lib.snapshot_scrub before being written to the resolved mailbox
        # path (the persistent workspace volume when the kernel is enabled).
        try:
            from molecule_runtime.lib.pre_stop import build_snapshot, write_snapshot
            adapter_state = adapter.pre_stop_state() if adapter else {}
            snapshot = build_snapshot(heartbeat, adapter_state)
            write_snapshot(snapshot)
        except Exception as pre_stop_err:
            print(f"Warning: pre-stop serialization failed (continuing): {pre_stop_err}")

        # Cancel initial prompt if still running
        if initial_prompt_task and not initial_prompt_task.done():
            initial_prompt_task.cancel()
        # Cancel the reprovision wake note if still in flight. Staged
        # consumption: the pending state is only cleared on send success,
        # so a cancel here re-arms the announcement on the next boot —
        # bounded by MAX_WAKE_ATTEMPTS, never an unbounded replay.
        if reprovision_wake_task and not reprovision_wake_task.done():
            reprovision_wake_task.cancel()
        # Cancel idle loop if running
        if idle_loop_task and not idle_loop_task.done():
            idle_loop_task.cancel()
        # Cancel the deferred poll-delivery starter if it never got past the
        # bind-wait (e.g. shutdown during a stalled startup).
        if poll_delivery_task and not poll_delivery_task.done():
            poll_delivery_task.cancel()
        # Cancel the daemon boot task if it never got past the bind-wait, then
        # terminate any spawned plugin daemons + their sockets (issue #215 — the
        # workspace owns its channel/trigger processes; they die with it). The
        # DaemonRuntime holder owns both the supervisor and the socket transport
        # (established at boot or hot-added post-boot), so one stop() reaps
        # whatever was armed. Await the boot task first so a cold-start still
        # inside ChannelEventSocketManager.start() finishes its bind rollback
        # before stop() touches the same state.
        if daemon_boot_task:
            if not daemon_boot_task.done():
                daemon_boot_task.cancel()
            try:
                await daemon_boot_task
            except asyncio.CancelledError:
                pass
            except Exception as daemon_start_err:  # noqa: BLE001
                print(f"Warning: plugin daemon starter failed: {daemon_start_err}")
        if daemon_runtime is not None:
            try:
                await daemon_runtime.stop()
            except Exception as daemon_stop_err:  # noqa: BLE001
                print(f"Warning: plugin daemon stop failed: {daemon_stop_err}")

def main_sync():  # pragma: no cover
    """Synchronous entry point for the `molecule-runtime` console script.

    Declared directly in pyproject.toml as the wheel's entry-point target
    (`molecule-runtime = "molecule_runtime.main:main_sync"`). Keep the wrapper
    stable so installed console scripts can enter the async runtime safely.
    """
    asyncio.run(main())


def prepare_sync():  # pragma: no cover
    """Entry point for the ``molecule-runtime-prepare`` console script.

    Runs boot-install + adapter.setup() to MATERIALIZE the workspace config
    (mcp_servers, persona, skills) then exits WITHOUT registering or serving,
    so a template can pre-write a complete config before launching its agent
    gateway. Exit 0 = config materialized; non-zero = fell short (the caller
    should fall back to its normal launch path).

    A DEDICATED console entry (not an env flag on ``molecule-runtime``) is the
    robust capability signal: a wheel predating this feature simply does not
    install the ``molecule-runtime-prepare`` binary, so a caller detects its
    absence (``command -v``) and skips the pre-step — an old runtime can never
    mis-interpret an unknown flag and serve when the caller expected a
    prepare-and-exit.
    """
    raise SystemExit(asyncio.run(main(prepare_only=True)) or 0)


if __name__ == "__main__":  # pragma: no cover
    main_sync()
