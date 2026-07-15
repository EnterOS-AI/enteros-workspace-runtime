"""Langfuse tracing — the SSOT trace *producer* for the platform ``/traces``
proxy.

Architecture (why this lives here and not in core or a single adapter):
  * The SDK owns the *comms contract*; **core stays contract-only** — its
    ``/traces`` endpoint is a read-only Langfuse proxy, it produces nothing.
  * The **runtime** owns the LLM turn, so it is the natural trace producer.
  * This module lives in the **shared runtime base** so EVERY adapter inherits
    tracing from ONE place: ``main.py`` wraps the executor at the single
    ``create_executor`` funnel, and the shared ``build_system_prompt`` records
    the consolidated prompt. No per-adapter duplication.

SDK-first contract:
  * The trace shape + tagging convention are the SSOT ``AgentTrace`` contract
    in molecule-ai-sdk (``contracts/workspace-comms/agent-trace.schema.json``,
    codegen'd as ``molecule_ai_contracts.workspace_comms_gen.AgentTrace``). This
    runtime producer and the external-workspace producer both emit that one
    shape, and molecule-core's reader consumes it — no drift across the three.
  * ``GET /workspaces/:id/traces`` filters Langfuse by ``tags=<workspace_id>``,
    which is why the workspace id is the load-bearing (required) field.

FAIL-OPEN by construction: any missing config, missing SDK, or error is a
no-op. Tracing must NEVER affect the turn — a broken Langfuse can't break the
agent. When ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``/``LANGFUSE_HOST``
are unset (the default), ``wrap_executor`` returns the executor unchanged with
zero overhead.
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # type-only: conform to the SDK contract without a hard dep
    # SSOT trace shape (molecule-ai-sdk contracts/workspace-comms/agent-trace).
    from molecule_ai_contracts.workspace_comms_gen import AgentTrace  # noqa: F401

log = logging.getLogger(__name__)

# Trace delivery runs OFF the turn. ``_emit`` ends in a blocking ``lf.flush()``;
# a slow/hung Langfuse must never stall the executor, A2A delivery, state
# polling, or heartbeats (fail-open covers errors, not latency). Emission is
# handed to a single daemon worker and the turn returns immediately. Under a
# persistent backend hang the in-flight count is bounded and further traces are
# DROPPED rather than queued unbounded — best-effort by design.
_MAX_INFLIGHT = 64
_executor: "ThreadPoolExecutor | None" = None
_inflight = 0
_dropped = 0  # cumulative traces dropped under backpressure (observability)
_submit_lock = threading.Lock()


def _submit_offloop(fn: Callable, *args: Any) -> None:
    """Run ``fn(*args)`` on the background trace worker. Non-blocking; drops the
    work (never blocks the turn, never grows unbounded) when the worker is
    saturated by a stalled backend. Fully fail-open."""
    global _executor, _inflight, _dropped
    try:
        with _submit_lock:
            if _inflight >= _MAX_INFLIGHT:
                # Backpressure: drop this trace, keep the turn moving. Log the
                # first drop and then periodically so a persistently hung
                # backend is visible without flooding the log.
                _dropped += 1
                if _dropped == 1 or _dropped % _MAX_INFLIGHT == 0:
                    log.warning(
                        "agent-trace worker saturated (backend slow/hung); "
                        "dropped %d trace(s) so far", _dropped,
                    )
                return
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="agent-trace"
                )
                atexit.register(lambda: _executor and _executor.shutdown(wait=False))
            _inflight += 1

        def _run() -> None:
            global _inflight
            try:
                fn(*args)
            except Exception:  # pragma: no cover - fail open
                pass
            finally:
                with _submit_lock:
                    _inflight -= 1

        try:
            _executor.submit(_run)
        except Exception:
            # submit() failed (e.g. interpreter shutdown) so _run — and its
            # decrement — will never execute. Release the slot we reserved here,
            # otherwise a transient failure permanently shrinks capacity until
            # _inflight pins at _MAX_INFLIGHT and every future trace is dropped.
            with _submit_lock:
                _inflight -= 1
            raise
    except Exception:  # pragma: no cover - never let trace scheduling break a turn
        pass


def _drain(timeout: float = 2.0) -> bool:
    """Test-only: block until in-flight trace emissions finish (or timeout).
    Production never calls this; it lets tests assert on off-loop emission
    without racing the background worker."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _submit_lock:
            if _inflight == 0:
                return True
        time.sleep(0.005)
    return False

# workspace_id -> last consolidated system prompt (recorded by the shared
# build_system_prompt so a turn's generation can carry the exact prompt sent
# to the LLM — the "consolidation + injection" view).
_system_prompts: dict[str, str] = {}
# workspace_id -> [{"label", "text"}] labeled prompt pieces (base identity,
# role, plugin rules, skills, a2a/hma, peers, …) so the trace shows WHAT
# consolidated into the system payload, not just the flattened blob.
_system_components: dict[str, list] = {}

# None = unresolved, False = disabled (no config / init failed), else a
# Langfuse client. Resolved once, lazily.
_client: Any = None


def record_system_prompt(workspace_id: str, prompt: str, components: "list | None" = None) -> None:
    """Called by the shared ``build_system_prompt`` after it assembles the full
    prompt, with the labeled constituent pieces. Cheap and fail-open."""
    try:
        if workspace_id and prompt:
            _system_prompts[workspace_id] = prompt
            if components:
                _system_components[workspace_id] = components
    except Exception:  # pragma: no cover - never let tracing break the build
        pass


def _resolve_host() -> str:
    """The Langfuse host to dial, self-healing a loopback value.

    A ``LANGFUSE_HOST`` pointing at the platform HOST's loopback
    (127.0.0.0/8, localhost, ::1 — the host-published UI port) is reachable
    from the platform but NEVER from inside this workspace container, so the
    producer would silently fail. When we detect such a value we fall back to
    the container-network endpoint (``MOLECULE_WORKSPACE_LANGFUSE_HOST`` or
    ``http://langfuse-web:3000``). A non-loopback host (a real cloud Langfuse)
    is a deliberate external target and is left untouched. This mirrors the
    provisioner-side rewrite so tracing works even when a stale global-secret
    ``LANGFUSE_HOST`` reaches the container."""
    host = os.environ.get("LANGFUSE_HOST", "")
    try:
        import ipaddress
        from urllib.parse import urlparse

        h = (urlparse(host).hostname or "").strip("[]")
        loopback = h == "localhost"
        if not loopback and h:
            try:
                loopback = ipaddress.ip_address(h).is_loopback
            except ValueError:
                loopback = False
        if loopback:
            return (
                os.environ.get("MOLECULE_WORKSPACE_LANGFUSE_HOST")
                or "http://langfuse-web:3000"
            )
    except Exception:  # pragma: no cover - never let host-resolution break tracing
        pass
    return host


def _langfuse():
    """Lazily resolve the Langfuse client. Returns None when tracing is off."""
    global _client
    if _client is not None:
        return _client or None
    if not (
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
        and os.getenv("LANGFUSE_HOST")
    ):
        _client = False
        return None
    try:
        from langfuse import Langfuse

        host = _resolve_host()
        kw = dict(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=host,
        )
        # Bounded per-request timeout so a single hung flush can't wedge the
        # background trace worker forever. Defensive — a langfuse version that
        # doesn't accept `timeout` falls back to the default.
        try:
            _client = Langfuse(**kw, timeout=int(os.getenv("LANGFUSE_TIMEOUT", "5")))
        except TypeError:
            _client = Langfuse(**kw)
        log.info("langfuse tracing enabled -> %s", host)
    except Exception as e:  # pragma: no cover - fail open on any init error
        log.warning("langfuse init failed (tracing disabled): %s", e)
        _client = False
        return None
    return _client


def enabled() -> bool:
    return _langfuse() is not None


def _extract_text(event: Any) -> str:
    """Best-effort text extraction from an a2a event (Message with .parts[].text)."""
    parts = getattr(event, "parts", None)
    if parts:
        out = []
        for p in parts:
            t = getattr(p, "text", "") or ""
            if t:
                out.append(t)
        return "".join(out)
    return getattr(event, "text", "") or ""


_STEP_KEYS = ("kind", "text", "name", "input", "result")


def build_agent_trace(
    workspace_id: str,
    model: str,
    user_input: str,
    output: str,
    tool_uses: "list | None" = None,
    steps: "list | None" = None,
    session_id: str = "",
    name: str = "agent-turn",
) -> dict:
    """Assemble the canonical SSOT ``AgentTrace`` record (contract:
    molecule-ai-sdk workspace-comms/agent-trace) from a turn's captured fields,
    BEFORE it is mapped onto the concrete Langfuse backend. Keeping this as the
    single internal representation is what lets the drift gate + the conformance
    test prove the producer never diverges from the SDK shape. Only ``system_prompt``
    is looked up from the per-workspace record; every field is best-effort except
    the load-bearing ``workspace_id`` tag."""
    trace: dict = {"workspace_id": workspace_id or ""}
    if name:
        trace["name"] = name
    if user_input:
        trace["input"] = user_input
    if output:
        trace["output"] = output
    system_prompt = _system_prompts.get(workspace_id, "")
    if system_prompt:
        trace["system_prompt"] = system_prompt
    if model:
        trace["model"] = model
    if tool_uses:
        trace["tool_uses"] = [str(t) for t in tool_uses]
    if session_id:
        trace["session_id"] = session_id
    if steps:
        norm = []
        for st in steps:
            # The SSOT schema types every step field as a string ("Producers
            # serialize input/result to strings so the shape is uniform across
            # runtimes"). The tool_calls fallback in _emit passes raw dict args
            # as `input`, so coerce non-str values here — otherwise the emitted
            # record violates its own contract.
            item = {}
            for k in _STEP_KEYS:
                v = st.get(k)
                if v is None:
                    continue
                s = v if isinstance(v, str) else str(v)
                if s:
                    item[k] = s
            if item:
                norm.append(item)
        if norm:
            trace["steps"] = norm
    return trace


def _emit(
    workspace_id: str,
    model: str,
    user_input: str,
    output: str,
    tool_uses: list,
    tool_calls: "list | None" = None,
    steps: "list | None" = None,
    session_id: str = "",
) -> None:
    lf = _langfuse()
    if lf is None:
        return
    try:
        # Ordered per-turn steps (SSOT contract AgentTrace.steps): prefer the
        # runtime's ordered thinking+tool steps, else synthesize from the flat
        # tool_calls list. Fed to build_agent_trace, which normalizes + string-
        # coerces them into the canonical record; the span loop below then wires
        # THAT canonical shape (not this raw list) so what the conformance test
        # validates is exactly what reaches the backend.
        ordered = steps if steps else [
            {"kind": "tool_call", "name": tc.get("name"),
             "input": tc.get("input"), "result": tc.get("output")}
            for tc in (tool_calls or [])
        ]
        # Canonical SSOT record (conforms to the vendored agent-trace schema).
        # The Langfuse trace/generation/span calls below are ITS backend mapping.
        at = build_agent_trace(
            workspace_id, model, user_input, output, tool_uses, ordered,
            session_id=session_id,
        )
        system_prompt = _system_prompts.get(workspace_id, "")
        components = _system_components.get(workspace_id, [])
        agent = os.getenv("MOLECULE_AGENT_NAME") or os.getenv("AGENT_NAME") or ""
        wsid = at["workspace_id"]
        trace = lf.trace(
            name=at.get("name", "agent-turn"),
            tags=[wsid] if wsid else [],
            session_id=at.get("session_id") or None,
            input=at.get("input", ""),
            output=at.get("output", ""),
            metadata={"agent": agent, "workspace_id": wsid, "tool_uses": at.get("tool_uses", [])},
        )
        # The generation carries the EXACT payload sent to the LLM. When we have
        # the decomposed pieces, emit ONE labeled system message per piece so the
        # trace shows WHAT consolidated (base identity, role, plugin rules,
        # skills, a2a/hma, peers, …); else fall back to the flattened prompt.
        if components:
            messages = [
                {"role": "system", "name": c.get("label", "system"),
                 "content": f"## {c.get('label', 'system')}\n\n{c.get('text', '')}"}
                for c in components
            ]
        else:
            messages = [{"role": "system", "content": system_prompt}]
        messages.append({"role": "user", "content": user_input})
        trace.generation(
            name="llm-turn",
            model=model or "",
            input=messages,
            output=output,
            metadata={"tool_uses": tool_uses, "prompt_component_labels": [c.get("label") for c in components]},
        )
        # Emit each canonical step as an observation IN SEQUENCE so the trace
        # shows HOW the agent decided — thinking blocks + tool calls interleaved.
        # Iterate at["steps"] (the normalized, string-coerced, schema-conformant
        # record) — NOT the raw `ordered` list — so the wire matches exactly what
        # the conformance test validates.
        for st in at.get("steps", []):
            try:
                if st.get("kind") == "thinking":
                    trace.span(name="thinking", input=None, output=st.get("text"),
                               metadata={"kind": "thinking"})
                else:
                    trace.span(name="tool:" + str(st.get("name", "tool")),
                               input=st.get("input"), output=st.get("result"),
                               metadata={"kind": "tool_call", "tool": st.get("name")})
            except Exception:
                pass
        lf.flush()
    except Exception as e:  # pragma: no cover - fail open on any emit error
        log.warning("langfuse trace emit failed: %s", e)


class _CapturingQueue:
    """Proxies an a2a EventQueue: forwards every event to the real queue while
    capturing text parts so the turn's output can be traced. Any attribute not
    overridden delegates to the wrapped queue."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.captured: list[str] = []

    async def enqueue_event(self, event: Any):
        try:
            self.captured.append(_extract_text(event))
        except Exception:
            pass
        return await self._inner.enqueue_event(event)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


try:  # AgentExecutor base for isinstance/duck-typing parity; fail-open import.
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutor
except Exception:  # pragma: no cover
    _AgentExecutor = object  # type: ignore


class TracingExecutor(_AgentExecutor):  # type: ignore[misc]
    """Wraps an adapter's executor and emits one Langfuse trace per turn.

    Observes only — delegates ``execute``/``cancel`` (and everything else) to
    the inner executor. Capturing the output via a proxied event queue keeps
    this runtime-agnostic: it works for ClaudeSDKExecutor, RuntimeA2AExecutor,
    and any future adapter executor, since they all enqueue their reply."""

    def __init__(self, inner: Any, workspace_id: str, model: str):
        self._inner = inner
        self._wsid = workspace_id or ""
        self._model = model or ""

    async def execute(self, context: Any, event_queue: Any):
        cap = _CapturingQueue(event_queue)
        user_input = ""
        try:
            from molecule_runtime.executor_helpers import extract_message_text

            user_input = extract_message_text(context.message) or ""
        except Exception:
            pass
        # A2A conversation id, so multiple turns group into one Traces-tab
        # session (SSOT AgentTrace.session_id). a2a-sdk exposes it as
        # ``context_id``; older shapes use ``contextId``. Best-effort.
        session_id = ""
        try:
            session_id = str(
                getattr(context, "context_id", None)
                or getattr(context, "contextId", None)
                or ""
            )
        except Exception:
            pass
        try:
            return await self._inner.execute(context, cap)
        finally:
            try:
                output = "".join(cap.captured).strip()
                tool_uses = list(getattr(self._inner, "_last_tool_uses", []) or [])
                # Structured per-tool calls (name/input/output) when the executor
                # exposes them; falls back to the names-only list otherwise.
                tool_calls = list(getattr(self._inner, "_last_tool_calls", []) or [])
                # Ordered thinking + tool steps (SSOT AgentTrace.steps) when the
                # executor exposes them; the tracer falls back to tool_calls.
                steps = list(getattr(self._inner, "_last_steps", []) or [])
                # Off the turn: submit and return immediately so a slow Langfuse
                # flush cannot stall the executor / A2A delivery / heartbeats.
                _submit_offloop(
                    _emit, self._wsid, self._model, user_input, output,
                    tool_uses, tool_calls, steps, session_id,
                )
            except Exception as e:  # pragma: no cover
                log.warning("langfuse post-turn trace failed: %s", e)

    async def cancel(self, *args, **kwargs):
        return await self._inner.cancel(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def wrap_executor(executor: Any, workspace_id: str, model: str) -> Any:
    """Wrap ``executor`` with tracing when Langfuse is configured; otherwise
    return it unchanged (zero overhead when tracing is off). Fail-open: any
    error returns the original executor."""
    try:
        if executor is None or not enabled():
            return executor
        log.info("wrapping executor with Langfuse tracing (workspace=%s)", workspace_id)
        return TracingExecutor(executor, workspace_id, model)
    except Exception as e:  # pragma: no cover
        log.warning("langfuse wrap_executor failed (returning unwrapped): %s", e)
        return executor
