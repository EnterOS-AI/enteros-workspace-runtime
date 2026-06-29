"""obs-smoke emitter: drive the REAL runtime telemetry path into live Langfuse.

Imports the same ``molecule_runtime.builtin_tools.telemetry`` an agent runs,
initialises the Langfuse OTLP bridge from LANGFUSE_* env, emits a gen_ai.* span
(a representative LLM call) tagged with ``RUNID``, and force-flushes so the
export completes before exit. The companion ``obs_smoke.sh`` then queries the
Langfuse API for the trace named ``obs-smoke-<RUNID>`` — proving the deep
LLM/A2A trace path is wired end-to-end (runtime → Langfuse ingest → query API).
"""

import os
import time

from opentelemetry import trace

from molecule_runtime.builtin_tools.telemetry import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    get_tracer,
    setup_telemetry,
)

runid = os.environ["RUNID"]
setup_telemetry(service_name="obs-smoke")
tracer = get_tracer()

with tracer.start_as_current_span(f"obs-smoke-{runid}") as span:
    span.set_attribute(GEN_AI_SYSTEM, "openai")
    span.set_attribute(GEN_AI_REQUEST_MODEL, "gpt-4o-mini")
    span.set_attribute(GEN_AI_OPERATION_NAME, "chat")
    span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, 11)
    span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, 7)
    span.set_attribute("obs.smoke.runid", runid)
    with tracer.start_as_current_span("llm-call-child") as child:
        child.set_attribute("gen_ai.response.finish_reasons", "stop")
        time.sleep(0.05)

provider = trace.get_tracer_provider()
flush = getattr(provider, "force_flush", None)
ok = flush() if flush else None
time.sleep(1.0)
try:
    provider.shutdown()
except Exception:
    pass
print(f"EMITTED runid={runid} force_flush_ok={ok}")
