"""Bridge between runtime agent and A2A protocol, with SSE streaming support.

Non-blocking inbound (task #378)
--------------------------------
When ``MOLECULE_A2A_NONBLOCKING=true``, ``_core_execute`` cooperates with
``molecule_runtime.runtime_inbox`` so a second POST sharing the running
turn's ``context_id`` returns within ~50 ms instead of queuing behind the
in-flight runtime turn. The in-flight turn polls the inbox between
``astream_events`` iterations and re-runs with the merged message history
when an interrupt fires. See ``runtime_inbox.py`` for the full design and
upstream a2a-protocol spec citation ("Task Execution and Interruption" §3).

SSE streaming architecture
--------------------------
The A2A SDK (``DefaultRequestHandler`` + ``EventQueue``) owns the SSE transport
layer.  This executor's job is to push the right event types into the queue as
work progresses:

  1. ``TaskStatusUpdateEvent(state=working)``       — immediately signals start
  2. ``TaskArtifactUpdateEvent(chunk, append=…)``   — one per LLM text token
  3. ``Message(final_text)``                        — terminal event

Client compatibility
--------------------
*Non-streaming* (``message/send``):
    ``ResultAggregator.consume_all()`` processes status/artifact events
    (updating the task in the store) and returns the final ``Message``
    immediately — backward-compatible with ``a2a_client.py`` which reads
    ``data["result"]["parts"][0]["text"]``.

*Streaming* (``message/stream``):
    ``consume_and_emit()`` yields every event above as SSE, letting the client
    render tokens in real time.

native runtime integration
---------------------
Uses ``agent.astream_events(version="v2")`` to receive ``on_chat_model_stream``
events with ``AIMessageChunk`` payloads.  Text is extracted from both plain
strings (OpenAI / Groq) and Anthropic-style content-block lists.  Non-text
content (tool_use, etc.) is silently skipped.  A fresh ``artifact_id`` is
generated for each new LLM ``run_id`` so tool-call cycles are grouped cleanly.
"""

import asyncio
import functools
import logging
import os
import uuid

from molecule_runtime import turn_lease as _turn_lease
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
# KI-009: a2a-sdk v1 renames a2a.utils → a2a.helpers; TextPart removed (Part takes text= directly)
from a2a.helpers import new_text_message
from molecule_runtime.shared_runtime import (
    extract_history as _extract_history,
    brief_task,
    set_current_task,
)
from molecule_runtime.executor_helpers import (
    build_user_content_with_files,
    collect_outbound_files,
    error_detail_for_external,
    ensure_tool_activity_file,
    extract_attached_files,
    read_delegation_results,
    sanitize_agent_error,
    task_state_value,
    tool_activity_file,
)
from molecule_runtime.attachment_vision import append_image_descriptions
from molecule_runtime.runtime_inbox import (
    current_context_id as _current_context_id,
    get_inbox as _get_runtime_inbox,
    is_nonblocking_enabled as _nonblocking_enabled,
)
from molecule_runtime.builtin_tools.telemetry import (
    A2A_TASK_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    WORKSPACE_ID_ATTR,
    _incoming_trace_context,
    gen_ai_system_from_model,
    get_tracer,
    record_llm_token_usage,
)
from molecule_runtime.context_budget import (
    get_model_context_window,
    should_compact_context,
    should_emit_budget_warning,
)
from molecule_runtime.compact import (
    DEFAULT_KEEP_RECENT_N,
    compact_messages,
)
from molecule_runtime.platform_auth import get_workspace_id as _get_workspace_id

logger = logging.getLogger(__name__)

# CWE-20 (issue #14): _WORKSPACE_ID becomes an OpenTelemetry span
# attribute; "unknown" sentinel preserved for the no-env fallback so the
# tracer keeps working before the workspace ID is provisioned.
try:
    _WORKSPACE_ID = _get_workspace_id()
except ValueError:
    _WORKSPACE_ID = "unknown"

# runtime agent cycle budget per turn. Library default is 25; 500 covers
# PM fan-outs (plan → 6 delegations → 6 awaits → 6 results → synthesize ≈
# 30+ steps even before retries). Overridable via MOLECULE_RECURSION_LIMIT.
DEFAULT_RECURSION_LIMIT = 500


def _extract_plain_message_text(context: RequestContext) -> str:
    """Extract only text parts, leaving file parts for explicit attachment handling."""
    message = getattr(context, "message", None)
    parts = getattr(message, "parts", None) or []
    texts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
            continue
        root = getattr(part, "root", None)
        root_text = getattr(root, "text", None) if root is not None else None
        if root_text:
            texts.append(root_text)
    return " ".join(texts).strip()


def _parse_recursion_limit() -> int:
    """Read MOLECULE_RECURSION_LIMIT; fall back to DEFAULT_RECURSION_LIMIT
    with a WARNING log on any unparseable or non-positive value."""
    raw = os.environ.get("MOLECULE_RECURSION_LIMIT", "")
    if not raw:
        return DEFAULT_RECURSION_LIMIT
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "MOLECULE_RECURSION_LIMIT=%r is not an integer; using default %d",
            raw, DEFAULT_RECURSION_LIMIT,
        )
        return DEFAULT_RECURSION_LIMIT
    if n <= 0:
        logger.warning(
            "MOLECULE_RECURSION_LIMIT=%d is not positive; using default %d",
            n, DEFAULT_RECURSION_LIMIT,
        )
        return DEFAULT_RECURSION_LIMIT
    return n

# ---------------------------------------------------------------------------
# Compliance (OWASP Top 10 for Agentic Apps) — optional, lazy-loaded
# ---------------------------------------------------------------------------

try:
    from molecule_runtime.builtin_tools.compliance import (
        AgencyTracker,
        PromptInjectionError,
        redact_pii as _redact_pii,
        sanitize_input as _sanitize_input,
    )
    _COMPLIANCE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _COMPLIANCE_AVAILABLE = False


@functools.lru_cache(maxsize=1)
def _get_compliance_cfg():
    """Return ComplianceConfig or None (cached for process lifetime)."""
    try:
        from molecule_runtime.config import load_config
        return load_config().compliance
    except Exception:
        return None


def _extract_chunk_text(content) -> list[str]:
    """Extract text strings from an LLM streaming chunk's content field.

    Handles both provider content styles:
    - OpenAI / Groq: ``content`` is a plain ``str`` (empty for tool-call chunks).
    - Anthropic:     ``content`` is a list of typed blocks, e.g.
        ``[{"type": "text", "text": "Hello"}, {"type": "tool_use", ...}]``

    Only ``"text"`` blocks are returned; ``tool_use``, ``tool_result``, and
    other non-text blocks are filtered out so raw tool JSON never appears in
    the SSE stream.

    Args:
        content: ``chunk.content`` value from an ``on_chat_model_stream`` event.

    Returns:
        List of non-empty text strings.
    """
    if isinstance(content, str):
        return [content] if content else []
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)
            elif isinstance(block, str) and block:
                texts.append(block)
        return texts
    return []


# Typed source marker for outbound/inbound A2A messages. Carried in
# params.metadata (sender) and surfaced on the received Message (receiver).
A2A_MESSAGE_SOURCE_TYPE = "source_type"
A2A_SOURCE_SELF_CRON = "self-cron"
A2A_SOURCE_SELF_HARVESTER = "self-harvester"
# Idle self-wake (main.py _run_idle_loop): periodically re-pokes the agent
# while the workspace is idle. Marked as a routine self-ping so (a) it drops
# rather than queues behind an in-flight turn, and (b) its output is subject
# to the autonomous-loop replay guard — the idle self-wake was the driver of
# the 2026-06-29 runaway delegation-result replay incident.
A2A_SOURCE_SELF_IDLE = "self-idle"

# Mailbox-kernel autonomous self-turn kinds (native default-ON since 2026-07-13;
# MOLECULE_MAILBOX_KERNEL=0 opts out; injected via molecule_runtime.kernel). Each is a ROUTINE SELF-PING — same class
# as cron/idle/harvester — so it drops rather than queues behind an in-flight
# turn AND its output runs through the UNCHANGED evaluate_autonomous_output
# replay guard (registration in _ROUTINE_SELF_SOURCE_TYPES below is what wires
# that governance; NO new suppression code is added anywhere).
#   scheduler   — mailbox scheduler tick
#   goal-nudge  — goal / plan re-engagement nudge
#   delegation  — explicit delegation-result harvest self-turn
A2A_SOURCE_SELF_SCHEDULER = "self-scheduler"
A2A_SOURCE_SELF_GOAL_NUDGE = "self-goal-nudge"
A2A_SOURCE_SELF_DELEGATION = "self-delegation-result"
# Lifecycle wake-up calls (task #219 §5): the boot initial_prompt + the
# reprovision-wake announcement. Previously stamped NO source_type, so they
# bypassed the replay guard and queued behind in-flight turns. Registering the
# type here makes every lifecycle wake guard-governed like the other self-pings
# (a runaway greeting/announce loop trips the breaker), and drop-not-queue on
# the non-blocking fast-path (harmless — lifecycle wakes fire at boot/restart
# when no turn is in flight).
A2A_SOURCE_SELF_LIFECYCLE = "self-lifecycle"

# Routine self-pings the runtime sends to ITSELF: the platform cron tick, the
# heartbeat delegation-results harvester, and the idle self-wake. Under the
# non-blocking fast-path these must NOT queue behind (or interrupt) a long
# in-flight turn — the cron/idle recur every cycle and delegation results are
# injected from DELEGATION_RESULTS_FILE at the next turn, so dropping a
# mid-turn routine ping loses nothing while avoiding both task-interruption
# and ping pile-up. These are also the ONLY turns the autonomous-loop replay
# guard governs — a user-directed turn is never suppressed.
_ROUTINE_SELF_SOURCE_TYPES = (
    A2A_SOURCE_SELF_CRON,
    A2A_SOURCE_SELF_HARVESTER,
    A2A_SOURCE_SELF_IDLE,
    # Mailbox-kernel kinds — governed identically to the legacy self-pings.
    A2A_SOURCE_SELF_SCHEDULER,
    A2A_SOURCE_SELF_GOAL_NUDGE,
    A2A_SOURCE_SELF_DELEGATION,
    A2A_SOURCE_SELF_LIFECYCLE,
)

# Deprecated text-prefix fallback. Wording drift has already been observed
# (cron ticks no longer start with the registered literal), so new senders
# MUST stamp A2A_MESSAGE_SOURCE_TYPE. The prefix list is retained only for
# backward compatibility with platform versions that have not yet adopted the
# typed marker.
_ROUTINE_SELF_MESSAGE_PREFIXES = (
    "This is your own scheduled work tick",  # platform cron self-tick
    "Delegation results are ready",          # heartbeat delegation harvester
)


def _get_message_metadata(context: RequestContext) -> dict | None:
    """Return the inbound A2A envelope's metadata dict, if any."""
    metadata = None
    message = getattr(context, "message", None)
    if message is not None:
        metadata = getattr(message, "metadata", None)
    if metadata is None:
        metadata = getattr(context, "metadata", None)
    return metadata if isinstance(metadata, dict) else None


def _get_message_source_type(context: RequestContext) -> str | None:
    """Return the typed source_type marker from the inbound A2A envelope, if any."""
    metadata = _get_message_metadata(context)
    return metadata.get(A2A_MESSAGE_SOURCE_TYPE) if metadata is not None else None


# Metadata key the trigger daemon attaches (SDK scaffold) so the runtime can
# attribute a completed self-scheduled turn's outcome back to its schedule.
A2A_MESSAGE_SCHEDULE_NAME = "schedule_name"


def _attribute_schedule_outcome(
    context: RequestContext,
    *,
    final_text: str | None = None,
    error_text: str | None = None,
) -> None:
    """Record a self-scheduled turn's outcome for the auto-disable/stale engine.

    Pass ``final_text`` for a completed turn (classified empty/ok) or ``error_text``
    for a failed one (classified provider-error/neutral). A no-op for everything
    except a trigger-daemon fire — a fire is identified by
    ``source_type == self-scheduler`` AND a ``schedule_name`` in the envelope
    metadata (the mailbox scheduler tick shares the source_type but carries no
    schedule_name, so it is correctly ignored). On the 3rd consecutive provider
    error the schedule is disabled via the source-preserving
    :meth:`ScheduleStore.set_enabled`. Fully guarded: health bookkeeping must
    never break a turn, so any failure is logged and swallowed.
    """
    try:
        if _get_message_source_type(context) != A2A_SOURCE_SELF_SCHEDULER:
            return
        metadata = _get_message_metadata(context) or {}
        schedule_name = metadata.get(A2A_MESSAGE_SCHEDULE_NAME)
        if not isinstance(schedule_name, str) or not schedule_name:
            return  # older plugin without the correlation key — nothing to attribute
        # Lazy imports: keep these (jsonschema/cronspec-dragging) modules off the
        # module-load path, and out of the hot path for non-scheduled turns.
        from molecule_runtime import schedule_outcome
        from molecule_runtime.trigger_state import resolve_grid_path, resolve_trigger_state_dir

        if error_text is not None:
            outcome = schedule_outcome.classify_error_text(error_text)
            error = error_text
        else:
            outcome = schedule_outcome.classify_final_text(final_text or "")
            error = ""

        action = schedule_outcome.record_and_persist(
            resolve_trigger_state_dir(), schedule_name, outcome, error=error
        )
        if action.disable:
            from molecule_runtime.schedule_engine import DISABLE_AFTER_SDK_ERRORS
            from molecule_runtime.schedule_store import ScheduleStore

            ScheduleStore(resolve_grid_path()).set_enabled(schedule_name, False)
            logger.warning(
                "auto-disabled schedule %r after %d consecutive provider errors "
                "(RFC invariant #8)",
                schedule_name,
                DISABLE_AFTER_SDK_ERRORS,
            )
    except Exception as exc:  # never let health bookkeeping break the turn
        logger.debug("schedule-outcome attribution skipped: %s", exc)


def _is_routine_self_message(context: RequestContext, text: str) -> bool:
    """True for the agent's own routine self-pings (cron tick / harvester).

    Prefers the typed source_type marker; falls back to the legacy text-prefix
    list only when no marker is present. This prevents wording drift from
    silently breaking the drop-vs-queue decision (issue #138).
    """
    source_type = _get_message_source_type(context)
    if source_type in _ROUTINE_SELF_SOURCE_TYPES:
        return True
    if source_type is not None:
        # A marker exists but it is not one of the routine self-ping types.
        return False
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in _ROUTINE_SELF_MESSAGE_PREFIXES)


def _tooltrace_to_trace_shapes(tool_trace: list[dict]) -> tuple[list, list, list]:
    """Map the astream ``tool_trace`` onto the SSOT AgentTrace capture shapes
    read by ``molecule_runtime.tracing.TracingExecutor``.

    Each ``tool_trace`` entry is ``{tool, input, output_preview}`` (see the
    on_tool_start / on_tool_end handlers in ``_core_execute``). Returns
    ``(tool_uses, tool_calls, steps)``:
      * ``tool_uses``  — ordered tool-name list (AgentTrace.tool_uses)
      * ``tool_calls`` — ``{name, input, output}`` (the _emit tool_calls fallback)
      * ``steps``      — ordered ``tool_call`` steps (AgentTrace.steps)
    No ``thinking`` steps on the native path: ``_extract_chunk_text`` filters
    reasoning blocks out of the stream, so a turn's steps are its tool calls.
    """
    tool_uses = [e.get("tool", "") for e in tool_trace]
    tool_calls = [
        {"name": e.get("tool", ""), "input": e.get("input", ""),
         "output": e.get("output_preview", "")}
        for e in tool_trace
    ]
    steps = [
        {"kind": "tool_call", "name": e.get("tool", ""),
         "input": e.get("input", ""), "result": e.get("output_preview", "")}
        for e in tool_trace
    ]
    return tool_uses, tool_calls, steps


class RuntimeA2AExecutor(AgentExecutor):
    """Bridges runtime agent to A2A event model with SSE streaming support.

    Always uses ``agent.astream_events()`` so that:
    - Streaming clients (``message/stream``) receive token-level SSE events.
    - Non-streaming clients (``message/send``) receive the final ``Message``
      collected from the same stream — no duplicate LLM call, full compat.
    """

    def __init__(self, agent, heartbeat=None, model: str = "unknown"):
        self.agent = agent  # Compiled runtime graph (create_react_agent output)
        self._heartbeat = heartbeat
        self._model = model  # e.g. "anthropic:claude-sonnet-4-6"
        # runtime#133: per-context_id LRU of the last turn's
        # input_tokens. Used by the compact-and-continue hook to
        # act on the previous turn's usage BEFORE the next turn's
        # LLM call. Bounded (256 entries, see the post-LLM-call
        # block) so a long-running executor doesn't grow this
        # unboundedly.
        self._last_input_tokens: dict[str, int] = {}
        # Per-turn trace capture read by molecule_runtime.tracing.TracingExecutor
        # (getattr on the wrapped inner). Populated from the astream tool_trace at
        # turn completion so the Langfuse trace shows the ordered tool calls; reset
        # each turn so a tool-less turn doesn't inherit the prior turn's steps.
        self._last_tool_uses: list = []
        self._last_tool_calls: list = []
        self._last_steps: list = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Execute a task from an A2A request with SSE streaming.

        Event emission sequence:
          1. TaskStatusUpdateEvent(working)           — immediate start signal
          2. TaskArtifactUpdateEvent chunks           — token-by-token via astream_events
          3. Message(final_text)                      — terminal; non-streaming clients
                                                        return on this; streaming clients
                                                        also receive it as the last SSE event.
        """
        await self._core_execute(context, event_queue)

    async def _core_execute(self, context: RequestContext, event_queue: EventQueue) -> str:
        """Core execution pipeline for an A2A request.

        Returns the final response text (empty string on empty input or error).

        Event emission sequence:
          1. TaskStatusUpdateEvent(working)           — immediate start signal
          2. TaskArtifactUpdateEvent chunks           — token-by-token via astream_events
          3. Message(final_text)                      — terminal event
        """
        # Reset per-turn trace capture (see __init__) so a turn that makes no
        # tool calls reports empty rather than inheriting the prior turn's steps.
        self._last_tool_uses = []
        self._last_tool_calls = []
        self._last_steps = []
        user_input = _extract_plain_message_text(context)
        # Classify ROUTINE SELF-PING (idle self-wake / cron tick / delegation
        # harvester) up front, from the ORIGINAL message — before the
        # delegation-result injection below rewrites user_input. Only these
        # autonomous turns are governed by the replay guard at the end of the
        # turn; a user-directed turn is never suppressed. (Detection prefers
        # the typed source_type marker, so the later injection is irrelevant —
        # capturing here keeps the text-prefix fallback honest too.)
        _is_routine_turn = _is_routine_self_message(context, user_input)
        # Inject delegation results from prior turns. Heartbeat writes
        # completed delegation rows to DELEGATION_RESULTS_FILE and sends
        # a self-message to wake the agent; this consumes the file and
        # surfaces the results as context so the agent can act on them
        # without needing an explicit check_task_status call.
        # Results are prepended so they are visible even when the
        # self-message text is overwritten by a subsequent user message.
        pending_results = read_delegation_results()
        if pending_results:
            logger.info("A2A execute: injecting %d delegation result(s)", pending_results.count("\n") + 1)
            user_input = f"[Delegation results available]\n{pending_results}\n\n{user_input}"
        _attached_files = extract_attached_files(getattr(context, "message", None))
        if _attached_files:
            user_input = await append_image_descriptions(user_input, _attached_files)
        if not user_input and not _attached_files:
            parts = getattr(getattr(context, "message", None), "parts", None)
            logger.warning("A2A execute: no text content in message parts: %s", parts)
            await event_queue.enqueue_event(
                new_text_message("Error: message contained no text content.")
            )
            return ""

        # ── OA-01: Prompt injection check (OWASP Agentic Top 10) ────────────
        _compliance_cfg = _get_compliance_cfg() if _COMPLIANCE_AVAILABLE else None
        if _COMPLIANCE_AVAILABLE and _compliance_cfg and _compliance_cfg.mode == "owasp_agentic":
            try:
                user_input = _sanitize_input(
                    user_input,
                    prompt_injection_mode=_compliance_cfg.prompt_injection,
                    context_id=context.context_id or "",
                )
            except PromptInjectionError as exc:
                await event_queue.enqueue_event(
                    new_text_message(f"Request blocked: {exc}")
                )
                return ""

        logger.info("A2A execute: user_input=%s", user_input[:200])

        # ── OTEL: task_receive span ──────────────────────────────────────────
        parent_ctx = _incoming_trace_context.get()
        tracer = get_tracer()

        _result: str = ""  # captured inside the span for return after it closes

        with tracer.start_as_current_span("task_receive", context=parent_ctx) as task_span:
            task_span.set_attribute(WORKSPACE_ID_ATTR, _WORKSPACE_ID)
            task_span.set_attribute(A2A_TASK_ID, context.context_id or "")
            task_span.set_attribute("a2a.input_preview", user_input[:256])

            # Resolve IDs — the RequestContextBuilder always sets them, but
            # we generate fallbacks for safety (e.g. in unit tests).
            task_id = context.task_id or str(uuid.uuid4())
            context_id = context.context_id or str(uuid.uuid4())

            # Task #377 — stamp the active context_id into the runtime
            # contextvar so any subprocess-spawning tool (sandbox, etc.)
            # can self-register its handle on the matching inbox entry.
            # Cancel() then has a Process handle to SIGTERM, propagating
            # the canvas "Stop All" all the way down to the bash child.
            _current_context_id.set(context_id)

            # A2A v1 contract (a2a-sdk ≥ 1.0): enqueue a Task event before any
            # TaskStatusUpdateEvent. The framework only auto-creates the Task
            # on continuation messages (existing task_id resolves via
            # task_manager.get_task()). For fresh requests get_task() returns
            # None and the SDK rejects the first status update with
            # InvalidAgentResponseError("Agent should enqueue Task before
            # TaskStatusUpdateEvent event") — see a2a/server/agent_execution/
            # active_task.py for the validation site. PR #2170 migrated the
            # surface to v1 but missed this contract; the synth-E2E gate
            # surfaced it on every run after staging deploy.
            if getattr(context, "current_task", None) is None:
                from a2a.types import Task, TaskStatus
                await event_queue.enqueue_event(
                    Task(
                        id=task_id,
                        context_id=context_id,
                        status=TaskStatus(state=task_state_value("TASK_STATE_SUBMITTED")),
                    )
                )

            updater = TaskUpdater(event_queue, task_id, context_id)

            # ── Task #378: non-blocking fast-path ────────────────────────
            # When the feature flag is on, register this context's inbox
            # entry. If a turn is already running for this context_id,
            # request an interrupt + return immediately so the POST handler
            # acks within ~50 ms instead of blocking behind the live turn.
            _inbox_entry = None
            _nonblocking = _nonblocking_enabled()
            if _nonblocking:
                _inbox_entry = await _get_runtime_inbox().get_or_create(context_id)
                if _inbox_entry.turn_in_flight:
                    # CTO model: queue, don't break. A new message arriving while a
                    # turn is in flight is fast-acked (~50ms, no 300s head-of-line
                    # wedge) and QUEUED; the running turn finishes uninterrupted, then
                    # drains + answers it as a follow-up turn. Only an explicit Stop
                    # (tasks/cancel) breaks a turn in flight.
                    if _is_routine_self_message(context, user_input):
                        # Routine self-ping (cron tick / delegation harvester):
                        # fast-ack and DROP — not queued, not interrupting. The cron
                        # recurs and delegation results are injected from
                        # DELEGATION_RESULTS_FILE next turn, so nothing is lost, and
                        # we avoid stacking identical pings behind a long task.
                        logger.info(
                            "A2A execute: routine self-ping while turn in flight for "
                            "context_id=%s — fast-ack + drop (recurs next cycle)",
                            context_id,
                        )
                    else:
                        logger.info(
                            "A2A execute: turn in flight for context_id=%s — deferring "
                            "new message (process after current turn)",
                            context_id,
                        )
                        _accepted = _inbox_entry.defer_message(
                            build_user_content_with_files(user_input, _attached_files)
                        )
                        if not _accepted:
                            # Inbox at capacity — emit structured backpressure so
                            # the caller can retry rather than silently dropping.
                            await updater.complete(
                                message=new_text_message(
                                    '{"status":"busy","retry":true}',
                                    task_id=task_id,
                                    context_id=context_id,
                                )
                            )
                            return ""
                    # Fast-ack the POST so the platform proxy gets a prompt 200
                    # instead of blocking ~300s behind the live turn.
                    await updater.complete(
                        message=new_text_message(
                            "[Acknowledged — queued; the agent will respond after it "
                            "finishes its current step.]",
                            task_id=task_id,
                            context_id=context_id,
                        )
                    )
                    return ""
                _inbox_entry.turn_in_flight = True

            # Turn-lease activity watcher handle (MUST-FIX 1/3). Initialized
            # here — before anything in the try body can raise — so the finally
            # can always reference it. Stays None on the kernel-off path (no
            # lease installed), so its cancellation is a no-op there.
            _lease_watcher_task = None
            try:
                # set_current_task INSIDE the try so active_tasks is always
                # decremented by the finally block even if CancelledError hits
                # during the heartbeat HTTP push. Moving it outside the try
                # created a window where cancellation left active_tasks stuck
                # at 1, permanently blocking queue drain. (#2026)
                user_content = build_user_content_with_files(user_input, _attached_files)
                await set_current_task(self._heartbeat, brief_task(user_input))
                messages = _extract_history(context)
                if messages:
                    logger.info("A2A execute: injecting %d history messages", len(messages))
                # runtime#133 compact-and-continue (step 2 + step 4):
                # if the PREVIOUS turn's input_tokens for this
                # context was at the watermark, compact the
                # history before adding the new user message. The
                # next LLM call (now on the compacted history) is
                # the one that would have overflowed, so this
                # shortcut prevents the 400 entirely on the
                # common path. The detection layer (step 1,
                # context_budget.should_compact_context) decided
                # the watermark cross; this is the deterministic
                # act-on-it step. Step 4's brief notice is the
                # logger.info immediately below — observable, not
                # silent. _last_input_tokens is updated in the
                # post-LLM-call block further down (so this
                # turn's compaction decision is based on the
                # PREVIOUS turn's usage). If _last_input_tokens
                # for this context is missing (first turn,
                # process restart, or context-id rotation), we
                # fall through without compacting — the LLM call
                # itself will return 400 if we're already over,
                # and the existing error path takes over.
                _last_inp = getattr(self, "_last_input_tokens", {}).get(context_id)
                if _last_inp is not None and _last_inp > 0:
                    _ctx_win = get_model_context_window(self._model)
                    if should_compact_context(int(_last_inp), _ctx_win):
                        compacted, _stats = compact_messages(
                            messages,
                            keep_recent_n=DEFAULT_KEEP_RECENT_N,
                        )
                        if _stats.dropped_count > 0:
                            logger.info(
                                "context_compacted: context_id=%s model=%s "
                                "before=%d after=%d dropped=%d system_preserved=%s "
                                "trigger=last_turn_input_tokens=%d threshold_pct=85 "
                                "— runtime#133 compact-and-continue",
                                context_id[:8], self._model,
                                _stats.original_count, _stats.compacted_count,
                                _stats.dropped_count, _stats.system_preserved,
                                int(_last_inp),
                            )
                            messages = compacted
                messages.append(("human", user_content))

                # Recursion limit: see DEFAULT_RECURSION_LIMIT and
                # _parse_recursion_limit() at module top. Re-read on every
                # call so the env var can be hot-changed between requests.
                recursion_limit = _parse_recursion_limit()
                run_config = {
                    "configurable": {"thread_id": context_id},
                    "run_name": f"a2a-{context_id[:8]}",
                    "recursion_limit": recursion_limit,
                }

                # ── OTEL: llm_call span ──────────────────────────────────────
                with tracer.start_as_current_span("llm_call") as llm_span:
                    llm_span.set_attribute(GEN_AI_OPERATION_NAME, "chat")
                    llm_span.set_attribute(GEN_AI_SYSTEM, gen_ai_system_from_model(self._model))
                    llm_span.set_attribute(GEN_AI_REQUEST_MODEL, self._model)
                    llm_span.set_attribute(WORKSPACE_ID_ATTR, _WORKSPACE_ID)

                    # ── Step 1: signal "working" to streaming clients ─────────
                    await updater.start_work()

                    # ── Step 2: stream tokens via native runtime astream_events ────
                    # Each "on_chat_model_stream" event carries an AIMessageChunk.
                    # We emit one TaskArtifactUpdateEvent per text chunk so SSE
                    # clients can render tokens in real time.
                    # artifact_id resets on each new LLM run_id so agent→tool→agent
                    # cycles each get their own artifact slot.

                    artifact_id = str(uuid.uuid4())
                    has_streamed = False   # True after first chunk for current artifact
                    current_run_id = None  # Detects new LLM call in a ReAct cycle
                    accumulated: list[str] = []    # All text for the final Message
                    last_ai_message = None          # Saved for token-usage telemetry

                    # ── OA-03: Excessive agency tracker ──────────────────────
                    _agency = (
                        AgencyTracker(
                            max_tool_calls=_compliance_cfg.max_tool_calls_per_task,
                            max_duration_seconds=float(_compliance_cfg.max_task_duration_seconds),
                        )
                        if _COMPLIANCE_AVAILABLE and _compliance_cfg and _compliance_cfg.mode == "owasp_agentic"
                        else None
                    )

                    # ── Tool trace: collect every tool invocation for
                    # platform-level observability ────────────────────
                    # Keyed by run_id so parallel tool calls (native runtime
                    # supports them) pair start→end correctly. Capped at
                    # MAX_TOOL_TRACE entries to prevent runaway loops from
                    # ballooning the JSONB payload.
                    MAX_TOOL_TRACE = 200
                    tool_trace: list[dict] = []
                    tool_trace_by_run: dict[str, dict] = {}

                    # Task #378: the astream loop is wrapped in a
                    # while-True so an interrupt (new POST for the same
                    # context_id) can abort the in-flight turn, drain
                    # the inbox, append the queued messages to history,
                    # and re-enter astream with the merged context. The
                    # subprocess registered in the inbox entry — if any
                    # — is terminated so a `bash -c "sleep 600"` doesn't
                    # block the new turn.
                    _idle_cap = float(os.environ.get("A2A_COMPLETION_IDLE_TIMEOUT_SECONDS", "900"))
                    # Arm the turn lease (MUST-FIX 1). Pinned to _idle_cap by
                    # default so the lease and the idle-cap complement rather
                    # than fight. No-op when the mailbox kernel is off.
                    _turn_lease.reset_current()
                    # MUST-FIX 1/3: resolve the subprocess tool-activity file and
                    # (kernel-on only) start a background watcher that refreshes
                    # the lease from it. A codex/openclaw/hermes turn whose
                    # child is churning tools bumps this file even when astream
                    # emits no native event, so the watcher keeps the lease fresh
                    # and the idle-cap handler below does NOT mistake a live
                    # subprocess for a stall. When the kernel is off no lease is
                    # installed -> watch_activity_file returns immediately and
                    # _lease_watcher_task stays None -> default flow byte-identical.
                    _tool_activity_path = tool_activity_file()
                    if _turn_lease.current() is not None and _lease_watcher_task is None:
                        # Kernel ON: materialize a PRIVATE per-turn activity file
                        # (parent 0700, file 0600) under the mailbox dir and
                        # EXPORT it via MOLECULE_TOOL_ACTIVITY_FILE so subprocess
                        # executors write to the SAME path instead of a
                        # world-writable /tmp file any process could touch to
                        # forge liveness. Kernel OFF: this block is skipped and
                        # _tool_activity_path keeps the legacy /tmp default
                        # (byte-identical, and never read since the lease is None).
                        _tool_activity_path = ensure_tool_activity_file()
                        try:
                            _lease_watcher_task = asyncio.create_task(
                                _turn_lease.watch_activity_file(_tool_activity_path)
                            )
                        except RuntimeError:
                            # No running loop (shouldn't happen inside execute) —
                            # fall back to the boundary-feed in the handler below.
                            _lease_watcher_task = None
                    while True:
                        _astream_iter = self.agent.astream_events(
                            {"messages": messages},
                            config=run_config,
                            version="v2",
                        )
                        _stopped = False
                        _aiter = _astream_iter.__aiter__()
                        # Kernel-ON non-destructive idle-cap (MUST-FIX 1): the
                        # pending __anext__ pull is kept as a task so an idle-cap
                        # timeout does NOT cancel a live subprocess turn. Reset per
                        # astream (re)start. Unused on the kernel-off path.
                        _pending_next = None
                        while True:
                            if _turn_lease.current() is None:
                                # ── Kernel OFF: EXACT original idle-cap path,
                                # byte-identical to the proven push/openclaw flow. ──
                                try:
                                    event = await asyncio.wait_for(_aiter.__anext__(), _idle_cap)
                                except StopAsyncIteration:
                                    break
                                except asyncio.TimeoutError:
                                    # No runtime event for _idle_cap s: the completion stalled.
                                    # Fail the turn as a NORMAL error (not a wedge) so the
                                    # single-threaded executor returns and serves the next
                                    # request instead of hanging until a watchdog restart.
                                    try:
                                        await _astream_iter.aclose()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    raise TimeoutError("completion stalled: no runtime event for %ss" % _idle_cap)
                            else:
                                # ── Kernel ON: NON-destructive idle-cap. Wait on the
                                # pending pull WITHOUT cancelling it, so a subprocess
                                # churning tools (refreshing the lease via
                                # MOLECULE_TOOL_ACTIVITY_FILE, source C) is NOT killed
                                # at the cap. A stall is declared ONLY when the lease
                                # has ALSO gone stale (no tool activity for the TTL,
                                # pinned to the idle-cap) — "neither declares a stall
                                # earlier than the other" (MUST-FIX 1). An explicit
                                # Stop (interrupt_event, set out-of-band by cancel())
                                # still ends the turn even if no event has flowed. ──
                                if _pending_next is None:
                                    _pending_next = asyncio.ensure_future(_aiter.__anext__())
                                _done, _ = await asyncio.wait({_pending_next}, timeout=_idle_cap)
                                if not _done:
                                    _interrupted = (
                                        _inbox_entry is not None
                                        and _inbox_entry.interrupt_event.is_set()
                                    )
                                    if not _interrupted and _turn_lease.turn_is_alive_despite_idle(
                                        _tool_activity_path
                                    ):
                                        logger.debug(
                                            "SSE: idle-cap elapsed but turn lease is live "
                                            "(subprocess tool activity) — continuing turn"
                                        )
                                        continue
                                    # Real stall (or Stop): cancel the pending pull,
                                    # close the stream, and fail exactly as the
                                    # kernel-off idle-cap does above.
                                    _pending_next.cancel()
                                    try:
                                        await _pending_next
                                    except BaseException:  # noqa: BLE001 — swallow the cancellation
                                        pass
                                    try:
                                        await _astream_iter.aclose()
                                    except Exception:  # noqa: BLE001
                                        pass
                                    raise TimeoutError("completion stalled: no runtime event for %ss" % _idle_cap)
                                try:
                                    event = _pending_next.result()
                                except StopAsyncIteration:
                                    break
                                finally:
                                    _pending_next = None
                            # Cooperative interrupt check — runs between
                            # every runtime event so even a long
                            # tool-call iteration can be aborted by an
                            # inbound message. The check is O(1)
                            # (asyncio.Event.is_set) so this stays
                            # zero-cost when no interrupt is pending.
                            if _inbox_entry is not None and _inbox_entry.interrupt_event.is_set():
                                logger.info(
                                    "A2A execute: STOP requested for context_id=%s — "
                                    "terminating turn (Stop button / tasks-cancel)",
                                    context_id,
                                )
                                _inbox_entry.kill_subprocess()
                                _stopped = True
                                # Close the iterator promptly so any
                                # pending native runtime coroutines see the
                                # cancellation and clean up.
                                try:
                                    await _astream_iter.aclose()
                                except Exception:  # noqa: BLE001
                                    pass
                                break

                            kind = event.get("event", "")

                            if kind == "on_chat_model_stream":
                                run_id = event.get("run_id", "")
                                if run_id and run_id != current_run_id:
                                    # New LLM run started — fresh artifact slot
                                    current_run_id = run_id
                                    artifact_id = str(uuid.uuid4())
                                    has_streamed = False

                                chunk = event.get("data", {}).get("chunk")
                                if chunk is not None:
                                    texts = _extract_chunk_text(chunk.content)
                                    for text in texts:
                                        await updater.add_artifact(
                                            parts=[Part(text=text)],  # v1: TextPart removed, Part takes text= directly
                                            artifact_id=artifact_id,
                                            append=has_streamed,  # False=first, True=append
                                            last_chunk=False,
                                        )
                                        has_streamed = True
                                        accumulated.append(text)

                            elif kind == "on_tool_start":
                                tool_name = event.get("name", "?")
                                tool_input = event.get("data", {}).get("input", "")
                                tool_run_id = event.get("run_id", "")
                                logger.debug("SSE: tool start — %s", tool_name)
                                if len(tool_trace) < MAX_TOOL_TRACE:
                                    entry = {
                                        "tool": tool_name,
                                        "input": str(tool_input)[:500] if tool_input else "",
                                    }
                                    tool_trace.append(entry)
                                    if tool_run_id:
                                        tool_trace_by_run[tool_run_id] = entry
                                if _agency is not None:
                                    _agency.on_tool_call(
                                        tool_name=tool_name,
                                        context_id=context_id,
                                    )
                                # MUST-FIX 1 (turn lease): a tool call is
                                # liveness. Touch the process-global lease so a
                                # long tool-running turn is not mistaken for a
                                # stall. No-op when the mailbox kernel is off
                                # (no lease installed) — default flow unchanged.
                                _turn_lease.touch_current()

                            elif kind == "on_tool_end":
                                tool_end_name = event.get("name", "?")
                                tool_output = event.get("data", {}).get("output", "")
                                tool_run_id = event.get("run_id", "")
                                logger.debug("SSE: tool end — %s", tool_end_name)
                                # Pair via run_id so parallel tool calls don't clobber each other.
                                entry = tool_trace_by_run.get(tool_run_id) if tool_run_id else None
                                if entry is not None:
                                    entry["output_preview"] = str(tool_output)[:300] if tool_output else ""
                                # Turn lease: 'resets on ANY tool call' — the
                                # end event is activity too. No-op when kernel off.
                                _turn_lease.touch_current()

                            elif kind == "on_chat_model_end":
                                # Capture the last completed AIMessage for token telemetry
                                output = event.get("data", {}).get("output")
                                if output is not None:
                                    last_ai_message = output

                        if _stopped:
                            # Explicit Stop mid-turn — terminate, do NOT restart. Discard
                            # any queued messages (stop means stop); cancel() emits the
                            # terminal CANCELED event.
                            if _inbox_entry is not None:
                                _inbox_entry.consume_pending()
                            break

                        if _inbox_entry is None:
                            # Non-blocking off — single turn, no deferral.
                            break

                        # Natural completion. Drain messages DEFERRED during this turn
                        # (queued via defer_message, never interrupting). If any, snapshot
                        # the accumulated text as an AI message, append the queued user
                        # messages, and run a follow-up turn with the merged history —
                        # "queue, don't break": the in-flight turn was never cut off.
                        _pending = _inbox_entry.consume_pending()
                        if not _pending:
                            break
                        _partial = "".join(accumulated).strip()
                        if _partial:
                            messages.append(("ai", _partial))
                            _inbox_entry.last_accumulated = _partial
                        # Each queued message becomes a fresh human turn
                        # in arrival order. The runtime agent treats
                        # them as a normal multi-turn continuation.
                        for _m in _pending:
                            messages.append(("human", _m))
                        # Reset stream state for the restart so artifact
                        # IDs and token accumulators don't bleed across.
                        accumulated.clear()
                        current_run_id = None
                        artifact_id = str(uuid.uuid4())
                        has_streamed = False

                    # Record token usage from the last completed LLM call
                    if last_ai_message is not None:
                        record_llm_token_usage(llm_span, {"messages": [last_ai_message]})
                        # runtime#133 (smallest-scope-first): emit a
                        # structured `context budget warning` log when
                        # the input token count crosses the
                        # compact-context watermark (default 85% of
                        # the model's context window). The actual
                        # compaction algorithm lives in the workspace
                        # agent (core); this hook surfaces the
                        # budget-pressure signal in the runtime's log
                        # so the future workspace-agent step has a
                        # deterministic event to filter on. The
                        # log fields (model, input_tokens,
                        # context_window, threshold_pct) are the
                        # minimum a downstream consumer needs to
                        # decide whether/how to compact. A2A-status
                        # event emission is intentionally NOT wired
                        # here — that's a follow-up ticket.
                        try:
                            usage = (
                                getattr(last_ai_message, "response_metadata", None) or {}
                            ).get("usage") or {}
                            inp = usage.get("input_tokens") or usage.get("prompt_tokens")
                            if inp is not None and int(inp) > 0:
                                # runtime#133 (smallest-scope-first):
                                # track this turn's input_tokens per
                                # context_id so the NEXT turn's
                                # compact-and-continue decision (the
                                # hook above, before
                                # ``messages.append``) can act on
                                # the previous turn's usage. LRU-
                                # bounded by hand to keep memory
                                # bounded across long-running
                                # executors that see many
                                # context_ids (each A2A session
                                # is one context_id; old ones are
                                # safe to forget because the
                                # turn-by-turn tracking only
                                # matters for the NEXT turn of an
                                # active session).
                                _lit = getattr(self, "_last_input_tokens", None)
                                if _lit is None:
                                    _lit = {}
                                    self._last_input_tokens = _lit
                                if len(_lit) > 256:
                                    # Drop the oldest half to
                                    # bound memory; LRU semantics
                                    # are approximate (dict
                                    # insertion order is FIFO
                                    # in CPython 3.7+).
                                    for _old in list(_lit.keys())[:128]:
                                        _lit.pop(_old, None)
                                _lit[context_id] = int(inp)
                                ctx_win = get_model_context_window(self._model)
                                # CR2 RC 13423: split the COMPACTION
                                # decision (urgent: yes whenever the
                                # previous turn crossed the watermark,
                                # including the at-the-wall case) from
                                # the WARNING emission (suppress at the
                                # wall — that would just be noise since
                                # the COMPACTION hook already fired). The
                                # prior single function conflated them
                                # and skipped compaction at the wall,
                                # which is exactly when it's needed most.
                                if should_emit_budget_warning(int(inp), ctx_win):
                                    pct = round(100.0 * int(inp) / ctx_win, 1) if ctx_win > 0 else 0.0
                                    logger.warning(
                                        "context_budget_warning: context_id=%s model=%s input_tokens=%d context_window=%d threshold_pct=%.0f used_pct=%.1f — runtime#133 detection layer (compaction step is the workspace agent's job)",
                                        context_id[:8], self._model, int(inp), ctx_win, 85.0, pct,
                                    )
                        except Exception as _cb_exc:  # never let telemetry break the executor
                            logger.debug("context_budget detection skipped: %s", _cb_exc)

                # Build final text from all accumulated streaming tokens
                final_text = "".join(accumulated).strip() or "(no response generated)"
                logger.info("A2A execute: response length=%d chars", len(final_text))

                # ── OA-02 / OA-06: Output PII redaction ──────────────────────
                if _COMPLIANCE_AVAILABLE and _compliance_cfg and _compliance_cfg.mode == "owasp_agentic":
                    final_text, _pii_types = _redact_pii(final_text)
                    if _pii_types:
                        from molecule_runtime.builtin_tools.audit import log_event as _audit_log
                        _audit_log(
                            event_type="compliance",
                            action="pii.redact",
                            resource="task_output",
                            outcome="redacted",
                            pii_types=_pii_types,
                            context_id=context_id,
                        )

                # ── Autonomous-loop replay guard ─────────────────────────────
                # Incident 2026-06-29: a platform agent's idle/cron/harvester
                # self-wake re-emitted the SAME delegation-result replay on
                # ~every turn (counter-bumping "Idempotency now N records", a
                # STALE "PR #195 not merged" verdict) until the session hit
                # ~130 messages and the workspace flipped to DEGRADED. Govern
                # ONLY routine self-pings (never a user turn): suppress an
                # already-delivered / no-new-info replay instead of re-
                # broadcasting it, and after N consecutive such no-ops trip a
                # circuit breaker that halts the loop + marks the runtime
                # degraded (the idle loop reads should_halt() and stops
                # firing). This is the enforce — not merely observe — gate.
                if _is_routine_turn:
                    from molecule_runtime import autonomous_loop_guard as _loop_guard

                    _decision = _loop_guard.evaluate_autonomous_output(final_text)
                    if _decision == _loop_guard.HALT:
                        logger.warning(
                            "A2A execute: autonomous-loop circuit breaker OPEN for "
                            "context_id=%s — halting self-fire (%s)",
                            context_id, _loop_guard.halt_reason(),
                        )
                        final_text = (
                            "[autonomous loop halted — "
                            + _loop_guard.halt_reason()
                            + "]"
                        )
                    elif _decision == _loop_guard.SUPPRESS:
                        logger.info(
                            "A2A execute: suppressing duplicate autonomous replay for "
                            "context_id=%s (already delivered / no new info) — ending turn",
                            context_id,
                        )
                        final_text = (
                            "[autonomous replay suppressed — delegation result already "
                            "delivered; no new info, ending turn]"
                        )

                # ── OTEL: task_complete span ─────────────────────────────────
                with tracer.start_as_current_span("task_complete") as done_span:
                    done_span.set_attribute(WORKSPACE_ID_ATTR, _WORKSPACE_ID)
                    done_span.set_attribute(A2A_TASK_ID, context_id)
                    done_span.set_attribute("task.has_response", bool(accumulated))
                    done_span.set_attribute("task.response_length", len(final_text))

                # ── Step 3: emit final Message ────────────────────────────────
                # Non-streaming: ResultAggregator.consume_all() returns this
                #   immediately as the response (a2a_client.py reads .parts[0].text).
                # Streaming: yielded as the last SSE event in the stream.
                #
                # If the reply mentions /workspace/... paths, stage each one
                # and emit as FileParts alongside the text so the canvas can
                # render a download button. Same contract the hermes executor
                # uses — every runtime going through this code path inherits it.
                _outbound = collect_outbound_files(final_text)
                if _outbound:
                    # NOTE: do NOT re-import `Part` here. It is already imported
                    # at module scope (line 42). A function-scope `from a2a.types
                    # import ... Part ...` would mark `Part` as a local name
                    # throughout this function under Python's scoping rules,
                    # making the earlier `Part(text=text)` call (line ~358, inside
                    # the astream_events loop) raise UnboundLocalError because
                    # the local binding is not yet in scope at that point.
                    #
                    # a2a-sdk 1.x flattened the Part shape: 0.x used
                    # `Part(root=TextPart(text=...))` / `Part(root=FilePart(file=
                    # FileWithUri(uri=..., name=..., mimeType=...)))` (Pydantic
                    # discriminated-union style). 1.x's Part is a single proto
                    # message with flat fields: text, url, filename, media_type,
                    # raw, data, metadata. TextPart/FilePart/FileWithUri were
                    # removed. Same for Message: messageId/taskId/contextId
                    # camelCase became message_id/task_id/context_id.
                    from a2a.types import Message, Role
                    _parts: list[Part] = [Part(text=final_text)] if final_text else []
                    for f in _outbound:
                        _parts.append(Part(
                            url="workspace:" + f["path"],
                            filename=f["name"],
                            media_type=f["mime_type"],
                        ))
                    msg = Message(
                        message_id=uuid.uuid4().hex,
                        # 1.x Role is a protobuf enum: ROLE_UNSPECIFIED,
                        # ROLE_USER, ROLE_AGENT. Old `Role.agent` (Pydantic
                        # lowercase enum) doesn't exist anymore.
                        role=Role.ROLE_AGENT,
                        parts=_parts,
                        task_id=task_id,
                        context_id=context_id,
                    )
                else:
                    msg = new_text_message(final_text, task_id=task_id, context_id=context_id)
                # Attach tool_trace via metadata when supported. Guarded with
                # hasattr because some test mocks return a plain string here.
                if tool_trace and hasattr(msg, "metadata"):
                    try:
                        msg.metadata = {"tool_trace": tool_trace}
                    except (AttributeError, TypeError):
                        # `new_text_message()` returns a plain string in
                        # MagicMock paths in tests, where assignment to
                        # .metadata raises despite hasattr being true (the
                        # mock has the attribute as a property). Suppression
                        # is intentional — production Message objects always
                        # accept the assignment. See #1787 + commit dcbcf19
                        # for the original test-mock motivation.
                        logger.debug("metadata attach skipped (non-Message return from new_text_message)")
                # Stash the ordered tool calls for the Langfuse tracer
                # (molecule_runtime.tracing reads these off the wrapped inner).
                if tool_trace:
                    (self._last_tool_uses,
                     self._last_tool_calls,
                     self._last_steps) = _tooltrace_to_trace_shapes(tool_trace)
                # A2A v1 (a2a-sdk ≥ 1.0): once Task is enqueued (above, PR #2558),
                # the executor is in task mode and raw Message enqueues are
                # rejected with InvalidAgentResponseError("Received Message
                # object in task mode. Use TaskStatusUpdateEvent or
                # TaskArtifactUpdateEvent instead."). updater.complete()
                # wraps the Message in a terminal TaskStatusUpdateEvent
                # (state=COMPLETED, final=True) which both streaming and
                # non-streaming clients accept.
                await updater.complete(message=msg)
                _result = final_text

                # RFC invariant #8: attribute a completed self-scheduled turn's
                # outcome (empty → stale streak; content → reset) to its schedule.
                # No-op for every non-trigger turn; fully guarded internally.
                _attribute_schedule_outcome(context, final_text=final_text)

            except Exception as e:
                logger.error("A2A execute error: %s", e, exc_info=True)
                try:
                    task_span.record_exception(e)
                    from opentelemetry.trace import StatusCode
                    task_span.set_status(StatusCode.ERROR, str(e))
                except Exception:
                    pass
                # A2A v1: in task mode, terminal errors must publish a
                # FAILED TaskStatusUpdateEvent (carrying the error Message)
                # rather than a raw Message enqueue. updater.failed() does
                # exactly this — both streaming and non-streaming clients
                # receive the error and stop polling.
                await updater.failed(
                    message=new_text_message(
                        sanitize_agent_error(exc=e, stderr=error_detail_for_external(e)),
                        task_id=task_id,
                        context_id=context_id,
                    )
                )
                # RFC invariant #8: a self-scheduled turn that failed on a
                # persistent provider error (rate-limit / quota, after the
                # adapter's retries) advances the auto-disable streak; an
                # internal error is neutral. Classified on the raw exception text.
                _attribute_schedule_outcome(context, error_text=str(e))
            finally:
                await set_current_task(self._heartbeat, "")
                # MUST-FIX 1/3: stop the turn-lease activity watcher. None on
                # the kernel-off path (never started), so this is a no-op there
                # — default flow byte-identical.
                if _lease_watcher_task is not None:
                    _lease_watcher_task.cancel()
                    try:
                        await _lease_watcher_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass
                # Task #378: release the inbox slot so the next POST
                # for this context_id starts a fresh turn. Done in the
                # finally so a mid-turn exception can't leak
                # ``turn_in_flight=True`` and dead-end the workspace.
                if _inbox_entry is not None:
                    _inbox_entry.turn_in_flight = False
                    _inbox_entry.interrupt_event.clear()

        return _result

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel a running task — propagate Stop All down to the bash subprocess.

        Task #377 — canvas "Stop All" arrives as an A2A tasks/cancel POST.
        The handler must propagate the signal DOWN into any active
        subprocess-spawning tool (sandbox / bash) so a long-running
        ``bash -c 'sleep 600'`` doesn't keep burning CPU after the user
        clicked Stop. Steps:

          1. Look up the inbox entry for this context_id.
          2. Set ``interrupt_event`` so the astream loop wakes between
             runtime events and bails out cooperatively (the same path
             a follow-on canvas message takes — #378).
          3. SIGTERM the registered subprocess via ``kill_subprocess()``.
          4. Grace-poll for up to 2s; SIGKILL via ``hard_kill_subprocess()``
             if the child hasn't exited (matches the dispatch SLA from
             feedback_canvas_stop_all 2026-05-20).
          5. Emit the protocol-required TASK_STATE_CANCELED event so the
             A2A client sees a terminal status update.

        Best-effort: if the feature flag is off or no entry exists, we
        still emit the canceled event so the A2A surface stays compliant.
        """
        context_id = getattr(context, "context_id", None) or ""
        task_id = getattr(context, "task_id", "") or ""
        if context_id and _nonblocking_enabled():
            entry = _get_runtime_inbox().peek(context_id)
            if entry is not None:
                logger.info(
                    "A2A cancel: propagating Stop All for context_id=%s "
                    "(turn_in_flight=%s, has_subprocess=%s)",
                    context_id, entry.turn_in_flight,
                    entry.current_subprocess is not None,
                )
                # Wake the astream loop so it bails out cooperatively.
                entry.interrupt_event.set()
                # SIGTERM the active tool subprocess (if any).
                terminated = entry.kill_subprocess()
                if terminated:
                    # Grace-poll for up to 2s; SIGKILL the child if it
                    # hasn't exited. Poll cadence 50ms = 40 iterations
                    # — empirically enough for an asyncio Process to
                    # flip returncode after a clean SIGTERM exit.
                    proc = entry.current_subprocess
                    for _ in range(40):
                        if proc is None:
                            break
                        rc = getattr(proc, "returncode", None)
                        if rc is not None:
                            break
                        if hasattr(proc, "poll") and callable(proc.poll):
                            if proc.poll() is not None:
                                break
                        await asyncio.sleep(0.05)
                    else:
                        # Loop exhausted without an exit — escalate to SIGKILL
                        entry.hard_kill_subprocess()

        from a2a.types import TaskStatus, TaskStatusUpdateEvent
        # a2a-sdk 1.x proto: TaskStatusUpdateEvent has fields
        # {task_id, context_id, status, metadata} — NO `final` field.
        # Finality is conveyed by the terminal TaskState
        # (TASK_STATE_CANCELED / COMPLETED / FAILED). The pre-existing
        # `final=True` kwarg silently raised proto AttributeError in
        # real cancel calls — fixed as part of task #377.
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(state=task_state_value("TASK_STATE_CANCELED")),
            )
        )
