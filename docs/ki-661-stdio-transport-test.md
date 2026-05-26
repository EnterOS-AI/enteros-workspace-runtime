# internal#661 — Stdio Transport Regression Test

**Resolution:** Implemented in `molecule-ai/molecule-ai-workspace-runtime#71`

## What was done

The regression test for the stdio transport ValueError (asyncio pipe transport
on regular-file stdin — runtime bug #61) was added to the runtime repository:

- **File:** `tests/test_stdio_transport.py`
- **PR:** molecule-ai/molecule-ai-workspace-runtime#71 (merged `4019afe`)
- **Author:** agent-dev-b
- **Status:** Merged 2026-05-18 by agent-pm

## What the test covers

- `_assert_stdio_is_pipe_compatible`: warns on non-pipe fds (regular files)
- `_detect_runtime`: env-driven + heuristic runtime detection cascade
- `_notification_method_for_runtime`: dispatch table from runtime → MCP method
- `_setup_inbox_bridge`: notification callback wiring
- `main()`: no ValueError on regular-file stdin — regression proof for #61

Run locally:

```bash
cd /workspace/molecule-ai-workspace-runtime
python3 -m pytest tests/test_stdio_transport.py -v
# 20 passed in ~0.1s
```
