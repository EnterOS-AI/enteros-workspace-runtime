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
    extract_attached_files,
    read_delegation_results,
    sanitize_agent_error,
    task_state_value,
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


# Routine self-pings the runtime sends to ITSELF: the platform cron tick and
# the heartbeat delegation-results harvester. Under the non-blocking fast-path
# these must NOT queue behind (or interrupt) a long in-flight turn — the cron
# recurs every cycle and delegation results are injected from
# DELEGATION_RESULTS_FILE at the next turn, so dropping a mid-turn routine ping
# loses nothing while avoiding both task-interruption and ping pile-up.
_ROUTINE_SELF_MESSAGE_PREFIXES = (
    "This is your own scheduled work tick",  # platform cron self-tick
    "Delegation results are ready",          # heartbeat delegation harvester
)


def _is_routine_self_message(text: str) -> bool:
    """True for the agent's own routine self-pings (cron tick / harvester)."""
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in _ROUTINE_SELF_MESSAGE_PREFIXES)


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

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Execute a task from an A2A request with SSE streaming.

        Routes through the Temporal durable workflow when a global
        ``TemporalWorkflowWrapper`` is initialised and connected to Temporal;
        otherwise falls back to ``_core_execute()`` (direct path).

        Event emission sequence:
          1. TaskStatusUpdateEvent(working)           — immediate start signal
          2. TaskArtifactUpdateEvent chunks           — token-by-token via astream_events
          3. Message(final_text)                      — terminal; non-streaming clients
                                                        return on this; streaming clients
                                                        also receive it as the last SSE event.
        """
        # ── Optional Temporal durable execution wrapper ──────────────────────
        # When a TemporalWorkflowWrapper is active this routes execution through
        # a MoleculeAIAgentWorkflow (task_receive → llm_call → task_complete).
        # Falls back silently to _core_execute() on any error or if Temporal
        # is unavailable, so the client always receives a response.
        try:
            from molecule_runtime.builtin_tools.temporal_workflow import get_wrapper as _get_temporal_wrapper

            _tw = _get_temporal_wrapper()
            if _tw is not None and _tw.is_available():
                return await _tw.run(self, context, event_queue)
        except Exception:
            pass  # Never let the wrapper path crash the executor

        await self._core_execute(context, event_queue)

    async def _core_execute(self, context: RequestContext, event_queue: EventQueue) -> str:
        """Core execution pipeline — called directly or from a Temporal activity.

        This is the original ``execute()`` body, extracted so that the Temporal
        ``llm_call`` activity can invoke it without re-entering the wrapper
        check and causing infinite recursion.

        Returns the final response text (empty string on empty input or error).

        Event emission sequence:
          1. TaskStatusUpdateEvent(working)           — immediate start signal
          2. TaskArtifactUpdateEvent chunks           — token-by-token via astream_events
          3. Message(final_text)                      — terminal event
        """
        user_input = _extract_plain_message_text(context)
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
                    if _is_routine_self_message(user_input):
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
                        _inbox_entry.defer_message(
                            build_user_content_with_files(user_input, _attached_files)
                        )
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
                    while True:
                        _astream_iter = self.agent.astream_events(
                            {"messages": messages},
                            config=run_config,
                            version="v2",
                        )
                        _stopped = False
                        _aiter = _astream_iter.__aiter__()
                        while True:
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

                            elif kind == "on_tool_end":
                                tool_end_name = event.get("name", "?")
                                tool_output = event.get("data", {}).get("output", "")
                                tool_run_id = event.get("run_id", "")
                                logger.debug("SSE: tool end — %s", tool_end_name)
                                # Pair via run_id so parallel tool calls don't clobber each other.
                                entry = tool_trace_by_run.get(tool_run_id) if tool_run_id else None
                                if entry is not None:
                                    entry["output_preview"] = str(tool_output)[:300] if tool_output else ""

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
                        sanitize_agent_error(exc=e), task_id=task_id, context_id=context_id
                    )
                )
            finally:
                await set_current_task(self._heartbeat, "")
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
